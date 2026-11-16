"""The race's "Our boat" filled from a Lab boat profile (POST /api/fleet/own-from-boat).

  * a cert boat: name, sail, GPH and the ToT for the race's column, read off the cert again;
  * ToD too when the race scores ToD;
  * a profile without a cert: name and sail only (plus its own GPH, if it has one);
  * the race's division and MMSI stay; numbers typed for another boat are dropped;
  * nothing is saved; an unknown boat or race → 404.

Offline: `fleetimport._orc_dump` is stubbed; the network is never touched.
"""
import json
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp

from fastapi.testclient import TestClient  # noqa: E402

from shared import boat_profile  # noqa: E402
from app import auth, boats, fleetimport, main  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


boats.EDITED_DIR = os.path.join(_tmp, "boats")
boats.BUNDLED_DIR = os.path.join(_tmp, "bundled")
os.makedirs(boats.EDITED_DIR)
os.makedirs(boats.BUNDLED_DIR)

REC = {"RefNo": "045600052L8", "SailNo": "USA 52521", "YachtName": "HOOLIGAN", "GPH": 438.9,
       "TMF_Offshore": 1.3889, "TND_Offshore_Medium": 394.9}
calls = []


def _dump(cc):
    calls.append(cc)
    if cc != "USA":
        raise RuntimeError("no such dump")
    return {"by_sail": {fleetimport._norm_sail(REC["SailNo"]): REC},
            "by_name": {fleetimport._norm(REC["YachtName"]): REC},
            "by_ref": {REC["RefNo"]: REC}, "n": 1}


fleetimport._orc_dump = _dump


def _boat(bid, name, sail, cert=None, gph=None, mmsi=""):
    b = {"schema_version": boat_profile.SCHEMA_VERSION, "boat_id": bid, "name": name,
         "sail_number": sail, "draft_m": 3.2, "orc": {"rating": None, "gph": gph, "scoring": ""},
         "mmsi": mmsi, "provenance": {"cert": cert} if cert else {}}
    boats.save_boat(b)


_boat("hooligan", "Hooligan", "USA 52521",
      cert={"ref_no": "045600052L8", "country": "USA", "gph": 438.9})
_boat("plain", "Plain Jane", "CAN 9", gph=600.0, mmsi="316000001")
_boat("nosail", "New", "")

SCORING = {"rating_system": "ORC", "rating_column": "TMF_Offshore", "rating_column_tod": ""}
RACE = {"race_id": "t-race", "name": "T", "rules_profile": {"scoring": SCORING},
        "own": {"boat": "Old Boat", "sail": "USA 1", "division": "orc", "mmsi": "367000000",
                "orc_gph": 700.0, "rating": 1.01, "rating_col": "manual"}}
stored = {"race": RACE}
main.store.get_race = lambda rid: json.loads(json.dumps(stored["race"])) if rid == "t-race" else None
_saves = []
main.store.save_race = lambda *a, **k: _saves.append(a)

client = TestClient(main.app)
TEAM = {"Authorization": "Bearer " + auth.issue_token()}


def post(body):
    return client.post("/api/fleet/own-from-boat", json=body, headers=TEAM)


print("1) a cert boat: name, sail, GPH and the ToT for the race's column")
r = post({"race_id": "t-race", "boat_id": "hooligan"})
own = r.json()["own"]
check("200", r.status_code == 200)
check("name + sail", own["boat"] == "Hooligan" and own["sail"] == "USA 52521")
check("GPH 438.9", own["orc_gph"] == 438.9)
check("ToT off the race's column, tagged", own["rating"] == 1.3889 and own["rating_col"] == "TMF_Offshore")
check("no ToD when the race scores none", own.get("tod_allowance") is None and "tod_col" not in own)
check("division + MMSI kept", own["division"] == "orc" and own["mmsi"] == "367000000")
check("cert looked up in its country", calls and calls[-1] == "USA")
check("no note", r.json()["note"] is None)

print("2) the race scores ToD too")
r = post({"race_id": "t-race", "boat_id": "hooligan",
          "scoring": dict(SCORING, rating_column_tod="TND_Offshore_Medium")})
own = r.json()["own"]
check("ToD allowance filled", own["tod_allowance"] == 394.9 and own["tod_col"] == "TND_Offshore_Medium")
check("ToT still filled", own["rating"] == 1.3889)

print("3) the screen's unsaved own is the base")
r = post({"race_id": "t-race", "boat_id": "hooligan",
          "own": {"boat": "x", "sail": "y", "division": "doublehanded", "mmsi": ""}})
own = r.json()["own"]
check("unsaved division kept", own["division"] == "doublehanded")

print("4) a race with no rating column yet: GPH only, and a note")
r = post({"race_id": "t-race", "boat_id": "hooligan",
          "scoring": {"rating_system": "ORC", "rating_column": ""}})
j = r.json()
check("GPH, no ToT", j["own"]["orc_gph"] == 438.9 and j["own"].get("rating") is None)
check("note says set it in Rules", "Rules" in (j["note"] or ""))

print("5) a profile without a cert: name and sail, its own GPH; other boat's numbers dropped")
r = post({"race_id": "t-race", "boat_id": "plain"})
own = r.json()["own"]
check("name + sail", own["boat"] == "Plain Jane" and own["sail"] == "CAN 9")
check("old boat's ToT dropped", own.get("rating") is None and "rating_col" not in own)
check("profile GPH", own["orc_gph"] == 600.0)
check("profile MMSI wins", own["mmsi"] == "316000001")
check("division kept", own["division"] == "orc")

print("6) the same boat without a cert keeps its typed numbers")
stored["race"] = dict(RACE, own={"boat": "Plain Jane", "sail": "CAN 9", "rating": 0.99,
                                 "rating_col": "manual", "division": "orc"})
own = post({"race_id": "t-race", "boat_id": "plain"}).json()["own"]
check("typed ToT kept", own["rating"] == 0.99 and own["rating_col"] == "manual")
stored["race"] = RACE

print("6b) a profile with no sail number never inherits the old boat's sail or numbers")
j = post({"race_id": "t-race", "boat_id": "nosail"}).json()["own"]
check("sail cleared", j["boat"] == "New" and j["sail"] == "")
check("old boat's ToT and GPH dropped", j.get("rating") is None and j.get("orc_gph") is None
      and "rating_col" not in j)

print("7) the ORC list down: GPH from the profile, no ToT, a note")
fleetimport._orc_dump = lambda cc: (_ for _ in ()).throw(RuntimeError("down"))
j = post({"race_id": "t-race", "boat_id": "hooligan"}).json()
check("GPH from the cert facts", j["own"]["orc_gph"] == 438.9 and j["own"].get("rating") is None)
check("note says the list didn't load", "didn't load" in (j["note"] or ""))
fleetimport._orc_dump = _dump

print("8) unknown boat or race → 404; nothing saved")
check("unknown boat 404", post({"race_id": "t-race", "boat_id": "nope"}).status_code == 404)
check("unknown race 404", post({"race_id": "nope", "boat_id": "hooligan"}).status_code == 404)
check("no boat_id 404", post({"race_id": "t-race"}).status_code == 404)
check("nothing saved", not _saves and stored["race"]["own"]["boat"] == "Old Boat")

print("ALL OK" if ok else "FAILURES")
raise SystemExit(0 if ok else 1)
