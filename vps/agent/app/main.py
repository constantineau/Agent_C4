"""Agent_C4 agent service: REST health + tool introspection + WebSocket chat.

The web app opens a WebSocket to /ws and exchanges JSON messages with the shared crew
thread. Each inbound message runs the agent loop (agent.answer) in a threadpool because
the tools use a synchronous DB pool.
"""
import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from shared.tool_contracts import AGENT_TOOLS
from .db import pool
from . import agent, tools, navigator, alerts, summarizer

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


@app.get("/health")
def health():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "llm": "live" if API_KEY_PRESENT else "fallback (no API key)"}


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
    return tools.get_fatigue()


@app.get("/sail")
def sail(tws: float | None = None, twa: float | None = None, hoisted: str | None = None):
    """Sail-range advice for the dial: zones, optimal sail, position, next crossover.
    tws/twa default to the latest live values; hoisted is the crew-reported sail."""
    return tools.get_sail_advice(tws=tws, twa=twa, hoisted=hoisted)


@app.get("/course")
def course(route: str | None = None):
    """The active course: marks + per-leg bearing/distance (for the schematic plot)."""
    return navigator.get_course(route)


@app.get("/navigator")
def nav(route: str | None = None):
    """Next mark, ETA, leg type, and laylines from live position + wind."""
    return navigator.get_navigator(route)


@app.post("/course/practice")
def practice_course(leg_nm: float = 1.0):
    """Drop a windward/leeward practice course from the live position + wind (route 'practice')."""
    return navigator.make_practice_course(leg_nm)


@app.get("/tactics")
def tactics_ep(route: str | None = None):
    """Tactical read: lifted/headed, favored side, leverage (practice/debrief — RRS 41)."""
    return tools.get_tactics(route)


@app.get("/forecast")
def forecast_ep(lat: float | None = None, lon: float | None = None, hours: int = 12):
    """Wind forecast (Open-Meteo) for a position; defaults to the live position."""
    return tools.fetch_forecast(lat, lon, hours)


@app.get("/route")
def route_ep(route: str | None = None, target: str = "next"):
    """Isochrone optimal route to the next mark (or 'finish') through the forecast wind."""
    return tools.get_route(route, target)


@app.get("/alerts")
def alerts_ep():
    """Currently-active alerts (collision/safety/performance), most severe first."""
    return tools.get_alerts()


@app.post("/summary")
async def summary_ep(minutes: float | None = None):
    """On-demand short recap of the recent window; stored in agent_summaries."""
    return await run_in_threadpool(summarizer.make_summary, minutes)


@app.post("/debrief")
async def debrief_ep(minutes: float | None = None):
    """On-demand fuller window report (speed vs polar, wind, alerts fired); stored."""
    return await run_in_threadpool(summarizer.make_debrief, minutes)


@app.get("/summaries")
def summaries_ep(limit: int = 5):
    """Recent stored summaries / debriefs (newest first)."""
    return tools.get_summaries(limit)


@app.get("/polar-analysis")
async def polar_analysis_ep(hours: float | None = None, min_samples: int | None = None,
                            point_of_sail: str | None = None):
    """Observed-vs-rated polar mined from the archive (% of polar by TWS/TWA)."""
    return await run_in_threadpool(tools.get_polar_analysis, hours, min_samples, point_of_sail)


@app.websocket("/ws")
async def ws(websocket: WebSocket):
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
