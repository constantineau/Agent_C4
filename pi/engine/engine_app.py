"""SR33 onboard deterministic engine service (Phase 9.1) — runs ON THE BOAT (Pi 4).

This is the in-race-legal half of the three-tier architecture. It serves the SAME REST
endpoints the iPad already uses against the cloud, but computed ONBOARD from the boat's own
data (the Phase-2 SQLite archive + a live Signal K cache, via `OnboardSource`) — so it is the
boat's own computer crunching its own sensors (Expedition-class), not an "outside source"
under RRS 41. There is **no LLM, no tool-loop, and no race gate** here: every endpoint is a
direct deterministic response and all of them are legal while racing.

It reuses the exact engine modules from the cloud agent package (`app.navigator` /
`app.tactics` / `app.routing` / `app.fatigue` / `app.sails` / `app.weather`), which read
through `datasource.active()`. With `DATA_SOURCE=onboard` that resolves to `OnboardSource`, so
the same code produces the same outputs from local data. The multi-source instrument strip is
built by `app.onboard_conditions` (the cloud builds it off Postgres in `tools.py`).

Cloud counterpart for parity: `vps/agent/app/main.py`. Differences: no /auth, no /ws chat, no
alerts/summarizer/polar-analysis (those are cloud / C4 Performance Lab), no race gate.
"""
import os

os.environ.setdefault("DATA_SOURCE", "onboard")  # this service is always the onboard backend

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import (navigator, tactics, routing, weather, sails, fatigue, onboard_conditions,
                 datasource, ais, fleet, deviation, drift, selector, reoptimize, strategy,
                 matcher, buoys, racelog, watches)

app = FastAPI(title="Agent_C4 Onboard Engine", version="0.1.0")
# The iPad reaches the Pi directly over boat-local Wi-Fi in race mode; allow cross-origin so a
# browser pointed straight at the engine works without a proxy.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "ok", "service": "onboard-engine", "llm": "none (deterministic)",
            "data_source": os.environ.get("DATA_SOURCE", "onboard")}


@app.get("/conditions")
def conditions():
    """Compact best-value-per-channel for the iPad instrument strip."""
    return onboard_conditions.get_strip()


@app.get("/conditions/full")
def conditions_full():
    """All sources per channel (the multi-source view)."""
    return onboard_conditions.get_current_conditions()


@app.get("/sources")
def sources():
    return onboard_conditions.get_sources()


@app.get("/series")
def series(minutes: int = 720, max_points: int = 600):
    """TWS + TWD time series from the onboard full-res archive, for the dashboard race chart.
    Returns downsampled [{t, tws, twd}] (knots, degrees) over the last `minutes` — the whole-race
    record the boat captured, independent of when/whether the iPad dashboard was open."""
    import math
    src = datasource.active()
    tws_rows = src.series("environment.wind.speedTrue", minutes)       # [(epoch, m/s)]
    twd_rows = src.series("environment.wind.directionTrue", minutes)   # [(epoch, rad)]
    if not tws_rows:
        return {"available": False, "points": [], "note": "no archived wind in the window"}
    # downsample the TWS series to keep the payload small, then match the nearest TWD by time
    stride = max(1, math.ceil(len(tws_rows) / max_points))
    sampled = tws_rows[::stride]
    if sampled and sampled[-1] is not tws_rows[-1]:
        sampled.append(tws_rows[-1])
    pts, j = [], 0
    for (t, ms) in sampled:
        twd = None
        if twd_rows:
            while j + 1 < len(twd_rows) and abs(twd_rows[j + 1][0] - t) <= abs(twd_rows[j][0] - t):
                j += 1
            twd = round(math.degrees(twd_rows[j][1]) % 360, 1)
        pts.append({"t": round(t, 1), "tws": round(ms * 1.943844, 2), "twd": twd})
    return {"available": True, "minutes": minutes, "count": len(pts), "points": pts}


@app.get("/fatigue")
def fatigue_ep():
    """Helm fatigue index (0–100) + components + rotation recommendation."""
    return fatigue.compute_fatigue_index()


@app.get("/sail")
def sail(tws: float | None = None, twa: float | None = None, hoisted: str | None = None):
    """Sail-range advice for the dial + the SAIL tile. tws/twa default to the latest live
    values; `hoisted` defaults to the CREW-SET sail (the sails bar / /sails/state — the primary
    driver of the flying combination), so the read is about the sail actually up. The frozen
    boat model (jib TWS bands, the Code-0 envelope) refines the fit when a bundle is aboard."""
    if tws is None or twa is None:
        s = onboard_conditions.get_strip()
        tws = tws if tws is not None else s.get("tws")
        twa = twa if twa is not None else s.get("twa")
    if hoisted is None:
        hoisted = (matcher.get_sail_state() or {}).get("hoisted")
    bm = (deviation._load_playbook() or {}).get("boat_model")
    return sails.get_sail_advice(tws, twa, hoisted, boat_model=bm)


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


@app.post("/course/load")
def course_load(body: dict):
    """Load a RaceDefinition course into the onboard navigator (the deployed homework). Body:
    {definition, course_id?, route?}. Writes the flattened marks to the Pi marks store + activates."""
    from shared.race_def import course_to_marks
    definition = (body or {}).get("definition") or {}
    marks, skipped, cid = course_to_marks(definition, (body or {}).get("course_id"))
    if not marks:
        return {"loaded": False, "detail": "no usable marks (need coordinates)"}
    route = (body or {}).get("route") or definition.get("race_id") or "race"
    datasource.active().save_course(route, marks)
    navigator.set_active(route)
    return {"loaded": True, "route": route, "course_id": cid, "marks": len(marks), "skipped": skipped}


@app.get("/tactics")
def tactics_ep(route: str | None = None):
    """Tactical read: lifted/headed, favored side, leverage."""
    return tactics.get_tactics(route)


@app.get("/forecast")
def forecast_ep(lat: float | None = None, lon: float | None = None, hours: int = 12):
    """Wind forecast (Open-Meteo — common public data) for a position; defaults to live."""
    if lat is None or lon is None:
        s = onboard_conditions.get_strip()
        lat = lat if lat is not None else s.get("lat")
        lon = lon if lon is not None else s.get("lon")
    if lat is None or lon is None:
        return {"available": False, "note": "no position to forecast for"}
    return weather.get_forecast(lat, lon, hours)


@app.get("/route")
def route_ep(route: str | None = None, target: str = "next"):
    """Isochrone optimal route to the next mark (or 'finish') through the forecast wind."""
    return routing.get_route(route, target)


@app.get("/ais")
def ais_ep(max_range_nm: float = 12):
    """AIS traffic with range/bearing + live CPA/TCPA vs own ship — collision + fleet awareness.

    Always legal in-race: the targets come from the boat's OWN AIS receiver (other-vessel Signal K
    contexts) and the geometry is computed by the boat's OWN computer. Threat-sorted (closing,
    smallest CPA first)."""
    return ais.get_ais_targets(max_range_nm)


@app.post("/fleet/load")
def fleet_load(body: dict):
    """Load the fleet homework (roster + scoring + own rating + public-tracker config) onboard. Body:
    {definition, own?} or {fleet, scoring, own?, tracker?}. Frozen at the gun; legal in-race."""
    from shared.race_def import fleet_blob
    body = body or {}
    if body.get("definition") is not None:
        blob = fleet_blob(body["definition"], body.get("own"))
    else:
        blob = {"fleet": body.get("fleet") or [], "scoring": body.get("scoring") or {},
                "own": body.get("own") or {}, "tracker": body.get("tracker") or {}}
    datasource.active().save_fleet(blob)
    return {"loaded": True, "roster_size": len(blob["fleet"])}


@app.get("/fleet")
def fleet_ep(max_range_nm: float = 40.0):
    """Handicap-aware fleet tactics: roster-matched competitors with course progress + corrected-time
    delta (who I need to beat, by how much), plus unmatched AIS traffic. Always legal in-race (own
    receiver + own computer + pre-loaded public roster)."""
    return fleet.get_fleet(max_range_nm)


@app.post("/playbook/load")
def playbook_load(body: dict):
    """Load the frozen Lab-2 playbook bundle (`c4.playbook/v1`) onboard — the homework the
    route-deviation core measures against. Body is the bundle itself, or {bundle: ...}. Frozen at
    the gun; legal in-race (own computer interpreting pre-loaded homework). Replaces any prior."""
    body = body or {}
    bundle = body.get("bundle") if "variants" not in body else body
    if not bundle or not (bundle.get("variants")):
        return {"loaded": False, "detail": "no bundle with variants"}
    datasource.active().save_playbook(bundle)
    deviation.reset_state()          # a new playbook → clear the Schmitt/trend memory
    drift.reset_state()
    matcher.clear_state()            # forget play arm/sustain memory too
    return {"loaded": True, "race_id": bundle.get("race_id"),
            "variants": len(bundle.get("variants") or []),
            "plays": len(bundle.get("plays") or []),
            "recommended": bundle.get("recommended")}


@app.get("/plays")
def plays(route: str | None = None):
    """Playbook v2 Phase D — the Tier-1 PLAY MATCHER: every play in the frozen v2 bundle evaluated
    against the boat's live signals (deterministic Schmitt sustain; armed first). Legal in-race —
    own instruments + pre-loaded homework; works with no Orin aboard."""
    return matcher.get_plays(route)


@app.get("/buoys")
def buoys_ep(route: str | None = None):
    """Live NDBC buoy observations + the UP-COURSE leading indicator (common public data available
    to all boats, read by the boat's own computer — legal in-race like GRIB). Stations come from
    the frozen bundle's `buoys` block (else env/fallback); cached ~10 min; best-effort offline."""
    return buoys.get_buoys(route)


@app.get("/sails/state")
def sails_state():
    """The crew-set sail state (hoisted + out-of-service) the matcher's crew-armed signals read."""
    return matcher.get_sail_state()


@app.post("/sails/state")
def sails_state_set(body: dict):
    """Update the crew sail state: {flying?: ['C0','J2'], reef?: 'R1'|'', hoisted?: 'A3'|'',
    out_of_service?: ['A2', ...]}. The configuration is a SET (the boat flies combinations —
    C0 alone, C0+J2, kite+staysail); `hoisted` is the legacy single-sail setter. Every change
    is also APPENDED to the onboard sail log (timestamped history for the race log / debrief).
    The out-of-service list is the gear-loss plays' arming signal — there is no instrument for
    a blown kite; the crew declares it here (and clears it when repaired)."""
    body = body or {}
    return matcher.set_sail_state(hoisted=body.get("hoisted"), flying=body.get("flying"),
                                  reef=body.get("reef"),
                                  out_of_service=body.get("out_of_service"))


@app.get("/watch")
def watch_status():
    """The watch system: who's on now, countdown to the next change, the full block schedule
    (the CREW tile + coach read). Deterministic scheduling on the boat's own computer — always
    legal in-race; empty-but-valid when no plan is loaded."""
    return watches.get_watch()


@app.post("/watch")
def watch_set(body: dict = None):
    """Replace or live-edit the watch plan: {plan: {...}} (Lab homework / block editor) ·
    {action: 'hold'|'swap'|'all_hands', minutes?} (the iPad quick edits) · {clear: true}."""
    return watches.set_watch(body)


@app.get("/session")
def session_status():
    """The RACE LOG state: active session + recent windows. Sessions mark what the boat KEEPS
    (full-res, backfilled, debriefed); everything outside them is pruned after the retention
    period and never leaves the boat. No Lab prep / RaceDefinition needed."""
    return racelog.status()


@app.post("/session/start")
def session_start(body: dict = None):
    """Start a session: {name?, race_id?, kind?: race|practice} — all optional (one tap at the
    gun; the loaded playbook's race_id is picked up automatically when aboard)."""
    body = body or {}
    return racelog.start(name=body.get("name"), race_id=body.get("race_id"),
                         kind=body.get("kind"))


@app.post("/session/end")
def session_end():
    return racelog.end()


@app.get("/sails/log")
def sails_log(since: float | None = None, until: float | None = None):
    """The timestamped crew sail-state history (append-only; onboard engine store)."""
    src = datasource.active()
    if not hasattr(src, "sail_log"):
        return {"available": False, "note": "sail log is onboard-only"}
    return {"available": True, "log": src.sail_log(since, until)}


@app.get("/deviation")
def deviation_ep(route: str | None = None, variant: str | None = None, since: float | None = None):
    """Route-deviation vs the active playbook variant's frozen optimal track: XTE, along-track
    progress, time-behind-optimal, VMC deficit — with fuzzy consider/commit status. Deterministic,
    always legal in-race (own GPS + own computer + pre-loaded homework); `na` with no playbook
    aboard. `variant` overrides the recommended default; `since` re-anchors time-behind to the gun."""
    return deviation.get_deviation(route=route, variant=variant, since=since)


@app.get("/drift")
def drift_ep(route: str | None = None):
    """Forecast-drift vs the playbook's frozen forecast reference: how far the live common forecast
    (Open-Meteo) has moved from what the plan was built on, over the still-future route waypoints —
    directional shift (veered/backed) + speed change, with fuzzy consider/commit status. Deterministic,
    legal in-race (own computer + common public data); `na` with no playbook / no reference aboard."""
    return drift.get_drift(route=route)


@app.get("/selector")
def selector_ep(route: str | None = None):
    """Branch SELECTOR: unifies the wind-shift + route-deviation + forecast-drift triggers into one
    recommendation over the frozen playbook — HOLD the recommended variant / SWITCH to a pre-authored
    variant / OFF-SCRIPT (no branch aboard for the favoured side). Deterministic, legal in-race (own
    instruments + pre-loaded homework); this layer deterministically picks a pre-authored branch (the
    copilot above it only narrates + condition-matches — it never originates strategy)."""
    return selector.get_selector(route=route)


@app.get("/strategy")
def strategy_ep(route: str | None = None):
    """STRATEGY SYNTHESIS (Tier-1, deterministic): the higher-order read of the OVERALL plan — the
    selector's HOLD/SWITCH/OFF-SCRIPT backbone fused with the handicap fleet picture into a grounded
    assessment + a cross-signal CONCORDANCE (are forecast-drift, fleet position and route-deviation
    pointing the same way as the wind shift, or fighting it?) + one recommendation. The deterministic
    fallback + the digest the onboard LLM copilot phrases and may extend. Legal in-race (own instruments
    + pre-loaded homework + common public data)."""
    return strategy.get_strategy_signals(route=route)


@app.get("/reoptimize")
def reoptimize_ep(route: str | None = None):
    """Onboard RE-OPTIMIZER (graceful-degradation tier 2/3): a fresh route from the live position
    through the remaining marks on own polars + the common Open-Meteo forecast, flagged OFF-PLAYBOOK,
    with its divergence from the frozen plan. The fallback when the selector says off-script. CPU-heavy
    (isochrone chain) → cached + on-demand. Legal in-race (own computer + own polars + common data)."""
    return reoptimize.get_reoptimize(route=route)
