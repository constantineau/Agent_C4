"""One chart identity for the Lab, the Pi and the Orin (#143 review).

The Lab stamps a carried land chart with a course fingerprint (orinassets → geo.course_fingerprint),
the Orin checks it (pi/orin/reroute/job.py: geo.course_fingerprint + boats.safety_depth_m), and
the Pi's GPS export checks it too (vps/agent/app/landchart.py). All of them must go through
shared/chartid.py, so they can't drift apart; a golden value pins the format itself.

Run (Lab image): python3 vps/lab/test_chartid.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

from shared import chartid  # noqa: E402
from app import boats  # noqa: E402
from app.geo import obstacles as geo  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


DEF = {"race_id": "r", "courses": [{"id": "c", "start": {"lat": 44.0, "lon": -82.0},
       "marks": [{"name": "A", "type": "buoy", "lat": 44.1, "lon": -82.0, "rounding": "port"},
                 {"name": "Rock", "type": "island", "lat": 44.15, "lon": -81.9,
                  "rounding": "starboard"}],
       "finish": {"points": [{"lat": 44.2, "lon": -82.0}]}}],
       "zones": [{"name": "Box", "type": "exclusion",
                  "geometry": {"north": 44.07, "south": 44.05, "west": -82.02, "east": -81.98}}]}
BOATS = [None, {}, {"draft_m": None}, {"draft_m": 2.13}, {"draft_m": 2.13, "safety_margin_m": 0.8},
         {"draft_m": 1.5, "safety_margin_m": 0}]

print("depth:")
for b in BOATS:
    check(f"Lab boats.safety_depth_m == shared for {b}",
          boats.safety_depth_m(b) == chartid.safety_depth_m(b))
check("the shared default is the Lab's (2.63)", chartid.safety_depth_m(None) == 2.63)

print("fingerprint:")
for b in BOATS:
    d = boats.safety_depth_m(b)
    check(f"Lab geo.course_fingerprint == shared (depth {d})",
          geo.course_fingerprint(DEF, "c", d) == chartid.course_fingerprint(DEF, "c", d))
check("depth None too", geo.course_fingerprint(DEF, "c", None) == chartid.course_fingerprint(DEF, "c", None))
moved = {**DEF, "zones": []}
check("a changed zone changes it", chartid.course_fingerprint(moved, "c", 2.63)
      != chartid.course_fingerprint(DEF, "c", 2.63))
GOLDEN = chartid.course_fingerprint(DEF, "c", 2.63)
print("   golden", GOLDEN)
check("golden value (the carried-chart format: change it only with every reader)",
      GOLDEN == "c802d289b333963e")

print("the Pi reads charts through the shared one:")
try:
    src = open(os.path.join(HERE, "..", "agent", "app", "landchart.py")).read()
    check("landchart.py uses shared.chartid for the fingerprint and the depth",
          "chartid.course_fingerprint(" in src and "chartid.safety_depth_m(" in src
          and "hashlib" not in src)
except OSError:
    check("landchart.py readable", False)
src = open(os.path.join(HERE, "..", "..", "pi", "orin", "reroute", "job.py")).read()
check("the Orin checks with geo.course_fingerprint + boats.safety_depth_m (both shared)",
      "geo.course_fingerprint(" in src and "boats.safety_depth_m(" in src)

print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
