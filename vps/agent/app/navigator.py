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
import threading
import time

from shared import staleness

from . import datasource

BOAT_ID = os.environ.get("BOAT_ID", "sr33")
ROUND_NM = 0.06            # within this of a mark = rounded; advance to the next
LAYLINE_TOL_DEG = 4.0      # within this of a layline bearing = "on the layline"

# Active course, shared in-process by the web endpoints and the agent tool so "what's next"
# in chat matches what's on the iPad plot. Updated when a course is loaded/generated, and
# persisted through the datasource kv (where the store offers it) — every deploy rebuilds the
# engine container, and without the restore a restart silently dropped back to 'default' with
# no course (navigator/tactics/checklist-location dark until someone reloaded the homework).
_active = "default"
_restored = False


def _course_changed(route):
    """Tell the re-route (orinroute) the course aboard changed: routes made before count as an
    old course (#125). Lazy import: orinroute imports this module."""
    from . import orinroute
    orinroute.bump_load_id(route)


def set_active(route):
    """Make `route` the active course. A SWITCH to another route is a course change, so it gets
    a new course load id (a /course/load then sets its own). Callers that also write marks hold
    orinroute.COURSE_LOCK across both."""
    global _active, _restored
    from . import orinroute
    new = route or "default"
    with orinroute.COURSE_LOCK:          # a switch is a course change (re-entrant for callers)
        changed = new != active_route()
        _active = new
        _restored = True
        if changed:
            _course_changed(new)
    try:
        ds = datasource.active()
        if hasattr(ds, "save_active_route"):
            ds.save_active_route(_active)
    except Exception:
        pass


def active_route():
    global _active, _restored
    if not _restored:
        _restored = True
        try:
            ds = datasource.active()
            if hasattr(ds, "get_active_route"):
                saved = ds.get_active_route()
                if saved:
                    _active = saved
        except Exception:
            pass
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


# --- course progression -----------------------------------------------------
# Mark advance used to be a stateless PROXIMITY test — "next mark = the first one you are not
# within ROUND_NM of" — which silently never fires on a distance race. On Bayview Mackinac 2026
# the Start stayed `next_mark` for the entire race: the boat is inside 111 m of it for only a few
# seconds at the gun, and the instant it is further away the Start satisfies the test again (and
# there was no latch, so even that instant would have reverted). ETA went null for the whole race
# — VMG toward a mark astern is negative — and everything keyed on the mark index went with it:
# matcher leg gating failed open, tactics leverage never computed, the onboard re-route resolved
# its destination back to the Start, and the buoy layer hunted corroborators behind the boat.
#
# Advance is now a PLANE crossing: you have passed a mark once you are beyond the plane through
# it normal to the leg you are sailing — the OUTBOUND leg for the start line, the INBOUND leg for
# a rounding. That is monotone on a normal course, so it is correct with no stored state at all,
# which also keeps it replayable at an arbitrary moment. Close aboard (<= ROUND_NM) still counts
# for a rounding, so windward-leeward buoy racing behaves exactly as before; it does NOT count for
# the start, which is left only by crossing it (see _is_start). The kv latch is a ratchet layered on
# top for the cases plane-crossing alone cannot see: drifting back over the line pre-start, or a
# course that doubles back on itself.
PASS_NM = 0.05             # distance beyond a mark's plane that counts as passed (~90 m)
# The ratchet is persistent, so a single bad fix could latch us onto the wrong mark for the rest
# of the race. That is not hypothetical: the Jul 18 archive shows own-ship position contaminated
# by the AIS receiver channel (see pi/archiver — AIS contexts were archived under boat_id), so
# 17% of position reads were some other vessel. An advance therefore counts only once
# PROGRESS_CONFIRM DISTINCT fixes (by fix time) agree on it, and a fix that does not advance
# clears the count (#41). One /strategy poll reads the navigator 4-5 times on the same sample;
# those are one fix, not five. Until the advance is confirmed the navigator keeps answering with
# the confirmed mark, so the chartplotter (gpsout) never steers to a mark seen on one fix (#246).
PROGRESS_CONFIRM = 2
_pending = {}              # route -> {"i": index, "fixes": [distinct fix keys]}
HIST_MAX = 50              # how many earlier marks Undo can step back through
# Progress reads and writes (the poll, Start, Undo, a mark pick) are read-modify-write on one kv
# blob, from several request threads: one lock. Never take COURSE_LOCK while holding it.
_PROGRESS_LOCK = threading.RLock()
# Set false to ignore the stored ratchet and run on geometry alone (the replay rig does this so
# a frame depends only on its own timestamp, never on which frames were computed before it).
PROGRESS_LATCH = os.environ.get("NAV_PROGRESS_LATCH", "true").strip().lower() != "false"
# A reading older than its channel's DEAD limit (wall clock) is not "now" (#45.1, #247): an old
# position is no position, old wind is no wind. The limits are the one per-channel table,
# `shared/staleness.py`: aboard 10 s for the GPS fix, COG and SOG, 15 s for heading and wind; in
# the cloud 120 s (it sees the boat through the uplink). NAV_MAX_AGE_S, when set, overrides every
# channel with one number; 0 turns the check off.
_NAV_MAX_AGE_ENV = os.environ.get("NAV_MAX_AGE_S")
NAV_MAX_AGE_S = None if _NAV_MAX_AGE_ENV in (None, "") else float(_NAV_MAX_AGE_ENV)


def _max_age(key):
    """Dead limit (s) for one `_PATHS` key; NAV_MAX_AGE_S overrides it when set."""
    if NAV_MAX_AGE_S is not None:
        return NAV_MAX_AGE_S
    return staleness.dead_s(_PATHS[key])


def _beyond_nm(lat, lon, m, axis_deg):
    """Signed distance (nm) the boat lies beyond mark `m`, measured along `axis_deg`.

    Positive = past the plane through `m` normal to the axis; negative = still short of it."""
    d = _hav_nm(m["lat"], m["lon"], lat, lon)
    if d == 0.0:
        return 0.0
    return d * math.cos(math.radians(_bearing(m["lat"], m["lon"], lat, lon) - axis_deg))


def _plane_axis(marks, i):
    """Bearing of the axis marks[i]'s plane is normal to: the leg INTO it, or for the start the
    leg OUT of it. None on a one-mark course."""
    m = marks[i]
    if i > 0:
        return _bearing(marks[i - 1]["lat"], marks[i - 1]["lon"], m["lat"], m["lon"])
    if len(marks) > 1:
        return _bearing(m["lat"], m["lon"], marks[i + 1]["lat"], marks[i + 1]["lon"])
    return None


def _is_start(marks, i):
    """Is marks[i] the start? The course's first mark when it is named Start (course_to_marks
    names it so). The start is left by CROSSING it, never by being close to it: a boat milling
    near the start mark before the gun is close aboard but has not started (#246 follow-up).

    The race definition gives the start as ONE point (the committee boat stands in for the line;
    there are no pin/committee ends to build a segment from), so "crossed the line" is the plane
    through that point normal to the leg out to the first mark, PASS_NM beyond it, confirmed on
    PROGRESS_CONFIRM distinct fixes like every other advance."""
    return i == 0 and str(marks[0].get("name", "")).strip().lower() == "start"


def _passed(marks, i, lat, lon):
    """Has the boat passed marks[i]? Pure — no I/O and no stored state, so it is unit-testable
    and gives the same answer for a moment however you arrive at it. Close aboard counts as a
    rounding for every mark but the start, which only counts once it is crossed."""
    m = marks[i]
    if not _is_start(marks, i) and _hav_nm(lat, lon, m["lat"], m["lon"]) <= ROUND_NM:
        return True                                                   # close aboard: a rounding
    axis = _plane_axis(marks, i)
    if axis is None:
        return False                                                   # a one-mark course
    return _beyond_nm(lat, lon, m, axis) >= PASS_NM


def _short_of(marks, i, lat, lon):
    """Is the boat on the near side of marks[i]'s plane? What releases a hold (Undo, a pick, a
    Start). By the plane alone: close aboard but behind the line is short of it, so a Start tapped
    near the start line releases as soon as the boat is behind it."""
    axis = _plane_axis(marks, i)
    if axis is None:
        return True
    return _beyond_nm(lat, lon, marks[i], axis) < PASS_NM


def _next_index(marks, lat, lon, floor=0):
    """Index of the mark being sailed to. Advances from `floor` while marks lie behind us, and
    never past the last one — the finish stays the target once it becomes the target."""
    i = max(0, min(int(floor), len(marks) - 1))
    while i < len(marks) - 1 and _passed(marks, i, lat, lon):
        i += 1
    return i


def _course_fp(marks):
    """Stable fingerprint of the course geometry — a stored ratchet is void if the course
    changes, so loading new homework cannot leave the navigator latched onto an old leg.
    hashlib (not hash()) because str hashing is salted per process."""
    import hashlib
    body = "|".join(f"{m['seq']}:{m['lat']:.6f},{m['lon']:.6f}" for m in marks)
    return hashlib.sha1(body.encode()).hexdigest()[:16]


def _store():
    """The datasource when it keeps course progress (the onboard kv), else None: the replay rig
    (latch off) and the cloud (no kv) run on geometry alone, which is still correct."""
    if not PROGRESS_LATCH:
        return None
    try:
        ds = datasource.active()
    except Exception:
        return None
    if not (hasattr(ds, "get_nav_progress") and hasattr(ds, "save_nav_progress")):
        return None
    return ds


def _load_progress(ds, route, marks):
    """This run's progress for `route`: {fp, i, run, hist, hold}. A record for other course
    geometry (a new course under the same name) is void: a fresh run at the first mark."""
    fp = _course_fp(marks)
    rec = ds.get_nav_progress(route) or {}      # a read failure raises: never taken as "no run"
    if rec.get("fp") != fp:
        return {"fp": fp, "i": 0, "run": None, "hist": [], "hold": None}
    n = len(marks)
    clamp = lambda v: max(0, min(int(v), n - 1))          # noqa: E731
    hold = rec.get("hold")
    return {"fp": fp, "i": clamp(rec.get("i", 0)), "run": rec.get("run"),
            "hist": [clamp(v) for v in (rec.get("hist") or [])][-HIST_MAX:],
            "hold": None if hold is None else clamp(hold)}


def _save_progress(ds, route, rec):
    """True when the progress is on disk. A failed write (disk full, DB locked) is said out loud:
    the crew's controls report it rather than claiming a change the next poll won't show."""
    try:
        ds.save_nav_progress(route, rec)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[navigator] progress not saved for {route!r}: {type(exc).__name__}: {exc}", flush=True)
        return False


def _fix_key(s):
    """What makes two reads the SAME fix: its time when the source says, else its position."""
    t = s.get("fix_t")
    if t is not None:
        return ("t", round(float(t), 3))
    return ("p", round(s["lat"], 7), round(s["lon"], 7))


def _advance(route, marks, lat, lon, fix):
    """(index of the mark being sailed to, progress record or None). Geometry proposes, the
    stored run decides: an advance is kept once PROGRESS_CONFIRM distinct fixes agree on it.

    After Undo or a mark pick the chosen mark is HELD: geometry can't move past it until a fix
    shows the boat short of it again. Otherwise an Undo of an advance that geometry made (a course
    that doubles back) would be re-made on the next fix."""
    ds = _store()
    if ds is None:
        return _next_index(marks, lat, lon, 0), None
    with _PROGRESS_LOCK:
        try:
            # the marks again, now under the lock: a course load that replaced them since this
            # poll read them has (or will have) reset the run for the new marks, and this stale
            # snapshot must not write its own run over that one
            if _course_fp(_marks(route) or []) != _course_fp(marks):
                return _next_index(marks, lat, lon, 0), None
            rec = _load_progress(ds, route, marks)
        except Exception as exc:  # noqa: BLE001
            # can't read the run right now: answer from geometry, write nothing
            print(f"[navigator] progress unreadable for {route!r}: {type(exc).__name__}: {exc}",
                  flush=True)
            return _next_index(marks, lat, lon, 0), None
        floor = rec["i"]
        if rec["hold"] is not None:
            if rec["hold"] == floor and not _short_of(marks, floor, lat, lon):
                _pending.pop(route, None)
                return floor, rec                      # still held: wait for a fix short of it
            rec["hold"] = None                         # released: normal advance from here
            _save_progress(ds, route, rec)
        idx = _next_index(marks, lat, lon, floor)
        if idx == floor:
            _pending.pop(route, None)                  # a fix that doesn't advance resets the count
            return floor, rec
        p = _pending.get(route)
        if not p or p["i"] != idx or p.get("fp") != rec["fp"]:
            p = _pending[route] = {"i": idx, "fp": rec["fp"], "fixes": []}
        if fix not in p["fixes"]:
            p["fixes"].append(fix)
        if len(p["fixes"]) < PROGRESS_CONFIRM:
            return floor, rec                          # seen on one fix: not yet
        _pending.pop(route, None)
        new = dict(rec, hist=(rec["hist"] + [floor])[-HIST_MAX:], i=idx)
        if not _save_progress(ds, route, new):
            return floor, rec                          # not stored: answer what is stored
        return idx, new


def _set_progress(route, change):
    """Run `change(rec, marks) -> (ok, note)` on `route`'s progress under the lock and save it.
    Returns the result dict the crew's controls show."""
    from . import orinroute
    ds = _store()
    if ds is None:
        return {"ok": False, "no_store": True, "note": "Marks can't be changed here."}
    saved_failed = False
    # COURSE_LOCK first (the order everywhere): a course load can't swap the marks between this
    # read and the write, so a pick never lands on another course's run
    with orinroute.COURSE_LOCK, _PROGRESS_LOCK:
        route = route or active_route()
        marks = _marks(route)
        if not marks:
            return {"ok": False, "note": "No course loaded."}
        try:
            rec = _load_progress(ds, route, marks)
        except Exception as exc:  # noqa: BLE001
            print(f"[navigator] progress unreadable for {route!r}: {type(exc).__name__}: {exc}",
                  flush=True)
            return {"ok": False, "save_failed": True, "route": route,
                    "note": "Couldn't read the marks. Try again."}
        ok, note = change(rec, marks)
        if ok:
            _pending.pop(route, None)
            if not _save_progress(ds, route, rec):
                ok, note = False, "Couldn't save. Try again."
                saved_failed = True
    out = {"ok": ok, "route": route}
    if saved_failed:
        out["save_failed"] = True
    if note:
        out["note"] = note
    if ok:
        m = marks[rec["i"]]
        out.update({"index": rec["i"], "next_mark": m["name"], "can_undo": bool(rec["hist"])})
    return out


def start_run(route=None, keep_undo=True):
    """A new navigation run (#246): the next mark is the first one again. Called on a course load
    (keep_undo False: a new course has nothing to go back to) and on the crew's Start tap. The
    tap keeps the old position for Undo and HOLDS the start until the boat is seen behind the
    line: otherwise a stray tap mid-race would advance on its own over the next fixes, and one
    Undo would land on the start instead of the mark the boat was sailing to."""
    def change(rec, marks):
        rec["hist"] = (rec["hist"] + [rec["i"]])[-HIST_MAX:] if keep_undo else []
        rec.update({"i": 0, "hold": 0 if keep_undo else None, "run": int(time.time())})
        return True, None
    return _set_progress(route, change)


def undo_mark(route=None):
    """Undo the last mark change (an advance, a pick, or a Start): back to the mark before it,
    held there until the boat is seen short of it."""
    def change(rec, marks):
        if not rec["hist"]:
            return False, "Nothing to undo."
        rec["i"] = rec["hist"].pop()
        rec["hold"] = rec["i"]
        return True, None
    return _set_progress(route, change)


def pick_mark(index, route=None):
    """The crew sets the next mark by its place in the course: the right one of a repeated mark
    (lap 2's Windward, not lap 1's). Held there until the boat is seen short of it."""
    def change(rec, marks):
        try:
            k = int(index)
        except (TypeError, ValueError):
            return False, "Pick a mark."
        if not 0 <= k < len(marks):
            return False, "No such mark."
        if k != rec["i"]:
            rec["hist"] = (rec["hist"] + [rec["i"]])[-HIST_MAX:]
        rec["i"] = k
        rec["hold"] = k
        return True, None
    return _set_progress(route, change)


# --- live state -------------------------------------------------------------
_PATHS = {
    "lat": "navigation.position.latitude", "lon": "navigation.position.longitude",
    "cog": "navigation.courseOverGroundTrue", "heading": "navigation.headingTrue",
    "sog": "navigation.speedOverGround", "tws": "environment.wind.speedTrue",
    "twd": "environment.wind.directionTrue",
}


def _latest():
    """Freshest value (any source) for the navigation/wind paths we need, converted to degrees and
    knots. Also `ages` ({key: seconds old, or None when the source can't say}) and `fix_t` (the
    position's time), from the same priority pick as each value (`latest_sample`). The values are
    returned whatever their age; get_navigator judges them with `_fresh` (#45.1)."""
    src = datasource.active()
    sample = getattr(src, "latest_sample", None)
    out, at = {}, {}
    for key, path in _PATHS.items():
        if sample is None:
            out[key], at[key] = src.latest_value(path), None
            continue
        smp = sample(path)
        out[key] = smp[0] if smp else None
        at[key] = float(smp[1]) if smp and smp[0] is not None and smp[1] is not None else None
    # convert
    for k in ("cog", "heading", "twd"):
        if out[k] is not None:
            out[k] = math.degrees(out[k]) % 360
    if out["sog"] is not None:
        out["sog"] *= 1.943844
    if out["tws"] is not None:
        out["tws"] *= 1.943844
    now = time.time()
    out["ages"] = {k: (None if e is None else now - e) for k, e in at.items()}
    out["fix_t"] = (min(at["lat"], at["lon"])
                    if at.get("lat") is not None and at.get("lon") is not None else None)
    return out


def _fresh(s):
    """`s` with every reading older than its channel's dead limit set to None (a position counts
    as one reading: lat and lon go together). Readings whose age is unknown are kept."""
    ages = s.get("ages") or {}
    if (NAV_MAX_AGE_S is not None and NAV_MAX_AGE_S <= 0) or not ages:
        return s
    old = lambda k: ages.get(k) is not None and ages[k] > _max_age(k)     # noqa: E731
    out = dict(s)
    for k in ("cog", "heading", "sog", "tws", "twd"):
        if old(k):
            out[k] = None
    if old("lat") or old("lon"):
        out["lat"] = out["lon"] = None
        out["fix_age_s"] = max(ages.get("lat") or 0, ages.get("lon") or 0)
    return out


# --- ETA -------------------------------------------------------------------
# ETA used to be `distance / instantaneous VMC`: project this second's COG/SOG onto the bearing
# to the mark and divide. Two things are wrong with that over a distance leg. It assumes you can
# sail straight at the mark, which upwind you cannot — no amount of smoothing fixes an estimator
# that ignores tacking. And it is driven by a single sample, so it moves with every wave.
#
# Measured on the Jul 18 archive, 700 samples over 3 h, scoring each estimator by how stable the
# ARRIVAL TIME it predicts is (as the clock advances by d, a good ETA falls by d — so the
# predicted arrival should sit still, and its wander is the error; this needs no knowledge of the
# real arrival, which the archive never reaches):
#
#   instantaneous VMC (old)              arrival spread 74.34 h, median 15 s jump 78.7 min
#   closing rate over a 30 min window    arrival spread 11.68 h, median jump  2.1 min
#   polars w/ a beat/reach branch        arrival spread 33.58 h, worst jump  26.37 h
#   polars, best VMG, 20 min wind        arrival spread  6.41 h, median jump  0.4 min
#
# The branch version is the instructive failure: `if twa < beat` is a DISCONTINUITY, so however
# much the wind is smoothed, drifting across the close-hauled angle makes the answer leap hours.
# Maximising speed-made-good over sailable headings removes the branch entirely — when the mark
# is fetchable the maximum sits at the direct angle, and when it is inside the no-go zone the
# maximum slides to close-hauled on its own, continuously.
ETA_WIND_MIN = float(os.environ.get("NAV_ETA_WIND_MIN", "20"))   # wind-smoothing window


def _smoothed_wind(minutes=None):
    """(tws_kn, twd_deg) averaged over the window — circular mean for direction, so 359 deg and
    1 deg average to 0 rather than 180. (None, None) when there is not enough history."""
    mins = ETA_WIND_MIN if minutes is None else minutes
    try:
        src = datasource.active()
        tws = [v for _, v in src.series(_PATHS["tws"], mins) if v is not None]
        twd = [v for _, v in src.series(_PATHS["twd"], mins) if v is not None]
    except Exception:
        return None, None
    if not tws or not twd:
        return None, None
    sy = sum(math.sin(v) for v in twd)          # radians in the archive, raw SI
    sx = sum(math.cos(v) for v in twd)
    return (sum(tws) / len(tws)) * 1.943844, (math.degrees(math.atan2(sy, sx)) + 360) % 360


def _polar_curves(pts):
    """{tws: [(twa, target_stw), ...] sorted} from the flat polar table."""
    by = {}
    for t, a, stw in pts:
        if stw:
            by.setdefault(t, []).append((a, stw))
    for t in by:
        by[t].sort()
    return by


def _polar_speed(by, tws_kn, twa):
    """Target boat speed at (tws, twa), linearly interpolated in BOTH axes.

    The polar is a coarse, unevenly spaced grid — 14 angles per curve with gaps like
    90 -> 110 -> 135 deg, and wind speeds every 2-4 kn. Snapping to the nearest grid point puts
    the discontinuities straight back, which is the whole thing this estimator exists to avoid.
    Below the smallest charted angle the boat cannot sail at all, so that returns 0 rather than
    close-hauled speed — otherwise dead upwind looks fetchable and the ETA comes out optimistic."""
    speeds = sorted(by)
    if not speeds:
        return None

    def on_curve(t):
        row = by[t]
        if twa < row[0][0]:
            return 0.0                       # inside the no-go zone
        if twa >= row[-1][0]:
            return row[-1][1]
        for (a0, s0), (a1, s1) in zip(row, row[1:]):
            if a0 <= twa <= a1:
                return s0 + (s1 - s0) * ((twa - a0) / (a1 - a0) if a1 > a0 else 0.0)
        return row[-1][1]

    if tws_kn <= speeds[0]:
        # below the lightest curve: scaled toward 0 in no wind, as the routers read it
        # (shared/polar.py, #64) — never that curve's full speed in a drifter
        return on_curve(speeds[0]) * (max(0.0, tws_kn) / speeds[0] if speeds[0] > 0 else 1.0)
    if tws_kn >= speeds[-1]:
        return on_curve(speeds[-1])
    for t0, t1 in zip(speeds, speeds[1:]):
        if t0 <= tws_kn <= t1:
            f = (tws_kn - t0) / (t1 - t0) if t1 > t0 else 0.0
            return on_curve(t0) * (1 - f) + on_curve(t1) * f
    return on_curve(speeds[-1])


def _polar_vmg_to_mark(tws_kn, twa_to_mark):
    """Best speed made good toward a mark lying `twa_to_mark` off the wind, in kn.

    The boat must actually GET there, so a heading off the mark has to be paid back on the other
    board: the answer is the best mix of two headings either side of the mark (one heading when
    it points straight at it) — the polar's convex hull in the mark's direction. For a mark inside
    the no-go zone that is the upwind VMG / cos(angle to the mark), not one board's VMG, which
    read ~13-17% fast (#41). Tacking time is not modelled. Continuous through the close-hauled
    boundary by construction: no branch to jump across."""
    try:
        pts = datasource.active().polars_stw()          # [(tws, twa, target_stw)]
    except Exception:
        return None
    by = _polar_curves(pts)
    if not by:
        return None
    t = abs(_wrap180(twa_to_mark))
    left, right = [], []                    # (made good, across) for headings each side of the mark
    for h in range(-180, 181, 2):
        stw = _polar_speed(by, tws_kn, float(abs(h)))
        if not stw:
            continue
        r = math.radians(h - t)
        p, q = stw * math.cos(r), stw * math.sin(r)
        if p <= 0:
            continue
        if q <= 1e-9:
            left.append((p, q))
        if q >= -1e-9:
            right.append((p, q))
    best = 0.0
    for p1, q1 in left:
        for p2, q2 in right:
            d = q2 - q1
            # the mix whose resultant points at the mark (both on it: either one)
            v = max(p1, p2) if d <= 1e-9 else (q2 * p1 - q1 * p2) / d
            if v > best:
                best = v
    return best or None


def _best_angles(tws_kn):
    """Optimal upwind and downwind TWA (deg) from the polar at the nearest TWS."""
    if tws_kn is None:
        return 42.0, 150.0
    up, dn = datasource.active().best_angles(tws_kn)
    return (up if up is not None else 42.0), (dn if dn is not None else 150.0)


def _next_rounding(marks, nxt, twd, beat, run, twa_to_mark):
    """Geometry of the leg AFTER the next mark — the homework for the upcoming rounding.

    Pure function (no I/O) so it's unit-testable. Returns the exit course (bearing from the
    next mark to the one after it), the TWA the boat will sail once round, that leg's type
    (beat/reach/run), and the maneuver implied by the angle change — or None when the next mark
    is the finish (nothing to prep after it) or the wind is unknown (can't resolve a TWA).

    `twa_to_mark` is SIGNED (bearing to the mark minus TWD, -180..180) so the side of the wind is
    known: in and out on opposite sides with the turn through head to wind is a tack, through
    dead downwind a gybe (#183-2). Unsigned angles called 315 -> 045 with wind north "hold
    course"."""
    if twd is None:
        return None
    try:
        i = marks.index(nxt)
    except ValueError:
        return None
    if i + 1 >= len(marks):
        return None                       # next mark is the last (finish) — no leg after
    after = marks[i + 1]
    exit_course = _bearing(nxt["lat"], nxt["lon"], after["lat"], after["lon"])
    exit_twa = _adiff(exit_course, twd)   # 0..180, angle off the wind on the next leg
    exit_type = "beat" if exit_twa < beat else "run" if exit_twa > run else "reach"
    # Maneuver implied by how much the TWA opens (bear away) or closes (head up) at the mark,
    # and whether the wind changes side (tack or gybe).
    if twa_to_mark is None:
        maneuver = "round"
    else:
        s_in = _wrap180(twa_to_mark)
        s_out = _wrap180(exit_course - twd)
        a_in = abs(s_in)
        turn = ("bear away" if exit_twa > a_in + 12 else
                "head up" if exit_twa < a_in - 12 else None)
        if s_in * s_out < 0 and a_in + exit_twa < 180:          # through head to wind
            maneuver = {"head up": "head up and tack",
                        "bear away": "tack and bear away"}.get(turn, "tack")
        elif s_in * s_out < 0:                                   # through dead downwind
            maneuver = {"bear away": "bear away and gybe",
                        "head up": "gybe and head up"}.get(turn, "gybe")
        else:
            maneuver = turn or "hold course"
    return {
        "exit_mark": after["name"],
        "exit_course_deg": round(exit_course, 1),
        "exit_twa_deg": round(exit_twa, 1),
        "exit_leg_type": exit_type,
        "maneuver": maneuver,
    }


def _marks(route):
    return datasource.active().marks(route)


def _age_text(sec):
    sec = int(round(sec))
    return f"{sec} s" if sec < 120 else f"{sec // 60} min" if sec < 7200 else f"{sec // 3600} h"


def _current_tack(s, twd):
    """'starboard' / 'port' from the heading (else COG): wind over the starboard side is
    starboard tack. None when neither is known, or the boat is head to wind or dead downwind."""
    hdg = s.get("heading") if s.get("heading") is not None else s.get("cog")
    if hdg is None or twd is None:
        return None
    rel = _wrap180(twd - hdg)               # where the wind comes from, off the bow (+ = starboard)
    if abs(rel) < 5 or abs(rel) > 175:
        return None
    return "starboard" if rel > 0 else "port"


def _layline_text(layline, tack, leg_type, mark):
    """The call when the boat is ON a layline (#183-3). It used to say "Tack to lay" always, even
    to a boat already sailing the layline. On that tack: laying the mark. On the other tack:
    tack (gybe on a run) to lay it. Tack unknown: just where we are."""
    if tack is None:
        return f"On the {layline} layline to {mark}."
    if tack == layline:
        return f"On the {layline} layline. Laying {mark}."
    return f"On the {layline} layline. {'Tack' if leg_type == 'beat' else 'Gybe'} to lay {mark}."


def get_course(route: str = None):
    """The course marks + per-leg bearing/distance (for the plot). An explicit `route` also makes
    it the active route (the route picker); a plain read never writes — re-persisting the route it
    just read could undo a course load that switched routes in between (#82 review)."""
    if route:
        set_active(route)
    else:
        route = active_route()
    marks = _marks(route)
    legs = []
    for a, b in zip(marks, marks[1:]):
        legs.append({"from": a["name"], "to": b["name"],
                     "distance_nm": round(_hav_nm(a["lat"], a["lon"], b["lat"], b["lon"]), 2),
                     "bearing_deg": round(_bearing(a["lat"], a["lon"], b["lat"], b["lon"]), 1)})
    return {"available": bool(marks), "route": route, "marks": marks, "legs": legs}


def get_navigator(route: str = None):
    """Next mark, ETA, leg type, and laylines from live position + wind."""
    route = route or active_route()
    marks = _marks(route)
    if not marks:
        return {"available": False, "note": f"No course loaded for '{route}'."}
    s = _fresh(_latest())
    if s["lat"] is None or s["lon"] is None:
        if s.get("fix_age_s") is not None:
            return {"available": False, "position_age_s": round(s["fix_age_s"]),
                    "note": f"No fresh position (last fix {_age_text(s['fix_age_s'])} ago)."}
        return {"available": False, "note": "No position yet."}
    lat, lon = s["lat"], s["lon"]

    # next mark = the first one we have not passed (plane crossing), as far as this run has
    # confirmed it on distinct fixes, or where the crew put it
    idx, rec = _advance(route, marks, lat, lon, _fix_key(s))
    nxt = marks[idx]
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
            angle = beat
        elif twa_to_mark > run:                  # mark is below optimal run -> must gybe
            leg["type"] = "run"
            angle = run
        else:
            leg["type"] = "reach"
            angle = None
        if angle is not None:
            # Starboard tack has the wind over the starboard side: heading = TWD - TWA (as in
            # tactics, routing and windphrase). It was TWD + TWA, which named every tack wrong (#41).
            stbd = (twd - angle) % 360
            port = (twd + angle) % 360
            # layline bearings from the mark = reciprocal of the courses sailed toward it
            leg["laylines"] = {
                "starboard_course": round(stbd, 1), "port_course": round(port, 1),
                "starboard_from_mark": round((stbd + 180) % 360, 1),
                "port_from_mark": round((port + 180) % 360, 1),
            }
            ds, dp = _adiff(brg, stbd), _adiff(brg, port)
            near, name = (ds, "starboard") if ds < dp else (dp, "port")
            if near <= LAYLINE_TOL_DEG:
                layline_call = _layline_text(name, _current_tack(s, twd), leg["type"], nxt["name"])
            else:
                layline_call = f"{round(near)}° below the {name} layline to {nxt['name']}."

    # ETA on the boat's polars at the smoothed wind — see the note above _smoothed_wind.
    eta_min, eta_basis = None, None
    eta_tws, eta_twd = _smoothed_wind()
    if eta_tws and eta_twd is not None:
        vmg = _polar_vmg_to_mark(eta_tws, _adiff(brg, eta_twd))
        if vmg and vmg > 0.2:
            eta_min, eta_basis = round(dist / vmg * 60, 1), "polar"
    if eta_min is None and s["sog"] and s["cog"] is not None:
        # No polars or not enough wind history yet (a fresh archive, or the first minutes of a
        # session). Instantaneous made-good is poor, but it is better than a blank tile.
        vmc = s["sog"] * math.cos(math.radians(_adiff(s["cog"], brg)))
        if vmc > 0.2:
            eta_min, eta_basis = round(dist / vmc * 60, 1), "made-good"

    # The leg after the next mark — homework for the upcoming rounding (None at the finish).
    twa_to_mark = _wrap180(brg - twd) if twd is not None else None       # signed: side of the wind
    next_rounding = _next_rounding(marks, nxt, twd, beat, run, twa_to_mark)

    return {
        "available": True, "route": route,
        "position": {"lat": round(lat, 5), "lon": round(lon, 5)},
        "next_mark": {"name": nxt["name"], "seq": nxt["seq"],
                      # position in the ordered marks list — leg N of the course arrives at
                      # marks[N], so this doubles as the current 1-based leg number (matcher)
                      "index": idx,
                      "distance_nm": round(dist, 2), "bearing_deg": round(brg, 1),
                      # eta_basis says WHICH estimator answered — "polar" is the real one,
                      # "made-good" the degraded fallback, so a debrief can tell them apart
                      "eta_min": eta_min, "eta_basis": eta_basis},
        "leg": leg, "layline_call": layline_call, "next_rounding": next_rounding,
        "wind": {"twd": None if twd is None else round(twd, 1),
                 "tws": None if tws is None else round(tws, 1),
                 "beat_twa": round(beat, 1), "run_twa": round(run, 1)},
        "marks_total": len(marks),
        # the crew's Start / Undo / pick controls (#246): None where marks can't be changed
        "progress": None if rec is None else {"can_undo": bool(rec["hist"]),
                                              "run_started": rec.get("run"),
                                              "held": rec.get("hold") is not None},
        "remaining_nm": round(sum(
            _hav_nm(marks[i]["lat"], marks[i]["lon"], marks[i+1]["lat"], marks[i+1]["lon"])
            for i in range(idx, len(marks) - 1)) + dist, 1),
    }


def make_practice_course(leg_nm: float = 1.0):
    """Drop a windward/leeward practice course from the live position + wind, stored as route
    'practice'. Leeward mark at the boat, windward mark leg_nm straight upwind (toward TWD)."""
    s = _fresh(_latest())
    if s["lat"] is None or s["lon"] is None:
        return {"available": False, "note": "No position yet."}
    if s["twd"] is None:
        # it used to lay the windward mark due north (TWD 0) and report success (#183-4)
        return {"available": False, "note": "No wind direction yet."}
    twd = s["twd"]
    lat, lon = s["lat"], s["lon"]
    # offset leg_nm in bearing twd (upwind) — simple equirectangular step
    d_rad = (leg_nm / 60.0)  # nm -> deg latitude
    wlat = lat + d_rad * math.cos(math.radians(twd))
    wlon = lon + d_rad * math.sin(math.radians(twd)) / max(0.1, math.cos(math.radians(lat)))
    marks = [(1, "Leeward (start)", lat, lon), (2, "Windward", wlat, wlon),
             (3, "Leeward (finish)", lat, lon)]
    from . import orinroute
    with orinroute.COURSE_LOCK:          # one course change, like /course/load (#125)
        datasource.active().save_practice_course(marks)
        set_active("practice")
        _course_changed("practice")      # new marks under the same name: always a new course
        run = start_run("practice", keep_undo=False)     # a new course is a new run (#246)
    if run.get("save_failed"):
        return {"available": False, "note": "Couldn't reset the marks. Try again."}
    return {"available": True, "route": "practice", "leg_nm": leg_nm,
            "twd": round(twd, 1), "marks": len(marks)}
