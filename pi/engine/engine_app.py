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

from app import navigator, tactics, routing, weather, sails, fatigue, onboard_conditions

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


@app.get("/fatigue")
def fatigue_ep():
    """Helm fatigue index (0–100) + components + rotation recommendation."""
    return fatigue.compute_fatigue_index()


@app.get("/sail")
def sail(tws: float | None = None, twa: float | None = None, hoisted: str | None = None):
    """Sail-range advice for the dial. tws/twa default to the latest live values."""
    if tws is None or twa is None:
        s = onboard_conditions.get_strip()
        tws = tws if tws is not None else s.get("tws")
        twa = twa if twa is not None else s.get("twa")
    return sails.get_sail_advice(tws, twa, hoisted)


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
