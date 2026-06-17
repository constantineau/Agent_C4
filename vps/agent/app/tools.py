"""SQL-backed agent tools — collect-everything / multi-source paradigm.

Live data is captured per (source, path) in telemetry_raw. These tools surface ALL sources
for each quantity with provenance and freshness, so the agent can cross-check redundant
sensors, spot disagreement, and account for uncalibrated/unreliable sources rather than
trusting a single value.
"""
import math
import os
from datetime import datetime, timezone

from .db import pool
from . import fatigue
from . import sails
from . import navigator
from . import tactics
from . import weather
from . import routing
from . import ais

BOAT_ID = os.environ.get("BOAT_ID", "sr33")

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
    "environment.wind.angleTrueWater": ("twa", "°", _rad_to_deg),
    "environment.wind.directionTrue":  ("twd", "°", _rad_to_deg),
    "environment.depth.belowTransducer": ("depth", "m", _id),
    "environment.water.temperature":   ("water_temp", "°C", _k_to_c),
    "steering.rudderAngle":            ("rudder_angle", "°", _rad_to_deg),
}
CHANNEL_TO_PATH = {ch: p for p, (ch, _, _) in PRESENT.items()}
# "sensors disagree" threshold by display unit (spread beyond expected noise)
DISAGREE = {"°": 6.0, "kn": 0.6, "m": 1.0, "°C": 2.0, "°/s": 5.0}
# A ranked source must be fresher than this to be used before falling back to the next.
FAILOVER_AGE_S = 45


def _load_priority():
    """channel -> ordered list of source matchers (rank 1 first)."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT channel, match FROM source_priority WHERE boat_id = %s ORDER BY channel, rank",
            (BOAT_ID,),
        ).fetchall()
    prio = {}
    for r in rows:
        prio.setdefault(r["channel"], []).append(r["match"].lower())
    return prio


def _choose_preferred(channel, readings, prio):
    """Pick the lead reading by priority, failing over when the preferred source is stale/absent.
    Returns (reading, reason, fell_back)."""
    matchers = prio.get(channel, [])
    for i, m in enumerate(matchers):
        fresh = [r for r in readings if m in r["source"].lower() and r["age_s"] <= FAILOVER_AGE_S]
        if fresh:
            best = min(fresh, key=lambda r: r["age_s"])
            return best, f"priority rank {i+1} ({m})", i > 0
    best = min(readings, key=lambda r: r["age_s"])
    if matchers:
        return best, "no preferred source fresh — using freshest available", True
    return best, "no priority set — freshest available", False


def _age(ts):
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - ts).total_seconds(), 1)


def _latest_per_source(paths, max_age_min):
    """Latest numeric value per (path, source) within the window."""
    with pool.connection() as conn:
        return conn.execute(
            "SELECT DISTINCT ON (path, source) path, source, value, time "
            "FROM telemetry_raw WHERE boat_id = %s AND path = ANY(%s) "
            "AND time > now() - %s::interval AND value IS NOT NULL "
            "ORDER BY path, source, time DESC",
            (BOAT_ID, list(paths), f"{int(max_age_min)} minutes"),
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
        channels[ch]["readings"].append(
            {"source": r["source"], "value": round(conv(r["value"]), 3),
             "age_s": _age(r["time"])}
        )
    for ch, c in channels.items():
        vals = [x["value"] for x in c["readings"]]
        c["freshest_age_s"] = min(x["age_s"] for x in c["readings"])
        if len(vals) > 1:
            c["spread"] = round(max(vals) - min(vals), 3)
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


def get_strip():
    """Compact best-value-per-channel for the web instrument strip (freshest source wins)."""
    cc = get_current_conditions(max_age_minutes=5)
    if not cc.get("available"):
        return {"available": False}
    ch = cc["channels"]

    def best(name):
        c = ch.get(name)
        if not c:
            return None
        return c["preferred"]["value"]   # priority-ranked lead source (with failover)

    ages = [c["freshest_age_s"] for c in ch.values()]
    heading = best("heading_true")
    if heading is None:
        heading = best("heading_mag")
    fi = fatigue.compute_fatigue_index()
    return {
        "available": True, "as_of": cc["as_of"],
        "data_age_seconds": min(ages) if ages else None,
        "stale": (min(ages) if ages else 999) > 30,
        "stw": best("stw"), "sog": best("sog"), "tws": best("tws"), "twa": best("twa"),
        "twd": best("twd"), "aws": best("aws"), "awa": best("awa"), "heading": heading,
        "heel": best("heel"), "depth": best("depth"),
        "cog": best("cog"), "lat": best("lat"), "lon": best("lon"),
        # Helm fatigue index (0–100) for the strip; null while warming up / unavailable.
        "fatigue": fi.get("index"),
        "fatigue_level": fi.get("level"),
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


def _latest_value(path):
    """Freshest single value (any source) for a path, converted to display units."""
    with pool.connection() as conn:
        r = conn.execute(
            "SELECT value, time FROM telemetry_raw WHERE boat_id=%s AND path=%s "
            "AND value IS NOT NULL ORDER BY time DESC LIMIT 1", (BOAT_ID, path),
        ).fetchone()
    if not r:
        return None
    conv = PRESENT.get(path, (None, None, _id))[2]
    return conv(r["value"])


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
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT tws, twa, target_stw, target_vmg, (abs(tws-%s)+abs(twa-%s)) AS dist "
            "FROM polars WHERE boat_id=%s ORDER BY dist LIMIT 1",
            (tws, abs(twa), BOAT_ID),
        ).fetchone()
    if not row:
        return {"available": False, "note": "no polar data loaded"}
    return {"available": True, "query": {"tws": tws, "twa": twa},
            "nearest_bucket": {"tws": row["tws"], "twa": row["twa"]},
            "target_stw": row["target_stw"], "target_vmg": row["target_vmg"]}


def get_ais_targets(max_range_nm: float = 12):
    """AIS traffic with range/bearing and CPA/TCPA computed live vs own ship (see ais.py)."""
    return ais.get_ais_targets(max_range_nm)


def get_route_status(route: str = "default"):
    lat, lon = _latest_value("navigation.position.latitude"), _latest_value("navigation.position.longitude")
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
            "finish": legs[-1], "legs": legs}


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
    "get_route_status": get_route_status,
    "fetch_forecast": fetch_forecast,
    "log_note": log_note,
}


def dispatch(name: str, args: dict):
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}'"}
    return fn(**(args or {}))
