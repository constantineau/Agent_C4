"""Tack and gybe loss (#375) — find every manoeuvre in a sailed track and say what it cost.

Pure: fixes in, a JSON-clean dict out. No network, no files, no boat constants. It reads the same
fix shape every debrief track carries ({t, lat, lon, sog, cog} plus, from the boat's own log,
the measured {tws, twa, stw}) so a boat-log recording, a GPX export and a tracker track all go
through one path.

How it works
------------
1. **Which side the wind is on, per fix.** From the measured true wind angle where the fix has
   one (signed: + = wind over the starboard side). Where it doesn't (a paddlewheel or wind
   dropout, or a GPX/YB track with position only) the angle is the course against a wind
   direction: the measured one either side of the hole, or — with no wind instruments at all —
   one GUESSED from the track itself: the bisector of the two courses either side of each turn
   lies on the wind axis. A GPS-only answer is labelled `basis: "gps"`, `confidence: "low"`,
   and says so in `note`.
2. **A manoeuvre is a change of side** held long enough on both sides (short flickers are
   noise: a dead run's wind angle wanders across 180° every few seconds) and with a real change
   of course. Through 0° it is a tack, through 180° a gybe.
3. **Loss is measured from positions**, not integrated from a noisy speed: distance made along
   the wind axis (toward the wind upwind, away from it downwind) across a window around the
   turn, against what the steady VMG before and after would have made in the same time. A
   steady current moves the baseline and the turn alike, so it cancels.
   loss_s = loss_m / steady VMG — the time it would take to sail back the lost distance.
4. **It says when it can't.** A hole in the log, a neighbouring turn eating the steady stretch,
   an upwind-to-downwind change (a mark rounding — VMG can't measure that), or an instrument
   fault from the trust sweep: the manoeuvre stays in the list with a plain flag, its loss is
   None or it is left out of the averages (`counted: False`), never quietly averaged in.

The output shape is meant to be read by other views (the replay map and strip chart mark
manoeuvres from it): every manoeuvre has `id`, `t` (epoch s at head-to-wind / stern-to-wind),
`t_start`/`t_end` (the turn), `window` ([t0, t1] the loss was measured over), `lat`/`lon`.
"""
import bisect
import math
import os

from . import track as _track

VERSION = 1

# ---- tuning (seconds, degrees, knots) — generic sailing numbers, nothing boat-specific --------
NEUTRAL_DEG = float(os.environ.get("MAN_NEUTRAL_DEG", "12"))   # |TWA| < this or > 180-this: no side
MIN_RUN_S = float(os.environ.get("MAN_MIN_RUN_S", "15"))       # a side held for less is noise
NEUTRAL_BREAK_S = 60.0           # this long with no side splits a run
MIN_TURN_DEG = 30.0              # a side change with less course change is not a manoeuvre
PRE_GAP_S = 20.0                 # the loss window starts at least this long before head-to-wind
POST_MIN_S = 40.0                # ...and ends at least this long after it
POST_MAX_S = 120.0               # ...or later, once the boat is back up to speed, up to this
BASE_S = 60.0                    # steady stretch either side, for the VMG baseline
BASE_MIN_S = 15.0                # shorter than this is no baseline
TURN_TOL_DEG = 10.0              # within this of the steady course = turn not started / finished
RECOVER_FRAC = 0.9               # "back up to speed" = 90% of the steady speed before the turn
MAX_FIX_INTERVAL_S = 30.0        # sparser than this (a tracker) can't show a tack
MIN_VMG_KN = 0.6                 # below this steady VMG (a reach, a drift) loss means nothing
TWD_HALF_S = 120.0               # measured wind direction smoothed over ±this
TWD_FILL_S = 900.0               # a wind-direction hole is filled from measurements this close
AXIS_HALF_S = 2700.0             # GPS-only: wind axis from turns within ±45 min
KN = 1852.0 / 3600.0             # m/s per knot
M_PER_DEG = 111_195.0

TYPES = ("tack", "gybe")

# Every fix field the analysis reads. `track_revision` hashes exactly these, so a track whose
# times or instrument values change (not just its positions) reads as a new track to the card.
FIX_FIELDS = ("t", "lat", "lon", "sog", "cog", "tws", "twa", "stw")


def track_revision(track):
    """A short content hash of a stored track: every fix's FIX_FIELDS, plus its sail log and
    trust sweep (both feed the answer). Changes whenever anything the analysis reads changes."""
    import hashlib
    import json
    t = track or {}
    rows = [[f.get(k) for k in FIX_FIELDS] for f in (t.get("fixes") or ())]
    blob = json.dumps([rows, t.get("sail_log") or [], t.get("trust")], sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def _wrap180(d):
    return ((d + 180.0) % 360.0) - 180.0


def _side_name(s):
    return "starboard" if s > 0 else "port"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _circ_mean(angles, weights=None):
    sx = sy = 0.0
    for i, a in enumerate(angles):
        w = 1.0 if weights is None else weights[i]
        sx += w * math.sin(math.radians(a))
        sy += w * math.cos(math.radians(a))
    if abs(sx) < 1e-9 and abs(sy) < 1e-9:
        return None
    return math.degrees(math.atan2(sx, sy)) % 360.0


def _r(v, nd=1):
    return None if v is None else round(v, nd)


class _Track:
    """Cleaned fixes as parallel arrays, local metres, smoothed course and speed."""

    def __init__(self, fixes):
        rows = {}
        for f in fixes or ():
            t, lat, lon = _num(f.get("t")), _num(f.get("lat")), _num(f.get("lon"))
            if t is None or lat is None or lon is None:
                continue
            rows[t] = f          # one fix per timestamp, the last wins
        ts = sorted(rows)
        self.t = ts
        self.n = len(ts)
        self.dt = None
        if not ts:
            return
        fs = [rows[t] for t in ts]
        lat0 = _num(fs[0]["lat"])
        lon0 = _num(fs[0]["lon"])
        k = math.cos(math.radians(lat0))
        self.lat = [_num(f["lat"]) for f in fs]
        self.lon = [_num(f["lon"]) for f in fs]
        self.x = [(lo - lon0) * M_PER_DEG * k for lo in self.lon]
        self.y = [(la - lat0) * M_PER_DEG for la in self.lat]
        self.sog = [_num(f.get("sog")) for f in fs]
        self.cog = [_num(f.get("cog")) for f in fs]
        self.stw = [_num(f.get("stw")) for f in fs]
        twa = []
        for f in fs:
            a = _num(f.get("twa"))
            twa.append(None if a is None else _wrap180(a))
        self.twa_meas = twa
        dts = sorted(ts[i + 1] - ts[i] for i in range(self.n - 1))
        self.dt = dts[len(dts) // 2] if dts else None
        self._smooth()

    def _window(self, i, half):
        lo = bisect.bisect_left(self.t, self.t[i] - half)
        hi = bisect.bisect_right(self.t, self.t[i] + half) - 1
        return lo, hi

    def _smooth(self):
        """Course: the circular mean of COG over ±half a few seconds, or the bearing of the
        displacement over that span where COG is missing. Speed over ground likewise falls back
        to the displacement."""
        half = max(2.0, 1.5 * (self.dt or 1.0))
        self.course, self.gspeed = [], []
        for i in range(self.n):
            lo, hi = self._window(i, half)
            if hi <= lo:
                lo, hi = max(0, i - 1), min(self.n - 1, i + 1)
            cogs = [self.cog[j] for j in range(lo, hi + 1) if self.cog[j] is not None]
            span = self.t[hi] - self.t[lo]
            dx, dy = self.x[hi] - self.x[lo], self.y[hi] - self.y[lo]
            pos_course = (math.degrees(math.atan2(dx, dy)) % 360.0) if span > 0 and (dx or dy) else None
            c = _circ_mean(cogs) if cogs else pos_course
            self.course.append(c)
            s = self.sog[i]
            if s is None and span > 0:
                s = math.hypot(dx, dy) / span / KN
            self.gspeed.append(s)

    def pos(self, t):
        """Position (x, y) at time t, linear between fixes; None outside the track."""
        if t < self.t[0] or t > self.t[-1]:
            return None
        j = bisect.bisect_left(self.t, t)
        if self.t[j] == t:
            return self.x[j], self.y[j]
        a, b = j - 1, j
        f = (t - self.t[a]) / (self.t[b] - self.t[a])
        return (self.x[a] + f * (self.x[b] - self.x[a]), self.y[a] + f * (self.y[b] - self.y[a]))

    def pos_smooth(self, t, half=2.0):
        """Position at t averaged over ±half s (GPS jitter at a window's ends would otherwise
        land straight in the distance made). Exact on a straight line at steady speed."""
        pts = [self.pos(min(max(t + k * half / 2.0, self.t[0]), self.t[-1])) for k in (-2, -1, 0, 1, 2)]
        pts = [p for p in pts if p is not None]
        if not pts or t < self.t[0] or t > self.t[-1]:
            return None
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    def idx(self, t0, t1):
        return range(bisect.bisect_left(self.t, t0), bisect.bisect_right(self.t, t1))

    def max_gap(self, t0, t1):
        """Longest stretch without a fix inside [t0, t1] (edges included)."""
        r = self.idx(t0, t1)
        if len(r) == 0:
            return t1 - t0
        pts = [t0] + [self.t[i] for i in r] + [t1]
        return max(pts[i + 1] - pts[i] for i in range(len(pts) - 1))


# ---- wind direction -------------------------------------------------------------------------
def _instrument_twd(tr):
    """Per fix: (twd, True) from measured wind, smoothed over ±TWD_HALF_S and filled across
    holes up to TWD_FILL_S; (None, False) where no measurement is near."""
    raw_t, raw_s, raw_c = [], [], []
    for i in range(tr.n):
        a, c = tr.twa_meas[i], tr.course[i]
        if a is None or c is None:
            continue
        d = math.radians((c + a) % 360.0)
        raw_t.append(tr.t[i]); raw_s.append(math.sin(d)); raw_c.append(math.cos(d))
    if not raw_t:
        return [None] * tr.n
    ps, pc = [0.0], [0.0]
    for s, c in zip(raw_s, raw_c):
        ps.append(ps[-1] + s); pc.append(pc[-1] + c)
    out = []
    for i in range(tr.n):
        t = tr.t[i]
        lo = bisect.bisect_left(raw_t, t - TWD_HALF_S)
        hi = bisect.bisect_right(raw_t, t + TWD_HALF_S)
        if hi <= lo:
            # a hole: the measurements nearest before and after, if close enough
            b = bisect.bisect_left(raw_t, t)
            near = [j for j in (b - 1, b) if 0 <= j < len(raw_t) and abs(raw_t[j] - t) <= TWD_FILL_S]
            if not near:
                out.append(None)
                continue
            sx = sy = 0.0
            for j in near:            # each side's own smoothed direction, weighted alike
                lo2 = bisect.bisect_left(raw_t, raw_t[j] - TWD_HALF_S)
                hi2 = bisect.bisect_right(raw_t, raw_t[j] + TWD_HALF_S)
                sx += (ps[hi2] - ps[lo2]) / (hi2 - lo2)
                sy += (pc[hi2] - pc[lo2]) / (hi2 - lo2)
        else:
            sx, sy = ps[hi] - ps[lo], pc[hi] - pc[lo]
        out.append(math.degrees(math.atan2(sx, sy)) % 360.0 if (sx or sy) else None)
    return out


def _legs(tr):
    """Steady legs: maximal runs where the course holds within 15° of the run's mean for 30 s+.
    → [(i0, i1, mean_course, mean_speed)]."""
    legs, i = [], 0
    while i < tr.n:
        if tr.course[i] is None:
            i += 1
            continue
        sx = math.sin(math.radians(tr.course[i])); sy = math.cos(math.radians(tr.course[i]))
        j = i + 1
        while j < tr.n and tr.course[j] is not None:
            m = math.degrees(math.atan2(sx, sy))
            if abs(_wrap180(tr.course[j] - m)) > 15.0:
                break
            sx += math.sin(math.radians(tr.course[j])); sy += math.cos(math.radians(tr.course[j]))
            j += 1
        if tr.t[j - 1] - tr.t[i] >= 30.0:
            sp = [tr.gspeed[k] for k in range(i, j) if tr.gspeed[k] is not None]
            legs.append((i, j - 1, math.degrees(math.atan2(sx, sy)) % 360.0,
                         sum(sp) / len(sp) if sp else None))
        i = j
    return legs


def _gps_twd(tr, hint=None):
    """GUESS the wind direction per fix from the track alone (no wind instruments).

    Each turn between two steady legs of similar speed whose courses differ by 50–150° has its
    bisector on the wind axis (pointing into the wind for a tack, away for a gybe). The axis at
    a time is the weighted mean of nearby bisectors (on doubled angles, so a tack's and a gybe's
    agree). Which end is upwind: the `hint` if given (e.g. a forecast direction), else the end
    whose turns were sailed slower (upwind legs are slower than downwind ones). With turns of
    one kind only there is nothing to compare, and they are taken to be tacks.

    → (twd per fix or None, note)"""
    legs = _legs(tr)
    cands = []                       # (t, bisector, weight, mean speed)
    for a, b in zip(legs, legs[1:]):
        if tr.t[b[0]] - tr.t[a[1]] > 90.0:
            continue
        d = _wrap180(b[2] - a[2])
        if not (50.0 <= abs(d) <= 150.0):
            continue
        if a[3] and b[3] and abs(a[3] - b[3]) / max(a[3], b[3]) > 0.3:
            continue                  # speed changed a lot: a mark rounding, not a tack or gybe
        w = min(tr.t[a[1]] - tr.t[a[0]], tr.t[b[1]] - tr.t[b[0]])
        sp = [s for s in (a[3], b[3]) if s]
        cands.append(((tr.t[a[1]] + tr.t[b[0]]) / 2.0, (a[2] + d / 2.0) % 360.0, w,
                      sum(sp) / len(sp) if sp else None))
    if not cands:
        return [None] * tr.n, "No clear turns to read the wind from."
    all_axis = _circ_mean([2 * c[1] for c in cands], [c[2] for c in cands])
    if all_axis is None:
        return [None] * tr.n, "No clear turns to read the wind from."
    ax = all_axis / 2.0                      # 0..180: the wind is from ax or ax+180
    note = "No wind instruments: wind direction guessed from the track."
    if hint is not None:
        up = ax if abs(_wrap180(hint - ax)) <= 90 else ax + 180.0
    else:
        g1 = [c[3] for c in cands if c[3] and abs(_wrap180(c[1] - ax)) <= 90]
        g2 = [c[3] for c in cands if c[3] and abs(_wrap180(c[1] - ax)) > 90]
        if g1 and g2:
            up = ax if sum(g1) / len(g1) <= sum(g2) / len(g2) else ax + 180.0
        else:
            only = [c for c in cands if (abs(_wrap180(c[1] - ax)) <= 90) == bool(g1)]
            up = _circ_mean([c[1] for c in only]) if only else ax
            note += " All turns look alike, so they are read as tacks."
    up %= 360.0
    out = []
    ct = [c[0] for c in cands]
    for i in range(tr.n):
        t = tr.t[i]
        lo = bisect.bisect_left(ct, t - AXIS_HALF_S)
        hi = bisect.bisect_right(ct, t + AXIS_HALF_S)
        near = cands[lo:hi] or cands
        a2 = _circ_mean([2 * c[1] for c in near], [c[2] for c in near])
        a = (a2 if a2 is not None else all_axis) / 2.0
        out.append(a % 360.0 if abs(_wrap180(a - up)) <= 90 else (a + 180.0) % 360.0)
    return out, note


# ---- detection ------------------------------------------------------------------------------
def _sides(tr, twa):
    """Runs of fixes on one side: [(side, i_first, i_last)], noise flickers dropped.

    A run's held time counts only the time between CONSECUTIVE valid readings on that side, and a
    dropout (no wind angle at all for longer than a few fixes) splits the run. Otherwise a few
    scattered readings across long dropouts would add up to a "held" side and make turns out of
    nothing (Codex on #396)."""
    dt = tr.dt or 1.0
    drop_s = max(15.0, 3.0 * dt)
    runs = []                        # [side, i0, i1, held_s]
    prev = None                      # the last fix with any wind angle (neutral or not)
    for i in range(tr.n):
        a = twa[i]
        if a is None:
            continue
        dropout = prev is not None and tr.t[i] - tr.t[prev] > drop_s
        last_valid, prev = prev, i
        if abs(a) < NEUTRAL_DEG or abs(a) > 180.0 - NEUTRAL_DEG:
            continue
        s = 1 if a > 0 else -1
        if runs and runs[-1][0] == s and not dropout and tr.t[i] - tr.t[runs[-1][2]] <= NEUTRAL_BREAK_S:
            if runs[-1][2] == last_valid:          # straight on from the previous reading
                runs[-1][3] += tr.t[i] - tr.t[last_valid]
            runs[-1][2] = i
        else:
            runs.append([s, i, i, min(dt, drop_s)])
    for _ in range(3):               # drop flickers, merge what they split, repeat
        runs = [r for r in runs if r[3] >= MIN_RUN_S]
        merged = []
        for r in runs:
            if merged and merged[-1][0] == r[0]:
                merged[-1][2] = r[2]
                merged[-1][3] += r[3]
            else:
                merged.append(list(r))
        runs = merged
    return [(r[0], r[1], r[2]) for r in runs]


def _mean_course(tr, t0, t1):
    cs = [tr.course[i] for i in tr.idx(t0, t1) if tr.course[i] is not None]
    return _circ_mean(cs) if cs else None


def _mean_abs_twa(twa, tr, t0, t1):
    xs = [abs(twa[i]) for i in tr.idx(t0, t1) if twa[i] is not None]
    return sum(xs) / len(xs) if xs else None


def _find(tr, twa):
    runs = _sides(tr, twa)
    out = []
    for (s1, a0, a1), (s2, b0, b1) in zip(runs, runs[1:]):
        if s1 == s2:
            continue
        ta, tb = tr.t[a1], tr.t[b0]
        # through 0° (tack) or 180° (gybe)? the angle either side of the crossing says
        edge = [abs(twa[i]) for i in (a1, b0)]
        kind = "tack" if sum(edge) / 2.0 < 90.0 else "gybe"
        # the crossing moment: the fix closest to 0/180 between the runs, else interpolate
        between = [i for i in range(a1 + 1, b0) if twa[i] is not None]
        if between:
            key = (lambda i: abs(twa[i])) if kind == "tack" else (lambda i: -abs(twa[i]))
            t0 = tr.t[min(between, key=key)]
        else:
            da = edge[0] if kind == "tack" else 180.0 - edge[0]
            db = edge[1] if kind == "tack" else 180.0 - edge[1]
            t0 = ta + (tb - ta) * (da / (da + db) if da + db > 0 else 0.5)
        lo_run, hi_run = tr.t[a0], tr.t[b1]
        c_before = _mean_course(tr, max(lo_run, ta - 45.0), ta - 10.0) \
            or _mean_course(tr, max(lo_run, ta - 45.0), ta)
        c_after = _mean_course(tr, tb + 10.0, min(hi_run, tb + 45.0)) \
            or _mean_course(tr, tb, min(hi_run, tb + 45.0))
        if c_before is None or c_after is None:
            continue
        turn = abs(_wrap180(c_after - c_before))
        if turn < MIN_TURN_DEG:
            continue
        out.append({"type": kind, "t0": t0, "from": s1, "to": s2, "c_before": c_before,
                    "c_after": c_after, "turn": turn, "run_lo": lo_run, "run_hi": hi_run,
                    "ta": ta, "tb": tb})
    return out


def _turn_bounds(tr, m):
    t0 = m["t0"]
    t_start = t0 - 15.0
    for i in reversed(tr.idx(t0 - 45.0, t0)):
        c = tr.course[i]
        if c is not None and abs(_wrap180(c - m["c_before"])) <= TURN_TOL_DEG:
            t_start = tr.t[i]
            break
    t_end = t0 + 15.0
    for i in tr.idx(t0, t0 + 60.0):
        c = tr.course[i]
        if c is not None and abs(_wrap180(c - m["c_after"])) <= TURN_TOL_DEG:
            t_end = tr.t[i]
            break
    return min(t_start, t0), max(t_end, t0)


def _vmg(tr, t0, t1, u):
    """Mean velocity along unit vector u (m/s) between t0 and t1, from positions."""
    a, b = tr.pos_smooth(t0), tr.pos_smooth(t1)
    if a is None or b is None or t1 <= t0:
        return None
    return ((b[0] - a[0]) * u[0] + (b[1] - a[1]) * u[1]) / (t1 - t0)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _trust_ran(trust):
    return isinstance(trust, dict) and trust.get("available") is True


def _in_danger(trust, t0, t1):
    dz = ((trust or {}).get("danger") or {})
    hit = []
    for ch, ivs in dz.items():
        for iv in ivs or ():
            try:
                a, b = float(iv[0]), float(iv[1])
            except (TypeError, ValueError, IndexError):
                continue
            if a < t1 and b > t0:
                hit.append(ch)
                break
    return sorted(set(hit))


# ---- the analysis -----------------------------------------------------------------------------
def analyze(fixes, sail_log=None, trust=None, twd_hint=None):
    """Every tack and gybe in `fixes`, with its loss. See the module docstring for the method.

    `sail_log`: the crew's timestamped sail log (boat-log tracks) — each manoeuvre gets the sail
    setup it was sailed under. `trust`: the `/racelog/trust` sweep stored with the track; a
    manoeuvre near a danger interval is flagged and left out of the averages. `twd_hint`: a rough
    wind direction (degrees from) to tell upwind from downwind on a GPS-only track."""
    tr = _Track(fixes)
    if tr.dt is not None and tr.dt > MAX_FIX_INTERVAL_S:
        return _empty(f"Fixes are {round(tr.dt)} s apart. Too far apart to see tacks and gybes.",
                      fix_interval_s=tr.dt)
    if tr.n < 20 or tr.dt is None:
        return _empty("Too few fixes to look for tacks and gybes.")
    inst = _instrument_twd(tr)
    gps, gps_note = (None, None)
    if any(v is None for v in inst):
        gps, gps_note = _gps_twd(tr, _num(twd_hint))
    twd, basis_i = [], []
    for i in range(tr.n):
        if inst[i] is not None:
            twd.append(inst[i]); basis_i.append("instruments")
        elif gps is not None and gps[i] is not None:
            twd.append(gps[i]); basis_i.append("gps")
        else:
            twd.append(None); basis_i.append(None)
    # the angle the detection reads: measured where there is one, else course against the wind
    twa = []
    for i in range(tr.n):
        if tr.twa_meas[i] is not None:
            twa.append(tr.twa_meas[i])
        elif twd[i] is not None and tr.course[i] is not None:
            twa.append(_wrap180(twd[i] - tr.course[i]))
        else:
            twa.append(None)

    found = _find(tr, twa)
    for m in found:
        m["t_start"], m["t_end"] = _turn_bounds(tr, m)

    out = []
    for k, m in enumerate(found):
        prev_end = found[k - 1]["t_end"] if k > 0 else tr.t[0]
        next_start = found[k + 1]["t_start"] if k + 1 < len(found) else tr.t[-1]
        out.append(_measure(tr, twa, twd, basis_i, m, k, prev_end, next_start, sail_log, trust))

    bases = {m["basis"] for m in out} or {b for b in basis_i if b}
    basis = ("instruments" if bases == {"instruments"} else "gps" if bases == {"gps"}
             else "mixed" if bases else "none")
    confidence = ("low" if basis in ("gps", "none") else "medium" if basis == "mixed" or tr.dt > 5
                  else "high")
    notes = []
    if basis in ("gps", "mixed") and gps_note:
        notes.append(gps_note + " Tack and gybe losses are rough.")
    if basis == "none":
        notes.append(gps_note or "No wind direction, so no tacks or gybes.")
    if not out and basis != "none":
        notes.append("No tacks or gybes found.")
    if any("instruments not checked" in m["flags"] for m in out):
        notes.append("The instrument check didn't run on this track, so turns read off the "
                     "wind instruments aren't in the averages.")
    return {"available": True, "version": VERSION, "basis": basis, "confidence": confidence,
            "note": " ".join(notes) or None, "fix_interval_s": _r(tr.dt, 1),
            "trust_checked": _trust_ran(trust),
            "maneuvers": out, "summary": summarize(out)}


def _empty(note, fix_interval_s=None):
    return {"available": False, "version": VERSION, "note": note, "maneuvers": [],
            "fix_interval_s": _r(fix_interval_s, 1), "summary": summarize([])}


def _measure(tr, twa, twd, basis_i, m, k, prev_end, next_start, sail_log, trust):
    t0, flags = m["t0"], []
    t_start, t_end = m["t_start"], m["t_end"]
    j0 = min(range(tr.n), key=lambda i: abs(tr.t[i] - t0))
    lat, lon = tr.lat[j0], tr.lon[j0]

    # steady before/after: point of sail and the reference wind direction
    tw_b = _mean_abs_twa(twa, tr, max(prev_end, t_start - BASE_S), t_start)
    tw_a = _mean_abs_twa(twa, tr, t_end + 10.0, min(next_start, t_end + 10.0 + BASE_S))
    ref = [twd[i] for i in tr.idx(max(prev_end, t_start - BASE_S), t_start) if twd[i] is not None] \
        + [twd[i] for i in tr.idx(t_end, min(next_start, t_end + BASE_S)) if twd[i] is not None]
    ref_twd = _circ_mean(ref) if ref else twd[j0]
    basis = basis_i[j0] or "gps"

    # speed channel: the paddlewheel if it read through the turn, else speed over ground
    span = list(tr.idx(t_start - BASE_S, t_end + POST_MAX_S))
    use_stw = bool(span) and sum(1 for i in span if tr.stw[i] is not None) >= 0.9 * len(span)
    spd = tr.stw if use_stw else tr.gspeed

    def mean_spd(a, b):
        return _mean([spd[i] for i in tr.idx(a, b)])

    entry = mean_spd(t_start - 10.0, t_start) or (spd[j0] if spd[j0] is not None else None)
    steady = mean_spd(max(prev_end, t_start - BASE_S), t_start) or entry
    mins = [spd[i] for i in tr.idx(t_start, t_end + 30.0) if spd[i] is not None]
    vmin = min(mins) if mins else None
    exit_ = mean_spd(t_end, t_end + 0.5 * (tr.dt or 1.0)) or mean_spd(t_end, t_end + 5.0)
    t_rec = None
    if steady:
        idx = list(tr.idx(t_end, t0 + POST_MAX_S))
        for n, i in enumerate(idx):
            near = [spd[j] for j in idx[max(0, n - 1):n + 2] if spd[j] is not None]
            if near and sum(near) / len(near) >= RECOVER_FRAC * steady:
                t_rec = tr.t[i]
                break
    if t_rec is None:
        flags.append("didn't get back up to speed in 2 min")

    # the loss window and the steady stretches either side of it
    w0 = min(t_start - 5.0, t0 - PRE_GAP_S)
    w1 = max(t0 + POST_MIN_S, (t_rec or 0.0) + 5.0)
    w1 = min(w1, t0 + POST_MAX_S)
    clipped = False
    if w0 < prev_end:
        w0, clipped = prev_end, True
        flags.append("too soon after the last turn")
    if w1 > next_start:
        w1, clipped = next_start, True
        flags.append("next turn came too soon")
    b0 = (max(prev_end, w0 - BASE_S), w0)
    b1 = (w1, min(next_start, w1 + BASE_S, tr.t[-1]))
    loss_m = loss_s = vmg_b = vmg_a = None

    upwind_b = tw_b is not None and tw_b < 90.0
    upwind_a = tw_a is not None and tw_a < 90.0
    trusted = True
    bad = _in_danger(trust, b0[0], b1[1])
    if basis == "instruments" and not _trust_ran(trust):
        # No instrument check over this track: its wind-based numbers are unverified, so they
        # stay out of the averages (same rule as a danger window, but said differently).
        trusted = False
        flags.append("instruments not checked")
    elif bad:
        trusted = False
        flags.append("instrument fault here (" + ", ".join(bad) + ")")
    max_gap = max(15.0, 3.0 * (tr.dt or 1.0))
    if tw_b is None or tw_a is None or ref_twd is None:
        flags.append("no steady stretch to compare")
    elif upwind_b != upwind_a:
        flags.append("upwind on one side, downwind on the other (mark?)")
    elif tr.max_gap(w0, w1) > max_gap:
        flags.append("gap in the log")
    else:
        u = (math.sin(math.radians(ref_twd)), math.cos(math.radians(ref_twd)))
        if not upwind_b:
            u = (-u[0], -u[1])
        base = []
        if b0[1] - b0[0] >= BASE_MIN_S and tr.max_gap(*b0) <= max_gap:
            vmg_b = _vmg(tr, b0[0], b0[1], u)
            base.append(vmg_b)
        if b1[1] - b1[0] >= BASE_MIN_S and tr.max_gap(*b1) <= max_gap:
            vmg_a = _vmg(tr, b1[0], b1[1], u)
            base.append(vmg_a)
        base = [b for b in base if b is not None]
        actual = _vmg(tr, w0, w1, u)
        if not base or actual is None:
            flags.append("no steady stretch to compare")
        elif clipped:
            pass                          # the window was cut short: a loss would be too small
        else:
            vb = sum(base) / len(base)
            if vb < MIN_VMG_KN * KN:
                flags.append("reaching: no VMG to lose")
            else:
                # The steady boat sails the old side until head-to-wind and the new side after,
                # so each side's VMG is weighted by its own share of the window. With one side
                # only, that side stands for both. Weighting by side also cancels most of a few
                # degrees' error in the wind direction (each side's error is the other's
                # opposite) and a wind shift across the turn.
                if vmg_b is not None and vmg_a is not None:
                    ideal = vmg_b * (t0 - w0) + vmg_a * (w1 - t0)
                else:
                    ideal = vb * (w1 - w0)
                loss_m = ideal - actual * (w1 - w0)
                loss_s = loss_m / vb
    cfg = _track.config_at(sail_log, t0)
    if not _track._config_plausible(cfg, min(x for x in (tw_b, tw_a) if x is not None)
                                    if (tw_b is not None or tw_a is not None) else None):
        cfg = None
    conf = "low" if basis == "gps" else ("medium" if (tr.dt or 1.0) > 5 else "high")
    return {
        "id": k, "type": m["type"], "t": _r(t0, 1), "t_start": _r(t_start, 1),
        "t_end": _r(t_end, 1), "window": [_r(w0, 1), _r(w1, 1)],
        "lat": round(lat, 6), "lon": round(lon, 6),
        "from_side": _side_name(m["from"]), "to_side": _side_name(m["to"]),
        "course_before": _r(m["c_before"]), "course_after": _r(m["c_after"]),
        "turn_deg": _r(m["turn"]), "twa_before": _r(tw_b), "twa_after": _r(tw_a),
        "twd": _r(ref_twd), "entry_kn": _r(entry, 2), "min_kn": _r(vmin, 2),
        "exit_kn": _r(exit_, 2), "speed_source": "stw" if use_stw else "sog",
        "recover_s": _r(t_rec - t0 if t_rec is not None else None),
        "vmg_before_kn": _r(vmg_b / KN if vmg_b is not None else None, 2),
        "vmg_after_kn": _r(vmg_a / KN if vmg_a is not None else None, 2),
        "loss_m": _r(loss_m), "loss_s": _r(loss_s), "config": cfg,
        "basis": basis, "confidence": conf, "trusted": trusted,
        "counted": loss_m is not None and trusted, "flags": flags,
    }


_AVG_KEYS = ("loss_s", "loss_m", "recover_s", "entry_kn", "min_kn", "exit_kn")


def _avg(ms):
    row = {"n": len(ms)}
    for key in _AVG_KEYS:
        xs = [m[key] for m in ms if m.get(key) is not None]
        row[key] = _r(sum(xs) / len(xs), 2 if key.endswith("_kn") else 1) if xs else None
    return row


def summarize(maneuvers):
    """Averages over the manoeuvres that count (a loss, no instrument fault): per type, and per
    type split by the side the boat turned ONTO and by the sail setup."""
    good = [m for m in maneuvers if m.get("counted")]
    out = {"found": len(maneuvers), "counted": len(good), "groups": []}
    for kind in TYPES:
        ms = [m for m in good if m["type"] == kind]
        out[kind] = _avg(ms)
        for side in ("port", "starboard"):
            g = [m for m in ms if m["to_side"] == side]
            if g:
                out["groups"].append({"type": kind, "by": "side", "key": side, **_avg(g)})
        for cfg in sorted({m.get("config") or "" for m in ms}):
            g = [m for m in ms if (m.get("config") or "") == cfg]
            out["groups"].append({"type": kind, "by": "config", "key": cfg or None, **_avg(g)})
    return out
