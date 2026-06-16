"""SQL-backed implementations of the agent's tools.

The LLM never sees raw NMEA — it calls these, which read facts from TimescaleDB.
Each returns a plain JSON-serializable dict. `dispatch(name, args)` routes a tool call
from the Claude loop (or the deterministic fallback) to the right function.
"""
import math
import os
from datetime import datetime, timezone

from shared.units import TELEMETRY_CHANNELS, CHANNEL_UNITS
from .db import pool

BOAT_ID = os.environ.get("BOAT_ID", "sr33")


def _age_seconds(ts: datetime) -> float:
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return round((now - ts).total_seconds(), 1)


def _haversine_nm(lat1, lon1, lat2, lon2):
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r_nm * 2 * math.asin(min(1.0, math.sqrt(a)))


def _bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def get_current_conditions():
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM telemetry WHERE boat_id = %s ORDER BY time DESC LIMIT 1",
            (BOAT_ID,),
        ).fetchone()
    if not row:
        return {"available": False, "note": "no telemetry yet"}
    age = _age_seconds(row["time"])
    return {
        "available": True,
        "time": row["time"].isoformat(),
        "data_age_seconds": age,
        "stale": age > 30,
        "units": CHANNEL_UNITS,
        **{c: row[c] for c in TELEMETRY_CHANNELS},
    }


def get_history(channel: str, window_minutes: int, aggregation: str = "avg"):
    if channel not in TELEMETRY_CHANNELS:
        return {"error": f"unknown channel '{channel}'", "valid": list(TELEMETRY_CHANNELS)}
    window = f"{int(window_minutes)} minutes"
    col = channel  # validated against the whitelist above — safe to interpolate
    with pool.connection() as conn:
        if aggregation == "series":
            rows = conn.execute(
                f"SELECT time, {col} AS value FROM telemetry "
                "WHERE boat_id = %s AND time > now() - %s::interval "
                f"AND {col} IS NOT NULL ORDER BY time",
                (BOAT_ID, window),
            ).fetchall()
            return {
                "channel": channel,
                "window_minutes": window_minutes,
                "points": [{"time": r["time"].isoformat(), "value": r["value"]} for r in rows],
            }
        fn = {"avg": "avg", "min": "min", "max": "max", "last": "last"}.get(aggregation, "avg")
        if fn == "last":
            expr = f"last({col}, time)"
        else:
            expr = f"{fn}({col})"
        row = conn.execute(
            f"SELECT {expr} AS value FROM telemetry "
            "WHERE boat_id = %s AND time > now() - %s::interval",
            (BOAT_ID, window),
        ).fetchone()
    return {"channel": channel, "window_minutes": window_minutes,
            "aggregation": aggregation, "value": row["value"]}


def get_polar_target(tws: float, twa: float):
    # Nearest polar bucket by (tws, twa) distance. Phase 4 will interpolate.
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT tws, twa, target_stw, target_vmg, "
            "  (abs(tws - %s) + abs(twa - %s)) AS dist "
            "FROM polars WHERE boat_id = %s ORDER BY dist LIMIT 1",
            (tws, abs(twa), BOAT_ID),
        ).fetchone()
    if not row:
        return {"available": False, "note": "no polar data loaded (open item §9)"}
    return {
        "available": True,
        "query": {"tws": tws, "twa": twa},
        "nearest_bucket": {"tws": row["tws"], "twa": row["twa"]},
        "target_stw": row["target_stw"],
        "target_vmg": row["target_vmg"],
    }


def get_ais_targets(max_range_nm: float = 12):
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (mmsi) mmsi, name, range_nm, bearing, cpa_nm, tcpa_min, sog, cog "
            "FROM ais_targets WHERE boat_id = %s AND time > now() - interval '5 minutes' "
            "ORDER BY mmsi, time DESC",
            (BOAT_ID,),
        ).fetchall()
    targets = [dict(r) for r in rows if r["range_nm"] is None or r["range_nm"] <= max_range_nm]
    targets.sort(key=lambda t: (t["cpa_nm"] is None, t["cpa_nm"] or 1e9))
    return {"count": len(targets), "max_range_nm": max_range_nm, "targets": targets}


def get_route_status(route: str = "default"):
    with pool.connection() as conn:
        pos = conn.execute(
            "SELECT lat, lon, sog FROM telemetry WHERE boat_id = %s AND lat IS NOT NULL "
            "ORDER BY time DESC LIMIT 1",
            (BOAT_ID,),
        ).fetchone()
        marks = conn.execute(
            "SELECT seq, name, lat, lon FROM waypoints WHERE route = %s ORDER BY seq",
            (route,),
        ).fetchall()
    if not pos:
        return {"available": False, "note": "no position fix yet"}
    if not marks:
        return {"available": False, "note": f"no waypoints for route '{route}' (open item §9)"}
    legs = []
    for m in marks:
        dist = _haversine_nm(pos["lat"], pos["lon"], m["lat"], m["lon"])
        brg = _bearing_deg(pos["lat"], pos["lon"], m["lat"], m["lon"])
        eta_hr = (dist / pos["sog"]) if pos.get("sog") else None
        legs.append({"seq": m["seq"], "name": m["name"], "distance_nm": round(dist, 2),
                     "bearing_deg": round(brg, 1),
                     "eta_hours": round(eta_hr, 2) if eta_hr else None})
    return {"available": True, "route": route, "next_mark": legs[0],
            "finish": legs[-1], "legs": legs}


def fetch_forecast(lat: float, lon: float):
    # Phase 4: server-side GFS/NOAA fetch. Stubbed so the tool surface is complete.
    return {"available": False, "note": "forecast fetch not wired yet (Phase 4 / §9 GRIB source)",
            "query": {"lat": lat, "lon": lon}}


def log_note(text: str, author: str = None):
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO crew_notes (boat_id, author, text) VALUES (%s, %s, %s) "
            "RETURNING id, time",
            (BOAT_ID, author, text),
        ).fetchone()
        conn.commit()
    return {"logged": True, "id": row["id"], "time": row["time"].isoformat()}


DISPATCH = {
    "get_current_conditions": get_current_conditions,
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
