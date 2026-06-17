"""Navigator — where the boat is on the course, what's next, and the laylines.

Given the active course (marks in the waypoints table) and live position/wind, this computes
the next mark (bearing/distance/ETA), the leg type (beat/reach/run), and the windward- or
leeward-mark laylines from the boat's optimal up/downwind angles. The schematic plot and the
Navigator panel render this; get_navigator is also an agent tool.

Course positioning is navigation (allowed even racing); the *tactical* layer (favored side,
shifts, leverage in 5.3) is what the race/practice toggle gates for RRS 41.
"""
import math
import os

from . import datasource

BOAT_ID = os.environ.get("BOAT_ID", "sr33")
ROUND_NM = 0.06            # within this of a mark = rounded; advance to the next
LAYLINE_TOL_DEG = 4.0      # within this of a layline bearing = "on the layline"

# Active course, shared in-process by the web endpoints and the agent tool so "what's next"
# in chat matches what's on the iPad plot. Updated when a course is loaded/generated.
_active = "default"


def set_active(route):
    global _active
    _active = route or "default"


def active_route():
    return _active


# --- geo (kept local to avoid importing tools, which imports this) ----------
def _hav_nm(a, b, c, d):
    r = 3440.065
    p1, p2 = math.radians(a), math.radians(c)
    dphi, dl = math.radians(c - a), math.radians(d - b)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(min(1.0, math.sqrt(h)))


def _bearing(a, b, c, d):
    p1, p2, dl = math.radians(a), math.radians(c), math.radians(d - b)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _wrap180(d):
    return (d + 180) % 360 - 180


def _adiff(a, b):
    return abs(_wrap180(a - b))


# --- live state -------------------------------------------------------------
_PATHS = {
    "lat": "navigation.position.latitude", "lon": "navigation.position.longitude",
    "cog": "navigation.courseOverGroundTrue", "heading": "navigation.headingTrue",
    "sog": "navigation.speedOverGround", "tws": "environment.wind.speedTrue",
    "twd": "environment.wind.directionTrue",
}


def _latest():
    """Freshest value (any source) for the navigation/wind paths we need, in SI."""
    src = datasource.active()
    out = {key: src.latest_value(path) for key, path in _PATHS.items()}
    # convert
    for k in ("cog", "heading", "twd"):
        if out[k] is not None:
            out[k] = math.degrees(out[k]) % 360
    if out["sog"] is not None:
        out["sog"] *= 1.943844
    if out["tws"] is not None:
        out["tws"] *= 1.943844
    return out


def _best_angles(tws_kn):
    """Optimal upwind and downwind TWA (deg) from the polar at the nearest TWS."""
    if tws_kn is None:
        return 42.0, 150.0
    up, dn = datasource.active().best_angles(tws_kn)
    return (up if up is not None else 42.0), (dn if dn is not None else 150.0)


def _marks(route):
    return datasource.active().marks(route)


def get_course(route: str = None):
    """The course marks + per-leg bearing/distance (for the plot). Sets the active route."""
    route = route or _active
    set_active(route)
    marks = _marks(route)
    legs = []
    for a, b in zip(marks, marks[1:]):
        legs.append({"from": a["name"], "to": b["name"],
                     "distance_nm": round(_hav_nm(a["lat"], a["lon"], b["lat"], b["lon"]), 2),
                     "bearing_deg": round(_bearing(a["lat"], a["lon"], b["lat"], b["lon"]), 1)})
    return {"available": bool(marks), "route": route, "marks": marks, "legs": legs}


def get_navigator(route: str = None):
    """Next mark, ETA, leg type, and laylines from live position + wind."""
    route = route or _active
    marks = _marks(route)
    if not marks:
        return {"available": False, "note": f"no course loaded for route '{route}'"}
    s = _latest()
    if s["lat"] is None or s["lon"] is None:
        return {"available": False, "note": "no position fix yet"}
    lat, lon = s["lat"], s["lon"]

    # next mark = first by seq still more than a rounding radius away
    nxt = next((m for m in marks
                if _hav_nm(lat, lon, m["lat"], m["lon"]) > ROUND_NM), marks[-1])
    dist = _hav_nm(lat, lon, nxt["lat"], nxt["lon"])
    brg = _bearing(lat, lon, nxt["lat"], nxt["lon"])

    twd, tws = s["twd"], s["tws"]
    beat, run = _best_angles(tws)
    leg = {"type": "reach", "laylines": None}
    layline_call = None
    if twd is not None:
        twa_to_mark = _adiff(brg, twd)          # angle off the wind if we aimed at the mark
        if twa_to_mark < beat:                   # mark is above close-hauled -> must beat
            leg["type"] = "beat"
            stbd = (twd + beat) % 360             # close-hauled headings
            port = (twd - beat) % 360
        elif twa_to_mark > run:                  # mark is below optimal run -> must gybe
            leg["type"] = "run"
            stbd = (twd + run) % 360
            port = (twd - run) % 360
        else:
            leg["type"] = "reach"
            stbd = port = None
        if stbd is not None:
            # layline bearings from the mark = reciprocal of the courses sailed toward it
            leg["laylines"] = {
                "starboard_course": round(stbd, 1), "port_course": round(port, 1),
                "starboard_from_mark": round((stbd + 180) % 360, 1),
                "port_from_mark": round((port + 180) % 360, 1),
            }
            ds, dp = _adiff(brg, stbd), _adiff(brg, port)
            near, name = (ds, "starboard") if ds < dp else (dp, "port")
            if near <= LAYLINE_TOL_DEG:
                layline_call = f"On the {name}-tack layline — you can {'tack' if leg['type']=='beat' else 'gybe'} and lay {nxt['name']}."
            else:
                layline_call = f"{round(near)}° below the {name} layline to {nxt['name']}."

    # ETA from velocity made good toward the mark (projects COG/SOG onto the bearing)
    eta_min = None
    if s["sog"] and s["cog"] is not None:
        vmc = s["sog"] * math.cos(math.radians(_adiff(s["cog"], brg)))
        if vmc > 0.2:
            eta_min = round(dist / vmc * 60, 1)

    return {
        "available": True, "route": route,
        "position": {"lat": round(lat, 5), "lon": round(lon, 5)},
        "next_mark": {"name": nxt["name"], "seq": nxt["seq"],
                      "distance_nm": round(dist, 2), "bearing_deg": round(brg, 1),
                      "eta_min": eta_min},
        "leg": leg, "layline_call": layline_call,
        "wind": {"twd": None if twd is None else round(twd, 1),
                 "tws": None if tws is None else round(tws, 1),
                 "beat_twa": round(beat, 1), "run_twa": round(run, 1)},
        "marks_total": len(marks),
        "remaining_nm": round(sum(
            _hav_nm(marks[i]["lat"], marks[i]["lon"], marks[i+1]["lat"], marks[i+1]["lon"])
            for i in range(marks.index(nxt), len(marks) - 1)) + dist, 1),
    }


def make_practice_course(leg_nm: float = 1.0):
    """Drop a windward/leeward practice course from the live position + wind, stored as route
    'practice'. Leeward mark at the boat, windward mark leg_nm straight upwind (toward TWD)."""
    s = _latest()
    if s["lat"] is None or s["lon"] is None:
        return {"available": False, "note": "no position fix to anchor a course"}
    twd = s["twd"] if s["twd"] is not None else 0.0
    lat, lon = s["lat"], s["lon"]
    # offset leg_nm in bearing twd (upwind) — simple equirectangular step
    d_rad = (leg_nm / 60.0)  # nm -> deg latitude
    wlat = lat + d_rad * math.cos(math.radians(twd))
    wlon = lon + d_rad * math.sin(math.radians(twd)) / max(0.1, math.cos(math.radians(lat)))
    marks = [(1, "Leeward (start)", lat, lon), (2, "Windward", wlat, wlon),
             (3, "Leeward (finish)", lat, lon)]
    datasource.active().save_practice_course(marks)
    set_active("practice")
    return {"available": True, "route": "practice", "leg_nm": leg_nm,
            "twd": round(twd, 1), "marks": len(marks)}
