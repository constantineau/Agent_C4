"""Navigator ETA — the polar best-VMG estimator.

Time to Mark was rated 0/10 after Bayview Mackinac 2026. Two independent defects: the mark
sequencer never advanced (fixed in d5de8d1, covered by test_navigator_progress.py), and the ETA
itself was `distance / instantaneous VMC` — this file covers the second.

That estimator assumed the boat could sail straight at the mark, which upwind it cannot, and
took its speed from a single sample. Replayed over the real race it predicted an arrival time
that wandered 74 h, jumping a median of 79 minutes between consecutive 15 s polls.

What matters here is CONTINUITY. The instructive failure while fixing this was an estimator that
branched on `if twa < beat`: however much the wind was smoothed, drifting across the close-hauled
angle made the answer leap hours. Maximising VMG over sailable headings has no branch to jump
across, and interpolating the polar (a coarse, unevenly spaced grid) keeps the same property in
the wind axis. These tests exist mainly to stop either discontinuity being reintroduced.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_navigator_eta.py
"""
from app import navigator as NAV

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# A miniature polar with the same awkward shape as the real one: a no-go zone below 40 deg,
# unevenly spaced angles, and only a few wind speeds.
PTS = []
for tws, rows in ((6.0, [(40, 3.4), (52, 4.3), (90, 5.2), (135, 4.6), (180, 3.1)]),
                  (12.0, [(40, 5.6), (52, 6.8), (90, 7.9), (135, 7.2), (180, 5.4)]),
                  (20.0, [(40, 6.4), (52, 7.6), (90, 8.8), (135, 8.4), (180, 6.6)])):
    for twa, stw in rows:
        PTS.append((tws, float(twa), stw))

BY = NAV._polar_curves(PTS)


class _Src:
    def polars_stw(self):
        return PTS


NAV.datasource.active = lambda: _Src()

# --- the polar interpolator -------------------------------------------------
print("_polar_speed:")
check("on a grid point", abs(NAV._polar_speed(BY, 12.0, 90.0) - 7.9) < 1e-9)
check("interpolates between angles", 6.8 < NAV._polar_speed(BY, 12.0, 71.0) < 7.9)
check("interpolates between wind speeds", 5.2 < NAV._polar_speed(BY, 16.0, 90.0) < 8.8)
check("no-go zone below the smallest angle is ZERO, not close-hauled speed",
      NAV._polar_speed(BY, 12.0, 20.0) == 0.0)
check("below the lightest curve clamps, does not extrapolate to nothing",
      NAV._polar_speed(BY, 2.0, 90.0) == 5.2)
check("above the strongest curve clamps", NAV._polar_speed(BY, 40.0, 90.0) == 8.8)

# continuity in the wind axis: no step bigger than a small epsilon across the grid boundaries
print("continuity (the property the old estimator lacked):")
worst, at = 0.0, None
prev = None
for i in range(40, 241):                       # 4.0 -> 24.0 kn in 0.1 kn steps
    t = i / 10.0
    v = NAV._polar_speed(BY, t, 75.0)
    if prev is not None and abs(v - prev) > worst:
        worst, at = abs(v - prev), t
    prev = v
check(f"speed is continuous in TWS (max 0.1 kn step {worst:.4f} at {at})", worst < 0.05)

worst, at, prev = 0.0, None, None
for a in range(0, 1801):                       # 0 -> 180 deg in 0.1 deg steps
    v = NAV._polar_speed(BY, 12.0, a / 10.0)
    if prev is not None and abs(v - prev) > worst:
        worst, at = abs(v - prev), a / 10.0
    prev = v
# the no-go edge is a genuine cliff (0 -> 5.6 kn); everything else must be smooth
check(f"speed is continuous in TWA except at the no-go edge (worst {worst:.3f} at {at})",
      at is not None and abs(at - 40.0) < 0.3)

# --- best VMG toward the mark ----------------------------------------------
print("_polar_vmg_to_mark:")
beam = NAV._polar_vmg_to_mark(12.0, 90.0)
check("beam reach makes good roughly boat speed", 7.0 < beam < 8.0)
up = NAV._polar_vmg_to_mark(12.0, 0.0)
check("dead upwind still makes progress (tacking), but slower than a reach", 0 < up < beam)
check("dead upwind VMG is close-hauled speed x cos(beat angle)",
      abs(up - 5.6 * 0.766) < 0.35)
check("sign of the angle does not matter", NAV._polar_vmg_to_mark(12.0, -60.0)
      == NAV._polar_vmg_to_mark(12.0, 60.0))
check("more wind makes good more speed",
      NAV._polar_vmg_to_mark(20.0, 90.0) > NAV._polar_vmg_to_mark(6.0, 90.0))

print("continuity of VMG across the close-hauled boundary:")
worst, at, prev = 0.0, None, None
for a in range(0, 901):                        # 0 -> 90 deg to the mark, 0.1 deg steps
    v = NAV._polar_vmg_to_mark(12.0, a / 10.0)
    if prev is not None and abs(v - prev) > worst:
        worst, at = abs(v - prev), a / 10.0
    prev = v
check(f"VMG has no cliff as the mark crosses close-hauled (worst step {worst:.4f} kn at {at})",
      worst < 0.05)

check("empty polar yields no estimate", NAV._polar_vmg_to_mark(12.0, 90.0) is not None)


class _Empty:
    def polars_stw(self):
        return []


NAV.datasource.active = lambda: _Empty()
check("no polar aboard -> None (caller falls back to made-good)",
      NAV._polar_vmg_to_mark(12.0, 90.0) is None)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
