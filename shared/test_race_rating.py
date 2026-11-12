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

print("the shipped race file carries its own columns (migrated off the code map):")
with open(os.path.join(ROOT, "vps", "lab", "races", "bayview_mackinac_2026.json")) as f:
    shipped = json.load(f)
by_course = {c["id"]: R.rating_spec(shipped, c["id"])["column"] for c in shipped["courses"]}
check("every course has a column", all(by_course.values()) and len(by_course) == 2)
check("countries come from the file", R.rating_countries(shipped) == ["USA", "CAN"])
check("no rating warning", not any("Rating" in x for x in R.validate(shipped)[1]))

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
