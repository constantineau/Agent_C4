"""#67 — the fatigue speed deficit and the archive polar mining read the polar target interpolated
on the grid (`shared.polar.target`), not the nearest bucket by |dTWS kn| + |dTWA deg|.

Old behaviour on the committed SR33 cert: 11 kn / 90° read the 10 or 12 kn row (7.56 or 7.94)
instead of 7.75; 2 kn / 90° read the 4 kn row's full 4.66 instead of half of it. No DB: the
datasource and the polar_tool pool are stubbed. Exits non-zero on any failure.
"""
import math
import os
import re
import sys
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
SQL = os.path.join(HERE, "..", "db", "seed", "polars_sr33.sql")
PTS = [(float(t), float(a), float(s)) for t, a, s in re.findall(
    r"\(\s*'sr33'\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,", open(SQL).read())]

from app import datasource, fatigue, polar_tool   # noqa: E402


def _nearest(tws, twa):
    a = abs(((float(twa) + 180.0) % 360.0) - 180.0)
    t, w, s = min(PTS, key=lambda p: abs(p[0] - tws) + abs(p[1] - a))
    return {"tws": t, "twa": w, "target_stw": s, "target_vmg": None}


class _Src:
    def polars_stw(self):
        return PTS

    def polar_nearest(self, tws, twa):   # the old path, so old code fails on values, not attributes
        return _nearest(tws, twa)


datasource.active = lambda: _Src()

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


near = lambda a, b: a is not None and abs(a - b) < 1e-6

print("fatigue._target_stw (speed deficit → fatigue index)")
check("cert table parsed", len(PTS) == 126)
check("11 kn / 90° interpolates the rows = 7.75 (was 7.56 or 7.94)", near(fatigue._target_stw(11, 90), 7.75))
check("2 kn / 90° = half the 4 kn row (was the 4 kn row's 4.66)", near(fatigue._target_stw(2, 90), 4.66 / 2))
check("pinching 10 kn / 35° grades against the beat speed 6.52", near(fatigue._target_stw(10, 35), 6.52))
check("inside 30° → no target", fatigue._target_stw(10, 25) is None)
check("wrapped TWA 270° reads as 90°", near(fatigue._target_stw(11, 270), 7.75))


# --- polar_tool.mine with a fake pool: one telemetry query, one polar table query -------------
_KN = 1.943844
_SAMPLES = []          # rows the telemetry query returns: {stw, tws, twa} in SI


class _Res:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return None    # the old per-bin nearest query is not served: old code gets no target


class _Conn:
    def execute(self, sql, params=()):
        if "FROM polars" in sql:
            return _Res([{"tws": t, "twa": a, "target_stw": s} for t, a, s in PTS])
        return _Res(list(_SAMPLES))


class _Pool:
    @contextmanager
    def connection(self):
        yield _Conn()


polar_tool.pool = _Pool()
# 6 slices at 11 kn / 90°, STW 7.75 kn → exactly 100% of the interpolated target
_SAMPLES[:] = [{"stw": 7.75 / _KN, "tws": 11 / _KN, "twa": math.radians(90)}] * 6
polar_tool.TWS_BIN = 1.0           # keep the 11 kn centre (the ORC grid is 2 kn)
a = polar_tool.mine(hours=1, min_samples=6, percentile=90)
b = (a.get("observed_polar") or [{}])[0]

print("polar_tool.mine (observed vs rated, archive)")
check("one bin mined at 11 kn / 90°", a["available"] and b.get("tws_kn") == 11 and b.get("twa_deg") == 90)
check("target at the bin centre interpolated = 7.75 (was nearest bucket)", near(b.get("target_stw_kn"), 7.75))
check("7.75 kn sailed = 100% of polar", b.get("percent_of_polar") == 100)
check("bin inside 30° has no target", polar_tool._target_for(PTS, 10, 15) is None)
check("2 kn / 90° bin centre = half the 4 kn row", near(polar_tool._target_for(PTS, 2, 90), 4.66 / 2))

print("\nALL OK" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
