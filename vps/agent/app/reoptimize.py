"""Onboard RE-OPTIMIZER — the graceful-degradation fallback route (Lab-3 tier 2/3).

When the branch SELECTOR flags OFF-SCRIPT (a persistent shift favours a side the frozen playbook has no
pre-authored variant for), the crew still needs a route. This computes a FRESH optimal route ONBOARD —
from the boat's live position, through the REMAINING course marks to the finish, on the boat's OWN polars
through the common Open-Meteo forecast. The Pi has no cfgrib, so it reuses the onboard isochrone
(`routing.route_leg`) chained per remaining mark — NOT the GRIB lab optimizer.

It is LEGAL in-race (own computer + own polars + own position + common public data available to all), but
it is NOT the frozen homework — so it is explicitly flagged `off_playbook` and reports how far it DIVERGES
from the active variant's frozen track (max/mean cross-track), per perflab item-2's degradation ladder:
pre-authored branch (the selector) → onboard re-optimize (this) → the LLM/crew are told "this is an onboard
re-route, off the playbook". Deterministic, Tier-1. The isochrone chain is CPU-heavy → cached, and served
on demand (`GET /reoptimize`), not on every poll.
"""
import time

from . import deviation
from . import navigator as NAV
from . import routing
from . import sails

_cache = {"key": None, "t": 0, "val": None}
CACHE_TTL = 30


def _remaining_marks(nav, marks):
    """The marks from the next one through the finish (inclusive) — what's left to sail."""
    nxt = (nav.get("next_mark") or {}).get("name")
    idx = next((i for i, m in enumerate(marks) if m["name"] == nxt), 0)
    return marks[idx:]


def _vs_playbook(fresh_path):
    """How far the fresh route departs from the active frozen variant's optimal track — so the crew
    sees how off-script it is. Reuses deviation's polyline projection (max/mean cross-track of the
    fresh path points onto the frozen variant polyline)."""
    try:
        bundle = deviation._load_playbook()
        if not bundle:
            return {"available": False, "note": "no playbook aboard to compare against"}
        v = deviation._pick_variant(bundle, None)
        frozen = ((v.get("route") or {}).get("path")) if v else None
        if not frozen or len(frozen) < 2:
            return {"available": False, "note": "active variant has no frozen track"}
        xtes = [fix["perp"] for p in fresh_path
                if (fix := deviation._project(p["lat"], p["lon"], frozen))]
        if not xtes:
            return {"available": False, "note": "could not compare"}
        return {"available": True, "variant": str(v.get("id")),
                "max_divergence_nm": round(max(xtes), 2),
                "mean_divergence_nm": round(sum(xtes) / len(xtes), 2)}
    except Exception:
        return {"available": False, "note": "comparison failed"}


def get_reoptimize(route=None):
    """A fresh onboard route through the remaining marks (own polars + Open-Meteo), flagged
    off-playbook, with its divergence from the frozen plan. `available:False` with no fix / no course."""
    nav = NAV.get_navigator(route)
    if not nav.get("available"):
        return {"available": False, "note": nav.get("note", "no navigator fix")}
    marks = NAV._marks(nav["route"])
    if not marks:
        return {"available": False, "note": "no course marks loaded"}
    remaining = _remaining_marks(nav, marks)
    s = NAV._latest()
    slat, slon = s.get("lat"), s.get("lon")
    if slat is None:
        return {"available": False, "note": "no position fix"}

    live = (s.get("tws"), s.get("twd"))
    key = (round(slat, 3), round(slon, 3), tuple(m["name"] for m in remaining),
           round(live[0] or 0), round(live[1] or 0))
    if _cache["key"] == key and time.time() - _cache["t"] < CACHE_TTL:
        return _cache["val"]

    wind, use_fcst = routing.make_wind_fn(slat, slon, live)
    t0 = t = time.time()
    cur = (slat, slon)
    full_path = [{"lat": round(slat, 5), "lon": round(slon, 5)}]
    all_legs, leg_summ = [], []
    for m in remaining:
        # which sail this leg wants: the direct-course TWA at the leg midpoint → the onboard sail
        # model (same crossovers the sail dial uses). Clamps to the up sail on a beat, like the dial.
        mid_lat, mid_lon = (cur[0] + m["lat"]) / 2, (cur[1] + m["lon"]) / 2
        tws_l, twd_l = wind(mid_lat, mid_lon, t)
        sail = None
        if tws_l:
            twa_l = abs(NAV._wrap180(NAV._bearing(cur[0], cur[1], m["lat"], m["lon"]) - twd_l))
            sail = (sails.get_sail_advice(tws_l, twa_l) or {}).get("optimal_sail")
        leg = routing.route_leg(cur[0], cur[1], m["lat"], m["lon"], wind, t, live[1] or 0)
        full_path += leg["path"][1:]          # skip the duplicated leg-start point
        all_legs += leg["legs"]
        leg_summ.append({"mark": m["name"], "eta_min": round((leg["reached_t"] - t) / 60, 1),
                         "sailed_nm": round(leg["sailed_nm"], 2), "sail": sail})
        t = leg["reached_t"]
        cur = (m["lat"], m["lon"])

    # the peel sequence across the remaining course (consecutive de-dup), for the crew
    sail_plan = []
    for lg in leg_summ:
        if lg.get("sail") and (not sail_plan or sail_plan[-1] != lg["sail"]):
            sail_plan.append(lg["sail"])

    tacks = sum(1 for a, b in zip(all_legs, all_legs[1:]) if a["tack"] != b["tack"])
    sailed = sum(NAV._hav_nm(full_path[i]["lat"], full_path[i]["lon"],
                             full_path[i + 1]["lat"], full_path[i + 1]["lon"])
                 for i in range(len(full_path) - 1))

    out = {
        "available": True, "off_playbook": True, "route": nav["route"],
        "wind_source": "forecast" if use_fcst else "live wind (no forecast)",
        "marks": [m["name"] for m in remaining], "legs": leg_summ, "sail_plan": sail_plan,
        "eta_min": round((t - t0) / 60, 1), "tacks": tacks, "sailed_nm": round(sailed, 2),
        "recommended_heading": all_legs[0]["hdg"] if all_legs else None,
        "first_tack": all_legs[0]["tack"] if all_legs else None,
        "path": full_path,
        "vs_playbook": _vs_playbook(full_path),
        "note": ("Onboard re-optimization — a FRESH route on your own polars through the "
                 + ("Open-Meteo forecast" if use_fcst else "current measured wind")
                 + ", from your position through the remaining marks. OFF THE PLAYBOOK (not the "
                 "frozen homework) — legal in-race, but flagged as an onboard re-route."),
    }
    _cache.update(key=key, t=time.time(), val=out)
    return out
