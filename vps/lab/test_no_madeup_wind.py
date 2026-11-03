"""#203: the Lab/Orin router never sails a made-up 12 kn northerly (the Lab side of #156).

Where no forecast reaches a point, `route_leg` used to route on a constant 12 kn from 000°, with
no warning unless that was over 40% of the route. Now, as on the boat (Cole, 2026-11-02):
  1) a point the forecast misses takes the wind of the nearest covered point within 10 nm, and
     the route says "Forecast gaps filled from nearby.";
  2) farther than that there is no wind: the router doesn't sail there. A route that needs it
     comes back INCOMPLETE and degraded, never finished on invented wind.

No network: the wind frames are built in memory. The westerly makes a northbound course a beam
reach (no tacks); the made-up northerly would make it a dead beat (tacks).
"""
import os
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
for _seed in (os.path.join(HERE, "..", "db", "seed"), "/srv"):
    if os.path.exists(os.path.join(_seed, "polars_sr33.sql")):
        os.environ["POLARS_FILE"] = os.path.join(_seed, "polars_sr33.sql")
        os.environ["SAIL_POLARS_FILE"] = os.path.join(_seed, "sr33_sail_polars.json")
        os.environ["CROSSOVERS_FILE"] = os.path.join(_seed, "sr33_crossovers.json")
        break
os.environ["INGESTED_DIR"] = tempfile.mkdtemp()

from app import optimizer as OPT, polars as POL  # noqa: E402
from app.wind import grib  # noqa: E402
from app.wind.windfield import WindField  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1_800_000_000.0          # a fixed epoch: nothing here reads the clock
H = 3600.0
LAT = np.arange(43.0, 45.01, 0.25)
LON = np.arange(-83.5, -81.99, 0.25)          # the grid stops at 82.0 W


def field(lon=LON):
    frames = [grib.GribFrame("gfs", "det", T0 + k * H, LAT, lon, np.full((LAT.size, lon.size), 6.0),
                             np.full((LAT.size, lon.size), 0.0), True) for k in range(25)]
    return WindField({("gfs", "det"): frames}, [{"model": "gfs"}], (45, 43, -83.5, -82.0),
                     T0, T0 + 24 * H)


WF = field()
GAPS = getattr(OPT, "GAPS_FILLED", "Forecast gaps filled from nearby.")


def course(lon):
    return {"race_id": "n", "name": "N", "zones": [], "courses": [{
        "id": "c", "start": {"lat": 43.5, "lon": lon}, "marks": [],
        "finish": {"points": [{"lat": 44.3, "lon": lon}]}}]}


def route(lon, wf=WF):
    return OPT.optimize_course(course(lon), "c", T0, wf, avoid=False, emit_exploration=False,
                               time_budget_s=60, resolution="fast")


print("(control) inside the forecast: a westerly beam reach north, no tacks")
r0 = route(-82.5)
print(f"     complete={r0.get('complete')} tacks={r0.get('total_tacks')} warnings={r0.get('warnings')}")
check("complete, no tacks, no gap warning", r0["available"] and r0["complete"]
      and r0["total_tacks"] == 0 and GAPS not in r0["warnings"])

print("1) a course ~3 nm east of the grid: filled from the nearest covered point")
check("(control) the forecast misses that line", WF.wind_at(43.9, -81.94, T0) is None)
r1 = route(-81.94)
check(f"complete ({r1.get('complete')})", r1["available"] and r1["complete"])
check(f"sailed in the westerly, not a made-up northerly: {r1.get('total_tacks')} tacks",
      r1.get("total_tacks") == 0)
check("…and says so", GAPS in r1.get("warnings", []))

print("2) a course ~30 nm east of the grid: no wind there, no finished route")
r2 = route(-81.3)
print(f"     complete={r2.get('complete')} degraded={r2.get('degraded')} "
      f"tacks={r2.get('total_tacks')} warnings={r2.get('warnings')}")
check("not finished on invented wind", not (r2.get("available") and r2.get("complete")))
check("degraded or refused", r2.get("degraded") or not r2.get("available"))

print("3) route_leg alone: no fallback wind at all")
leg = OPT.route_leg(WF, POL.polars_stw(), 43.5, -81.3, T0, 44.3, -81.3)
check(f"a leg with no wind anywhere near never reaches its mark ({leg.get('reached')})",
      leg.get("reached") is False)

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
