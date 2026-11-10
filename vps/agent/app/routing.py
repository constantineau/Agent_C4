"""Isochrone weather routing (5.4).

Computes the fastest sailing route from the boat to a mark through the (forecast) wind field,
using the boat polars. Classic isochrone method: from the start, repeatedly fan out every
heading over a short time step, advance each by the polar boatspeed at the local true-wind
angle, then prune the candidates to the outer envelope (one per bearing sector) — that
envelope is the isochrone. When the envelope reaches the destination, backtrack parents to
recover the optimal path (which naturally tacks upwind / gybes downwind).

Wind comes from weather.wind_at (Open-Meteo); if the forecast is unavailable at the start it
falls back to the live measured wind held constant, so routing still runs on the bench. One route
never mixes the two (#157), and with neither there is no route at all (#156). RRS 41: routing is
shore tactical advice — practice/debrief unless the RC clears it; the UI gates it by race mode.
"""
import math
import time

from shared import polar as SPOL

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
_POLARS_AT = 0.0
_POLARS_TTL_S = 60.0      # a Deploy push can replace the boat's polar (#304): re-read now and then


_POLARS_GEN = 0          # orinassets.generation() at start: no push seen yet


def _deploy_gen():
    """Changes whenever a Deploy push replaces the boat's polar (#304): re-read at once."""
    try:
        from . import orinassets
        return orinassets.generation()
    except Exception:
        return None


def _polars():
    global _POLARS, _POLARS_AT, _POLARS_GEN
    now = time.monotonic()
    gen = _deploy_gen()
    if _POLARS is None or now - _POLARS_AT >= _POLARS_TTL_S or gen != _POLARS_GEN:
        _POLARS = [(tws, twa, stw) for (tws, twa, stw) in datasource.active().polars_stw() if stw]
        _POLARS_AT, _POLARS_GEN = now, gen
    return _POLARS


def _polar_speed(tws, twa):
    """Polar boatspeed at (tws, twa), interpolated on the grid (`shared.polar`, the same lookup the
    Lab optimizer uses): 0 below the beat angle (no-go) or with no data, scaled toward 0 under the
    lightest charted wind."""
    return SPOL.lookup(_polars(), tws, twa)


def _advance(lat, lon, brg, dist_nm):
    b = math.radians(brg)
    return (lat + dist_nm * math.cos(b) / 60.0,
            lon + dist_nm * math.sin(b) / (60.0 * max(0.1, math.cos(math.radians(lat)))))


def _tack(hdg, twd):
    return "stbd" if NAV._wrap180(twd - hdg) > 0 else "port"


def _pt_to_seg_nm(plat, plon, alat, alon, blat, blon):
    """Nearest distance (nm) from point P to segment A→B, on a flat local plane about A."""
    coslat = math.cos(math.radians(alat))
    px, py = (plon - alon) * 60.0 * coslat, (plat - alat) * 60.0
    bx, by = (blon - alon) * 60.0 * coslat, (blat - alat) * 60.0
    L2 = bx * bx + by * by
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, (px * bx + py * by) / L2))
    return math.hypot(px - bx * t, py - by * t)


def _seg_blocked(alat, alon, blat, blon, avoid):
    """True if the segment A→B passes within any avoid disk (lat, lon, radius_nm). `avoid` is the
    compact frozen island set — the onboard obstacle homework, distilled from the cloud geo mask."""
    if not avoid:
        return False
    for clat, clon, r in avoid:
        if _pt_to_seg_nm(clat, clon, alat, alon, blat, blon) < r:
            return True
    return False


def _pt_in_ring(x, y, ring):
    """Ray-cast point-in-polygon. `ring` is [(x, y), ...] in (lon, lat)."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _ccw(ax, ay, bx, by, cx, cy):
    return (cy - ay) * (bx - ax) > (by - ay) * (cx - ax)


def _segs_cross(a, b, c, d):
    """Do segments A-B and C-D properly intersect? (points are (x, y))."""
    return (_ccw(*a, *c, *d) != _ccw(*b, *c, *d)) and (_ccw(*a, *b, *c) != _ccw(*a, *b, *d))


def _seg_crosses_poly(alat, alon, blat, blon, polys):
    """True if the segment A→B enters any exclusion polygon. `polys` is a list of rings of (lon, lat)
    — the frozen zone homework. Checks endpoint-inside (short isochrone steps) + edge intersection."""
    if not polys:
        return False
    a, b = (alon, alat), (blon, blat)
    for ring in polys:
        if len(ring) < 3:
            continue
        if _pt_in_ring(blon, blat, ring) or _pt_in_ring(alon, alat, ring):
            return True
        for i in range(len(ring)):
            if _segs_cross(a, b, ring[i - 1], ring[i]):
                return True
    return False


NO_WIND = "No wind data. Can't route."
FCST_GAP = "Forecast incomplete. Can't route."
GAPS_FILLED = "Forecast gaps filled from nearby."
OLD_FCST = "Old forecast."
FCST_TOO_OLD = "Forecast too old. Can't route."
TOO_LONG = "Re-route took too long. Try again."
SRC_FCST = "forecast"
SRC_FCST_FILLED = "forecast, gaps filled from nearby"
SRC_FCST_OLD = "old forecast"
SRC_LIVE = "live wind (no forecast)"


class NoWindError(Exception):
    """The wind field had nothing usable for a point the route needed. str() is the crew's reason."""


class RouteTimeout(Exception):
    """The route ran past its wall-clock deadline. str() is the crew's reason."""


class WindField:
    """wind(lat, lon, epoch) → (tws, twd), one source for the whole route.

    Forecast mode: the Open-Meteo forecast per ~0.1° cell. After the first failed fetch (a new
    cell, or the refresh of an expired one) the run reads the cache only, so a dead link costs one
    10 s timeout, not one per cell (#157). A cell with no fresh forecast takes, in order: its own
    old forecast, the nearest fresh cached cell, the nearest old one. Gaps are counted in `filled`,
    old forecast in `stale`, and the label says so. A hole is filled only from a cell within
    weather.FILL_NM, and a forecast past weather.MAX_AGE_S is never used: with nothing younger
    in reach the route is refused (FCST_TOO_OLD). It never falls back
    to the boat's measured wind mid-route.
    Live mode: the measured wind (tws, twd) held constant.
    `deadline` (epoch): past it, the next sample raises RouteTimeout, which stops route_leg cleanly
    (it samples the wind at every node)."""

    def __init__(self, use_forecast, live=None, deadline=None):
        self.use_forecast = use_forecast
        self.live = live
        self.deadline = deadline
        self.filled = set()          # cells whose forecast came from a nearby cell
        self.stale = set()           # cells routed on an old forecast (its refresh failed)
        self._offline = False

    def __call__(self, lat, lon, epoch):
        if self.deadline is not None and time.time() > self.deadline:
            raise RouteTimeout(TOO_LONG)
        if not self.use_forecast:
            return self.live
        # a max age set below the cache TTL (C4_FORECAST_MAX_AGE_H < 0.5) must hold for a cell
        # still inside the TTL too (#203): too old here falls through to a younger cell nearby
        if not self._offline:
            w = weather.wind_at(lat, lon, epoch)   # fresh only: None when a refresh failed
            if w and self._young(lat, lon):
                return w
            if not w:
                self._offline = True     # a fetch just failed: the cache only from here on
        cell = weather._cell(lat, lon)
        w = weather.cached_wind_at(lat, lon, epoch)
        if w and self._young(lat, lon):
            return w
        age = weather.cell_age_s(lat, lon)
        w = weather.cached_wind_at(lat, lon, epoch, stale_ok=True)
        if w and age <= weather.MAX_AGE_S:   # too old: a younger cell nearby may still fill it
            self.stale.add(cell)
            return w
        w, fresh, age = weather.nearest_cached_wind(lat, lon, epoch)
        if not w:
            raise NoWindError(FCST_TOO_OLD if age is not None else FCST_GAP)
        if not fresh:
            self.stale.add(cell)
        self.filled.add(cell)
        return w

    @staticmethod
    def _young(lat, lon):
        age = weather.cell_age_s(lat, lon)
        return age is None or age <= weather.MAX_AGE_S

    @property
    def source(self):
        """The label a route carries: what wind it was really computed in."""
        if not self.use_forecast:
            return SRC_LIVE
        base = SRC_FCST_OLD if self.stale else SRC_FCST
        return base + SRC_FCST_FILLED[len(SRC_FCST):] if self.filled else base

    def warnings(self):
        """Short crew notes for what the label says."""
        return ([OLD_FCST] if self.stale else []) + ([GAPS_FILLED] if self.filled else [])


def make_wind_fn(slat, slon, live):
    """The route's wind field: the Open-Meteo forecast if reachable at the start, else the live
    measured wind held constant (so routing still runs with no network). `live` is
    (tws_kn, twd_deg); a partial live wind (either one None) counts as none.
    Returns (WindField, use_forecast), or (None, False) when there is no forecast and no complete
    live wind: the caller must refuse with NO_WIND rather than route on made-up wind (#156)."""
    if weather.wind_at(slat, slon, time.time()) is not None:
        return WindField(True), True
    if weather.cell_status(slat, slon) == "stale":
        # the start cell's forecast expired and its refresh just failed: route on the old one
        # from the cache (labelled "old forecast", refused past MAX_AGE_S), no more fetches
        wf = WindField(True)
        wf._offline = True
        return wf, True
    tws, twd = (tuple(live) + (None, None))[:2] if live else (None, None)
    if tws is None or twd is None:
        return None, False
    return WindField(False, (tws, twd)), False


def _wind_ok(w):
    return bool(w) and w[0] is not None and w[1] is not None


def route_leg(slat, slon, dlat, dlon, wind, t0, live_twd=0.0, avoid=None, avoid_polys=None):
    """One isochrone leg from (slat,slon) to (dlat,dlon) through `wind()`, starting at epoch `t0`.
    The reusable core of `get_route` — chained per remaining mark by the onboard re-optimizer. `avoid`
    is an optional list of (lat,lon,radius_nm) obstacle disks (the frozen island homework) and
    `avoid_polys` a list of exclusion-zone rings [(lon,lat),...]; steps that cross either are pruned,
    so the route detours around land / no-go zones.
    Returns {path:[{lat,lon}], legs:[{hdg,tack}], reached_t, reached, short_nm, sailed_nm, direct_nm}.
    `reached` is False when the leg never closed on the mark (MAX_STEPS ran out, or every heading was
    blocked): the path then ends at the frontier node nearest the mark, `short_nm` from it, and the
    caller must NOT chain the next leg from the mark (#31).
    Each leg's tack is judged against the wind the route was computed in at that node (#158);
    `live_twd` is no longer used and stays only for the call signature. Raises NoWindError when
    `wind()` has nothing for a node (it never invents a wind)."""

    def _blocked(a_lat, a_lon, b_lat, b_lon):
        return _seg_blocked(a_lat, a_lon, b_lat, b_lon, avoid) or \
            _seg_crosses_poly(a_lat, a_lon, b_lat, b_lon, avoid_polys)
    direct = NAV._hav_nm(slat, slon, dlat, dlon)
    start = {"lat": slat, "lon": slon, "t": t0, "parent": None, "hdg": None}
    frontier = [start]
    reached = None
    headings = list(range(0, 360, HSTEP))

    for _ in range(MAX_STEPS):
        cand = {}
        for node in frontier:
            w = wind(node["lat"], node["lon"], node["t"])
            if not _wind_ok(w):
                raise NoWindError(NO_WIND)
            tws, twd = w
            # can we lay the mark from here this step? (only if the direct segment is clear of land)
            dmark = NAV._hav_nm(node["lat"], node["lon"], dlat, dlon)
            bmark = NAV._bearing(node["lat"], node["lon"], dlat, dlon)
            twa_m = abs(NAV._wrap180(bmark - twd))
            sp_m = _polar_speed(tws, twa_m)
            if sp_m > 0.3 and dmark <= sp_m * DT_H and \
                    not _blocked(node["lat"], node["lon"], dlat, dlon):
                reached = {"lat": dlat, "lon": dlon,
                           "t": node["t"] + (dmark / sp_m) * 3600, "parent": node, "hdg": bmark,
                           "twd": twd}
                break
            for hdg in headings:
                twa = abs(NAV._wrap180(hdg - twd))
                sp = _polar_speed(tws, twa)
                if sp < 0.3:
                    continue
                nlat, nlon = _advance(node["lat"], node["lon"], hdg, sp * DT_H)
                if (avoid or avoid_polys) and _blocked(node["lat"], node["lon"], nlat, nlon):
                    continue                          # this step would cross an obstacle/zone → prune it
                rng = NAV._hav_nm(slat, slon, nlat, nlon)
                sec = round(NAV._bearing(slat, slon, nlat, nlon) / SECTOR)
                # keep the farthest-advanced candidate per bearing sector (the isochrone)
                if sec not in cand or rng > cand[sec]["rng"]:
                    cand[sec] = {"lat": nlat, "lon": nlon, "t": node["t"] + DT_H * 3600,
                                 "parent": node, "hdg": hdg, "rng": rng, "twd": twd}
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

    arrived = reached is not None
    if not reached:
        # didn't converge — return the frontier point nearest the mark as a best effort, flagged
        reached = min(frontier, key=lambda n: NAV._hav_nm(n["lat"], n["lon"], dlat, dlon))

    # backtrack the path
    path, node, legs = [], reached, []
    while node is not None:
        path.append({"lat": round(node["lat"], 5), "lon": round(node["lon"], 5)})
        if node["hdg"] is not None:
            legs.append({"hdg": round(node["hdg"]), "tack": _tack(node["hdg"], node["twd"])})
        node = node["parent"]
    path.reverse(); legs.reverse()
    sailed = sum(NAV._hav_nm(path[i]["lat"], path[i]["lon"], path[i + 1]["lat"], path[i + 1]["lon"])
                 for i in range(len(path) - 1))
    return {"path": path, "legs": legs, "reached_t": reached["t"],
            "reached": arrived,
            "short_nm": 0.0 if arrived else round(NAV._hav_nm(reached["lat"], reached["lon"], dlat, dlon), 2),
            "sailed_nm": sailed, "direct_nm": direct}


def get_route(route: str = None, target: str = "next"):
    """Optimal route from the boat to the next mark (target='next') or the course finish."""
    nav = NAV.get_navigator(route)
    if not nav.get("available"):
        return {"available": False, "note": nav.get("note", "No position yet.")}
    marks = NAV._marks(nav["route"])
    if target == "finish":
        dest = marks[-1]
    else:
        dest = next((m for m in marks if m["name"] == nav["next_mark"]["name"]), marks[-1])
    s = NAV._latest()
    slat, slon = s["lat"], s["lon"]
    if slat is None:
        return {"available": False, "note": "No position yet."}

    live = (s.get("tws"), s.get("twd"))
    wind, use_fcst = make_wind_fn(slat, slon, live)
    if wind is None:
        return {"available": False, "target": dest["name"], "note": NO_WIND}

    key = (round(slat, 3), round(slon, 3), dest["name"], target, round(live[0] or 0),
           round(live[1] or 0), use_fcst, _deploy_gen())     # a new push → a new route
    if _cache["key"] == key and time.time() - _cache["t"] < CACHE_TTL:
        return _cache["val"]

    t0 = time.time()
    try:
        leg = route_leg(slat, slon, dest["lat"], dest["lon"], wind, t0)
    except NoWindError as exc:
        return {"available": False, "target": dest["name"], "note": str(exc)}
    if not leg["reached"]:
        out = {"available": False, "target": dest["name"], "reached": False,
               "short_nm": leg["short_nm"],
               # the search horizon (MAX_STEPS * DT_H) ran out; no partial ETA reported
               "note": f"No route to {dest['name']}. Stopped {leg['short_nm']} nm short."}
        _cache.update(key=key, t=time.time(), val=out)
        return out
    path, legs, direct = leg["path"], leg["legs"], leg["direct_nm"]

    # count tacks/gybes = tack changes along the path
    tacks = sum(1 for a, b in zip(legs, legs[1:]) if a["tack"] != b["tack"])
    sailed = leg["sailed_nm"]
    eta_min = round((leg["reached_t"] - t0) / 60, 1)

    out = {
        "available": True, "route": nav["route"], "target": dest["name"],
        "wind_source": wind.source, "forecast_gaps": len(wind.filled),
        "forecast_old": bool(wind.stale),
        "direct_nm": round(direct, 2), "sailed_nm": round(sailed, 2),
        "eta_min": eta_min, "tacks": tacks,
        "recommended_heading": legs[0]["hdg"] if legs else None,
        "first_tack": legs[0]["tack"] if legs else None,
        "path": path,
        "note": "Fastest route. Practice use only (RRS 41 in a race).",
    }
    _cache.update(key=key, t=time.time(), val=out)
    return out
