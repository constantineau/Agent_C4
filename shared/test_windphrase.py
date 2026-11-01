"""shared/windphrase.py: the crew-facing wind words. One source for the engine, the coach and the
LoRA snapshots, so a wrong word here is wrong everywhere.

The rules under test (agreed with the skipper): never "veer"/"back", always "shifted right/left",
always the "from A° to B°" pair, boat frame first (LIFTED/HEADED on a tack), and the favoured side
flips between a beat and a run.

Run:  python3 shared/test_windphrase.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import windphrase as W  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


print("shift_sign / shift_word:")
check("clockwise is right", W.shift_sign(250, 265) == 1 and W.shift_word(1) == "right")
check("counter-clockwise is left", W.shift_sign(265, 250) == -1 and W.shift_word(-1) == "left")
check("under 1.5° is steady", W.shift_sign(250, 251.4) == 0 and W.shift_word(0) == "steady")
check("across north: 355 -> 10 is a right shift", W.shift_sign(355, 10) == 1)
check("across north: 10 -> 355 is a left shift", W.shift_sign(10, 355) == -1)

print("point_of_sail:")
check("beat / windward are upwind", W.point_of_sail("Beat to Cove") == "upwind"
      and W.point_of_sail("windward leg") == "upwind")
check("run / leeward are downwind", W.point_of_sail("run") == "downwind"
      and W.point_of_sail("Leeward") == "downwind")
check("reach is reach", W.point_of_sail("close reach") == "reach")
check("unknown or missing leg defaults to upwind", W.point_of_sail(None) == "upwind"
      and W.point_of_sail("leg 3") == "upwind")

print("favored_side (point-of-sail aware):")
check("right shift on a beat favours the right", W.favored_side(1, "upwind") == "right")
check("right shift on a run favours the left", W.favored_side(1, "downwind") == "left")
check("left shift on a run favours the right", W.favored_side(-1, "downwind") == "right")
check("a reach has no favoured side", W.favored_side(1, "reach") is None)
check("steady has no favoured side", W.favored_side(0, "upwind") is None)

print("phase_on_tack (geometric, same up and down wind):")
check("right shift lifts starboard", W.phase_on_tack(1, "starboard") == "lifted")
check("right shift heads port", W.phase_on_tack(1, "port") == "headed")
check("left shift lifts port, downwind too", W.phase_on_tack(-1, "port", "downwind") == "lifted")
check("unknown tack is even", W.phase_on_tack(1, None) == "even" and W.phase_on_tack(1, "x") == "even")
check("steady is even", W.phase_on_tack(0, "port") == "even")

print("nominal_heading:")
check("starboard upwind = TWD - 42", W.nominal_heading(250, "starboard", "upwind") == 208)
check("port upwind = TWD + 42, wrapped", W.nominal_heading(340, "port", "upwind") == 22)
check("starboard downwind = TWD - 150", W.nominal_heading(250, "starboard", "downwind") == 100)

print("describe_shift:")
s = W.describe_shift(250, 265, tack="starboard", leg="beat")
print(f"      {s!r}")
check("boat frame first: LIFTED on starboard ~15°", s.startswith("LIFTED on starboard ~15° and holding"))
check("says 'wind shifted right, from 250° to 265°'", "wind shifted right, from 250° to 265°" in s)
check("right side of the beat favoured", s.endswith("right side of the beat favored"))
s = W.describe_shift(265, 250, leg="run")
check("no tack: no boat-frame lead", s.startswith("wind shifted left, from 265° to 250°"))
check("left shift on a run favours the right of the run", s.endswith("right side of the run favored"))
s = W.describe_shift(250, 265, pos="reach", tack="port")
check("reach: no favoured side", "favored" not in s and "HEADED on port" in s)
check("across north the degrees wrap", "from 355° to 10°" in W.describe_shift(355, 10))
check("steady wind", W.describe_shift(250, 251) == "wind steady near 251° — no persistent shift")
check("oscillating: half the swing, around the current direction",
      W.describe_shift(250, 260, persistent=False, oscillation_deg=10).startswith("oscillating ±5° around 260°"))
check("not persistent, no swing: steady", "steady near 260°" in W.describe_shift(250, 260, persistent=False))

print("describe_drift:")
d = W.describe_drift(250, 270, tws_change_kn=2.5, leg="beat")
print(f"      {d!r}")
check("says was/now and the size", "moved right — was 250°, now 270° (~20°)" in d)
check("building wind with a sign", "building +2.5 kn" in d)
check("favours the right on a beat", d.endswith("would favour the right"))
check("easing wind", "easing -1.0 kn" in W.describe_drift(250, 250, tws_change_kn=-1.0))
check("holding forecast", W.describe_drift(250, 251).startswith("forecast holding near 251°"))

print("the vocabulary rule:")
texts = [W.describe_shift(a, b, tack=t, pos=p) for a, b in ((250, 265), (265, 250), (355, 10))
         for t in ("port", "starboard", None) for p in ("upwind", "downwind", "reach")]
texts += [W.describe_drift(a, b, tws_change_kn=k) for a, b in ((250, 270), (270, 250)) for k in (1, -1)]
check("never 'veer' or 'back' in any phrase",
      not any(w in t.lower() for t in texts for w in ("veer", "backed", "backing", " back ")))

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
