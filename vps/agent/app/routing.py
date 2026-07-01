"""Isochrone weather routing (5.4).

Computes the fastest sailing route from the boat to a mark through the (forecast) wind field,
using the boat polars. Classic isochrone method: from the start, repeatedly fan out every
heading over a short time step, advance each by the polar boatspeed at the local true-wind
angle, then prune the candidates to the outer envelope (one per bearing sector) — that
envelope is the isochrone. When the envelope reaches the destination, backtrack parents to
recover the optimal path (which naturally tacks upwind / gybes downwind).

Wind comes from weather.wind_at (Open-Meteo); if the forecast is unavailable it falls back to
the live measured wind held constant, so routing still runs on the bench. RRS 41: routing is
shore tactical advice — practice/debrief unless the RC clears it; the UI gates it by race mode.
"""
import math
import time

from . import navigator as NAV
from . import weather
from . import datasource

HSTEP = 12          # heading fan resolution (deg)
DT_H = 0.2          # time step (hours)
SECTOR = 3.0        # isochrone pruning: bearing-from-start bucket (deg)
MAX_STEPS = 160
_cache = {"key": None, "t": 0, "val": None}
CACHE_TTL = 25

_POLARS = None


def _polars():
    global _POLARS
    if _POLARS is None:
        _POLARS = [(tws, twa, stw) for (tws, twa, stw) in datasource.active().polars_stw() if stw]
    return _POLARS


def _polar_speed(tws, twa):
    P = _polars()
    if not P or twa < 30:        # no-go zone / no data → can't make ground to weather
        return 0.0
    return min(P, key=lambda p: abs(p[0] - tws) + abs(p[1] - twa))[2]


def _advance(lat, lon, brg, dist_nm):
    b = math.radians(brg)
    return (lat + dist_nm * math.cos(b) / 60.0,
            lon + dist_nm * math.sin(b) / (60.0 * max(0.1, math.cos(math.radians(lat)))))


def _tack(hdg, twd):
    return "stbd" if NAV._wrap180(twd - hdg) > 0 else "port"


def make_wind_fn(slat, slon, live):
    """A wind(lat,lon,epoch)→(tws,twd) closure: the Open-Meteo forecast if reachable at the start,
    else the live measured wind held constant (so routing still runs with no network). `live` is
    (tws_kn, twd_deg). Returns (wind_fn, use_forecast)."""
    use_fcst = weather.wind_at(slat, slon, time.time()) is not None

    def wind(lat, lon, epoch):
        if use_fcst:
            w = weather.wind_at(lat, lon, epoch)
            if w:
                return w
        return live if live[0] is not None else (12.0, 0.0)
    return wind, use_fcst


def route_leg(slat, slon, dlat, dlon, wind, t0, live_twd=0.0):
    """One isochrone leg from (slat,slon) to (dlat,dlon) through `wind()`, starting at epoch `t0`.
    The reusable core of `get_route` — chained per remaining mark by the onboard re-optimizer.
    Returns {path:[{lat,lon}], legs:[{hdg,tack}], reached_t, sailed_nm, direct_nm}."""
    direct = NAV._hav_nm(slat, slon, dlat, dlon)
    start = {"lat": slat, "lon": slon, "t": t0, "parent": None, "hdg": None}
    frontier = [start]
    reached = None
    headings = list(range(0, 360, HSTEP))

    for _ in range(MAX_STEPS):
        cand = {}
        for node in frontier:
            tws, twd = wind(node["lat"], node["lon"], node["t"])
            if tws is None:
                tws, twd = 12.0, 0.0
            # can we lay the mark from here this step?
            dmark = NAV._hav_nm(node["lat"], node["lon"], dlat, dlon)
            bmark = NAV._bearing(node["lat"], node["lon"], dlat, dlon)
            twa_m = abs(NAV._wrap180(bmark - twd))
            sp_m = _polar_speed(tws, twa_m)
            if sp_m > 0.3 and dmark <= sp_m * DT_H:
                reached = {"lat": dlat, "lon": dlon,
                           "t": node["t"] + (dmark / sp_m) * 3600, "parent": node, "hdg": bmark}
                break
            for hdg in headings:
                twa = abs(NAV._wrap180(hdg - twd))
                sp = _polar_speed(tws, twa)
                if sp < 0.3:
                    continue
                nlat, nlon = _advance(node["lat"], node["lon"], hdg, sp * DT_H)
                rng = NAV._hav_nm(slat, slon, nlat, nlon)
                sec = round(NAV._bearing(slat, slon, nlat, nlon) / SECTOR)
                # keep the farthest-advanced candidate per bearing sector (the isochrone)
                if sec not in cand or rng > cand[sec]["rng"]:
                    cand[sec] = {"lat": nlat, "lon": nlon, "t": node["t"] + DT_H * 3600,
                                 "parent": node, "hdg": hdg, "rng": rng}
        if reached:
            break
        if not cand:
            break
        frontier = list(cand.values())
        # stop if the envelope has effectively reached the mark
        best = min(frontier, key=lambda n: NAV._hav_nm(n["lat"], n["lon"], dlat, dlon))
        if NAV._hav_nm(best["lat"], best["lon"], dlat, dlon) < 0.05:
            reached = best
            break

    if not reached:
        # didn't converge — return the frontier point nearest the mark as a best effort
        reached = min(frontier, key=lambda n: NAV._hav_nm(n["lat"], n["lon"], dlat, dlon))

    # backtrack the path
    path, node, legs = [], reached, []
    while node is not None:
        path.append({"lat": round(node["lat"], 5), "lon": round(node["lon"], 5)})
        if node["hdg"] is not None:
            legs.append({"hdg": round(node["hdg"]), "tack": _tack(node["hdg"], live_twd or 0)})
        node = node["parent"]
    path.reverse(); legs.reverse()
    sailed = sum(NAV._hav_nm(path[i]["lat"], path[i]["lon"], path[i + 1]["lat"], path[i + 1]["lon"])
                 for i in range(len(path) - 1))
    return {"path": path, "legs": legs, "reached_t": reached["t"],
            "sailed_nm": sailed, "direct_nm": direct}


def get_route(route: str = None, target: str = "next"):
    """Optimal route from the boat to the next mark (target='next') or the course finish."""
    nav = NAV.get_navigator(route)
    if not nav.get("available"):
        return {"available": False, "note": nav.get("note", "no navigator fix")}
    marks = NAV._marks(nav["route"])
    if target == "finish":
        dest = marks[-1]
    else:
        dest = next((m for m in marks if m["name"] == nav["next_mark"]["name"]), marks[-1])
    s = NAV._latest()
    slat, slon = s["lat"], s["lon"]
    if slat is None:
        return {"available": False, "note": "no position fix"}

    live = (s["tws"], s["twd"])
    wind, use_fcst = make_wind_fn(slat, slon, live)

    key = (round(slat, 3), round(slon, 3), dest["name"], target, round(live[0] or 0), round(live[1] or 0), use_fcst)
    if _cache["key"] == key and time.time() - _cache["t"] < CACHE_TTL:
        return _cache["val"]

    t0 = time.time()
    leg = route_leg(slat, slon, dest["lat"], dest["lon"], wind, t0, live[1] or 0)
    path, legs, direct = leg["path"], leg["legs"], leg["direct_nm"]

    # count tacks/gybes = tack changes along the path
    tacks = sum(1 for a, b in zip(legs, legs[1:]) if a["tack"] != b["tack"])
    sailed = leg["sailed_nm"]
    eta_min = round((leg["reached_t"] - t0) / 60, 1)

    out = {
        "available": True, "route": nav["route"], "target": dest["name"],
        "wind_source": "forecast" if use_fcst else "live wind (no forecast)",
        "direct_nm": round(direct, 2), "sailed_nm": round(sailed, 2),
        "eta_min": eta_min, "tacks": tacks,
        "recommended_heading": legs[0]["hdg"] if legs else None,
        "first_tack": legs[0]["tack"] if legs else None,
        "path": path,
        "note": ("Isochrone optimal route on the polars through the "
                 + ("Open-Meteo forecast" if use_fcst else "current measured wind")
                 + ". Practice/debrief — RRS 41 in a race."),
    }
    _cache.update(key=key, t=time.time(), val=out)
    return out
