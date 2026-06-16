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
    if not channels:
        return {"available": False, "note": "no telemetry in window"}
    return {
        "available": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "channels": channels,
        "note": ("Multiple sources per channel are redundant by design — cross-check them, "
                 "flag disagreement/stale/uncalibrated; see get_sources for reliability."),
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
        return min(c["readings"], key=lambda r: r["age_s"])["value"]

    ages = [c["freshest_age_s"] for c in ch.values()]
    heading = best("heading_true")
    if heading is None:
        heading = best("heading_mag")
    return {
        "available": True, "as_of": cc["as_of"],
        "data_age_seconds": min(ages) if ages else None,
        "stale": (min(ages) if ages else 999) > 30,
        "stw": best("stw"), "sog": best("sog"), "tws": best("tws"), "twa": best("twa"),
        "twd": best("twd"), "aws": best("aws"), "awa": best("awa"), "heading": heading,
        "heel": best("heel"), "depth": best("depth"),
    }


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
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (mmsi) mmsi, name, range_nm, bearing, cpa_nm, tcpa_min, sog, cog "
            "FROM ais_targets WHERE boat_id=%s AND time > now()-interval '5 minutes' "
            "ORDER BY mmsi, time DESC", (BOAT_ID,),
        ).fetchall()
    targets = [dict(r) for r in rows if r["range_nm"] is None or r["range_nm"] <= max_range_nm]
    targets.sort(key=lambda t: (t["cpa_nm"] is None, t["cpa_nm"] or 1e9))
    return {"count": len(targets), "max_range_nm": max_range_nm, "targets": targets}


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


def fetch_forecast(lat: float, lon: float):
    return {"available": False, "note": "forecast fetch not wired yet (Phase 4 / GRIB source)",
            "query": {"lat": lat, "lon": lon}}


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
