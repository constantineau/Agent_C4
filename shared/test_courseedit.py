"""Course edits aboard and course revisions (shared/courseedit.py, #406).

Pins: a repeated mark is edited by its place, not its name; a changed rounding side; a
shortened finish (a mark, or a gate as a finish line), with the islands after it kept as rock
with no side; a stale edit (another mark at that place) is refused; the base is never changed;
`same_geography` holds for these edits and fails for a moved mark or a new zone; the revision
block is checked by race_def.validate.

Run:  PYTHONPATH=. python3 shared/test_courseedit.py
"""
import copy
import sys

from shared import courseedit as CE
from shared import race_def

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def raises(fn, text):
    try:
        fn()
    except CE.EditError as exc:
        return str(exc) == text
    return False


# a lapped course: Start → Top → Bottom → Top → Gate → Rock (island) → Finish
LAPS = {"race_id": "laps", "name": "Laps", "year": 2027, "zones": [], "courses": [{
    "id": "c", "start": {"lat": 45.0, "lon": -80.0},
    "marks": [{"name": "Top", "type": "buoy", "lat": 45.1, "lon": -80.0, "rounding": "port"},
              {"name": "Bottom", "type": "buoy", "lat": 45.0, "lon": -80.01, "rounding": "port"},
              {"name": "Top", "type": "buoy", "lat": 45.1, "lon": -80.0, "rounding": "port"},
              {"name": "Gate", "type": "gate", "lat": 45.05, "lon": -80.02, "lat2": 45.05,
               "lon2": -80.0, "rounding": "gate"},
              {"name": "Rock", "type": "island", "lat": 45.2, "lon": -80.1,
               "rounding": "starboard", "radius_nm": 0.3}],
    "finish": {"points": [{"lat": 45.3, "lon": -80.0}]}}]}
BASE = copy.deepcopy(LAPS)


def marks(d):
    return d["courses"][0]["marks"]


print("a repeated mark is the right occurrence")
lap2 = CE.apply_edits(LAPS, "c", [{"op": "rounding", "pos": 2, "name": "Top", "side": "starboard"}])
check("only lap 2's Top changes side", [m["rounding"] for m in marks(lap2)][:3]
      == ["port", "port", "starboard"])
check("the base is never changed", LAPS == BASE)
check("nav index of lap 2's Top is 3 (Start 0, Top 1, Bottom 2)", CE.nav_index_of(LAPS, "c", 2) == 3)
check("an edit naming another mark at that place is refused",
      raises(lambda: CE.apply_edits(LAPS, "c", [{"op": "rounding", "pos": 1, "name": "Top",
                                                  "side": "port"}]),
             "Course changed. Pick the mark again."))
check("no such place → refused", raises(lambda: CE.apply_edits(
    LAPS, "c", [{"op": "skip_to", "pos": 9, "name": "Top"}]), "No such mark."))

print("a changed rounding side")
isl = CE.apply_edits(LAPS, "c", [{"op": "rounding", "pos": 4, "name": "Rock", "side": "port"}])
check("an island's side changes", marks(isl)[4]["rounding"] == "port")
check("a gate has no side to change", raises(lambda: CE.apply_edits(
    LAPS, "c", [{"op": "rounding", "pos": 3, "name": "Gate", "side": "port"}]),
    "A gate is sailed through. No side to change."))
check("a side that isn't port/starboard/none → refused", raises(lambda: CE.apply_edits(
    LAPS, "c", [{"op": "rounding", "pos": 0, "name": "Top", "side": "left"}]),
    "Pick port, starboard or none."))
check("the flattened marks don't change", race_def.course_to_marks(isl, "c")[0]
      == race_def.course_to_marks(LAPS, "c")[0])

print("a shortened finish")
short = CE.apply_edits(LAPS, "c", [{"op": "finish_at", "pos": 1, "name": "Bottom"}])
flat = race_def.course_to_marks(short, "c")[0]
check("finish at Bottom: Start, Top, Finish", [n for _s, n, _a, _o in flat] == ["Start", "Top", "Finish"])
check("the finish is Bottom's position", (flat[-1][2], flat[-1][3]) == (45.0, -80.01))
check("the nav places before it don't move", flat[:2] == race_def.course_to_marks(LAPS, "c")[0][:2])
check("the island after it stays, with no side", [(m["name"], m["rounding"]) for m in marks(short)]
      == [("Top", "port"), ("Rock", "none")])
check("it says so", short["courses"][0]["finish"]["type"] == "shortened")
gate = CE.apply_edits(LAPS, "c", [{"op": "finish_at", "pos": 3, "name": "Gate"}])
check("finish at a gate → a two-point finish line",
      len(gate["courses"][0]["finish"]["points"]) == 2)
check("can't finish at an island", raises(lambda: CE.apply_edits(
    LAPS, "c", [{"op": "finish_at", "pos": 4, "name": "Rock"}]), "Can't finish at an island."))
check("a later edit past the new finish → no such mark", raises(lambda: CE.apply_edits(
    LAPS, "c", [{"op": "finish_at", "pos": 1, "name": "Bottom"},
                {"op": "rounding", "pos": 3, "name": "Gate", "side": "port"}]), "No such mark."))
check("the shortened definition still validates", not race_def.validate(dict(short, rules_profile={"x": 1}))[0])

print("skip to a mark")
skip = CE.apply_edits(LAPS, "c", [{"op": "skip_to", "pos": 2, "name": "Top"}])
check("the course itself is unchanged", skip == LAPS)
check("can't sail to an island", raises(lambda: CE.apply_edits(
    LAPS, "c", [{"op": "skip_to", "pos": 4, "name": "Rock"}]), "Can't sail to an island. Pick a mark."))

print("same water: the Lab's land chart still fits")
check("a side change", CE.same_geography(LAPS, isl, "c"))
check("a shortened finish", CE.same_geography(LAPS, short, "c"))
check("a gate finish line", CE.same_geography(LAPS, gate, "c"))
moved = copy.deepcopy(short)
moved["courses"][0]["marks"][0]["lat"] = 45.12
check("a moved mark → not the same water", not CE.same_geography(LAPS, moved, "c"))
zoned = dict(isl, zones=[{"name": "No-go", "type": "exclusion",
                          "geometry": {"north": 45.2, "south": 45.1, "west": -80.1, "east": -80.0}}])
check("a new zone → not the same water", not CE.same_geography(LAPS, zoned, "c"))
no_rock = copy.deepcopy(LAPS)
no_rock["courses"][0]["marks"].pop()
check("an island dropped → not the same", not CE.same_geography(LAPS, no_rock, "c"))

print("the revision block")
check("summary", CE.summary({"number": 3, "at": 10, "origin": "boat", "edits": []})
      == {"number": 3, "at": 10, "origin": "boat"})
check("no number → no summary", CE.summary({"origin": "lab"}) is None and CE.summary(None) is None)
good = dict(LAPS, rules_profile={"x": 1},
            course_revision={"number": 2, "at": 5, "origin": "boat", "base": 1,
                             "edits": [{"op": "finish_at", "pos": 1, "name": "Bottom"}]})
check("a good revision validates", not race_def.validate(good)[0])
for bad, why in (({"number": 0, "origin": "lab"}, "number 0"),
                 ({"number": 2, "origin": "shore"}, "unknown origin"),
                 ({"number": 2, "origin": "boat", "edits": [{"op": "move"}]}, "unknown edit")):
    check(f"a bad revision ({why}) is an error",
          any("course_revision" in e for e in race_def.validate(dict(good, course_revision=bad))[0]))
check("no revision stated → fine", not race_def.validate(dict(LAPS, rules_profile={"x": 1}))[0])
check("RaceDefinition has the field", "course_revision" in race_def.RaceDefinition.__dataclass_fields__)

print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
