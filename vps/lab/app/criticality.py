"""Route criticality (#379): reverse isochrones + "time lost if you're here", per leg.

Expedition's "how critical is the route" picture. For each routed leg, two runs on the SAME
time-varying wind field the optimizer used:
  * forward: earliest time the boat can be at each point of a grid, leaving the leg start at the
    route's start time;
  * reverse: latest time the boat can leave each point and still reach the mark at the forward
    route's ETA (time runs backward from that ETA).
At a point, (earliest arrival) - (latest departure) is how much later than the best route the boat
finishes if it sails through that point. Shifted so the best point is 0, that is the time-loss grid.
A narrow band of low loss = the route is critical; a wide one = the side hardly matters.

Both runs are a time-dependent shortest path over a regular grid (16 headings per node), not the
isochrone fan: we need a time at EVERY point, not just the winning path. The physics is the
router's: its polar lookup, wind-over-water and current drift, helm x sea state, and borrowed
nearby wind where the forecast has a hole (none past FILL_NM: no route there). Obstacles exactly
as the router: every step is checked with `ObstacleField.crosses` (the exact supercover, and a step
leaving the chart box counts as blocked). Tack and sail-change costs are left out, so the grid
route is a little quicker than the real one; only the differences matter here.

Opt-in (`optimize_course(..., criticality=True)`): it costs a few seconds per leg.
"""
from __future__ import annotations

import heapq
import math
import os

CELLS = int(os.environ.get("ROUTE_CRIT_CELLS", "48"))         # grid nodes across the leg's long side
MAX_NODES = int(os.environ.get("ROUTE_CRIT_MAX_NODES", "4000"))
CAP_MIN = float(os.environ.get("ROUTE_CRIT_CAP_MIN", "20"))    # loss past this isn't sent (null)
BANDS_MIN = (5, 10, 20)                                       # the map's shades
MIN_SOG = 0.3
# 32 moves: every (a, b) step with |a|, |b| <= 3 and no common factor — headings every ~18 deg
# at worst, so a reach isn't forced far off its course.
MOVES = sorted({(a, b) for a in range(-3, 4) for b in range(-3, 4)
                if (a or b) and math.gcd(abs(a), abs(b)) == 1})


class _Grid:
    """A lat/lon grid of square-ish cells (in nm), symmetric about the leg's midpoint."""

    def __init__(self, slat, slon, dlat, dlon, cells=CELLS, max_nodes=MAX_NODES):
        self.clat, self.clon = (slat + dlat) / 2.0, (slon + dlon) / 2.0
        self.kx = 60.0 * max(0.15, math.cos(math.radians(self.clat)))     # nm per deg lon
        direct = math.hypot((dlat - slat) * 60.0, (dlon - slon) * self.kx)
        pad = 0.6 * direct + 0.5
        half_x = abs(dlon - slon) * self.kx / 2.0 + pad
        half_y = abs(dlat - slat) * 60.0 / 2.0 + pad
        cell = 2.0 * max(half_x, half_y) / max(4, cells)
        # odd node counts so the midpoint is a node and the grid mirrors about it
        nx = 2 * int(math.ceil(half_x / cell)) + 1
        ny = 2 * int(math.ceil(half_y / cell)) + 1
        while nx * ny > max_nodes:
            cell *= 1.15
            nx = 2 * int(math.ceil(half_x / cell)) + 1
            ny = 2 * int(math.ceil(half_y / cell)) + 1
        self.cell, self.nx, self.ny = cell, nx, ny
        self.lat0 = self.clat - (ny // 2) * cell / 60.0           # south-west node
        self.lon0 = self.clon - (nx // 2) * cell / self.kx
        self.dlat_deg, self.dlon_deg = cell / 60.0, cell / self.kx

    def ll(self, i, j):
        return self.lat0 + j * self.dlat_deg, self.lon0 + i * self.dlon_deg

    def snap(self, lat, lon):
        i = int(round((lon - self.lon0) / self.dlon_deg))
        j = int(round((lat - self.lat0) / self.dlat_deg))
        return min(max(i, 0), self.nx - 1), min(max(j, 0), self.ny - 1)


def _physics(wf, P, cur, waves, helm_factor, wave_coeffs, wave_uniform_hs):
    """speed(lat, lon, t, track_deg) -> knots over the ground along the track, or None. The
    router's model: polar at the wind over the water, x helm x sea state, plus the current."""
    from . import optimizer as OPT
    realized_on = (abs(helm_factor - 1.0) > 1e-3) or (waves is not None and getattr(waves, "loaded", False))

    def wind(lat, lon, t):
        return wf.wind_at(lat, lon, t) or OPT._nearby_wind(wf, lat, lon, t)

    def at(lat, lon, t):
        """Everything that depends on the node only: (tws, twd, set, drift, hs) or None."""
        w = wind(lat, lon, t)
        if not w:
            return None
        tws, twd = w
        cs = cd = 0.0
        if cur is not None:
            cs, cd = cur.current_at(lat, lon, t)
            tws, twd = OPT._wind_over_water(tws, twd, cs, cd)
            cd = cd or 0.0
        hs = 0.0
        if realized_on:
            hs = (wave_uniform_hs if wave_uniform_hs is not None
                  else (waves.wave_at(lat, lon, t) if waves is not None else 0.0))
        return tws, twd, cs, cd, hs

    def speed(env, track):
        tws, twd, cs, cd, hs = env
        twa = abs(OPT._wrap180(track - twd))
        sp = OPT._polar_speed(P, tws, twa)
        if sp < MIN_SOG:
            return None
        if realized_on:
            sp *= OPT._realized_factor(hs, twa, helm_factor, wave_coeffs)
        if cd > 0.01:                      # crab to hold the track: along + across the current
            rel = math.radians(cs - track)
            c_par, c_perp = cd * math.cos(rel), cd * math.sin(rel)
            if abs(c_perp) >= sp:
                return None
            sp = math.sqrt(sp * sp - c_perp * c_perp) + c_par
        return sp if sp >= MIN_SOG else None

    return at, speed


def _edges(g, obstacles):
    """Per node, its usable moves: [(neighbour, nm, bearing)], every step checked like the router's."""
    from . import optimizer as OPT
    blk = getattr(obstacles, "blocked", None) if obstacles else None
    free = [[not (blk and blk(*g.ll(i, j))) for i in range(g.nx)] for j in range(g.ny)]
    out = {}
    for j in range(g.ny):
        for i in range(g.nx):
            if not free[j][i]:
                continue
            la, lo = g.ll(i, j)
            lst = []
            for di, dj in MOVES:
                ii, jj = i + di, j + dj
                if not (0 <= ii < g.nx and 0 <= jj < g.ny) or not free[jj][ii]:
                    continue
                lb, lob = g.ll(ii, jj)
                if obstacles and obstacles.crosses(la, lo, lb, lob):
                    continue
                lst.append(((ii, jj), OPT._hav_nm(la, lo, lb, lob), OPT._bearing(la, lo, lb, lob)))
            out[(i, j)] = lst
    return out


def _forward(g, edges, at, speed, src, t0, horizon):
    """Earliest arrival time at every node, leaving `src` at t0 (time-dependent Dijkstra)."""
    best = {src: t0}
    pq = [(t0, src)]
    while pq:
        t, u = heapq.heappop(pq)
        if t > best.get(u, math.inf) or t > horizon:
            continue
        env = at(*g.ll(*u), t)
        if env is None:
            continue
        for v, nm, brg in edges.get(u, ()):
            sp = speed(env, brg)
            if sp is None:
                continue
            tv = t + nm / sp * 3600.0
            if tv < best.get(v, math.inf):
                best[v] = tv
                heapq.heappush(pq, (tv, v))
    return best


def _reverse(g, edges, at, speed, dst, eta, horizon):
    """Latest departure time from every node that still reaches `dst` at `eta` (time runs back).
    The step v->u is priced on the wind at u at the time the boat is there (the reverse run's
    clock); the boat sails v->u, so the track is the bearing v->u."""
    best = {dst: eta}
    pq = [(-eta, dst)]
    while pq:
        nt, u = heapq.heappop(pq)
        t = -nt
        if t < best.get(u, -math.inf) or t < horizon:
            continue
        env = at(*g.ll(*u), t)
        if env is None:
            continue
        for v, nm, brg in edges.get(u, ()):            # moves are symmetric: v is a predecessor too
            sp = speed(env, (brg + 180.0) % 360.0)
            if sp is None:
                continue
            tv = t - nm / sp * 3600.0
            if tv > best.get(v, -math.inf):
                best[v] = tv
                heapq.heappush(pq, (-tv, v))
    return best


def leg_band(wf, P, slat, slon, t0, dlat, dlon, eta, obstacles=None, cur=None, waves=None,
             helm_factor=1.0, wave_coeffs=None, wave_uniform_hs=None, cells=CELLS,
             cap_min=CAP_MIN):
    """The time-loss grid for one leg. `eta` = the forward route's arrival at the mark (the
    reverse run's clock starts there). Returns a dict for the map, or None when the grid finds no
    way from start to mark (blocked, or no wind)."""
    g = _Grid(slat, slon, dlat, dlon, cells=cells)
    at, speed = _physics(wf, P, cur, waves, helm_factor, wave_coeffs, wave_uniform_hs)
    edges = _edges(g, obstacles)
    src, dst = g.snap(slat, slon), g.snap(dlat, dlon)
    leg_s = max(600.0, float(eta) - float(t0))
    fw = _forward(g, edges, at, speed, src, float(t0), float(t0) + 3.0 * leg_s)
    bw = _reverse(g, edges, at, speed, dst, float(eta), float(eta) - 3.0 * leg_s)
    slack = {k: fw[k] - bw[k] for k in fw if k in bw}
    if dst not in fw or src not in bw or not slack:
        return None
    base = min(slack.values())
    loss = [None] * (g.nx * g.ny)
    for (i, j), s in slack.items():
        # "here" = within one cell: the best of this node and the neighbours it can sail straight
        # to. On a grid a beat's close-hauled diagonals reach only every other node; the rest
        # paid a parity detour twice (there and back), a checkerboard of ~7 min that isn't real.
        near = [slack[v] for v, _nm, _b in edges.get((i, j), ())
                if abs(v[0] - i) + abs(v[1] - j) == 1 and v in slack]
        m = (min([s] + near) - base) / 60.0
        if m <= cap_min:
            loss[j * g.nx + i] = round(m, 1)
    widths = {}
    for b in BANDS_MIN:            # mean width of each band: its area over the leg's length
        n = sum(1 for v in loss if v is not None and v <= b)
        direct = max(g.cell, math.hypot((dlat - slat) * 60.0, (dlon - slon) * g.kx))
        widths[str(b)] = round(n * g.cell * g.cell / direct, 2)
    return {
        "lat0": round(g.lat0, 6), "lon0": round(g.lon0, 6),
        "dlat": round(g.dlat_deg, 7), "dlon": round(g.dlon_deg, 7),
        "nx": g.nx, "ny": g.ny, "cell_nm": round(g.cell, 3),
        "loss_min": loss,                         # row-major from the south-west node; null = > cap
        "grid_minutes": round((fw[dst] - float(t0)) / 60.0, 1),
        "width_nm": widths,                       # mean width of the 5/10/20-min band
    }


def course_bands(wf, P, legs, cur=None, waves=None, helm_factor=1.0, wave_coeffs=None,
                 wave_uniform_hs=None, cells=CELLS):
    """Bands for every routed leg. `legs` = [{to, slat, slon, t0, dlat, dlon, eta, obstacles}],
    as the optimizer routed them. A leg that fails is left out (logged), never the route."""
    out = []
    for k, lg in enumerate(legs):
        try:
            b = leg_band(wf, P, lg["slat"], lg["slon"], lg["t0"], lg["dlat"], lg["dlon"], lg["eta"],
                         obstacles=lg.get("obstacles"), cur=cur, waves=waves,
                         helm_factor=helm_factor, wave_coeffs=wave_coeffs,
                         wave_uniform_hs=wave_uniform_hs, cells=cells)
        except Exception as exc:  # noqa: BLE001 — a band is a bonus; the route stands without it
            print(f"[criticality] leg {k}: {type(exc).__name__}: {exc}", flush=True)
            b = None
        if b:
            b.update({"leg": k, "to": lg.get("to")})
            out.append(b)
    return {"bands_min": list(BANDS_MIN), "cap_min": CAP_MIN, "legs": out}
