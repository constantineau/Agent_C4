"""#113 — the sail call text and the console's sail tile must give the same call.

The tile reads the crew-set sail's fit (overpowered -> "change now -> J2"). The text came from the
angle check alone, which an overpowered J1 passes, so it said "Jib (J1) is right" under a tile
saying change to J2 (Jul 18 replay, 20:07:31Z, TWA 328°, 18 kn). Both now come from the fit.
Exits non-zero on any failure.
"""
import json
import os

os.environ["BOAT_ID"] = "sr33"     # the shore agent's sail guide is its own boat's (#304)

from app import sails
from shared import sail_vocab

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


BOAT = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", "lab", "boats", "sr33.json")))
# The boat file's top-level shape is what the frozen bundle carries in boat_model (synthesis
# copies `code0` / `jib_crossovers` across as-is). Pin the Code 0 band so the C0 cases below don't
# drift with the crew's edits, and so the sweep exercises an ENABLED C0 (#227).
C0 = {"enabled": True, "tws_max": 9, "twa_min": 55, "twa_max": 110}
BOAT = dict(BOAT, code0=dict(C0))
lbl = lambda s: sails.label(s, BOAT)

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

print("C0 up, too much wind (12 kn, inside its angles): overpowered, change to the cert sail")
r = sails.get_sail_advice(12, 80, "C0", BOAT)
print("    fit", r["fit"], "| change_to", r["change_to"], "|", r["recommendation"])
check("fit overpowered", r["fit"] == "overpowered")
check("change_to is the cert sail", r["change_to"] and r["change_to"] == r["optimal_sail"])
check("text says change, and why", r["recommendation"]
      == f"Change to {lbl(r['change_to'])}. Too much wind for {lbl('C0')}.")

print("C0 up, wrong angle (7 kn, TWA 140, past its 110 limit): wrong angle")
r = sails.get_sail_advice(7, 140, "C0", BOAT)
print("    fit", r["fit"], "| change_to", r["change_to"], "|", r["recommendation"])
check("fit wrong_angle", r["fit"] == "wrong_angle")
check("change_to is the cert sail", r["change_to"] and r["change_to"] == r["optimal_sail"])
check("text says change, and why", r["recommendation"]
      == f"Change to {lbl(r['change_to'])}. {lbl('C0')} is wrong for this angle.")
r = sails.get_sail_advice(7, 40, "C0", BOAT)
check("too high (TWA 40, under its 55) is wrong angle too", r["fit"] == "wrong_angle")
r = sails.get_sail_advice(7, 360 - 80, "C0", BOAT)
check("port reach (arrives as 280) reads as 80, in range", r["fit"] == "in_range")

print("C0 up, within its band (7 kn, TWA 80): in range, text names the C0")
r = sails.get_sail_advice(7, 80, "C0", BOAT)
print("    fit", r["fit"], "| change_to", r["change_to"], "|", r["recommendation"])
check("fit in_range, no change", r["fit"] == "in_range" and r["change_to"] is None)
check("text names the Code 0, not the cert sail", r["recommendation"].startswith(f"{lbl('C0')} is right")
      and "Change" not in r["recommendation"])
r = sails.get_sail_advice(9, 55, "C0", BOAT)
check("limits are inclusive (9 kn, TWA 55)", r["fit"] == "in_range")

print("C0 without a usable band: logged only")
for name, c0 in (("disabled", dict(C0, enabled=False)), ("no band", {}),
                 ("band missing tws_max", {"enabled": True, "twa_min": 55, "twa_max": 110})):
    r = sails.get_sail_advice(12, 80, "C0", dict(BOAT, code0=c0))
    check(f"{name} -> no_model", r["fit"] == "no_model" and r["change_to"] is None)
r = sails.get_sail_advice(12, 80, "C0", dict(BOAT, code0={k: v for k, v in C0.items() if k != "enabled"}))
check("no 'enabled' flag counts as on (as the Lab does)", r["fit"] == "overpowered")

print("sweep: text and tile agree on every crew sail, wind and angle")
bad, fits = [], set()
for hoisted in sail_vocab.names(BOAT):     # sr33's own sails, from its profile (#305)
    for tws in (6, 9, 10, 14.5, 18, 24):
        for twa in (35, 55, 60, 90, 110, 120, 150, 210, 250, 300, 328):
            r = sails.get_sail_advice(tws, twa, hoisted, BOAT)
            tile_change = r["fit"] not in (None, "in_range", "no_model")
            rec = r["recommendation"]
            if tile_change and not rec.startswith(f"Change to {lbl(r['change_to'])}."):
                bad.append((hoisted, tws, twa, r["fit"], r["change_to"], rec))
            if r["fit"] == "in_range" and rec.startswith("Change to"):
                bad.append((hoisted, tws, twa, r["fit"], r["change_to"], rec))
            fits.add((hoisted, r["fit"]))
for b in bad[:8]:
    print("    disagree:", b)
check(f"no disagreements ({len(bad)} found)", not bad)
check("sweep covers an enabled C0 in every verdict",
      {("C0", f) for f in ("in_range", "overpowered", "wrong_angle")} <= fits)

print("no wind: no call, but the boat's sails still list (Codex on #334)")
r = sails.get_sail_advice(None, None, "J2", BOAT)
check("not available, says why", r["available"] is False and "wind" in r["note"])
check("the sails bar still gets the boat's sails",
      [x["name"] for x in r.get("sails") or []][:7] == sail_vocab.names(BOAT))
check("the crew's sail is still named", r.get("crew_sail") == "J2")

print("the driving sail reads the boat's family map (Codex on #334)")
from app import matcher  # noqa: E402
BIG = {"sail_inventory": ["J1", "BIGRED"], "sail_families": {"BIGRED": "asym"}}
check("BIGRED (asym by the map) drives over the J1", matcher._primary(["BIGRED", "J1"], BIG) == "BIGRED")
check("sr33's order unchanged", matcher._primary(["SS", "J2", "A3"], BOAT) == "A3"
      and matcher._primary(["J1", "C0"], BOAT) == "C0")
_orig = matcher._boat_sails
matcher._boat_sails = lambda: BIG
try:
    check("with no boat passed, the boat's own map is used", matcher._primary(["J1", "BIGRED"]) == "BIGRED")
finally:
    matcher._boat_sails = _orig

print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
