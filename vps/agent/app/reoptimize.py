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
import os
import threading
import time

from . import deviation
from . import navigator as NAV
from . import routing
from . import sails

# --- when is a cached route still good? ------------------------------------
# This isochrone is the most expensive thing the engine does — ~14 s for a 259 nm remaining
# course, and `dashboard.js` polls /strategy (which chains it) every 15 s. It was already meant
# to be cached, but the key carried raw instrument readings: position to 3 dp (~111 m), TWS to
# 1 kn, TWD to 1 deg. Measured against the Bayview Mackinac 2026 archive at the real 15 s poll:
# 466 distinct keys in 480 calls, only 14 of 479 consecutive polls repeating — a ~97% MISS rate,
# i.e. a 14 s route computation every poll, for the whole race.
#
# Bucketing the key is not the fix either: it only moves the problem to the bucket boundaries,
# where a TWD hovering near an edge chatters between two buckets forever. Measured, coarse
# buckets still recomputed 287 times in 2 h.
#
# So the test is a THRESHOLD ON THE DELTA from the conditions the cached route was actually
# computed at. There are no boundaries to chatter across: the route is reused until the world
# has moved enough to change it, however the readings wander in between. The isochrone is still
# computed from exact live values — this only decides WHEN to recompute.
CACHE_TTL = float(os.environ.get("REOPT_CACHE_TTL_S", "300"))     # recompute at least this often
# Stale-while-revalidate refreshes in a background thread, which reads whatever
# `datasource.active()` currently points at. On the boat that is one stable source. Under replay
# the harness reassigns it per frame, so a refresh started in one frame can finish reading
# another's — a route stitched from mixed timestamps, then cached for a later frame. Replay sets
# this false so every frame depends only on its own timestamp.
SWR = os.environ.get("REOPT_SWR", "true").strip().lower() != "false"
POS_TOL_NM = float(os.environ.get("REOPT_POS_TOL_NM", "0.5"))     # boat movement that matters
TWD_TOL_DEG = float(os.environ.get("REOPT_TWD_TOL_DEG", "15"))    # a real shift, not instrument noise
TWS_TOL_KN = float(os.environ.get("REOPT_TWS_TOL_KN", "4"))       # a real build/drop

_cache = {"ctx": None, "t": 0, "val": None}
_refresh_lock = threading.Lock()
_refreshing = False


def _structurally_changed(ctx, prev):
    """The cached route is about a DIFFERENT PROBLEM, not merely an older one.

    Kept separate from drift because a stale answer is only acceptable when it is still an
    answer to the same question. If a mark has been rounded, or new obstacle homework has come
    aboard, the cached track may run through an island or to a mark already behind us — so these
    force a synchronous recompute and are never served stale."""
    return (prev is None
            or ctx["marks"] != prev["marks"]
            or ctx["obstacles"] != prev["obstacles"])


def _drifted(ctx, prev, age_s):
    """True when the same problem has drifted far enough to be worth re-solving. Gradual, so it
    is safe to serve the previous route while a fresh one is computed behind the request."""
    if prev is None:
        return True
    if age_s >= CACHE_TTL:
        return True
    if NAV._hav_nm(prev["lat"], prev["lon"], ctx["lat"], ctx["lon"]) > POS_TOL_NM:
        return True
    # Live wind only invalidates a route that was actually COMPUTED from live wind. When the
    # forecast is reachable, make_wind_fn routes on the forecast at every point and `live` is
    # merely the no-network fallback plus an initial tack reference — so the answer barely
    # depends on it, and throwing the route away because the masthead moved was discarding work
    # over an input it hardly used. The forecast moves slowly (Open-Meteo is cached ~30 min);
    # CACHE_TTL is what picks that up.
    if prev.get("from_live_wind"):
        if abs(NAV._wrap180((ctx["twd"] or 0) - (prev["twd"] or 0))) > TWD_TOL_DEG:
            return True
        if abs((ctx["tws"] or 0) - (prev["tws"] or 0)) > TWS_TOL_KN:
            return True
    return False


def _next_course_index(nav, marks):
    """The navigator's next mark as a POSITION in the course — by the index it reports, checked
    against the name; else its seq; else the name only when it occurs once. A lapped course
    (Top → Bottom → Top) repeats names, so the first name match can be a lap behind."""
    nm = nav.get("next_mark") or {}
    name, idx, seq = nm.get("name"), nm.get("index"), nm.get("seq")
    if isinstance(idx, int) and 0 <= idx < len(marks) and marks[idx].get("name") == name:
        return idx
    if seq is not None:
        hit = [i for i, m in enumerate(marks) if m.get("seq") == seq and m.get("name") == name]
        if len(hit) == 1:
            return hit[0]
    hit = [i for i, m in enumerate(marks) if m.get("name") == name]
    return hit[0] if len(hit) == 1 else None


def _remaining_marks(nav, marks):
    """The marks from the next one through the finish (inclusive) — what's left to sail. Each
    carries its `course_index` so the re-route can say WHICH occurrence of a lapped mark it
    starts at."""
    idx = _next_course_index(nav, marks)
    if idx is None:
        idx = 0
    return [dict(m, course_index=i) for i, m in enumerate(marks)][idx:]


def _avoid_disks(bundle):
    """The frozen island obstacle homework → the (lat,lon,radius_nm) disks route_leg prunes against.
    Empty when the bundle carries no obstacles (the router then routes open-water, honestly flagged)."""
    isls = ((bundle or {}).get("obstacles") or {}).get("islands") or []
    return [(i["lat"], i["lon"], i.get("radius_nm") or 0.5)
            for i in isls if i.get("lat") is not None and i.get("lon") is not None]


def _parse_zones(bundle):
    """The frozen exclusion/TSS/hazard ZONES → (extra disks, polygon rings) for the router. Mirrors
    the lab's `geo._zone_rings` geometry contract: a GeoJSON polygon ({"coordinates": [[[lon,lat]..]]}
    or a bare ring), a bbox ({north,south,west,east}), or a circle ({center:[lat,lon], radius_nm})."""
    zdisks, polys = [], []
    for z in (((bundle or {}).get("obstacles") or {}).get("zones") or []):
        g = z.get("geometry") or {}
        if not g:
            continue
        try:
            if g.get("center") and g.get("radius_nm"):
                c = g["center"]                       # [lat, lon]
                if len(c) == 2:
                    zdisks.append((float(c[0]), float(c[1]), float(g["radius_nm"])))
            elif "coordinates" in g:
                coords = g["coordinates"]
                if coords and isinstance(coords[0][0], (int, float)):
                    coords = [coords]                 # bare ring → one-ring polygon
                for ring in coords:
                    if ring:
                        polys.append([(float(x), float(y)) for x, y in ring])
            elif all(k in g for k in ("north", "south", "west", "east")):
                n, s, w, e = g["north"], g["south"], g["west"], g["east"]
                polys.append([(w, s), (e, s), (e, n), (w, n)])
        except (TypeError, ValueError, KeyError):
            continue                                  # a malformed zone is skipped, not fatal
    return zdisks, polys


def _vs_playbook(bundle, fresh_path):
    """How far the fresh route departs from the active frozen variant's optimal track — so the crew
    sees how off-script it is. Reuses deviation's polyline projection (max/mean cross-track of the
    fresh path points onto the frozen variant polyline)."""
    try:
        if not bundle:
            return {"available": False, "note": "No gameplan to compare."}
        v = deviation._pick_variant(bundle, None)
        frozen = ((v.get("route") or {}).get("path")) if v else None
        if not frozen or len(frozen) < 2:
            return {"available": False, "note": "Gameplan option has no track."}
        xtes = [fix["perp"] for p in fresh_path
                if (fix := deviation._project(p["lat"], p["lon"], frozen))]
        if not xtes:
            return {"available": False, "note": "Could not compare."}
        return {"available": True, "variant": str(v.get("id")),
                "max_divergence_nm": round(max(xtes), 2),
                "mean_divergence_nm": round(sum(xtes) / len(xtes), 2)}
    except Exception:
        return {"available": False, "note": "Comparison failed."}


def get_reoptimize(route=None):
    """A fresh onboard route through the remaining marks (own polars + Open-Meteo), flagged
    off-playbook, with its divergence from the frozen plan. `available:False` with no fix / no course."""
    nav = NAV.get_navigator(route)
    if not nav.get("available"):
        return {"available": False, "note": nav.get("note", "No position yet.")}
    marks = NAV._marks(nav["route"])
    if not marks:
        return {"available": False, "note": "No course loaded."}
    remaining = _remaining_marks(nav, marks)
    s = NAV._latest()
    slat, slon = s.get("lat"), s.get("lon")
    if slat is None:
        return {"available": False, "note": "No position yet."}

    live = (s.get("tws"), s.get("twd"))
    bundle = deviation._load_playbook()
    zdisks, polys = _parse_zones(bundle)
    avoid = _avoid_disks(bundle) + zdisks             # islands + circular zones = disks

    ctx = {"lat": slat, "lon": slon, "tws": live[0], "twd": live[1],
           "marks": tuple(m["name"] for m in remaining),
           "obstacles": (len(avoid), len(polys))}
    prev = _cache["ctx"]
    age = time.time() - _cache["t"]
    if _structurally_changed(ctx, prev):
        return _compute(nav, remaining, slat, slon, live, bundle, avoid, polys, ctx)
    if not _drifted(ctx, prev, age):
        return _cache["val"]

    # STALE-WHILE-REVALIDATE, for drift only. A recompute costs ~14 s, and /strategy is a
    # synchronous endpoint the console aborts at 5 s — so blocking does not merely cost CPU, it
    # blanks the readouts that depend on it (the race-day flapping). The problem is unchanged
    # and only the conditions have moved, so serve the previous route immediately and refresh
    # behind the request.
    if SWR and _cache["val"] is not None:
        _kick_refresh(route)
        stale = dict(_cache["val"])
        stale["stale_s"] = round(age, 1)
        return stale

    return _compute(nav, remaining, slat, slon, live, bundle, avoid, polys, ctx)


def _kick_refresh(route):
    """Recompute in the background, at most one at a time. Daemon so it can never hold a
    shutdown open, and failures are swallowed — a stale route is a fine outcome, an exception
    escaping into a poll thread is not."""
    global _refreshing
    with _refresh_lock:
        if _refreshing:
            return
        _refreshing = True

    def run():
        global _refreshing
        try:
            _recompute_now(route)
        except Exception as exc:
            print(f"[reoptimize] background refresh failed ({exc}); keeping the cached route",
                  flush=True)
        finally:
            with _refresh_lock:
                _refreshing = False

    threading.Thread(target=run, name="reoptimize-refresh", daemon=True).start()


def _recompute_now(route=None):
    """Force a fresh isochrone and cache it (the background refresh path)."""
    nav = NAV.get_navigator(route)
    if not nav.get("available"):
        return None
    marks = NAV._marks(nav["route"])
    if not marks:
        return None
    remaining = _remaining_marks(nav, marks)
    s = NAV._latest()
    slat, slon = s.get("lat"), s.get("lon")
    if slat is None:
        return None
    live = (s.get("tws"), s.get("twd"))
    bundle = deviation._load_playbook()
    zdisks, polys = _parse_zones(bundle)
    avoid = _avoid_disks(bundle) + zdisks
    ctx = {"lat": slat, "lon": slon, "tws": live[0], "twd": live[1],
           "marks": tuple(m["name"] for m in remaining),
           "obstacles": (len(avoid), len(polys))}
    return _compute(nav, remaining, slat, slon, live, bundle, avoid, polys, ctx)


def _compute(nav, remaining, slat, slon, live, bundle, avoid, polys, ctx):
    """The heavy isochrone chain. Split out so the background refresh and the first-call path
    share exactly one implementation."""
    wind, use_fcst = routing.make_wind_fn(slat, slon, live)
    t0 = t = time.time()
    cur = (slat, slon)
    full_path = [{"lat": round(slat, 5), "lon": round(slon, 5)}]
    all_legs, leg_summ = [], []
    leg_end_idx = []          # full_path index where each leg reaches its mark (gpsout's mark vertices)
    for m in remaining:
        # which sail this leg wants: the direct-course TWA at the leg midpoint → the onboard sail
        # model (same crossovers the sail dial uses). Clamps to the up sail on a beat, like the dial.
        mid_lat, mid_lon = (cur[0] + m["lat"]) / 2, (cur[1] + m["lon"]) / 2
        tws_l, twd_l = wind(mid_lat, mid_lon, t)
        sail = None
        if tws_l:
            twa_l = abs(NAV._wrap180(NAV._bearing(cur[0], cur[1], m["lat"], m["lon"]) - twd_l))
            sail = (sails.get_sail_advice(tws_l, twa_l) or {}).get("optimal_sail")
        leg = routing.route_leg(cur[0], cur[1], m["lat"], m["lon"], wind, t, live[1] or 0,
                                avoid=avoid, avoid_polys=polys)
        if not leg.get("reached", True):
            # #31: the leg never closed on its mark (search horizon out on a long light-air leg, or
            # every heading blocked by the island/zone homework). Chaining the next leg from the
            # mark would jump the path there for free and understate the ETA — so refuse by name.
            done = [x["mark"] for x in leg_summ]
            out = {"available": False, "off_playbook": True, "route": nav["route"],
                   "degraded": True, "complete": False,
                   "incomplete": {"to": m["name"], "short_nm": leg.get("short_nm"),
                                  "routed_marks": done,
                                  "horizon_h": round(routing.MAX_STEPS * routing.DT_H, 1)},
                   "wind_source": "forecast" if use_fcst else "live wind (no forecast)",
                   # Why: no clear path within the routing.MAX_STEPS * DT_H search horizon, or the
                   # obstacle homework blocks it. No route offered rather than a wrong one.
                   "note": (f"No new route. Couldn't reach {m['name']} "
                            f"({leg.get('short_nm')} nm short).")}
            _cache.update(ctx=dict(ctx, from_live_wind=not use_fcst), t=time.time(), val=out)
            return out
        full_path += leg["path"][1:]          # skip the duplicated leg-start point
        leg_end_idx.append(len(full_path) - 1)
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
        "marks": [m["name"] for m in remaining], "legs": leg_summ,
        "sail_plan": sail_plan,
        "mark_idx": [m.get("course_index") for m in remaining],   # course positions (laps)
        "eta_min": round((t - t0) / 60, 1), "tacks": tacks, "sailed_nm": round(sailed, 2),
        "recommended_heading": all_legs[0]["hdg"] if all_legs else None,
        "first_tack": all_legs[0]["tack"] if all_legs else None,
        "path": full_path,
        "leg_end_idx": leg_end_idx,           # one per remaining mark, in order
        "avoids_islands": len(avoid), "avoids_zones": len(polys),
        "vs_playbook": _vs_playbook(bundle, full_path),
        # Fresh route on the boat's own polars, through the forecast (or measured wind when there
        # is none), from here through the remaining marks. Off the frozen playbook; legal in-race.
        "note": ("New route from here, off the gameplan"
                 + (", avoiding " + " and ".join(
                     ([f"{len(avoid)} island(s)"] if avoid else [])
                     + ([f"{len(polys)} no-go zone(s)"] if polys else []))
                    + "."
                    if (avoid or polys) else
                    ". No obstacles loaded, check the chart.")),
    }
    # record WHICH wind this route came from — _drifted needs it to decide whether live-wind
    # movement can invalidate the answer at all
    ctx = dict(ctx, from_live_wind=not use_fcst)
    _cache.update(ctx=ctx, t=time.time(), val=out)
    return out
