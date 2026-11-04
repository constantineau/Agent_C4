"""Tactics: the favoured side, the phase and the sentence must all agree on which way the wind went (#159).

The slope over the window and the now-vs-mean shift can disagree at the end of an oscillation
(wind went right for 12 min, then swung back left in the last sample). Before #159 the favoured
side and the phase came from the slope ("right", "lifted") while the sentence came from now-vs-mean
("wind shifted left -> left side favored"), and the selector switched on the slope's side.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_tactics_side.py
"""
import math
import sys

from app import tactics

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def run(twds, tack="starboard"):
    """Stub one minute-spaced TWD series; the last sample is 'now'. Upwind on the given tack."""
    cur = twds[-1]
    hdg = (cur - 42) % 360 if tack == "starboard" else (cur + 42) % 360

    class Src:
        def series(self, path, minutes):
            return [(60.0 * i, math.radians(v % 360)) for i, v in enumerate(twds)]

    tactics.datasource.active = lambda: Src()
    tactics.NAV._latest = lambda: {"twd": cur % 360, "lat": 44.0, "lon": -79.0,
                                   "heading": hdg, "cog": hdg}
    tactics.NAV.get_navigator = lambda route=None: {"available": False}
    return tactics.get_tactics()


def side_in_text(text):
    for s in ("left", "right"):
        if f"{s} side" in text:
            return s
    return None


print("oscillation end — wind went right, then swung back left in the last minute:")
r = run([0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 4])
print("  ", r["favored_side"], "|", r["phase"], "|", r["recommendation"])
text_side = side_in_text(r["recommendation"])
check("favoured side matches the sentence",
      r["favored_side"] == (text_side or "either"))
check("not called a lasting shift when slope and now-vs-mean disagree",
      r["wind"]["persistent"] is False and r["favored_side"] == "either" and r["phase"] == "even")

print("a real lasting right shift still reads right everywhere:")
r = run([0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24])
print("  ", r["favored_side"], "|", r["phase"], "|", r["recommendation"])
check("persistent", r["wind"]["persistent"] is True)
check("favoured right", r["favored_side"] == "right")
check("sentence says right", side_in_text(r["recommendation"]) == "right")
check("lifted on starboard", r["phase"] == "lifted" and "LIFTED" in r["recommendation"])

print("a real lasting left shift reads left everywhere:")
r = run([30, 28, 26, 24, 22, 20, 18, 16, 14, 12, 10, 8, 6])
print("  ", r["favored_side"], "|", r["phase"], "|", r["recommendation"])
check("favoured left", r["favored_side"] == "left")
check("sentence says left", side_in_text(r["recommendation"]) == "left")
check("headed on starboard", r["phase"] == "headed" and "HEADED" in r["recommendation"])

print("mirror: wind went left, then swung back right in the last minute:")
r = run([30, 28, 26, 24, 22, 20, 18, 16, 14, 12, 10, 8, 26])
print("  ", r["favored_side"], "|", r["phase"], "|", r["recommendation"])
check("favoured side matches the sentence", r["favored_side"] == (side_in_text(r["recommendation"]) or "either"))
check("not a lasting shift", r["wind"]["persistent"] is False)

sys.exit(0 if ok else 1)
