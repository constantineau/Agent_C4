"""SR33 onboard deterministic engine service (Phase 9.1) — runs ON THE BOAT (Pi 4).

This is the in-race-legal half of the three-tier architecture. It serves the SAME REST
endpoints the iPad already uses against the cloud, but computed ONBOARD from the boat's own
data (the Phase-2 SQLite archive + a live Signal K cache, via `OnboardSource`) — so it is the
boat's own computer crunching its own sensors (Expedition-class), not an "outside source"
under RRS 41. There is **no LLM and no tool-loop** here: every read endpoint is a direct
deterministic response and legal while racing. The one gate is on the WRITES that take shore
homework (course, fleet, playbook, checklist, a whole watch plan, plume, re-route assets): frozen
at the gun, refused while racing (`app.racegate`; racing ends only on the iPad's "Race over"). The Orin copilot's playbook goes
through here too (POST /playbook/load forwards it under the same lock). Crew inputs from the iPad (sail
state, checklist acks, watch quick edits, the race log, re-route buttons) are never gated.

It reuses the exact engine modules from the cloud agent package (`app.navigator` /
`app.tactics` / `app.routing` / `app.fatigue` / `app.sails` / `app.weather`), which read
through `datasource.active()`. With `DATA_SOURCE=onboard` that resolves to `OnboardSource`, so
the same code produces the same outputs from local data. The multi-source instrument strip is
built by `app.onboard_conditions` (the cloud builds it off Postgres in `tools.py`).

Cloud counterpart for parity: `vps/agent/app/main.py`. Differences: no /auth, no /ws chat, no
alerts/summarizer/polar-analysis (those are cloud / C4 Performance Lab), no race-mode gate on
reads.
"""
import json
import os
import threading
import time
import urllib.request

os.environ.setdefault("DATA_SOURCE", "onboard")  # this service is always the onboard backend

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from shared import archiver_status, boat_hardware, n2k_sources
from app import (navigator, tactics, routing, weather, sails, fatigue, onboard_conditions,
                 datasource, ais, fleet, deviation, drift, selector, reoptimize, strategy,
                 matcher, buoys, racelog, watches, trend, plangap, gpsout, checklist,
                 power, sensor_health, plume, orinroute, orinassets, racegate, boatlib,
                 hardware)

app = FastAPI(title="Agent_C4 Onboard Engine", version="0.1.0")
# The iPad reaches the Pi directly over boat-local Wi-Fi in race mode; allow cross-origin so a
# browser pointed straight at the engine works without a proxy.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "ok", "service": "onboard-engine", "llm": "none (deterministic)",
            "data_source": os.environ.get("DATA_SOURCE", "onboard"),
            # is the full-res archive being written? (the archiver's status file, read-only)
            "archiver": archiver_status.read()}


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


# The heading check's last DECIDED verdict and when it was decided, held here rather than in
# `sensor_health` so the module stays pure and the cloud's retro sweep cannot inherit the boat's
# live state. The pair carries the warn release band and the danger hold across polls.
_LAST_HEADING_STATUS = None
_LAST_HEADING_AT = None


@app.get("/health/sensors")
def sensor_health_ep():
    """Is an instrument that is still reporting still RIGHT, and where is each number FROM?
    Attitude range gate + a heading-vs-GPS-course cross-check + the provenance/policy checks.

    Added 2026-09-07 from the Jul 18 race: the compass/GPS was kicked at 22:58Z (roll stepped
    to 133 deg, then sat inverted for 23 minutes), was put back upright, and then read a
    quarter turn out — plausible-looking and wrong — for the remaining seven hours. Nothing
    aboard noticed either state.

    The provenance half (2026-09-08) is the other half of the same lesson: the priority policy,
    the AIS read filter and the per-channel `fell_back` flag all existed and none of them was
    ever shown to anyone, so "silently not in force" looked exactly like "working". This is the
    single endpoint the iPad's instrument-health chip reads."""
    global _LAST_HEADING_STATUS, _LAST_HEADING_AT
    now = time.time()
    out = sensor_health.assess(
        conditions=onboard_conditions.get_current_conditions(),
        previous=_LAST_HEADING_STATUS,
        previous_age_s=None if _LAST_HEADING_AT is None else max(0.0, now - _LAST_HEADING_AT))
    h = (out or {}).get("heading") or {}
    # A HELD verdict must not refresh its own age, or the hold never expires — it would keep
    # citing itself as the evidence for citing itself.
    if h.get("status") in ("ok", "warn", "danger") and not h.get("held"):
        _LAST_HEADING_STATUS, _LAST_HEADING_AT = h["status"], now
    return with_n2k_check(out, _N2K)


# ---- N2K address drift (#185-4) ------------------------------------------------------------
# N2K addresses are claimed at power-up and can move. The static address→device map in
# shared/n2k_sources was taken from one boat pull; if an address is re-claimed, priority would
# silently bind to whatever device now sits there. So the engine reads Signal K's own sources
# list at start (and again every few minutes, since a device can power up late or re-claim):
#   * source priority follows the VERIFIED map (`n2k_sources.verified`): the device, not the
#     address;
#   * the result rides /health/sensors as provenance.checks.n2k_addresses, so the DATA tile
#     shows it.
# A device that moved and was found again is a note (priority followed it). A mapped device
# that is no longer on the bus while something else holds its address is a warning. Never
# having read the list is "not checked": a note, never a pass and never a warning.
from shared.boat_id import require as _require_setting  # noqa: E402
BOAT_ID = _require_setting("BOAT_ID")   # no default: another boat must never write as sr33 (#307)
N2K_RECHECK_S = 300.0       # after a good read
N2K_RETRY_S = 30.0          # until the first good read (Signal K may start after the engine)
_N2K = {"checked_at": None, "error": None, "moved": [], "static": None, "live": {},
        "disproved": set()}


def _signalk_sources_url():
    explicit = os.environ.get("SIGNALK_SOURCES_URL")
    if explicit:
        return explicit
    ws = os.environ.get("SIGNALK_WS", "ws://localhost:3010/signalk/v1/stream?subscribe=all")
    base = ws.split("/signalk/", 1)[0]
    base = "https://" + base[6:] if base.startswith("wss://") else "http://" + base.split("://", 1)[-1]
    return base + "/signalk/v1/api/sources"


def flatten_sources(payload):
    """Signal K's REST `/signalk/v1/api/sources` nests N2K devices by provider then address
    (`{"n2k-socketcan": {"15": {"n2k": {...}}}}`); `sources-cache.json`, which the map was built
    from, is flat by `$source` label with the product info under `updates[].source`. Accept
    both: nested entries become the flat shape under their `$source` label (`n2k-socketcan.15`)."""
    out = {}
    for label, entry in (payload or {}).items():
        if not isinstance(entry, dict):
            continue
        if "updates" in entry:
            out[label] = entry
            continue
        for addr, dev in entry.items():
            if isinstance(dev, dict) and isinstance(dev.get("n2k"), dict):
                n = dict(dev["n2k"])
                if n.get("manufacturerName"):          # the name, never the numeric code
                    n["manufacturerCode"] = n["manufacturerName"]
                out[f"{label}.{addr}"] = {"updates": [{"source": n}]}
    return out


def apply_n2k_payload(payload, state=None, boat_id=None, now=None):
    """One check against a Signal K sources payload: install the verified map for priority and
    record what moved. Pure apart from `n2k_sources.set_verified` and `state`."""
    state = _N2K if state is None else state
    boat_id = boat_id or BOAT_ID
    payload = flatten_sources(payload)
    static = n2k_sources.DEVICES_BY_BOAT.get(boat_id, {})
    state["static"] = bool(static)
    if not static:                  # no map for this boat: nothing to verify, label matching
        state.update(checked_at=now or time.time(), error=None, moved=[])
        return state
    fresh = n2k_sources.build_from_signalk(payload)
    if not fresh:
        # nothing to compare against: that is "not checked", never "as expected" (and never a
        # fresh timestamp on old evidence)
        raise ValueError("no N2K product info in the sources list")
    # What has been SEEN, carried across reads: a later list that omits a device (not yet
    # re-announced) must not restore an identity an earlier read disproved. A fresh answer
    # wins; a device seen at a new address drops its old one.
    same, find = n2k_sources.same_device, n2k_sources.find
    prev = state.get("live") or {}
    for a, f in fresh.items():          # a serial-less re-announce keeps the serial we knew
        known = prev.get(a) or ({} if a in (state.get("disproved") or ()) else static.get(a)) or {}
        if not f.get("serial") and known.get("serial") and same(known, f):
            f["serial"] = known["serial"]
    live = {a: d for a, d in prev.items()
            if a not in fresh and not any(same(d, f) and d.get("serial") and f.get("serial")
                                          for f in fresh.values())}
    live.update(fresh)
    state["live"] = live
    seen = {a: {"updates": [{"source": {"manufacturerCode": d.get("manufacturer"),
                                        "modelId": d.get("model"),
                                        "modelSerialCode": d.get("serial")}}]}
            for a, d in live.items()}
    # A static address once disproved (its device seen elsewhere, or another device seen on
    # it) never falls back to its static identity, even after the evidence is overwritten.
    disproved = set(state.get("disproved") or ())
    disproved |= {a for a, d in static.items()
                  if (a in live and not same(live[a], d))
                  or find(d, live, prefer=a) not in (None, a)}
    state["disproved"] = disproved
    moved = n2k_sources.moved(seen, static)
    # a device that moved once and is now not seen anywhere is missing, even with its first
    # address silent: drift() can't see that (it needs the address to answer)
    listed = {m["source"] for m in moved}
    moved += [{"source": a, "expected": static[a].get("model"),
               "found": (live.get(a) or {}).get("model"), "now_at": None}
              for a in sorted(disproved)
              if a not in listed and find(static[a], live, prefer=a) is None]
    state["moved"] = moved
    base = {a: d for a, d in static.items() if a not in disproved}
    n2k_sources.set_verified(boat_id, n2k_sources.verified(seen, base))
    state.update(checked_at=now or time.time(), error=None)
    return state


def check_n2k_once(state=None, fetch=None):
    """Read Signal K's sources and apply them. A failed read keeps the last good map."""
    state = _N2K if state is None else state
    try:
        if fetch is None:
            with urllib.request.urlopen(_signalk_sources_url(), timeout=5) as r:
                payload = json.loads(r.read().decode("utf-8"))
        else:
            payload = fetch()
        if not isinstance(payload, dict) or not payload:
            raise ValueError("empty sources list")
        apply_n2k_payload(payload, state)
        return True
    except Exception as exc:        # a health check must never take the engine down
        state["error"] = str(exc)[:200]
        return False


def _n2k_loop():
    while True:
        good = check_n2k_once()
        time.sleep(N2K_RECHECK_S if good else N2K_RETRY_S)


@app.on_event("startup")
def _start_n2k_check():
    if os.environ.get("N2K_CHECK", "1") != "0":
        threading.Thread(target=_n2k_loop, name="n2k-check", daemon=True).start()


def n2k_check(state, now=None):
    """The provenance check row for the DATA tile."""
    now = now or time.time()
    if state.get("static") is False:
        return {"status": "ok", "moved": [], "checked_at": state.get("checked_at"),
                "reason": "no sensor map for this boat", "note": None}
    if not state.get("checked_at"):
        return {"status": "unknown", "moved": [], "checked_at": None, "error": state.get("error"),
                "reason": "sensor addresses not checked",
                "note": "sensor addresses not checked"}
    mv = state.get("moved") or []
    lost = [m for m in mv if not m.get("now_at")]
    found = [m for m in mv if m.get("now_at")]
    age = max(0.0, now - state["checked_at"])
    row = {"status": "warn" if lost else "ok", "moved": mv, "checked_at": state["checked_at"],
           "age_s": round(age, 1), "error": state.get("error")}
    row["reason"] = ("; ".join(f"{m['expected']} not found"
                               + (f", {m['source']} is now {m['found']}" if m.get("found") else "")
                               for m in lost)
                     if lost else "sensor addresses as expected")
    row["note"] = ("; ".join(f"{m['expected']} moved to {m['now_at']}, still used" for m in found)
                   if found else None)
    return row


def with_n2k_check(out, state, now=None):
    """Add the N2K address check to a /health/sensors payload. A warning raises the chip (a
    known warning always wins, even over "unknown"); "not checked" and moves are notes only."""
    if not isinstance(out, dict):
        return out
    row = n2k_check(state, now)
    prov = out.get("provenance")
    if isinstance(prov, dict):
        prov.setdefault("checks", {})["n2k_addresses"] = row
    if row["status"] == "warn":
        out.setdefault("flags", []).append(row["reason"])
        if isinstance(prov, dict):
            prov.setdefault("flags", []).append(row["reason"])
            if prov.get("status") != "danger":
                prov["status"] = "warn"
        if out.get("status") != "danger" and out.get("available"):
            out["status"] = "warn"
        out["reason"] = "; ".join(out["flags"])
    if row.get("note"):
        out.setdefault("notes", []).append(row["note"])
        if isinstance(prov, dict):
            prov.setdefault("notes", []).append(row["note"])
    return out


@app.get("/power")
def power_ep():
    """House-bank state: level, trend, projected time to the brownout floor, status.

    Added 2026-09-07. On Jul 18 the bank fell 12.91 -> 11.61 V over six hours of racing and at
    the bottom of that curve the full-res archiver and the AIS transceiver stopped in the same
    minute; the boat retired. The voltage was in the archive the whole time and no surface read
    it. This endpoint is that surface — deterministic, no LLM, legal in-race like the rest."""
    return power.assess()


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
        return {"available": False, "points": [], "note": "No wind data for this period."}
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
    old_wind = False
    if tws is None or twa is None:
        # The strip's tws/twa are null past the wind's dead limit (15 s aboard, #247): no sail
        # pick on old wind. Until then this read trusted wind up to the strip's 5-min window.
        s = onboard_conditions.get_strip()
        tws = tws if tws is not None else s.get("tws")
        twa = twa if twa is not None else s.get("twa")
        old_wind = bool({"tws", "twa"} & set(s.get("dead") or ()))
    if hoisted is None:
        hoisted = (matcher.get_sail_state() or {}).get("hoisted")
    bm = (deviation._load_playbook() or {}).get("boat_model")
    out = sails.get_sail_advice(tws, twa, hoisted, boat_model=bm)
    if old_wind and not out.get("available"):
        out = dict(out, note="Wind too old for a sail call.", wind_old=True)
    return out


@app.get("/course")
def course(route: str | None = None):
    """The active course: marks + per-leg bearing/distance (for the schematic plot)."""
    return navigator.get_course(route)


@app.get("/navigator")
def nav(route: str | None = None):
    """Next mark, ETA, leg type, and laylines from live position + wind."""
    return navigator.get_navigator(route)


def _nav_changed(out):
    """After the crew moved the next mark: the plotter's next-mark index is read fresh, not
    from the few-second cache."""
    if out.get("ok"):
        gpsout._NEXT_MARK["at"] = None
    return out


@app.post("/navigator/start")
def nav_start():
    """The crew's Start tap (#246): a new run, next mark = the first one. Undo puts it back."""
    return _nav_changed(navigator.start_run())


@app.post("/navigator/undo")
def nav_undo():
    """Undo the last mark change (an advance, a pick or a Start)."""
    return _nav_changed(navigator.undo_mark())


@app.post("/navigator/mark")
def nav_mark(body: dict):
    """The crew picks the next mark by its place in the course ({index}): the right one of a
    repeated mark."""
    return _nav_changed(navigator.pick_mark((body or {}).get("index")))


@app.post("/course/practice")
def practice_course(leg_nm: float = 1.0):
    """Drop a windward/leeward practice course from the live position + wind (route 'practice')."""
    return navigator.make_practice_course(leg_nm)


@app.post("/course/load")
def course_load(body: dict):
    """Load a RaceDefinition course into the onboard navigator (the deployed homework). Body:
    {definition, course_id?, route?}. Writes the flattened marks to the Pi marks store + activates.
    Pre-race homework: refused while a race session runs."""
    from shared.race_def import course_to_marks
    definition = (body or {}).get("definition") or {}
    marks, skipped, cid = course_to_marks(definition, (body or {}).get("course_id"))
    if not marks:
        return {"loaded": False, "detail": "no usable marks (need coordinates)"}
    route = (body or {}).get("route") or definition.get("race_id") or "race"
    # The full course (islands, rounding sides, zones) for the Orin's re-route is written to a
    # temp file first, then the marks, then the load id, then the file goes live, all under one
    # lock (two loads never interleave; /reroute/run reads under it too). The definition carries the
    # load id, so it only ever pairs with its own load's marks. Any failure: the old definition is
    # dropped and the load reports it.
    with racegate.hold() as racing, orinroute.COURSE_LOCK:
        if racing:
            return racegate.refused()
        tmp = None
        try:
            load_id = orinroute.new_load_id()
            tmp = orinroute.prepare_definition(definition, cid, route, load_id)
            datasource.active().save_course(route, marks)
            navigator.set_active(route)
            run = navigator.start_run(route, keep_undo=False)   # a course load is a new run (#246)
            if run.get("save_failed"):
                raise RuntimeError(f"navigator run not reset: {run.get('note')}")
            orinroute.set_load_id(route, load_id)
            if tmp:
                orinroute.commit_definition(tmp)
            else:
                orinroute.drop_definition()
        except Exception as exc:  # noqa: BLE001
            orinroute.discard(tmp)
            orinroute.drop_definition()
            print(f"[course/load] failed: {type(exc).__name__}: {exc}", flush=True)
            return {"loaded": False, "detail": "Couldn't save the course. Try again."}
        # still under the lock, so no /gps/show for the new course can slip in before it: stop a
        # plotter route made for any OTHER course (bounded, ~1.5 s at most; n2kout also ends it
        # by itself on the token, which the new load id has already changed)
        gpsout.clear_other_courses()
    return {"loaded": True, "route": route, "course_id": cid, "marks": len(marks), "skipped": skipped}


@app.post("/reroute/assets")
def reroute_assets_load(body: dict):
    """The Orin re-route's land chart + the Lab's reviewed boat profile, from the Lab's Deploy push
    (#139, #140). Kept here and sent with each re-route request. Pre-race homework: refused while
    a race session is running."""
    return orinassets.load(body)


@app.get("/reroute/assets")
def reroute_assets_get():
    """Which chart and boat profile the Orin's re-route gets (no geometry)."""
    return orinassets.get()


@app.get("/tactics")
def tactics_ep(route: str | None = None):
    """Tactical read: lifted/headed, favored side, leverage."""
    return tactics.get_tactics(route)


@app.get("/forecast")
def forecast_ep(lat: float | None = None, lon: float | None = None, hours: int = 12):
    """Wind forecast (Open-Meteo — common public data) for a position; defaults to live."""
    if lat is None or lon is None:
        s = onboard_conditions.get_strip()
        # a forecast is for an area: the last fix within 30 min will do, even past the GPS's
        # 10 s dead limit (#247), so the raw position is the fallback
        raw = s.get("raw") or {}
        lat = lat if lat is not None else (s.get("lat") if s.get("lat") is not None
                                           else (raw.get("lat") or {}).get("value"))
        lon = lon if lon is not None else (s.get("lon") if s.get("lon") is not None
                                           else (raw.get("lon") or {}).get("value"))
    if lat is None or lon is None:
        return {"available": False, "note": "No position yet."}
    return weather.get_forecast(lat, lon, hours)


@app.post("/plume/load")
def plume_load(body: dict):
    """Cache the Lab's forecast plume ON DISK (every router model along the planned route, run by
    run). The Lab pushes it over Tailscale whenever the boat is reachable, the same way its Deploy
    push loads the playbook. Replaces the previous plume."""
    return plume.load(body)


@app.post("/plume/onboard")
def plume_onboard(body: dict, request: Request):
    """Cache a plume the boat's OWN Orin computed from public model data (pi/orin/plume) — legal
    in-race, so taken while racing. Accepted only straight from the Orin's boat-local address,
    never through a proxy; the shore /plume/load rules are unchanged."""
    return plume.load_onboard(body, request.client.host if request.client else None,
                              headers=request.headers)


@app.get("/plume")
def plume_get():
    """The cached forecast plume + its age (`age_s` since the Lab recorded the newest run,
    `received_age_s` since the last push). Survives restarts and works offline."""
    return plume.get()


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
    {definition, own?} or {fleet, scoring, own?, tracker?}. Frozen at the gun: refused while a race
    session runs (the roster already aboard stays, and reading it is legal in-race)."""
    from shared.race_def import fleet_blob
    body = body or {}
    if body.get("definition") is not None:
        blob = fleet_blob(body["definition"], body.get("own"))
    else:
        blob = {"fleet": body.get("fleet") or [], "scoring": body.get("scoring") or {},
                "own": body.get("own") or {}, "tracker": body.get("tracker") or {}}
    with racegate.hold() as racing:
        if racing:
            return racegate.refused()
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
    the gun: refused while a race session runs (reading the one aboard is legal in-race).
    Replaces any prior.

    The same bundle goes on to the Orin copilot over the Pi-Orin cable (COPILOT_URL) while this
    load still holds the race log's session lock (stage + a time-bounded commit, see
    copilot_load), so the Orin's write can't land after a race starts. The Orin takes a playbook
    only from here. Its answer is under `copilot`."""
    body = body or {}
    bundle = body.get("bundle") if "variants" not in body else body
    if not bundle or not (bundle.get("variants")):
        return {"loaded": False, "detail": "no bundle with variants"}
    with racegate.hold() as racing:
        if racing:
            return racegate.refused()
        datasource.active().save_playbook(bundle)
        deviation.reset_state()          # a new playbook → clear the Schmitt/trend memory
        drift.reset_state()
        plangap.reset_state()            # the plan-gap read measures against the NEW promise
        matcher.clear_state()            # forget play arm/sustain memory too
        selector.reset_state()           # and the card's held verdict + downwind clock (#40)
        # a boat whose profile says it has no Orin (#313): nothing to send, said plainly
        if hardware.has_orin((bundle.get("boat_model") or {}).get("boat_id")):
            cop = copilot_load(bundle)   # still under the lock: the Orin writes before a race can start
        else:
            cop = {"loaded": False, "absent": True, "detail": boat_hardware.NO_ORIN}
    return {"loaded": True, "race_id": bundle.get("race_id"),
            "variants": len(bundle.get("variants") or []),
            "plays": len(bundle.get("plays") or []),
            "recommended": bundle.get("recommended"), "copilot": cop}


NO_COPILOT = "Can't reach the Orin. Playbook not sent there."
NO_CONFIRM = "The Orin didn't confirm. Check it."
MAX_ORIN_TTL_S = 10.0


def _copilot_post(path, body, timeout):
    """POST JSON to the Orin copilot over the cable: its answer dict, or {loaded: false, detail}.
    Never raises."""
    import json as _json
    import urllib.error
    import urllib.request
    url = os.environ.get("COPILOT_URL", "http://10.10.10.2:8300").rstrip("/") + path
    req = urllib.request.Request(url, data=_json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = _json.loads(r.read() or b"{}")
        return out if isinstance(out, dict) else {"loaded": False, "detail": NO_COPILOT}
    except urllib.error.HTTPError as exc:
        try:
            out = _json.loads(exc.read() or b"{}")
            if isinstance(out, dict) and out.get("detail"):
                return {"loaded": False, "detail": str(out["detail"])[:200]}
        except Exception:  # noqa: BLE001
            pass
        return {"loaded": False, "detail": f"Orin answered {exc.code}. Not loaded."}
    except Exception:  # noqa: BLE001
        return {"loaded": False, "detail": NO_COPILOT}


def copilot_load(bundle):
    """Load the bundle on the Orin copilot in two steps; called with the race log's session lock
    held, and it returns only when the Orin's write can no longer happen.

      1. stage {nonce, bundle}: the Orin holds it in memory and answers its commit TTL. Its TTL
         clock starts when it RECEIVES the stage (its own monotonic clock).
      2. commit {nonce}: sent only after the stage answered. The Orin writes only within the TTL,
         re-checked right before the file goes live.

    The commit is sent after the stage was received, and the Pi waits for its answer for at least
    the TTL (+1 s) before returning (urllib's timeout is per socket wait, so never less). So the
    Orin's write can only happen while this lock is held: a stalled request can't land after the
    Pi gave up and a race started. No wall clocks are compared (skew-proof). A race-start tap waits
    at most COPILOT_STAGE_TIMEOUT_S + the commit wait (~8 s). Never raises."""
    import secrets
    nonce = secrets.token_hex(12)
    st = _copilot_post("/playbook/stage", {"nonce": nonce, "bundle": bundle},
                       float(os.environ.get("COPILOT_STAGE_TIMEOUT_S", "4")))
    if not st.get("staged") or st.get("nonce") != nonce:
        return {"loaded": False, "detail": st.get("detail") or NO_COPILOT}
    try:
        ttl = float(st.get("ttl_s"))
    except (TypeError, ValueError):
        ttl = float("nan")
    if not 0 < ttl <= MAX_ORIN_TTL_S:
        return {"loaded": False, "detail": "Orin commit window is wrong. Not loaded."}
    wait = max(float(os.environ.get("COPILOT_COMMIT_TIMEOUT_S", "4")), ttl + 1.0)
    sent = time.monotonic()
    out = _copilot_post("/playbook/commit", {"nonce": nonce}, wait)
    if out.get("loaded") is True:
        approve_copilot_playbook(bundle_hash(bundle))     # the one the copilot may use
        return out
    # Not confirmed (a refusal, a reset, a timeout): the Orin may still be writing. Approve
    # NOTHING (a late file is never used, see copilot playbook.load), and keep holding the lock
    # until the Orin's commit window has certainly closed: a timeout is a maximum, not a minimum.
    approve_copilot_playbook(None)
    _sleep(max(0.0, sent + ttl + 1.0 - time.monotonic()))
    if out.get("detail") == NO_COPILOT:
        return {"loaded": False, "detail": NO_CONFIRM}
    return out


_sleep = time.sleep                      # tests swap it


def bundle_hash(bundle):
    """sha256 of the whole bundle as canonical JSON. MUST match pi/orin/copilot/playbook.bundle_hash."""
    import hashlib
    import json as _json
    return hashlib.sha256(_json.dumps(bundle, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False).encode("utf-8")).hexdigest()


def _approved_path():
    base = os.path.dirname(os.environ.get("ENGINE_DB", "/var/lib/sr33/engine/engine.db"))
    return os.path.join(base, "copilot-playbook.json")


def approve_copilot_playbook(h):
    """Record which playbook the Orin copilot may use (its hash, or None for none). Only ever
    called from copilot_load, i.e. under the race log's session lock with no race running, so
    the approved hash can't change during a race: it is the playbook pinned at the gun."""
    import json as _json
    import tempfile
    path = _approved_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".cpb-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        _json.dump({"hash": h, "at": int(time.time())}, f)
        f.flush()
        os.fsync(f.fileno())             # durable before it goes live: a power cut keeps one whole
    os.replace(tmp, path)
    dfd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


@app.get("/playbook/copilot")
def copilot_playbook_approved():
    """{hash}: the playbook the Orin copilot may use (the last one it confirmed under the race
    lock), or null for none. The copilot asks before it uses its file; anything else, it runs
    without a playbook."""
    import json as _json
    try:
        with open(_approved_path()) as f:
            h = (_json.load(f) or {}).get("hash")
    except (OSError, ValueError):
        h = None
    return {"hash": h if isinstance(h, str) else None}


@app.get("/playbook")
def playbook_status():
    """Which playbook is aboard (race, bundle id, variants/plays, signature prefix) — read-only,
    no geometry. The Lab's Deploy tab polls it to show what the boat actually holds."""
    return deviation.playbook_summary(deviation._load_playbook())


@app.get("/playbook/track")
def playbook_track(variant: str | None = None):
    """The loaded playbook's optimizer route — the ACTIVE variant's frozen `route.path` points —
    so the console course map can draw the plan line, not just the rhumb legs. Same variant pick
    as the deviation core (requested id → bundle recommended → first). Pre-loaded homework only:
    legal in-race."""
    bundle = deviation._load_playbook()
    if not bundle:
        return {"available": False, "note": "no playbook aboard"}
    v = deviation._pick_variant(bundle, variant)
    path = ((v or {}).get("route") or {}).get("path") or []
    if not path:
        return {"available": False, "note": "Gameplan option has no track."}
    return {"available": True, "race_id": bundle.get("race_id"),
            "variant": str(v.get("id") or v.get("name") or ""),
            "variant_label": v.get("name") or str(v.get("id") or "the plan"),
            "recommended": bundle.get("recommended"),
            "start_epoch": bundle.get("start_epoch"),
            "points": [{"lat": p.get("lat"), "lon": p.get("lon"), "t": p.get("t")} for p in path
                       if p.get("lat") is not None and p.get("lon") is not None]}


@app.get("/plays")
def plays(route: str | None = None):
    """Playbook v2 Phase D — the Tier-1 PLAY MATCHER: every play in the frozen v2 bundle evaluated
    against the boat's live signals (deterministic Schmitt sustain; armed first). Legal in-race —
    own instruments + pre-loaded homework; works with no Orin aboard."""
    return matcher.get_plays(route)


@app.post("/gps/show")
def gps_show(body: dict | None = None):
    """Put a route on the CHARTPLOTTER (Garmin 943) — the iPad's button. Assembles the frozen
    bundle variant's path (default: recommended) or the onboard re-route and hands it to the
    n2kout broadcaster (129285/129284/129283 on the boat's own bus — RRS-41 clean).
    Body: {source: "playbook"|"reroute"|"orin"|"reoptimize", variant?: "<id>", route?: "<route>"}.
    "reroute" is the newest route either re-router made (the Route tile's); it is never computed
    here, only sent."""
    body = body or {}
    return gpsout.show(source=body.get("source") or "playbook",
                       variant=body.get("variant"), route=body.get("route"))


@app.post("/gps/clear")
def gps_clear():
    """Stop broadcasting — the plotter drops the external route when the stream goes quiet."""
    return gpsout.clear()


@app.get("/gps/fix")
def gps_fix():
    """For n2kout (polled every second while a route is on the plotter): the freshest own-ship
    fix {lat, lon, t} or null, and the course token. Read-only."""
    return gpsout.fix_feed()


@app.post("/gps/leg-check")
def gps_leg_check(body: dict | None = None):
    """For n2kout, before it moves the plotter's steer-to on: is boat → waypoint clear of the
    obstacles aboard (the same checks as /gps/show)? {from, to, course} → {clear, reason}."""
    return gpsout.leg_check(body or {})


@app.get("/gps/status")
def gps_status(route: str | None = None):
    """The broadcaster's state + the bundle's variant ids (for the iPad's buttons)."""
    return gpsout.status(route)


@app.get("/trend")
def trend_ep(route: str | None = None):
    """Wind TREND — deterministic 1 h/3 h rate-of-change of own observed TWS/TWD (bucket means
    from the full-res archive). An observation, not an alarm; feeds the strategy picture, the
    play predicates (`tws_trend_kn_per_hr`/`twd_trend_deg_per_hr`) and the copilot's briefs."""
    return trend.get_trend(route)


@app.get("/plangap")
def plangap_ep(route: str | None = None):
    """PLAN GAP — own OBSERVED wind vs the frozen bundle's forecast promise for here/now (the
    read `drift`'s forecast-vs-forecast design deliberately can't make). Schmitt-banded; legal
    in-race (own instruments + pre-loaded homework)."""
    return plangap.get_plangap(route)


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
    out_of_service?: ['S1', ...]}. The configuration is a SET (the boat flies combinations —
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
    {action: 'hold'|'swap'|'all_hands'|'set_on', ...} (the iPad quick edits) · {clear: true}.
    A whole new plan is homework: refused while a race session runs. The crew's quick edits and
    clear are always taken."""
    if isinstance(body, dict) and "plan" in body:
        with racegate.hold() as racing:
            if racing:
                return racegate.refused()
            return watches.set_watch(body)
    return watches.set_watch(body)


@app.get("/checklist")
def checklist_status(route: str | None = None):
    """The RACE CHECKLIST (the SI/NOR `deliver_to_ipad` requirement subset): every item with live
    status (pending/active/done/manual) + measure — nav lights at sunset, the Cove Island gate
    photo on the approach, the finish procedure/photo/numbers on the finish approach. Deterministic
    triggers on the boat's own computer + pre-loaded homework — legal in-race; empty-but-valid
    when nothing is loaded."""
    return checklist.get_checklist(route)


@app.post("/checklist/load")
def checklist_load(body: dict):
    """Load the iPad checklist (homework `checklists_ipad`). Body: {items: [...]} /
    {checklists_ipad: [...]} / {definition}. Replaces any prior list; clears acks. Homework:
    refused while a race session runs (acks are always taken)."""
    with racegate.hold() as racing:
        if racing:
            return racegate.refused()
        return checklist.load(body)


@app.post("/checklist/ack")
def checklist_ack(body: dict):
    """Crew ack from the iPad: {id, undo?} — done for the current window (a sunset item
    re-arms the next evening). Returns the fresh checklist state."""
    return checklist.ack(body)


@app.get("/session")
def session_status():
    """The RACE LOG state: active session + recent windows. Sessions mark what the boat KEEPS
    (full-res, backfilled, debriefed); everything outside them is pruned after the retention
    period and never leaves the boat. No Lab prep / RaceDefinition needed.

    `archiver` says whether full-res rows are reaching the disk at all; the LOG button shows
    "Not recording: <reason>" when they are not (a session marks what to KEEP, it records
    nothing by itself)."""
    out = racelog.status()
    out["archiver"] = archiver_status.read()
    # the LOG picker (#320): the boat library, the boat being sailed, the playbook's boat
    try:
        out["boats"] = boatlib.view()
    except Exception as exc:  # noqa: BLE001 — the LOG button must work without it
        out["boats"] = {"boats": [], "current": None, "note": f"boats unreadable ({type(exc).__name__})"}
    return out


@app.get("/boats")
def boats_get():
    """The boat library aboard (synced from the Lab whenever the Pi has internet, #320), the boat
    being sailed now (the session's, else the last one used) and the polar it gets."""
    return boatlib.view()


@app.on_event("startup")
def _start_boat_library_sync():
    boatlib.start_sync()


@app.post("/session/start")
def session_start(body: dict = None):
    """Start a session: {name?, race_id?, kind?: race|practice, boat_id?, boat_name?}. The iPad
    always sends the kind the crew picked. With no kind (old clients) it is a RACE: a race session
    is what closes the homework gate, so the default fails closed. The boat sailed (#320): a
    library boat's id, or a name alone for an "Other boat"; none → the last boat used."""
    body = body or {}
    return racelog.start(name=body.get("name"), race_id=body.get("race_id"),
                         kind=body.get("kind"), boat_id=body.get("boat_id"),
                         boat_name=body.get("boat_name"))


@app.post("/session/end")
def session_end(body: dict = None):
    """End the running session: {id?}. The iPad sends the id it shows; if another session is
    running now (one started in between), nothing is ended: "Already ended"."""
    return racelog.end(session_id=(body or {}).get("id"))


@app.post("/race/over")
def race_over(body: dict = None):
    """The crew ends the race: {since?}. Racing (RRS 41, no shore loading) is its own state; this
    is the one way the crew ends it (an abandoned race too). The iPad sends the race start it
    shows; if a newer race is running now, nothing is ended: "Already ended". The recording
    session (LOG) is not touched. `since` is required: without it the boat can't tell which race
    the crew meant, so nothing is ended."""
    since = (body or {}).get("since")
    if since is None:
        return {"ok": False, "note": "Which race?"}
    return racelog.race_over(since=since)


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
    """The Pi's own small re-route (the fallback when the Orin is down): the LAST route it made,
    flagged OFF-PLAYBOOK, with its divergence from the frozen plan. Never computes one: a route is
    made only on the crew's button (POST /reoptimize/run). Legal in-race (own computer + own
    polars + common data)."""
    return reoptimize.get_reoptimize(route=route)


@app.post("/reoptimize/run")
def reoptimize_run(body: dict = None):
    """The crew's Re-route now, on the Pi (the iPad offers it when the Orin can't be reached).
    Body: {device?: "iPad 1"}. One run at a time, in the background (~15 s): 202 started, 409 busy
    or nothing to route. GET /reroute reports progress and the route."""
    code, out = reoptimize.run(device=(body or {}).get("device"))
    return JSONResponse(out, status_code=code)


@app.post("/reroute/run")
def reroute_run(body: dict = None):
    """Ask the Orin for ONE full re-route (the Lab's multi-model optimizer, pi/orin/reroute) from
    the boat to the remaining marks. Body: {device?: "iPad 1"} — who asked, kept with the result.
    Runs only when called (the crew's Re-route button, #125); one at a time. 202 started, 409 busy
    (with progress) or nothing to route, 503 when the Orin can't be reached (`orin: "unreachable"`)
    or is shedding load (`orin: "paused"`, #295; the iPad then offers the Pi). Legal in-race: own
    computers + public forecasts; the route is labelled "Off the playbook"."""
    code, out = orinroute.run((body or {}).get("device"))
    return JSONResponse(out, status_code=code)


@app.get("/reroute")
def reroute_get(compact: bool = False):
    """The Orin re-route's status + progress, and the last route (stored here, so it survives a
    restart and the Orin being off); the Pi fallback's status + last route under `pi`; and which
    is newest (`latest`). `compact=1` drops the tracks (the iPad's Route tile polls this)."""
    return orinroute.tile(compact=compact)


@app.get("/reroute/history")
def reroute_history(limit: int = 50):
    """Every Re-route now press and its outcome, newest first: when, which device, which engine,
    and for a finished route the weather it used (models, cycles, ages) and its gain vs the plan
    (vps/agent/app/reroutelog.py; the history view is #127)."""
    from app import reroutelog
    return {"runs": reroutelog.history(max(1, min(500, limit)))}
