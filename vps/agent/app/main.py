"""Agent_C4 agent service: REST health + tool introspection + WebSocket chat.

The web app opens a WebSocket to /ws and exchanges JSON messages with the shared crew
thread. Each inbound message runs the agent loop (agent.answer) in a threadpool because
the tools use a synchronous DB pool.
"""
import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from shared.tool_contracts import AGENT_TOOLS
from .db import pool
from . import agent, tools, navigator, alerts, summarizer, auth, race_mode, datasource, watches


def _race_gated(channel: str):
    """RRS 41: if racing, return a 403 refusal (and audit it); else None. Used to gate the REST
    endpoints that serve customized tactical/routing/perf/sail/fatigue/navigation advice."""
    if race_mode.racing():
        race_mode.audit_refusal(channel)
        return JSONResponse(
            {"withheld": True, "detail": race_mode.REFUSAL, "mode": "race"}, status_code=403
        )
    return None

API_KEY_PRESENT = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


class Hub:
    """Fan-out to every connected web client. Each connection drains its own queue, so an
    alert push and a chat reply never race on the same socket."""
    def __init__(self):
        self.queues: set[asyncio.Queue] = set()

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.queues.add(q)
        return q

    def unregister(self, q: asyncio.Queue):
        self.queues.discard(q)

    async def broadcast(self, payload: dict):
        for q in list(self.queues):
            q.put_nowait(payload)


hub = Hub()


async def alert_loop():
    """Evaluate alert rules every ALERT_EVAL_SECONDS and push new/updated/cleared deltas."""
    while True:
        try:
            changes = await run_in_threadpool(alerts.evaluate)
            for ch in changes:
                await hub.broadcast({"role": "alert", **ch})
        except Exception as exc:  # never let the loop die
            print(f"[alerts] eval error: {exc}", flush=True)
        await asyncio.sleep(alerts.EVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=True, timeout=30)
    task = asyncio.create_task(alert_loop())
    yield
    task.cancel()
    pool.close()


app = FastAPI(title="Agent_C4 Agent Service", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Gate every REST route behind the shared-password bearer token, except OPEN_PATHS.
    (WebSocket handshakes are scope 'websocket' and bypass HTTP middleware — /ws checks inline.)"""
    if request.method == "OPTIONS" or request.url.path in auth.OPEN_PATHS:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else None
    if not auth.verify_token(token):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.post("/auth")
async def authenticate(body: dict):
    """Exchange the shared boat password for a signed, time-limited bearer token."""
    if not auth.check_password((body or {}).get("password")):
        return JSONResponse({"detail": "invalid password"}, status_code=401)
    return {"token": auth.issue_token(), "ttl_hours": auth.AUTH_TTL_HOURS}


@app.get("/health")
def health():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "llm": "live" if API_KEY_PRESENT else "fallback (no API key)",
            "mode": race_mode.current_mode()}


@app.get("/mode")
def get_mode():
    """The authoritative race/practice mode (RRS 41 gate)."""
    return {"mode": race_mode.current_mode()}


@app.post("/mode")
def set_mode_ep(body: dict):
    """Set race/practice mode server-side. Body: {"mode": "race"|"practice"}. Audited."""
    try:
        mode = race_mode.set_mode((body or {}).get("mode"), actor="web")
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"mode": mode}


@app.get("/tools")
def list_tools():
    """Surface the tool contracts (handy for debugging Phase 0/4)."""
    return {"tools": [t["name"] for t in AGENT_TOOLS]}


@app.get("/conditions")
def conditions():
    """Compact best-value-per-channel for the web instrument strip."""
    return tools.get_strip()


@app.get("/conditions/full")
def conditions_full():
    """All sources per channel (the multi-source view the agent reasons over)."""
    return tools.get_current_conditions()


@app.get("/sources")
def sources():
    return tools.get_sources()


@app.get("/fatigue")
def fatigue():
    """Helm fatigue index (0–100) + components + rotation recommendation."""
    return _race_gated("/fatigue") or tools.get_fatigue()


@app.get("/watch")
def watch_status():
    """The watch system: who's on now, countdown to the next change, the block schedule.
    Crew scheduling, not performance advice — never race-gated (the in-race surface is the
    onboard engine's identical endpoint anyway)."""
    return watches.get_watch()


@app.post("/watch")
def watch_set(body: dict = None):
    """Replace or live-edit the watch plan: {plan: {...}} (Lab homework / block editor) ·
    {action: 'hold'|'swap'|'all_hands', minutes?} (quick edits) · {clear: true}."""
    return watches.set_watch(body)


@app.get("/sail")
def sail(tws: float | None = None, twa: float | None = None, hoisted: str | None = None):
    """Sail-range advice for the dial: zones, optimal sail, position, next crossover.
    tws/twa default to the latest live values; hoisted is the crew-reported sail."""
    return _race_gated("/sail") or tools.get_sail_advice(tws=tws, twa=twa, hoisted=hoisted)


@app.get("/course")
def course(route: str | None = None):
    """The active course: marks + per-leg bearing/distance (for the schematic plot)."""
    return navigator.get_course(route)


@app.get("/navigator")
def nav(route: str | None = None):
    """Next mark, ETA, leg type, and laylines from live position + wind."""
    return _race_gated("/navigator") or navigator.get_navigator(route)


@app.post("/course/practice")
def practice_course(leg_nm: float = 1.0):
    """Drop a windward/leeward practice course from the live position + wind (route 'practice')."""
    return navigator.make_practice_course(leg_nm)


@app.post("/course/load")
def course_load(body: dict):
    """Load a RaceDefinition course into the navigator (the homework→onboard link). Body:
    {definition, course_id?, route?}. Writes the flattened marks to the waypoints store and makes
    the route active so the navigator/plot use the real course. Pre-race setup — not gated."""
    from shared.race_def import course_to_marks
    definition = (body or {}).get("definition") or {}
    marks, skipped, cid = course_to_marks(definition, (body or {}).get("course_id"))
    if not marks:
        return JSONResponse({"detail": "no usable marks (need coordinates)"}, status_code=400)
    route = (body or {}).get("route") or definition.get("race_id") or "race"
    datasource.active().save_course(route, marks)
    navigator.set_active(route)
    return {"loaded": True, "route": route, "course_id": cid, "marks": len(marks),
            "skipped": skipped}


@app.get("/tactics")
def tactics_ep(route: str | None = None):
    """Tactical read: lifted/headed, favored side, leverage (practice/debrief — RRS 41)."""
    return _race_gated("/tactics") or tools.get_tactics(route)


@app.get("/forecast")
def forecast_ep(lat: float | None = None, lon: float | None = None, hours: int = 12):
    """Wind forecast (Open-Meteo) for a position; defaults to the live position."""
    return tools.fetch_forecast(lat, lon, hours)


@app.get("/route")
def route_ep(route: str | None = None, target: str = "next"):
    """Isochrone optimal route to the next mark (or 'finish') through the forecast wind."""
    return _race_gated("/route") or tools.get_route(route, target)


@app.get("/alerts")
def alerts_ep():
    """Currently-active alerts (collision/safety/performance), most severe first."""
    return tools.get_alerts()


@app.get("/racelog/sessions")
def racelog_sessions():
    """RACE-SESSION markers backfilled from the boat (`crew.session` readings) — the windows the
    owner recorded. Shore-side recall of own data (the Lab debrief's from-log source)."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT str_value FROM telemetry_raw WHERE path = 'crew.session' "
            "ORDER BY time DESC LIMIT 100").fetchall()
    import json as _json
    out, seen = [], set()
    for r in rows:                      # dict rows (psycopg row factory)
        try:
            d = _json.loads(r["str_value"])
        except Exception:
            continue
        key = d.get("id"), d.get("start_ts")
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return {"sessions": out}


@app.get("/racelog/track")
def racelog_track(start: float, end: float, max_points: int = 2000):
    """The boat's own track over a window, built from the backfilled full-res archive
    (position/SOG/COG in telemetry_raw) — the Lab debrief's 'boat log' track source. Far denser
    than a public tracker's fixes. Also returns the crew sail-state changes in the window."""
    from datetime import datetime, timezone
    import json as _json
    t0 = datetime.fromtimestamp(float(start), tz=timezone.utc)
    t1 = datetime.fromtimestamp(float(end), tz=timezone.utc)
    paths = ("navigation.position.latitude", "navigation.position.longitude",
             "navigation.speedOverGround", "navigation.courseOverGroundTrue")
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT extract(epoch FROM time)::float8 AS epoch, path, value FROM telemetry_raw "
            "WHERE path = ANY(%s) AND time BETWEEN %s AND %s AND value IS NOT NULL "
            "ORDER BY time", (list(paths), t0, t1)).fetchall()
        sails = conn.execute(
            "SELECT extract(epoch FROM time)::float8 AS epoch, str_value FROM telemetry_raw "
            "WHERE path = 'crew.sail.state' AND time BETWEEN %s AND %s ORDER BY time",
            (t0, t1)).fetchall()
    # bucket by second → fixes; a fix needs at least lat+lon (dict rows — psycopg row factory)
    buckets = {}
    for r in rows:
        buckets.setdefault(round(r["epoch"]), {})[r["path"]] = r["value"]
    fixes = []
    for t in sorted(buckets):
        b = buckets[t]
        if paths[0] in b and paths[1] in b:
            fixes.append({"t": t, "lat": b[paths[0]], "lon": b[paths[1]],
                          "sog": round(b[paths[2]] * 1.943844, 2) if paths[2] in b else None,
                          "cog": round(b[paths[3]] * 57.29577951308232, 1) if paths[3] in b else None})
    if len(fixes) > int(max_points):          # even thinning — the debrief doesn't need 1 Hz
        step = len(fixes) / float(max_points)
        fixes = [fixes[int(i * step)] for i in range(int(max_points))]
    sail_log = []
    for r in sails:
        try:
            sail_log.append({"t": r["epoch"], **_json.loads(r["str_value"])})
        except Exception:
            continue
    return {"fixes": fixes, "n": len(fixes), "sail_log": sail_log}


@app.post("/summary")
async def summary_ep(minutes: float | None = None):
    """On-demand short recap of the recent window; stored in agent_summaries."""
    return _race_gated("/summary") or await run_in_threadpool(summarizer.make_summary, minutes)


@app.post("/debrief")
async def debrief_ep(minutes: float | None = None):
    """On-demand fuller window report (speed vs polar, wind, alerts fired); stored."""
    return _race_gated("/debrief") or await run_in_threadpool(summarizer.make_debrief, minutes)


@app.get("/summaries")
def summaries_ep(limit: int = 5):
    """Recent stored summaries / debriefs (newest first)."""
    return tools.get_summaries(limit)


@app.get("/polar-analysis")
async def polar_analysis_ep(hours: float | None = None, min_samples: int | None = None,
                            point_of_sail: str | None = None):
    """Observed-vs-rated polar mined from the archive (% of polar by TWS/TWA)."""
    return _race_gated("/polar-analysis") or \
        await run_in_threadpool(tools.get_polar_analysis, hours, min_samples, point_of_sail)


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    # HTTP middleware doesn't see WS handshakes — check the token (passed as a query param,
    # since browsers can't set headers on a WebSocket from JS) before accepting.
    if not auth.verify_token(websocket.query_params.get("token")):
        await websocket.close(code=1008)  # policy violation
        return
    await websocket.accept()
    q = hub.register()
    history: list = []
    await websocket.send_json({"role": "system", "text": "Navigator online."})
    # Push the current active alerts so a freshly opened client shows the live banner state.
    try:
        for a in await run_in_threadpool(alerts.active_alerts):
            await websocket.send_json({"role": "alert", "event": "new", "alert": a})
    except Exception:
        pass

    async def reader():
        while True:
            msg = await websocket.receive_text()
            reply = await run_in_threadpool(agent.answer, msg, history)
            history.append({"role": "user", "content": msg})
            history.append({"role": "assistant", "content": reply})
            history[:] = history[-20:]  # cap thread memory
            q.put_nowait({"role": "assistant", "text": reply})

    async def writer():
        while True:
            await websocket.send_json(await q.get())

    rt = asyncio.create_task(reader())
    wt = asyncio.create_task(writer())
    try:
        await asyncio.wait({rt, wt}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        rt.cancel()
        wt.cancel()
        hub.unregister(q)
