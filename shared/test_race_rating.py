"""shared/race_def.py rating fields (#311): which certificate number a race scores on, and which
national authorities to search. Read by the Lab fleet import and the in-race fleet model.

Run:  python3 shared/test_race_rating.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from shared import race_def as R  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def race(**sc):
    return {"race_id": "r", "name": "r", "year": 2027, "rules_profile": {"scoring": sc},
            "courses": [{"id": "long"}, {"id": "short"}],
            "divisions": [{"id": "A", "course_ref": "long"}, {"id": "B", "course_ref": "short"}]}


PER = [{"course": "long", "column": "XX_A_TOT", "column_tod": "XX_A_TOD"},
       {"course": "short", "column": "XX_B_TOT", "column_tod": ""}]

print("rating_system:")
check("explicit field wins", R.rating_system({"rating_system": "phrf", "system": "ORC"}) == "PHRF")
check("one-design spelled loosely", R.rating_system({"rating_system": "one design"}) == "ONE_DESIGN")
check("system text naming one", R.rating_system({"system": "ORC"}) == "ORC")
check("mixed text is not guessed",
      R.rating_system({"method": "Time on Distance for PHRF; Time on Time for ORC"}) == "")
check("nothing → ''", R.rating_system({}) == "")

print("rating_spec:")
s = R.rating_spec(race(rating_system="ORC", course_columns=PER), "short")
check("per-course column for the course asked", s["column"] == "XX_B_TOT" and s["column_tod"] is None)
d = dict(race(rating_system="ORC", course_columns=PER), own={"division": "A"})
s = R.rating_spec(d)
check("no course → our division's course", s["column"] == "XX_A_TOT" and s["column_tod"] == "XX_A_TOD")
check("no course, no division, two columns → unknown",
      R.rating_spec(race(rating_system="ORC", course_columns=PER))["column"] is None)
check("race-wide column", R.rating_spec(race(rating_column="TMF_Inshore"), "long")["column"] == "TMF_Inshore")
check("nothing named → None (never a default)", R.rating_spec(race(system="ORC"))["column"] is None)

print("rating_countries:")
check("scoring list wins", R.rating_countries(dict(race(rating_countries=["gbr"]), venue_country="FRA"),
                                              [{"sail": "IRL 1"}]) == ["GBR"])
check("venue then sail prefixes by count",
      R.rating_countries(dict(race(), venue_country="gbr"),
                         [{"sail": "FRA 1"}, {"sail": "irl-2"}, {"sail": "IRL 3"}, {"sail": "GBR 4"},
                          {"sail": "12345"}, {"sail": "US60"}]) == ["GBR", "IRL", "FRA"])
check("nothing known → []", R.rating_countries(race(), [{"sail": "123"}]) == [])
check("saved fleet used when no entries given",
      R.rating_countries(dict(race(), fleet=[{"sail": "NED 9"}])) == ["NED"])

print("validate (race review):")
_e, w = R.validate(race(rating_system="ORC"))
check("ORC with no column is flagged", "Rating column unknown: pick one (scoring)" in w)
_e, w = R.validate(race(rating_system="ORC", course_columns=PER))
check("per-course columns satisfy it", not any("Rating column" in x for x in w))
_e, w = R.validate(dict(race(), fleet=[{"boat": "x"}]))
check("a fleet with no system is flagged", "Rating system unknown: set it in scoring" in w)
_e, w = R.validate(race(rating_system="XYZ"))
check("unknown system is flagged", any("'XYZ' unknown" in x for x in w))

print("clear_stale_ratings (on save):")
d = race(rating_system="ORC", course_columns=PER)
d["fleet"] = [
    {"boat": "OnA", "division": "A", "rating": 1.1, "rating_col": "XX_A_TOT",
     "tod_allowance": 600.0, "tod_col": "XX_A_TOD"},                      # matches course long
    {"boat": "MovedToB", "division": "B", "rating": 1.1, "rating_col": "XX_A_TOT"},   # B is short
    {"boat": "Typed", "division": "B", "rating": 0.9},                     # no tag: hand-typed
    {"boat": "TodOnly", "division": "B", "tod_allowance": 650.0, "tod_col": "XX_OLD"},
]
d["own"] = {"boat": "Us", "division": "B", "rating": 1.0, "rating_col": "XX_A_TOT"}
hit = R.clear_stale_ratings(d)
f = {e["boat"]: e for e in d["fleet"]}
check("a matching import is kept", f["OnA"]["rating"] == 1.1 and f["OnA"]["tod_allowance"] == 600.0)
check("an import off another course's column is cleared",
      f["MovedToB"]["rating"] is None and "rating_col" not in f["MovedToB"])
check("a hand-typed number is kept", f["Typed"]["rating"] == 0.9)
check("a stale ToD allowance is cleared (no ToD column on B)",
      f["TodOnly"]["tod_allowance"] is None and "tod_col" not in f["TodOnly"])
check("our boat is checked against its own division's course",
      d["own"]["rating"] is None and hit["own"] is True)
check("the cleared boats are named", hit["boats"] == ["MovedToB", "TodOnly"])
d2 = race(rating_system="ORC", rating_column="NEW")
d2["fleet"] = [{"boat": "x", "rating": 1.0, "rating_col": "OLD"}]
check("a race-wide column switch clears it", R.clear_stale_ratings(d2)["boats"] == ["x"])
check("nothing to clear → empty report", R.clear_stale_ratings(race()) == {"boats": [], "own": False})

print("legacy imports and typed numbers (tags):")
d = race(rating_system="ORC", rating_column="COL_B")
d["fleet"] = [{"boat": "LegacyOrc", "source": "yb+orc", "rating": 1.1},          # pre-tag import
              {"boat": "LegacyTyped", "source": "website", "rating": 0.9},       # pre-tag, typed
              {"boat": "TypedNow", "source": "yb+orc", "rating": 0.8, "rating_col": "manual"},
              {"boat": "NoRating", "source": "yb+orc", "rating": None}]
hit = R.clear_stale_ratings(d)
f = {e["boat"]: e for e in d["fleet"]}
check("an untagged ORC import is cleared (its column is unknown)", f["LegacyOrc"]["rating"] is None)
check("an untagged typed number is kept", f["LegacyTyped"]["rating"] == 0.9)
check("a number tagged manual is kept", f["TypedNow"]["rating"] == 0.8)
check("only real clears are named", hit["boats"] == ["LegacyOrc"])
d = race(rating_system="ORC", rating_column="COL_B")
d["fleet"] = [{"boat": "L", "source": "website+orc", "rating": 1.1}, {"boat": "T", "rating": 1.0}]
check("tag_legacy tags only untagged ORC imports", R.tag_legacy(d, "COL_B") == 1
      and d["fleet"][0]["rating_col"] == "COL_B" and "rating_col" not in d["fleet"][1])
check("a tagged legacy import on the current column is kept",
      R.clear_stale_ratings(d)["boats"] == [] and d["fleet"][0]["rating"] == 1.1)

print("boats whose division the race doesn't list:")
d = race(rating_system="ORC", course_columns=PER)
d["fleet"] = [{"boat": "x", "division": "Class E"}, {"boat": "y", "division": "A"}]
check("flagged with no race-wide column",
      "1 boat in a division the race doesn't list: rating column unknown" in R.validate(d)[1])
d["rules_profile"]["scoring"]["rating_column"] = "XX_A_TOT"
check("flagged with the race-wide fallback",
      "1 boat in a division the race doesn't list: race-wide rating column used" in R.validate(d)[1])
check("and the fallback is the column they get",
      R.rating_spec_for(d, {"division": "Class E"})["column"] == "XX_A_TOT")
# round 6: our division is on course short (XX_B_TOT); an unlisted division must NOT inherit it
d["own"] = {"boat": "Us", "division": "B"}
check("an unlisted division never inherits our course",
      R.rating_spec_for(d, {"division": "Class E"})["column"] == "XX_A_TOT")
d["fleet"] = [{"boat": "x", "division": "Class E", "rating": 1.1, "rating_col": "XX_A_TOT"}]
check("so a valid race-wide rating survives a save", R.clear_stale_ratings(d)["boats"] == []
      and d["fleet"][0]["rating"] == 1.1)
del d["rules_profile"]["scoring"]["rating_column"]
check("no race-wide column → unknown (not our course's)",
      R.rating_spec_for(d, {"division": "Class E"})["column"] is None)
check("our own boat still uses its course", R.rating_spec_for(d, d["own"])["column"] == "XX_B_TOT")
one = race(rating_system="ORC", course_columns=[{"course": "long", "column": "XX_A_TOT"}])
check("one course's column does not stand in when another course has none",
      R.rating_spec_for(one, {"division": "Z"})["column"] is None)
both = race(rating_system="ORC", course_columns=[{"course": "long", "column": "X"}, {"course": "short", "column": "X"}])
check("a column every course shares does", R.rating_spec_for(both, {"division": "Z"})["column"] == "X")

print("fleet_blob (what the boat gets) never carries a stale rating:")
d = race(rating_system="ORC", rating_column="COL_B")
d["fleet"] = [{"boat": "Stale", "rating": 1.1, "rating_col": "COL_A"},
              {"boat": "Legacy", "source": "website+orc", "rating": 1.2},
              {"boat": "Good", "rating": 1.3, "rating_col": "COL_B", "tod_allowance": 600.0,
               "tod_col": "TOD_OLD"},
              {"boat": "Typed", "rating": 0.9}]
d["own"] = {"boat": "Us", "rating": 1.0, "rating_col": "COL_A"}
blob = R.fleet_blob(d)
got = {e["boat"]: (e["rating"], e["tod_allowance"]) for e in blob["fleet"]}
check("stale and legacy ratings are not shipped", got["Stale"][0] is None and got["Legacy"][0] is None)
check("current and typed ratings are", got["Good"][0] == 1.3 and got["Typed"][0] == 0.9)
check("a stale ToD allowance is not shipped", got["Good"][1] is None)
check("our boat's stale rating is not shipped", blob["own"]["rating"] is None)
check("the blob names them", blob["stale_ratings"] == ["Stale", "Legacy", "Good", "(our boat)"])
check("the definition itself is untouched", d["fleet"][0]["rating"] == 1.1 and d["own"]["rating"] == 1.0)
check("a clean race has no stale list", "stale_ratings" not in R.fleet_blob(race()))
d = race(rating_system="ORC", course_columns=PER)
d["fleet"] = [{"boat": "a", "division": "A"}, {"boat": "b", "division": "B"}, {"boat": "z", "division": "Z"}]
d["own"] = {"boat": "Us", "division": "B"}
blob = R.fleet_blob(d)
cols = {e["boat"]: (e["rating_col"], e["tod_col"]) for e in blob["fleet"]}
check("the blob carries each boat's own columns", cols == {"a": ("XX_A_TOT", "XX_A_TOD"),
                                                         "b": ("XX_B_TOT", None), "z": (None, None)})
check("and our boat's", (blob["own"]["rating_col"], blob["own"]["tod_col"]) == ("XX_B_TOT", None))
# an explicit own (the Pi's /fleet/load override) is checked the same way
blob = R.fleet_blob(race(rating_system="ORC", rating_column="COL_B"),
                    {"boat": "Us", "rating": 1.0, "rating_col": "COL_A"})
check("an own override off another column is not loaded", blob["own"]["rating"] is None
      and blob["stale_ratings"] == ["(our boat)"])
ov = {"boat": "Us", "rating": 0.9, "rating_col": "COL_B"}
blob = R.fleet_blob(race(rating_system="ORC", rating_column="COL_B"), ov)
check("an own override on the column is loaded as given", blob["own"]["rating"] == 0.9
      and "stale_ratings" not in blob and ov["rating"] == 0.9)

print("the shipped race file carries its own columns (migrated off the code map):")
with open(os.path.join(ROOT, "vps", "lab", "races", "bayview_mackinac_2026.json")) as f:
    shipped = json.load(f)
by_course = {c["id"]: R.rating_spec(shipped, c["id"])["column"] for c in shipped["courses"]}
check("every course has a column", all(by_course.values()) and len(by_course) == 2)
check("countries come from the file", R.rating_countries(shipped) == ["USA", "CAN"])
check("no rating warning", not any("Rating" in x for x in R.validate(shipped)[1]))

print("a ToD-only course row (Codex round 8):")
dt = race(rating_system="ORC", course_columns=[{"course": "long", "column": "", "column_tod": "LONG_TOD"}])
dt["fleet"] = [{"name": "x", "division": "A", "orc_gph": 600, "tod_allowance": 620, "tod_col": "LONG_TOD"}]
sp = R.rating_spec(dt, "long")
check("the row is read: ToD column named", sp["column_tod"] == "LONG_TOD")
check("save keeps a tagged ToD allowance on it", R.clear_stale_ratings(dt)["boats"] == []
      and dt["fleet"][0]["tod_allowance"] == 620)
check("no 'Rating column unknown' for a ToD-only race",
      not any("Rating column unknown" in x for x in R.validate(dt)[1]))

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
