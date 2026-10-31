"""#47 — TWA arrives wrapped 0–360; the sail call and the polar lookups must read it folded.

The instruments publish a signed TWA but the record carries it wrapped to 0–360, so a port-tack
beat at 36° arrives as 324°. `sails.get_sail_advice` took abs() of that, read 324° as a run, and
called the J1 wrong — the red "change now" through the whole Jul 18 replay. The read path now
converts to signed [−180, 180) and every lookup folds. Exits non-zero on any failure.
"""
import math
import os
import sys

from shared import units
from app import datasource_onboard, onboard_conditions, sails, tools
from app.datasource_onboard import OnboardSource, _load_polars

datasource_onboard.POLARS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                              "..", "db", "seed", "polars_sr33.sql")

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


near = lambda a, b, tol=1e-6: a is not None and b is not None and abs(a - b) < tol

print("shared.units helpers")
check("324° → −36° signed", near(units.signed_twa(324), -36))
check("36° stays 36°", near(units.signed_twa(36), 36))
check("210° → −150° signed", near(units.signed_twa(210), -150))
check("fold 324° → 36°", near(units.fold_twa(324), 36))
check("fold −150° → 150°", near(units.fold_twa(-150), 150))
check("fold 180° → 180°", near(units.fold_twa(180), 180))
check("None passes through", units.signed_twa(None) is None and units.fold_twa(None) is None)

print("read path: angleTrueWater converts to SIGNED degrees (cloud + onboard tables)")
for name, table in (("tools.PRESENT", tools.PRESENT),
                    ("onboard_conditions.PRESENT", onboard_conditions.PRESENT)):
    conv = table["environment.wind.angleTrueWater"][2]
    check(f"{name}: 324° in radians → −36°", near(conv(math.radians(324)), -36, 1e-6))
    check(f"{name}: 36° in radians → 36°", near(conv(math.radians(36)), 36, 1e-6))
    check(f"{name}: 210° in radians → −150°", near(conv(math.radians(210)), -150, 1e-6))

print("sail call at 12 kn — each case as the record carries it (wrapped) and signed")
CASES = [
    # (label, twa as received, hoisted, expected sail, expected tack, folded twa)
    ("starboard beat 36°", 36, "J1", "J1", "stbd", 36),
    ("port beat, wrapped 324° (THE BUG)", 324, "J1", "J1", "port", 36),
    ("port beat, signed −36°", -36, "J1", "J1", "port", 36),
    ("starboard run 150°", 150, "S1", "S1", "stbd", 150),
    ("port run, wrapped 210°", 210, "S1", "S1", "port", 150),
    ("port run, signed −150°", -150, "S1", "S1", "port", 150),
]
for label, twa, hoisted, sail, tack, folded in CASES:
    r = sails.get_sail_advice(12, twa, hoisted)
    check(f"{label}: optimal {sail}", r.get("optimal_sail") == sail)
    check(f"{label}: tack {tack}", r.get("tack") == tack)
    check(f"{label}: |TWA| {folded}", near(r.get("twa_abs"), folded))
    check(f"{label}: {hoisted} up reads in range (no 'change now')", r.get("fit") == "in_range")
# the wrapped and signed reads of the same angle must be the SAME advice, not just the same sail
a, b = sails.get_sail_advice(12, 210, "S1"), sails.get_sail_advice(12, 150, "S1")
check("port run 210° gets the same recommendation as 150°",
      a["recommendation"] == b["recommendation"])

print("polar nearest bucket (onboard) folds the angle")
src = OnboardSource.__new__(OnboardSource)    # just the polar table — no archive, no live feed
src._polars = _load_polars()
check("onboard polar table loaded", len(src._polars) > 0)
n36, n324, nm36 = src.polar_nearest(12, 36), src.polar_nearest(12, 324), src.polar_nearest(12, -36)
check("12 kn / 324° reads the same bucket as 36°", n324 == n36)
check("12 kn / −36° reads the same bucket as 36°", nm36 == n36)
check("…and that bucket is a beat, not a run", n36 and n36["twa"] < 90)

print("source disagreement on TWA is the shortest arc (signed TWA at a dead run)")
check("179° vs −179° spread is 2°", near(units.channel_spread("twa", [179, -179]), 2))
check("heading 359° vs 1° spread is 2°", near(units.channel_spread("heading_true", [359, 1]), 2))
check("non-angle channel stays max − min", near(units.channel_spread("tws", [10, 12.5]), 2.5))

import time as _time                                   # noqa: E402
from app import datasource                            # noqa: E402


class _Readings:
    def latest_per_source(self, paths, max_age_minutes):
        now = _time.time()
        return [{"path": "environment.wind.angleTrueWater", "source": "a.1",
                 "value": math.radians(179), "epoch": now},
                {"path": "environment.wind.angleTrueWater", "source": "b.2",
                 "value": math.radians(181), "epoch": now}]


datasource.active = lambda: _Readings()
cc = onboard_conditions.get_current_conditions()
tw = cc["channels"]["twa"]
check("onboard: two sensors 2° apart at a dead run → spread 2°", near(tw["spread"], 2, 1e-3))
check("onboard: …and no disagreement flag", tw["disagreement"] is False)

print("\nALL OK" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
