"""#113 — the sail call text and the console's sail tile must give the same call.

The tile reads the crew-set sail's fit (overpowered -> "change now -> J2"). The text came from the
angle check alone, which an overpowered J1 passes, so it said "Jib (J1) is right" under a tile
saying change to J2 (Jul 18 replay, 20:07:31Z, TWA 328°, 18 kn). Both now come from the fit.
Exits non-zero on any failure.
"""
import json
import os

from app import sails

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


BOAT = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", "lab", "boats", "sr33.json")))
lbl = lambda s: sails.SAIL_LABEL.get(s, s)

print("the replay frame: J1 up, 18 kn, port beat 32° (arrives as 328°)")
r = sails.get_sail_advice(18, 328, "J1", BOAT)
print("    fit", r["fit"], "| change_to", r["change_to"], "|", r["recommendation"])
check("tile verdict: J1 overpowered, change to J2", r["fit"] == "overpowered" and r["change_to"] == "J2")
check("text says change to J2", r["recommendation"].startswith("Change to J2."))
check("text does not call J1 right", "is right" not in r["recommendation"]
      and not r["recommendation"].startswith(lbl("J1")))
check("text says why, plainly", "Too much wind" in r["recommendation"])

print("J3 up in 10 kn: underpowered, change to J1")
r = sails.get_sail_advice(10, 35, "J3", BOAT)
check("fit underpowered -> J1", r["fit"] == "underpowered" and r["change_to"] == "J1")
check("text says change to the J1", r["recommendation"].startswith(f"Change to {lbl('J1')}."))

print("J2 up and in its range: the text names the J2, not 'J1 is right'")
r = sails.get_sail_advice(17, 35, "J2", BOAT)
check("fit in range", r["fit"] == "in_range")
check("text names J2", r["recommendation"].startswith("J2") and "Change" not in r["recommendation"])

print("sweep: text and tile agree on every crew sail, wind and angle")
bad = []
for hoisted in sails.CREW_SAILS:
    for tws in (6, 10, 14.5, 18, 24):
        for twa in (35, 60, 90, 120, 150, 210, 300, 328):
            r = sails.get_sail_advice(tws, twa, hoisted, BOAT)
            tile_change = r["fit"] not in (None, "in_range", "no_model")
            rec = r["recommendation"]
            if tile_change and not rec.startswith(f"Change to {lbl(r['change_to'])}."):
                bad.append((hoisted, tws, twa, r["fit"], r["change_to"], rec))
            if r["fit"] == "in_range" and rec.startswith("Change to"):
                bad.append((hoisted, tws, twa, r["fit"], r["change_to"], rec))
for b in bad[:8]:
    print("    disagree:", b)
check(f"no disagreements ({len(bad)} found)", not bad)

print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
