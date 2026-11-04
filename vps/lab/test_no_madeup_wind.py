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

print("4) borrowed wind keeps its forecast health (Codex on #220)")
# the forecast ends 1 h after the gun, so the route sails on the held last frame; on a course
# ~3 nm east of the grid every node borrows that held frame, and the route must still say so
SHORT = WindField({("gfs", "det"): [grib.GribFrame(
    "gfs", "det", T0 + k * H, LAT, LON, np.full((LAT.size, LON.size), 6.0),
    np.full((LAT.size, LON.size), 0.0), True) for k in range(2)]},
    [{"model": "gfs"}], (45, 43, -83.5, -82.0), T0, T0 + H)
check("(control) inside the grid: says the forecast ends",
      OPT.FORECAST_ENDS in route(-82.5, SHORT).get("warnings", []))
r4 = route(-81.94, SHORT)
print(f"     complete={r4.get('complete')} degraded={r4.get('degraded')} warnings={r4.get('warnings')}")
check("outside the grid, on borrowed wind: still says the forecast ends",
      OPT.FORECAST_ENDS in r4.get("warnings", []) and r4.get("degraded"))
check("…the held-edge check sees the borrowed frame",
      OPT._forecast_held(SHORT, [{"lat": 43.9, "lon": -81.94, "t": T0 + 5 * H}]) == "end")

print("5) a forecast just inside the fill limit is found, not missed between rays (Codex on #220)")
# the forecast starts NE of the boat: its nearest corner is ~9.4 nm off, inside the 10 nm limit,
# but between the 8 search rays (every 45°, every 2.5 nm), which all miss it
CLAT = np.linspace(44.15, 45.0, 18)
CLON = np.linspace(-81.94, -81.0, 19)
CORNER = WindField({("gfs", "det"): [grib.GribFrame(
    "gfs", "det", T0 + k * H, CLAT, CLON, np.full((CLAT.size, CLON.size), 6.0),
    np.full((CLAT.size, CLON.size), 0.0), True) for k in range(25)]},
    [{"model": "gfs"}], (45, 44.15, -81.94, -81.0), T0, T0 + 24 * H)
check("(control) no forecast at the boat", CORNER.wind_at(44.0, -82.0, T0) is None)
check(f"(control) the corner is {OPT._hav_nm(44.0, -82.0, 44.15, -81.94):.2f} nm off",
      OPT._hav_nm(44.0, -82.0, 44.15, -81.94) < OPT.FILL_NM)
w5 = OPT._nearby_wind(CORNER, 44.0, -82.0, T0)
check(f"the boat borrows the corner's wind ({w5})", w5 is not None and abs(w5[0] - 11.7) < 0.5)
leg5 = OPT.route_leg(CORNER, POL.polars_stw(), 44.0, -82.0, T0, 44.6, -81.5)
check(f"a leg starting there reaches its mark ({leg5.get('reached')})", leg5.get("reached"))

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
