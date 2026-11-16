"""A re-read race finds the saved race it updates (racematch.find).

Race week: the SIs land, the team reads NOR + SIs again. The AI names the id from name + year and can
name it differently the second time; saved under the new id, the SIs would become a second race
without the fleet, boat, rating pick or notes.

Run: python3 test_racematch.py   (from vps/lab)
"""
import sys

from app import racematch

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


SAVED = [
    {"race_id": "sorc-ocean-race-2026", "name": "SORC Ocean Race", "year": 2026, "start_date": "2026-11-20"},
    {"race_id": "bayview-mackinac-2026", "name": "Bayview Mackinac Race", "year": 2026,
     "start_date": "2026-07-18"},
    {"race_id": "sorc-ocean-race-2027", "name": "SORC Ocean Race", "year": 2027, "start_date": "2027-11-19"},
]


def find(**draft):
    return racematch.find(draft, SAVED)


m = find(race_id="sorc-ocean-race-2026", name="SORC Ocean Race", year=2026)
check("same id: updates it as it is", m == {"race_id": "sorc-ocean-race-2026", "name": "SORC Ocean Race",
                                            "same_id": True})
m = find(race_id="2026-sorc-ocean-race", name="2026 SORC Ocean Race", year=2026, start_date="2026-11-20")
check("id worded differently, same race: offered", m and m["race_id"] == "sorc-ocean-race-2026"
      and m["same_id"] is False)
m = find(race_id="sorc-ocean-race-offshore-2026", name="SORC Ocean Race – Offshore Course", year=2026)
check("a longer name for it: offered", m and m["race_id"] == "sorc-ocean-race-2026")
m = find(race_id="the-sorc-ocean-race-2026", name="The SORC Ocean Race", year=None, start_date="2026-11-20")
check("no year, but the start date gives it", m and m["race_id"] == "sorc-ocean-race-2026")
check("another year's edition is not the same race",
      find(race_id="sorc-ocean-2028", name="SORC Ocean Race", year=2028) is None)
check("picks the edition of the draft's year",
      (find(race_id="sorc-2027", name="SORC Ocean Race", year=2027) or {}).get("race_id")
      == "sorc-ocean-race-2027")
check("start dates months apart: not the same race",
      find(race_id="x", name="SORC Ocean Race", year=2026, start_date="2026-03-01") is None)
check("a different race the same year: nothing offered",
      find(race_id="nassau-cup-2026", name="Nassau Cup Ocean Race", year=2026) is None)
check("one shared word is not enough",
      find(race_id="chicago-mackinac-2026", name="Chicago Mackinac Race", year=2026,
           start_date="2026-07-11") is None)
check("a shared date and one shared word is not the same race",
      racematch.find({"race_id": "chicago-mackinac-2026", "name": "Chicago Mackinac Race", "year": 2026,
                      "start_date": "2026-07-18"}, SAVED) is None)
WEEKLY = [{"race_id": "wnr-0701", "name": "Wednesday Night Race", "year": 2026, "start_date": "2026-07-01"},
          {"race_id": "wnr-0708", "name": "Wednesday Night Race", "year": 2026, "start_date": "2026-07-08"}]
check("two editions a week apart: the one on the draft's start date",
      (racematch.find({"race_id": "wednesday-night-race-0708", "name": "Wednesday Night Race", "year": 2026,
                       "start_date": "2026-07-08"}, WEEKLY) or {}).get("race_id") == "wnr-0708")
check("two that fit equally (no date to tell): nothing offered",
      racematch.find({"race_id": "wnr", "name": "Wednesday Night Race", "year": 2026}, WEEKLY) is None)
check("no saved races: nothing", racematch.find({"race_id": "a", "name": "A Race"}, []) is None)
check("a nameless draft: nothing", find(race_id="x", name="", year=2026) is None)
check("junk input: nothing", racematch.find(None, SAVED) is None
      and racematch.find({"name": "SORC Ocean Race", "year": 2026}, [None, {}]) is None)

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
