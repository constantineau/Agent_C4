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

Both runs are a time-dependent shortest path over a regular grid (32 headings per node), not the
isochrone fan: we need a time at EVERY point, not just the winning path. The physics is the
router's: its polar lookup, wind-over-water and current drift, helm x sea state, and borrowed
nearby wind where the forecast has a hole (none past FILL_NM: no route there). Every step, in
both runs, is priced on the wind where and when it LEAVES (the reverse run solves for that
departure time), so a reverse time is a passage the forward run could sail. A tack or gybe costs
the router's tack time (`optimizer.tack_cost_s()`, the boat's own on an optimize run): each grid
state carries the tack the boat is on. Obstacles exactly as the router: every step is checked with
`ObstacleField.crosses` (the exact supercover, and a step leaving the chart box counts as
blocked). Sail-change costs are left out.

A known limit (reviewed on #395, accepted; Expedition's bands are the same): when the wind changes
fast, the slack is only approximately the time lost. The reverse run prices the rest of the leg at
the times that meet the ORIGINAL ETA, while a boat that detoured reaches that point later and
meets different wind. The exact answer is a full onward route from every grid point, far too slow.
So each leg says how much its wind changes (`wind_change`) and is flagged `rough` past
ROUGH_DIR_DEG / ROUGH_KN; the map says "approximate when the wind changes fast".

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
# a leg whose wind changes more than this over its window is flagged "rough" (see wind_change)
ROUGH_DIR_DEG = float(os.environ.get("ROUTE_CRIT_ROUGH_DEG", "25"))
ROUGH_KN = float(os.environ.get("ROUTE_CRIT_ROUGH_KN", "4"))
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
                # flat over the grid (its cells are square in nm): the step v->u is exactly the
                # reverse of u->v, so the forward and reverse runs price the same track
                lst.append(((ii, jj), g.cell * math.hypot(di, dj),
                            math.degrees(math.atan2(di, dj)) % 360.0))
            out[(i, j)] = lst
    return out


def _side(env, track):
    """Which tack/gybe a step is on: +1 wind on one side of the track, -1 the other (a change
    of sign is a tack or a gybe, the router's rule); 0 dead up/down wind, which keeps the tack
    the boat is on (else a dead run would count as one gybe side and break the mirror)."""
    from . import optimizer as OPT
    a = OPT._wrap180(track - env[1])
    if abs(a) < 0.5 or abs(a) > 179.5:
        return 0
    return 1 if a > 0 else -1


def _cached(g, at, bucket_s=60.0):
    """`at` per node, the time rounded to `bucket_s`: the reverse run asks the same node at many
    nearly equal times while it solves for departures. Both runs use it, so they agree."""
    memo = {}

    def env(node, t):
        k = (node, int(t // bucket_s))
        if k not in memo:
            memo[k] = at(*g.ll(*node), k[1] * bucket_s)
        return memo[k]
    return env


def _forward(edges, env, speed, src, t0, horizon, tack_s):
    """Earliest arrival at every (node, side), leaving `src` at t0 (time-dependent Dijkstra).
    `side` = the tack the boat arrived on (0 at the start). A step on the other tack first pays
    `tack_s`, the router's tack cost. Each step is priced on the wind where and when it leaves."""
    best = {(src, 0): t0}
    pq = [(t0, src, 0)]
    while pq:
        t, u, s = heapq.heappop(pq)
        if t > best.get((u, s), math.inf) or t > horizon:
            continue
        e = env(u, t)
        if e is None:
            continue
        for v, nm, brg in edges.get(u, ()):
            side = _side(e, brg)
            dep, ed = t, e
            if s and side and side != s:                       # tack first; the step leaves after it
                dep = t + tack_s
                ed = env(u, dep)
                if ed is None:
                    continue
            sp = speed(ed, brg)
            if sp is None:
                continue
            tv = dep + nm / sp * 3600.0
            side = side or s
            if tv < best.get((v, side), math.inf):
                best[(v, side)] = tv
                heapq.heappush(pq, (tv, v, side))
    return best


def _latest_departure(env, speed, v, track, nm, t, s, tack_s, floor, iters=6, bisect=14):
    """Latest time the boat can leave node v on `track` and be ready to leave u at `t` on tack
    `s` (a tack at u costs `tack_s` first), priced like `_forward`: on the wind at v when it
    leaves. Returns (departure, side) or None. A few fixed-point passes find it in smooth wind;
    the answer is always CHECKED (sailed forward, it must arrive by t). If the passes don't give
    a checked one (a sharp wind change can make them flip-flop), it brackets a departure that
    arrives in time, stepping back (doubling) no further than `floor`, and bisects to the latest."""
    def arrive(x):
        e = env(v, x)
        sp = speed(e, track) if e is not None else None
        if sp is None:
            return None, 0
        side = _side(e, track)
        return x + nm / sp * 3600.0 + (tack_s if s and side and side != s else 0.0), side

    x = t
    for _ in range(iters):
        a = arrive(x)[0]
        if a is None:
            break
        nx = x - (a - t)                    # move the departure by how late/early it arrives
        if abs(nx - x) < 1.0:
            x = nx
            break
        x = nx
    a, side = arrive(x)
    if a is not None and a <= t + 1.0:
        return x, side
    hi = min(x, t)                          # leaving here (or later) doesn't make it
    step = max(60.0, nm / 20.0 * 3600.0)    # as if at 20 kn, then doubling
    lo = None
    while hi - step >= floor:
        a, _ = arrive(hi - step)
        if a is not None and a <= t + 1.0:
            lo = hi - step
            break
        hi, step = hi - step, step * 2.0
    if lo is None:
        return None                         # no departure in the window makes it: no edge
    for _ in range(bisect):
        mid = (lo + hi) / 2.0
        a, _ = arrive(mid)
        if a is not None and a <= t + 1.0:
            lo = mid
        else:
            hi = mid
    return lo, arrive(lo)[1]


def _reverse(edges, env, speed, dst, eta, horizon, tack_s):
    """Latest departure from every (node, side) that still reaches `dst` at `eta` (time runs
    back). `side` = the tack the boat leaves on (0 at the mark). Priced exactly like `_forward`:
    a step v->u is sailed on the wind at v when it LEAVES v (`_latest_departure` solves for that
    time and checks it); a tack at u costs `tack_s` before the boat can leave it."""
    best = {(dst, 0): eta}
    pq = [(-eta, dst, 0)]
    while pq:
        nt, u, s = heapq.heappop(pq)
        t = -nt
        if t < best.get((u, s), -math.inf) or t < horizon:
            continue
        for v, nm, brg in edges.get(u, ()):            # moves are symmetric: v is a predecessor too
            got = _latest_departure(env, speed, v, (brg + 180.0) % 360.0, nm, t, s, tack_s,
                                    horizon)
            if got is None:
                continue
            tv, side = got
            side = side or s
            if tv > best.get((v, side), -math.inf):
                best[(v, side)] = tv
                heapq.heappush(pq, (-tv, v, side))
    return best


def _slack(fw, bw, tack_s):
    """Per node: (earliest arrival) - (latest departure), over the tacks it arrives and leaves
    on (a change there costs a tack). Nodes only one run reaches are left out."""
    fin, bout = {}, {}
    for (n, s), t in fw.items():
        fin.setdefault(n, []).append((s, t))
    for (n, s), t in bw.items():
        bout.setdefault(n, []).append((s, t))
    out = {}
    for n, arr in fin.items():
        if n not in bout:
            continue
        out[n] = min(ta - tb + (tack_s if a and b and a != b else 0.0)
                     for a, ta in arr for b, tb in bout[n])
    return out


def wind_change(at, slat, slon, dlat, dlon, t0, eta):
    """How much the wind changes over the leg's window: the largest shift (deg) and change in
    speed (kn) at the start, the midpoint and the mark between the leg's start, middle and end
    times. The band is approximate when this is large (see the module notes)."""
    from . import optimizer as OPT
    pts = [(slat, slon), ((slat + dlat) / 2.0, (slon + dlon) / 2.0), (dlat, dlon)]
    times = [t0, (t0 + eta) / 2.0, eta]
    ddir = dkn = 0.0
    for la, lo in pts:
        ws = [w for w in (at(la, lo, t) for t in times) if w]
        for i in range(len(ws)):
            for k in range(i + 1, len(ws)):
                ddir = max(ddir, abs(OPT._wrap180(ws[i][1] - ws[k][1])))
                dkn = max(dkn, abs(ws[i][0] - ws[k][0]))
    return round(ddir), round(dkn, 1)


def leg_band(wf, P, slat, slon, t0, dlat, dlon, eta, obstacles=None, cur=None, waves=None,
             helm_factor=1.0, wave_coeffs=None, wave_uniform_hs=None, cells=CELLS,
             cap_min=CAP_MIN, tack_s=None):
    """The time-loss grid for one leg. `eta` = the forward route's arrival at the mark (the
    reverse run's clock starts there). `tack_s` = seconds a tack/gybe costs (None = the router's,
    `optimizer.tack_cost_s()`: the boat's own on an optimize run). Returns a dict for the map, or
    None when the grid finds no way from start to mark (blocked, or no wind)."""
    from . import optimizer as OPT
    tack_s = OPT.tack_cost_s() if tack_s is None else float(tack_s)
    g = _Grid(slat, slon, dlat, dlon, cells=cells)
    at, speed = _physics(wf, P, cur, waves, helm_factor, wave_coeffs, wave_uniform_hs)
    env = _cached(g, at)
    edges = _edges(g, obstacles)
    src, dst = g.snap(slat, slon), g.snap(dlat, dlon)
    leg_s = max(600.0, float(eta) - float(t0))
    fw = _forward(edges, env, speed, src, float(t0), float(t0) + 3.0 * leg_s, tack_s)
    bw = _reverse(edges, env, speed, dst, float(eta), float(eta) - 3.0 * leg_s, tack_s)
    slack = _slack(fw, bw, tack_s)
    fw_dst = min((t for (n, _s), t in fw.items() if n == dst), default=None)
    bw_src = max((t for (n, _s), t in bw.items() if n == src), default=None)
    if fw_dst is None or bw_src is None or not slack:
        return None
    base = min(slack.values())
    ddir, dkn = wind_change(at, slat, slon, dlat, dlon, float(t0), float(eta))
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
        "grid_minutes": round((fw_dst - float(t0)) / 60.0, 1),
        "reverse_minutes": round((float(eta) - bw_src) / 60.0, 1),   # the reverse run's leg time
        "tack_s": tack_s,
        # how much the wind changes over the leg: past ROUGH_* the band is only a rough guide
        "wind_change": {"dir_deg": ddir, "kn": dkn},
        "rough": ddir > ROUGH_DIR_DEG or dkn > ROUGH_KN,
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
