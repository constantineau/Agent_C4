"""A boat's own sail names and its own name in text (#305, #306).

sail_vocab: each sail's family comes from the profile (`sail_families`) or the usual shorthand;
logic keys on the family, so a genoa, an A2 or an S2 works like sr33's J1, A3 and S1.
boat_words: text names the boat from its profile; sr33's wording is unchanged; no boat → "the boat".
Exits non-zero on any failure.
"""
import json
import os

from shared import boat_words, sail_vocab as SV

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


with open(os.path.join(ROOT, "vps", "lab", "boats", "sr33.json")) as f:
    SR33 = json.load(f)
with open(os.path.join(ROOT, "vps", "lab", "testdata", "boats", "bravo.json")) as f:
    BRAVO = json.load(f)

print("families")
check("sr33's sails by shorthand", [(s["name"], s["family"]) for s in SV.inventory(SR33)] == [
    ("J1", "jib"), ("J2", "jib"), ("J3", "jib"), ("C0", "code0"), ("SS", "staysail"),
    ("A3", "asym"), ("S1", "sym")])
other = {"sail_inventory": ["G1", "g2", "A2", "S2", "Heavy", "Blooper"],
         "sail_families": {"heavy": "jib"}}
check("another boat: genoas, A2, S2, a named jib",
      [SV.family(n, other) for n in SV.names(other)] == ["genoa", "genoa", "asym", "sym", "jib", None])
check("a name that says nothing is 'other' in the inventory",
      SV.inventory(other)[-1] == {"name": "BLOOPER", "family": "other"})
check("a bad family is ignored", SV.family("X1", {"sail_inventory": ["X1"],
                                                  "sail_families": {"X1": "spinnaker"}}) is None)

print("matching the crew's words to the boat's sails")
check("'a2' on a boat with an A2", SV.match("a2", other) == "A2")
check("'A3 asym' on sr33", SV.match("A3 asym", SR33) == "A3")
check("an A2 on sr33 (not aboard) → None", SV.match("A2", SR33) is None)
check("no inventory: the shorthand stands", SV.match("a2", {}) == "A2")
check("no inventory, no shorthand → None", SV.match("Blooper", {}) is None)
check("the guide's sails count too", SV.match("J1-A", {"sail_inventory": ["A2"]}, extra=["J1"]) == "J1")

print("which sail drives")
check("kite over Code 0 over jib over staysail",
      sorted(["SS", "J2", "C0", "S1", "A3"], key=SV.drive_rank) == ["A3", "S1", "C0", "J2", "SS"])
check("sr33's sails in words", SV.describe_list(["J1", "A3", "S1"])
      == "J1 jib, A3 asymmetric, S1 symmetric kite")

print("one resolver over several sources")
m = SV.merge({"sail_inventory": ["J1", "BIGRED"], "sail_families": {"BIGRED": "asym"}},
             {"sail_inventory": ["J1", "HEAVY"], "sail_families": {"HEAVY": "jib", "BIGRED": "sym"}})
check("names from the first source that lists them", SV.names(m) == ["J1", "BIGRED"])
check("families merge, the first source wins a clash",
      SV.family("BIGRED", m) == "asym" and SV.family("HEAVY", m) == "jib")
check("nothing to merge: empty", SV.merge(None, {}) == {"sail_inventory": [], "sail_families": {}})

print("the boat's name")
check("sr33 unchanged: the SR33 racing yacht 'C4'",
      boat_words.describe(SR33) == "the SR33 racing yacht 'C4'")
check("sr33 unchanged: the SR33's", boat_words.possessive(SR33) == "the SR33's")
check("sr33 short: SR33", boat_words.short(SR33) == "SR33")
check("a name only in the seed's style splits", boat_words.short({"name": 'SR33 "C4"'}) == "SR33")
check("Bravo by its own name", boat_words.describe(BRAVO) == "the C&C 35 Mk I racing yacht 'Bravo'"
      and "sr33" not in boat_words.describe(BRAVO).lower())
check("no boat: the boat", boat_words.describe(None) == "the boat"
      and boat_words.possessive({}) == "the boat's" and boat_words.short(None) == "")

check("match needs the whole name: J10 is not J1",
      SV.match("J10", {"sail_inventory": ["J1"]}) is None
      and SV.match("J1", {"sail_inventory": ["J1"]}) == "J1"
      and SV.match("A3-A", {"sail_inventory": ["A3"]}) == "A3"
      and SV.match("A3 asym", {"sail_inventory": ["A3"]}) == "A3")

print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
