"""SQL-backed agent tools — collect-everything / multi-source paradigm.

Live data is captured per (source, path) in telemetry_raw. These tools surface ALL sources
for each quantity with provenance and freshness, so the agent can cross-check redundant
sensors, spot disagreement, and account for uncalibrated/unreliable sources rather than
trusting a single value.
"""
import math
import os
from datetime import datetime, timezone

from shared import n2k_sources
from shared import polar as shared_polar
from shared import staleness
from shared import units

from .db import pool
from . import datasource
from . import fatigue
from . import sails
from . import navigator
from . import tactics
from . import weather
from . import routing
from . import ais
from . import alerts as _alerts
from . import summarizer as _summarizer
from . import polar_tool as _polar_tool
from . import race_mode

from shared.boat_id import require as _require_setting  # noqa: E402
BOAT_ID = _require_setting("BOAT_ID")   # no default: another boat must never write as sr33 (#307)

_id = lambda x: x
_ms_to_kn = lambda x: x * 1.943844
_rad_to_deg = lambda x: x * 57.295779513
_k_to_c = lambda x: x - 273.15

# Signal K path -> (channel, display unit, converter). Redundant sources land on the same
# channel and are shown side-by-side.
PRESENT = {
    "navigation.speedThroughWater":    ("stw", "kn", _ms_to_kn),
    "navigation.speedOverGround":      ("sog", "kn", _ms_to_kn),
    "navigation.courseOverGroundTrue": ("cog", "°", _rad_to_deg),
    "navigation.headingTrue":          ("heading_true", "°", _rad_to_deg),
    "navigation.headingMagnetic":      ("heading_mag", "°", _rad_to_deg),
    "navigation.attitude.roll":        ("heel", "°", _rad_to_deg),
    "navigation.attitude.pitch":       ("pitch", "°", _rad_to_deg),
    "navigation.rateOfTurn":           ("rate_of_turn", "°/s", _rad_to_deg),
    "navigation.position.latitude":    ("lat", "°", _id),
    "navigation.position.longitude":   ("lon", "°", _id),
    "environment.wind.speedApparent":  ("aws", "kn", _ms_to_kn),
    "environment.wind.angleApparent":  ("awa", "°", _rad_to_deg),
    "environment.wind.speedTrue":      ("tws", "kn", _ms_to_kn),
    "environment.wind.angleTrueWater": ("twa", "°", units.rad_to_signed_twa_deg),  # signed, #47
    "environment.wind.directionTrue":  ("twd", "°", _rad_to_deg),
    "environment.depth.belowTransducer": ("depth", "m", _id),
    "environment.water.temperature":   ("water_temp", "°C", _k_to_c),
    "steering.rudderAngle":            ("rudder_angle", "°", _rad_to_deg),
    # The bank. Absent from both PRESENT tables until 2026-09-07, which is why a six-hour
    # discharge to 11.08 V went unremarked through the Jul 18 race — see app/power.py.
    "electrical.batteries.0.voltage": ("bank_voltage", "V", _id),
}
CHANNEL_TO_PATH = {ch: p for p, (ch, _, _) in PRESENT.items()}
# "sensors disagree" threshold by display unit (spread beyond expected noise)
DISAGREE = {"°": 6.0, "kn": 0.6, "m": 1.0, "°C": 2.0, "°/s": 5.0, "V": 0.5}
# A ranked source must be fresher than this to be used before falling back to the next. The cloud
# value of the channel table (`shared/staleness.py`, #247); every channel is 45 s here.
FAILOVER_AGE_S = staleness.failover_s("tws", "cloud")


def _load_priority():
    """channel -> ordered list of source matchers (rank 1 first) — the same table the engine's
    cloud reads use (`datasource.load_priority`)."""
    prio = datasource.load_priority()
    _warn_unresolved(prio)
    return prio


_warned_unresolved = False


def _warn_unresolved(prio):
    """Log once when a seeded matcher names nothing on this boat.

    This is the assertion the bug needed: an unresolvable matcher and a satisfied one produced
    identical output for a year, because the fallback ("freshest available") is also the happy
    path when the lead source is simply stale."""
    global _warned_unresolved
    if _warned_unresolved or not prio:
        return
    _warned_unresolved = True
    devices = n2k_sources.devices_for(BOAT_ID)
    if not devices:
        return
    labels = []
    try:
        with pool.connection() as conn:
            labels = [r["source"] for r in conn.execute(
                "SELECT DISTINCT source FROM telemetry_raw WHERE boat_id = %s "
                "AND time > now() - interval '7 days'", (BOAT_ID,)).fetchall()]
    except Exception:
        pass          # the check is diagnostic; never let it break a conditions read
    missing = n2k_sources.unresolved({m for ms in prio.values() for m in ms}, devices, labels)
    if missing:
        print(f"[tools] source_priority matchers that resolve to NO known source: {missing} — "
              f"those channels silently fall back to freshest-wins", flush=True)


def _choose_preferred(channel, readings, prio):
    """Pick the lead reading by priority, failing over when the preferred source is stale/absent.
    Returns (reading, reason, fell_back).

    Matching goes through `n2k_sources`, not `match in source`: the seeded matchers are device
    names (`orca`, `24xd`) while `$source` is an N2K address (`n2k-socketcan.15`), so the
    substring test could never succeed and every channel silently used the freshest source.
    Measured and fixed 2026-09-07 — see `shared/source_policy.py`."""
    devices = n2k_sources.devices_for(BOAT_ID)
    matchers = prio.get(channel, [])
    for i, m in enumerate(matchers):
        fresh = [r for r in readings
                 if n2k_sources.matches(r["source"], m, devices)
                 and r["age_s"] <= staleness.failover_s(channel)]
        if fresh:
            best = min(fresh, key=lambda r: r["age_s"])
            return best, f"priority rank {i+1} ({m})", i > 0
    best = min(readings, key=lambda r: r["age_s"])
    if matchers:
        return best, "no preferred source fresh — using freshest available", True
    return best, "no priority set — freshest available", False


def ais_bearing_sources():
    """Sources in `telemetry_raw` that carry AIS traffic — to be excluded from own-ship reads.
    One probe for the whole cloud agent: `datasource.ais_bearing_sources` (the CloudSource reads
    filter on it too, #45.2b). An empty or failed probe is retried, not memoised (#44.5)."""
    return datasource.ais_bearing_sources()


def _age(ts):
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - ts).total_seconds(), 1)


def _latest_per_source(paths, max_age_min):
    """Latest numeric value per (path, source) within the window, AIS-bearing sources left out
    (an AIS receiver's lat/lon is another boat's, #45.2b) — as the onboard read does."""
    ais = sorted(ais_bearing_sources())
    with pool.connection() as conn:
        return conn.execute(
            "SELECT DISTINCT ON (path, source) path, source, value, time "
            "FROM telemetry_raw WHERE boat_id = %s AND path = ANY(%s) "
            "AND time > now() - %s::interval AND value IS NOT NULL "
            + ("AND NOT (source = ANY(%s)) " if ais else "")
            + "ORDER BY path, source, time DESC",
            (BOAT_ID, list(paths), f"{int(max_age_min)} minutes") + ((ais,) if ais else ()),
        ).fetchall()


def get_current_conditions(max_age_minutes: int = 5):
    """Every live quantity, from EVERY reporting source, with freshness and a
    disagreement flag. Redundant sources are intentional — cross-check them."""
    rows = _latest_per_source(PRESENT.keys(), max_age_minutes)
    prio = _load_priority()
    channels = {}
    for r in rows:
        ch, unit, conv = PRESENT[r["path"]]
        channels.setdefault(ch, {"unit": unit, "readings": []})
        age = _age(r["time"])
        # ok / late / dead against the channel's limits (#291): the web page marks each reading
        # by it (cloud: late 45 s, dead 120 s) instead of its own 30 s.
        channels[ch]["readings"].append(
            {"source": r["source"], "value": round(conv(r["value"]), 3),
             "age_s": age, "state": staleness.state(ch, age)}
        )
    for ch, c in channels.items():
        vals = [x["value"] for x in c["readings"]]
        c["freshest_age_s"] = min(x["age_s"] for x in c["readings"])
        if len(vals) > 1:
            c["spread"] = round(units.channel_spread(ch, vals), 3)   # circular for angles (#47)
            c["disagreement"] = c["spread"] > DISAGREE.get(c["unit"], 1e9)
        # preferred source (with automatic failover) — keeps all readings visible
        best, reason, fell_back = _choose_preferred(ch, c["readings"], prio)
        c["preferred"] = {"source": best["source"], "value": best["value"],
                          "age_s": best["age_s"]}
        c["preferred_reason"] = reason
        if fell_back:
            c["fell_back"] = True   # preferred source stale/absent — on a backup
    if not channels:
        return {"available": False, "note": "no telemetry in window"}
    return {
        "available": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "channels": channels,
        "note": ("All sources are kept; `preferred` is the priority-ranked lead source with "
                 "automatic failover. `fell_back=true` means the preferred sensor was "
                 "stale/absent and a backup is in use — say so. Still cross-check disagreement."),
    }


def _alert_ages(have):
    """{channel: age_s} for alert-group key channels missing from the strip's 30-min window, from
    their last reading within ALERT_HOLD_S (24 h), so a dead sensor's alert outlives its number
    ("—" after 30 min). One bounded query, only when such a channel is missing."""
    missing = [CHANNEL_TO_PATH[c] for c in staleness.alert_channels()
               if c not in have and c in CHANNEL_TO_PATH]
    if not missing:
        return {}
    out = {}
    try:                        # a failed read here must never break the strip
        for r in _latest_per_source(missing, staleness.ALERT_HOLD_S // 60):
            ch = PRESENT[r["path"]][0]
            age = _age(r["time"])
            out[ch] = min(out.get(ch, age), age)
    except Exception:
        return {}
    return out


def get_strip():
    """Compact best-value-per-channel for the web instrument strip (freshest source wins)."""
    # Raw readings stay up to RAW_CAP_S (30 min) with their age; past each channel's DEAD limit
    # the value fields below go null, so nothing built on the strip (the sail call, the depth
    # alarm, the polar-deficit alert, the copilot's "Now:" line) uses it (#247). The raw number
    # and its state are in `raw` for the screens.
    cc = get_current_conditions(max_age_minutes=staleness.RAW_CAP_S // 60)
    if not cc.get("available"):
        return {"available": False}
    ch = cc["channels"]
    ages_by_ch = {name: c["preferred"]["age_s"] for name, c in ch.items()}

    def best(name):
        c = ch.get(name)
        if not c or staleness.is_dead(name, c["preferred"]["age_s"]):
            return None
        return c["preferred"]["value"]   # priority-ranked lead source (with failover)

    ages = [c["freshest_age_s"] for c in ch.values()]
    # True heading wins; a magnetic one is only a stand-in and says so (#183-8): on Lake Huron
    # variation is ~7-9°, so a bare "heading" that is really magnetic points the bow wrong.
    heading, heading_ref = best("heading_true"), "true"
    if heading is None:
        heading, heading_ref = best("heading_mag"), "magnetic"
    if heading is None:
        heading_ref = None
    # Fatigue is performance advice: the cloud withholds it while racing (DESIGN.md, ADR-0001;
    # /fatigue is 403 then, and this ungated strip used to hand out the same number, #48.2).
    # The Pi's own tile is not affected — that is `onboard_conditions.get_strip`.
    racing = race_mode.racing()
    fi = {} if racing else fatigue.compute_fatigue_index()
    return {
        "available": True, "as_of": cc["as_of"],
        "data_age_seconds": min(ages) if ages else None,
        "stale": (min(ages) if ages else 999) > 30,
        # Age of each value shown (the lead source's sample), per channel (#45.3). The two
        # fields above are the FRESHEST channel's age, so a fresh bank voltage keeps them near 0
        # while wind is minutes old; read the channel you use from here.
        "ages": ages_by_ch,
        # Per channel (#247): the lead's raw value, its age and ok/late/dead against that
        # channel's limits. A dead value is still shown (red age); past 30 min it is gone.
        "raw": {name: {"value": c["preferred"]["value"], "age_s": c["preferred"]["age_s"],
                       "state": staleness.state(name, c["preferred"]["age_s"])}
                for name, c in ch.items()},
        "dead": sorted(n for n, a in ages_by_ch.items() if staleness.is_dead(n, a)),
        # The per-channel stale alert (#247): one entry per sensor group whose key channel is dead.
        # (a channel gone from the screen after 30 min keeps its alert up to ALERT_HOLD_S)
        "dead_groups": staleness.dead_groups({**_alert_ages(set(ch)), **ages_by_ch}),
        "stw": best("stw"), "sog": best("sog"), "tws": best("tws"), "twa": best("twa"),
        "twd": best("twd"), "aws": best("aws"), "awa": best("awa"), "heading": heading,
        "heading_ref": heading_ref,
        "heel": best("heel"), "depth": best("depth"),
        "cog": best("cog"), "lat": best("lat"), "lon": best("lon"),
        # Helm fatigue index (0–100) for the strip; null while warming up / unavailable.
        "fatigue": fi.get("index"),
        "fatigue_level": fi.get("level"),
        **({"fatigue_withheld": True} if racing else {}),
    }


def get_fatigue():
    """Helm fatigue index (0–100) with per-component breakdown and a rotation recommendation."""
    return fatigue.compute_fatigue_index()


def get_sail_advice(tws: float = None, twa: float = None, hoisted: str = None):
    """Sail-range advice: optimal sail, position within the sail's TWA band, next crossover.
    Falls back to the latest live TWS/TWA when not supplied."""
    if tws is None or twa is None:
        s = get_strip()
        tws = tws if tws is not None else s.get("tws")
        twa = twa if twa is not None else s.get("twa")
    return sails.get_sail_advice(tws, twa, hoisted)


def get_navigator(route: str = None):
    """Next mark (bearing/distance/ETA), leg type, and laylines from live position + wind."""
    return navigator.get_navigator(route)


def get_tactics(route: str = None):
    """Tactical read: lifted/headed, favored side, leverage from the wind-shift trend."""
    return tactics.get_tactics(route)


def get_sources(max_age_minutes: int = 10):
    """Active sensor sources: what's reporting, how fresh, and curated reliability notes."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT source, max(time) AS last, count(DISTINCT path) AS paths, count(*) AS n "
            "FROM telemetry_raw WHERE boat_id = %s AND time > now() - %s::interval "
            "GROUP BY source ORDER BY source",
            (BOAT_ID, f"{int(max_age_minutes)} minutes"),
        ).fetchall()
        notes = conn.execute(
            "SELECT match, device, reliability, note FROM source_notes WHERE boat_id = %s",
            (BOAT_ID,),
        ).fetchall()
    out = []
    for r in rows:
        match = next((n for n in notes if n["match"].lower() in r["source"].lower()), None)
        out.append({
            "source": r["source"], "last_seen_s": _age(r["last"]),
            "paths": r["paths"], "samples": r["n"],
            "device": match["device"] if match else None,
            "reliability": match["reliability"] if match else "unknown",
            "note": match["note"] if match else None,
        })
    return {"count": len(out), "sources": out,
            "note": "Reliability is human-curated guidance; 'needs-calibration'/'unreliable' "
                    "sources should be treated with caution even if numbers look plausible."}


def get_history(channel: str, window_minutes: int, aggregation: str = "avg", source: str = None):
    """Trend/stats for a channel (or raw Signal K path) over a window, optionally one source."""
    path = CHANNEL_TO_PATH.get(channel, channel)
    conv = PRESENT.get(path, (None, None, _id))[2]
    window = f"{int(window_minutes)} minutes"
    params = [BOAT_ID, path, window]
    src_clause = ""
    if source:
        src_clause = " AND source = %s"
        params.append(source)
    with pool.connection() as conn:
        if aggregation == "series":
            rows = conn.execute(
                "SELECT time, source, value FROM telemetry_raw "
                "WHERE boat_id=%s AND path=%s AND time > now()-%s::interval AND value IS NOT NULL"
                + src_clause + " ORDER BY time", params,
            ).fetchall()
            return {"channel": channel, "path": path, "window_minutes": window_minutes,
                    "points": [{"time": r["time"].isoformat(), "source": r["source"],
                                "value": round(conv(r["value"]), 3)} for r in rows]}
        fn = {"avg": "avg", "min": "min", "max": "max"}.get(aggregation, "avg")
        row = conn.execute(
            f"SELECT {fn}(value) AS v, count(*) AS n FROM telemetry_raw "
            "WHERE boat_id=%s AND path=%s AND time > now()-%s::interval AND value IS NOT NULL"
            + src_clause, params,
        ).fetchone()
    return {"channel": channel, "path": path, "window_minutes": window_minutes,
            "aggregation": aggregation, "source": source,
            "value": round(conv(row["v"]), 3) if row["v"] is not None else None,
            "samples": row["n"]}




def _haversine_nm(a, b, c, d):
    r = 3440.065
    p1, p2 = math.radians(a), math.radians(c)
    dphi, dl = math.radians(c - a), math.radians(d - b)
    h = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return r * 2 * math.asin(min(1.0, math.sqrt(h)))


def _bearing(a, b, c, d):
    p1, p2, dl = math.radians(a), math.radians(c), math.radians(d - b)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1)*math.sin(p2) - math.sin(p1)*math.cos(p2)*math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def get_polar_target(tws: float, twa: float):
    """Polar target at (tws, twa), interpolated on the grid (`shared.polar.target`, the matcher's
    and fatigue's read), not the nearest bucket by |dTWS kn| + |dTWA deg| (#67): 11 kn / 90° read
    the 10 or 12 kn row. Pinching above the beat angle grades against the beat speed; inside 30°
    there is no target (None)."""
    table = [r for r in datasource.active().polars_stw() if r[2] is not None]
    if not table:
        return {"available": False, "note": "no polar data loaded"}
    stw = shared_polar.target(table, tws, twa)
    if not stw:
        return {"available": True, "query": {"tws": tws, "twa": twa}, "target_stw": None,
                "target_vmg": None, "note": "no polar target this close to the wind"}
    a = abs(((float(twa) + 180.0) % 360.0) - 180.0)
    return {"available": True, "query": {"tws": tws, "twa": twa},
            "target_stw": round(stw, 2),
            "target_vmg": round(stw * abs(math.cos(math.radians(a))), 2)}


def get_ais_targets(max_range_nm: float = 12):
    """AIS traffic with range/bearing and CPA/TCPA computed live vs own ship (see ais.py)."""
    return ais.get_ais_targets(max_range_nm)


def get_fleet(max_range_nm: float = 40):
    """Handicap-aware fleet tactics: roster-matched competitors with corrected-time delta (fleet.py)."""
    from . import fleet
    return fleet.get_fleet(max_range_nm)


def get_alerts():
    """Currently-active automated alerts (collision/safety/performance), most severe first."""
    return _alerts.get_alerts()


def get_summaries(limit: int = 5):
    """Recent stored on-demand summaries / debriefs (newest first)."""
    return _summarizer.get_summaries(limit)


def get_polar_analysis(hours: float = None, min_samples: int = None, point_of_sail: str = None):
    """Observed-vs-rated polar mined from the telemetry archive (% of ORC polar by TWS/TWA)."""
    return _polar_tool.get_polar_analysis(hours=hours, min_samples=min_samples,
                                          point_of_sail=point_of_sail)


def get_route_status(route: str = "default"):
    # The engine's own read (#45.1/#45.2b): the priority-preferred publisher with AIS sources left
    # out, and the fix's time kept so the answer can say how old the position is. The old
    # "newest row, any source" read could take an AIS target's position, and gave no age.
    src = datasource.active()
    la = src.latest_sample("navigation.position.latitude")
    lo = src.latest_sample("navigation.position.longitude")
    lat, lon = (la[0] if la else None), (lo[0] if lo else None)
    fix_age_s = (round(max(0.0, datetime.now(timezone.utc).timestamp() - min(la[1], lo[1])), 1)
                 if la and lo else None)
    with pool.connection() as conn:
        marks = conn.execute(
            "SELECT seq, name, lat, lon FROM waypoints WHERE route=%s ORDER BY seq", (route,),
        ).fetchall()
    if lat is None or lon is None:
        return {"available": False, "note": "no position fix yet"}
    if not marks:
        return {"available": False, "note": f"no waypoints for route '{route}'"}
    legs = []
    for m in marks:
        dist = _haversine_nm(lat, lon, m["lat"], m["lon"])
        legs.append({"seq": m["seq"], "name": m["name"], "distance_nm": round(dist, 2),
                     "bearing_deg": round(_bearing(lat, lon, m["lat"], m["lon"]), 1)})
    return {"available": True, "route": route, "next_mark": legs[0],
            "finish": legs[-1], "legs": legs, "fix_age_s": fix_age_s}


def fetch_forecast(lat: float = None, lon: float = None, hours: int = 12):
    """Wind forecast for a position (defaults to live position) — next N hours, Open-Meteo."""
    if lat is None or lon is None:
        s = get_strip()
        lat = lat if lat is not None else s.get("lat")
        lon = lon if lon is not None else s.get("lon")
    if lat is None or lon is None:
        return {"available": False, "note": "no position to forecast for"}
    return weather.get_forecast(lat, lon, hours)


def get_route(route: str = None, target: str = "next"):
    """Isochrone optimal route to the next mark (or 'finish') through the forecast wind."""
    return routing.get_route(route, target)


def log_note(text: str, author: str = None):
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO crew_notes (boat_id, author, text) VALUES (%s,%s,%s) RETURNING id, time",
            (BOAT_ID, author, text),
        ).fetchone()
        conn.commit()
    return {"logged": True, "id": row["id"], "time": row["time"].isoformat()}


DISPATCH = {
    "get_current_conditions": get_current_conditions,
    "get_sources": get_sources,
    "get_fatigue": get_fatigue,
    "get_sail_advice": get_sail_advice,
    "get_navigator": get_navigator,
    "get_tactics": get_tactics,
    "get_route": get_route,
    "get_history": get_history,
    "get_polar_target": get_polar_target,
    "get_ais_targets": get_ais_targets,
    "get_fleet": get_fleet,
    "get_alerts": get_alerts,
    "get_summaries": get_summaries,
    "get_polar_analysis": get_polar_analysis,
    "get_route_status": get_route_status,
    "fetch_forecast": fetch_forecast,
    "log_note": log_note,
}


def dispatch(name: str, args: dict):
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}'", "tool_failed": True}
    # One failing tool must not abort the whole chat turn (#183-9): the model gets an error result
    # for that call (agent.py marks it is_error) and can answer from the rest. The detail stays in
    # the server log; the model sees only the tool name and error type.
    try:
        return fn(**(args or {}))
    except Exception as exc:
        print(f"[tools] {name} failed: {exc!r}", flush=True)
        return {"error": f"{name} failed ({type(exc).__name__})", "tool_failed": True}
