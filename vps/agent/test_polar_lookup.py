"""#30 — the onboard engine's polar lookups interpolate on the grid instead of adding knots to degrees.

`routing._polar_speed` (the onboard isochrone) and `matcher._target_stw` (live polar %) used the same
|dTWS kn| + |dTWA deg| "nearest cell" as the Lab optimizer: 2 kn at 30° read the 10 kn beat speed. Both
now read `shared.polar` on the committed SR33 cert. Exits non-zero on any failure.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SQL = os.path.join(HERE, "..", "db", "seed", "polars_sr33.sql")
PTS = [(float(t), float(a), float(s)) for t, a, s in re.findall(
    r"\(\s*'sr33'\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,", open(SQL).read())]

from app import datasource, matcher, routing   # noqa: E402


class _Src:
    def polars_stw(self):
        return PTS


datasource.active = lambda: _Src()
routing._POLARS = None
matcher._POLAR_TABLE = None

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


near = lambda a, b: a is not None and abs(a - b) < 1e-6
sp = routing._polar_speed
print("routing._polar_speed (onboard isochrone)")
check("cert table parsed", len(PTS) == 126)
check("10 kn / 37.6° beat = 6.52", near(sp(10, 37.6), 6.52))
check("THE BUG: 2 kn / 30° = 0 (was the 10 kn row's 6.52)", sp(2, 30) == 0.0)
check("10 kn / 35° (inside the 37.6° beat angle) = 0 (was beat speed)", sp(10, 35) == 0.0)
check("10 kn / 45° interpolates 37.6°→52°", near(sp(10, 45), 6.52 + 0.70 * 7.4 / 14.4))
check("11 kn / 90° interpolates the rows", near(sp(11, 90), (7.56 + 7.94) / 2))
check("2 kn / 90° = half the 4 kn row (scaled, not clamped)", near(sp(2, 90), 4.66 / 2))

print("matcher._target_stw (live polar %)")
pol = matcher._polar_table()
check("pinching at 10 kn / 35° grades against the beat speed (6.52), not 0", near(matcher._target_stw(pol, 10, 35), 6.52))
check("inside 30° → no target", matcher._target_stw(pol, 10, 25) is None)
check("11 kn / 90° interpolated (was the nearest bucket)", near(matcher._target_stw(pol, 11, 90), 7.75))
check("negative TWA (port) reads the same as starboard", near(matcher._target_stw(pol, 11, -90), 7.75))

print("\nALL OK" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
