"""Test helper for the optimizer validation suite (#407): made-up boats, synthetic wind and current
fields, and an independent reference answer to grade the router against.

Nothing here imports the optimizer's routing code: the reference answers are worked out from the
polar and plain geometry, so a test compares the router with a second, simpler method rather than
with itself. Polar lookups go through `shared.polar` (the same reader the router uses), because the
question is "does the router pick the right route on this polar", not "does it read the polar".

Polars: testdata/polars/*.json, all made up and test-only, deliberately unlike each other:
  keelboat              a generic 35 ft keelboat
  displacement_cruiser  heavy, slow, stops at hull speed
  multihull             faster than the wind on a reach
  planing_sportboat     jumps onto the plane broad reaching from 14 kn
"""
import json
import math
import os

from shared import polar as SPOL

_HERE = os.path.dirname(os.path.abspath(__file__))
POLAR_DIR = os.path.join(_HERE, "testdata", "polars")
BOATS = ("keelboat", "displacement_cruiser", "multihull", "planing_sportboat")
_CACHE = {}

LAT0, LON0 = 44.0, -82.0         # open water, anywhere: no coastline is loaded in these tests


def polar(name):
    """A test boat's polar as the router takes it: a flat [(tws, twa, stw)] list (same object on
    every call, so the router's per-table caches work)."""
    if name not in _CACHE:
        with open(os.path.join(POLAR_DIR, f"{name}.json")) as f:
            d = json.load(f)
        _CACHE[name] = [(float(tws), float(a), float(s))
                        for tws, row in d["polar"].items() for a, s in row.items()]
    return _CACHE[name]


def speed(P, tws, twa):
    return SPOL.lookup(P, tws, abs(twa))


def vmg(P, tws, pos):
    """(best TWA, VMG along the wind axis) for 'beat' or 'run'."""
    a = SPOL.vmg_twa(SPOL.rows_for(P), tws, pos)
    return a, abs(speed(P, tws, a) * math.cos(math.radians(a)))


def wrap180(d):
    return (d + 180.0) % 360.0 - 180.0


def advance(lat, lon, brg, nm):
    """Flat-earth step (fine over the few nm these tests sail)."""
    b = math.radians(brg)
    return (lat + nm * math.cos(b) / 60.0,
            lon + nm * math.sin(b) / (60.0 * math.cos(math.radians(lat))))


def to_xy(lat, lon):
    """nm east, nm north of (LAT0, LON0)."""
    return ((lon - LON0) * 60.0 * math.cos(math.radians(LAT0)), (lat - LAT0) * 60.0)


# --- wind fields ------------------------------------------------------------------------------
class Field:
    """A wind field from a function wind(t_seconds, x_nm, y_nm) -> (tws, twd)."""
    loaded = True

    def __init__(self, fn):
        self.fn = fn
        self.series, self.meta = {}, []

    def wind_at(self, lat, lon, t):
        x, y = to_xy(lat, lon)
        return self.fn(t, x, y)

    def detail_at(self, lat, lon, t):
        tws, twd = self.wind_at(lat, lon, t)
        return {"tws": tws, "twd": twd, "confidence": 1.0}

    def status(self):
        return {"models": []}


def steady(tws, twd):
    return Field(lambda t, x, y: (tws, twd))


def persistent(tws, twd0, deg_per_h):
    """The wind swings steadily one way: + = shifts right (clockwise)."""
    return Field(lambda t, x, y: (tws, (twd0 + deg_per_h * t / 3600.0) % 360))


def oscillating(tws, twd0, amp, period_min):
    """The wind swings back and forth +-amp about twd0."""
    w = 2 * math.pi / (period_min * 60.0)
    return Field(lambda t, x, y: (tws, (twd0 + amp * math.sin(w * t)) % 360))


class TidalStream:
    """A current that runs `foul_set` at `drift` kn until `turn_s`, then the other way."""
    loaded = True
    source = "test"
    label = "test stream"

    def __init__(self, foul_set, drift, turn_s):
        self.foul_set, self.drift, self.turn_s = float(foul_set), float(drift), float(turn_s)

    def current_at(self, lat, lon, epoch):
        if epoch < self.turn_s:
            return (self.foul_set, self.drift)
        return ((self.foul_set + 180.0) % 360, self.drift)

    def status(self):
        return {"loaded": True, "source": self.source}


# --- reference answers --------------------------------------------------------------------------
def two_board_time_h(P, tws, twd, dist_nm, brg, tack_s):
    """Steady wind, mark `brg` from the start: the time of the best route with at most one tack,
    worked out from the polar alone (the textbook answer: in steady wind the order of the boards
    doesn't matter and one tack is the minimum). Searches every pair of headings, one each side of
    the mark, on a 1 deg grid and then 0.05 deg around the best pair. Returns
    (hours, tacks, hdg_a, hdg_b)."""
    mx, my = dist_nm * math.sin(math.radians(brg)), dist_nm * math.cos(math.radians(brg))
    best = None
    s = speed(P, tws, wrap180(brg - twd))
    if s > 0.3:
        best = (dist_nm / s, 0, brg, brg)       # one board straight at the mark

    def pair(ha, hb):
        va, vb = speed(P, tws, wrap180(ha - twd)), speed(P, tws, wrap180(hb - twd))
        if va < 0.3 or vb < 0.3:
            return None
        ax, ay = math.sin(math.radians(ha)), math.cos(math.radians(ha))
        bx, by = math.sin(math.radians(hb)), math.cos(math.radians(hb))
        det = ax * by - ay * bx
        if abs(det) < 1e-9:
            return None
        la, lb = (mx * by - my * bx) / det, (ax * my - ay * mx) / det
        if la < 0 or lb < 0:
            return None
        return la / va + lb / vb + tack_s / 3600.0

    two = None
    for i in range(1, 90):                       # board a right of the mark, board b left of it
        for j in range(1, 90):
            t = pair(brg + i, brg - j)
            if t is not None and (two is None or t < two[0]):
                two = (t, brg + i, brg - j)
    if two is not None:
        _t, ca, cb = two
        for i in range(-30, 31):
            for j in range(-30, 31):
                ha, hb = ca + i * 0.05, cb + j * 0.05
                if wrap180(ha - brg) <= 0 or wrap180(hb - brg) >= 0:
                    continue
                t = pair(ha, hb)
                if t is not None and t < two[0]:
                    two = (t, ha, hb)
        if best is None or two[0] < best[0]:
            best = (two[0], 1, two[1] % 360, two[2] % 360)
    return best


def water_wind(tws, twd, cset, cdrift):
    """The wind the boat feels over moving water: (tws, twd) less the current's own motion."""
    ax = tws * math.sin(math.radians(twd + 180.0)) - cdrift * math.sin(math.radians(cset))
    ay = tws * math.cos(math.radians(twd + 180.0)) - cdrift * math.cos(math.radians(cset))
    return math.hypot(ax, ay), (math.degrees(math.atan2(ax, ay)) + 180.0) % 360


def best_progress_kn(P, tws, twd, cset, cdrift, brg):
    """The fastest a boat can make good along `brg` over the ground in this wind and current
    (searches every heading, 0.5 deg grid). Can be negative: the stream sets it back."""
    w, d = water_wind(tws, twd, cset, cdrift)
    c = cdrift * math.cos(math.radians(cset - brg))
    best = None
    for k in range(720):
        h = k * 0.5
        v = speed(P, w, wrap180(h - d)) * math.cos(math.radians(h - brg)) + c
        best = v if best is None or v > best else best
    return best


def stream_go_time_h(P, tws, twd, stream, dist_nm, brg, dt_s=10.0, max_h=12.0):
    """Sail straight for a mark `dist_nm` along `brg` through a `TidalStream`, always making the
    best progress along the line (a 1-D simulator, cross-track ignored). Hours."""
    x = t = 0.0
    while t < max_h * 3600:
        cset, cdrift = stream.current_at(0, 0, t)
        v = best_progress_kn(P, tws, twd, cset, cdrift, brg)
        step = v * dt_s / 3600.0
        if x + step >= dist_nm:
            return (t + (dist_nm - x) / v * 3600.0) / 3600.0
        x, t = x + step, t + dt_s
    return None


def stream_wait_time_h(P, tws, twd, stream, dist_nm, brg):
    """Anchor until the stream turns, then sail the mark on the fair stream. Hours."""
    cset, cdrift = stream.current_at(0, 0, stream.turn_s)
    v = best_progress_kn(P, tws, twd, cset, cdrift, brg)
    return (stream.turn_s + dist_nm / v * 3600.0) / 3600.0


def around_disk_time_h(P, tws, twd, start, end, center, r, side):
    """Steady wind, a round island (center, radius r, all in nm x/y) between start and end: the
    time of the shortest path round it on `side` (+1 = counterclockwise round the island, -1 =
    clockwise), sailing each piece at the polar speed for its heading: tangent, arc (1 deg chords),
    tangent. The textbook shortest path round a convex obstacle."""
    cx, cy = center

    def ang(p):
        return math.atan2(p[1] - cy, p[0] - cx)

    def leg_h(p, q):
        dx, dy = q[0] - p[0], q[1] - p[1]
        d = math.hypot(dx, dy)
        if d < 1e-9:
            return 0.0
        h = math.degrees(math.atan2(dx, dy)) % 360
        return d / speed(P, tws, wrap180(h - twd))

    dp, de = math.hypot(start[0] - cx, start[1] - cy), math.hypot(end[0] - cx, end[1] - cy)
    t1 = ang(start) + side * math.acos(r / dp)
    t2 = ang(end) - side * math.acos(r / de)
    sweep = (t2 - t1) * side % (2 * math.pi)
    pts = [start]
    n = max(1, int(math.degrees(sweep)))
    for k in range(n + 1):
        a = t1 + side * sweep * k / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.append(end)
    return sum(leg_h(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def one_tack_shift_time_h(P, tws, wind_fn, mark_xy, tack_s, first, dt_s=10.0, max_h=12.0):
    """Time-varying wind (`wind_fn(t) -> twd`), mark at `mark_xy` (nm): the best route that sails
    close-hauled on the `first` tack ('port' or 'stbd'), tacks once at the best moment, then sails
    close-hauled until it can lay the mark and reaches straight in. A small brute-force simulator,
    independent of the router. Returns hours (None if no tack time gets there)."""
    beat = SPOL.vmg_twa(SPOL.rows_for(P), tws, "beat")
    v_beat = speed(P, tws, beat)

    def heading(t, tack):
        twd = wind_fn(t)
        return (twd + beat) % 360 if tack == "port" else (twd - beat) % 360

    def lay_angle_ok(t, x, y, tack):
        # can lay: the mark is no closer to the wind than the beat angle on this tack
        twd = wind_fn(t)
        b = math.degrees(math.atan2(mark_xy[0] - x, mark_xy[1] - y)) % 360
        off = wrap180(b - twd)
        return (off >= beat - 1e-6) if tack == "port" else (off <= -beat + 1e-6)

    def sail(t, x, y, tack, until_s=None):
        # close-hauled on `tack`; once it lays the mark, steer straight at it, step by step (the
        # wind keeps turning, so the angle and speed on the final board keep changing too), and
        # sail close-hauled again whenever a shift takes the lay away
        while t < max_h * 3600:
            if until_s is not None and t >= until_s:
                return (t, x, y)
            d = math.hypot(mark_xy[0] - x, mark_xy[1] - y)
            if until_s is None and lay_angle_ok(t, x, y, tack):
                b = math.degrees(math.atan2(mark_xy[0] - x, mark_xy[1] - y)) % 360
                s = speed(P, tws, wrap180(b - wind_fn(t)))
                if s < 0.3:
                    return None
                step = s * dt_s / 3600.0
                if step >= d:
                    return t + d / s * 3600
                h = math.radians(b)
            else:
                h = math.radians(heading(t, tack))
                step = v_beat * dt_s / 3600.0
            x, y, t = x + step * math.sin(h), y + step * math.cos(h), t + dt_s
        return None

    other = "stbd" if first == "port" else "port"
    best = None
    for k in range(1, int(max_h * 360)):
        t_tack = k * 60.0
        got = sail(0.0, 0.0, 0.0, first, until_s=t_tack)
        if got is None:
            break
        t1, x1, y1 = got
        if lay_angle_ok(t1, x1, y1, first):
            break                  # already past the layline before tacking: tacking later only loses
        end = sail(t1 + tack_s, x1, y1, other)
        if end is not None and (best is None or end < best):
            best = end
    return None if best is None else best / 3600.0


def course(marks, start=(LAT0, LON0)):
    """A RaceDefinition with one course through `marks` [(lat, lon)], finishing at the last one."""
    *mids, fin = marks
    return {"race_id": "optval", "zones": [], "courses": [{
        "id": "c", "start": {"lat": start[0], "lon": start[1]},
        "marks": [{"name": f"M{i + 1}", "type": "mark", "lat": la, "lon": lo}
                  for i, (la, lo) in enumerate(mids)],
        "finish": {"points": [{"lat": fin[0], "lon": fin[1]},
                              {"lat": fin[0], "lon": fin[1] + 0.0002}]}}]}


def header_tacker_time_h(P, tws, wind_fn, mean_twd, mark_xy, tack_s, thr_deg=2.0, dt_s=5.0,
                         max_h=12.0):
    """The textbook way to sail an oscillating beat, as a small simulator independent of the
    router: sail close-hauled, tack whenever headed more than `thr_deg` past the mean wind
    `mean_twd` (losing `tack_s` a tack), and fetch straight in once the mark can be laid.
    Starts on the lifted tack. Returns (hours, tacks)."""
    beat = SPOL.vmg_twa(SPOL.rows_for(P), tws, "beat")
    v = speed(P, tws, beat)
    x = y = t = 0.0
    tacks = 0
    # port tack is lifted when the wind is left of the mean, headed when it is right of it
    tack = "port" if wrap180(wind_fn(0.0) - mean_twd) <= 0 else "stbd"
    while t < max_h * 3600:
        twd = wind_fn(t)
        d = math.hypot(mark_xy[0] - x, mark_xy[1] - y)
        b = math.degrees(math.atan2(mark_xy[0] - x, mark_xy[1] - y)) % 360
        off = wrap180(b - twd)
        if abs(off) >= beat - 1e-6:                   # can lay it: sail straight at it
            lay = "port" if off > 0 else "stbd"
            if lay != tack:                           # laying it on the other tack: that's a tack
                tack = lay
                tacks += 1
                t += tack_s
                continue
            s = speed(P, tws, off)
            step = s * dt_s / 3600.0
            if step >= d:
                return (t + d / s * 3600.0) / 3600.0, tacks
            x, y = x + step * math.sin(math.radians(b)), y + step * math.cos(math.radians(b))
            t += dt_s
            continue
        shift = wrap180(twd - mean_twd)
        if (tack == "port" and shift > thr_deg) or (tack == "stbd" and shift < -thr_deg):
            tack = "stbd" if tack == "port" else "port"
            tacks += 1
            t += tack_s
            continue
        h = math.radians(twd + beat if tack == "port" else twd - beat)
        step = v * dt_s / 3600.0
        x, y = x + step * math.sin(h), y + step * math.cos(h)
        t += dt_s
    return None, tacks
