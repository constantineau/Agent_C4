"""#309: a race picks its own weather stations in the Lab, and the playbook uses them.

No network: every station-list read goes through obsarea._get (stubbed), and the live probes are
stubbed on synthesis.
  1) the finder lists the stations around a course with the automatic picks marked `auto`;
  2) add by id: curated list, then each area source; unknown id → None;
  3) picks are cleaned (bad rows dropped, one per id, capped);
  4) a new source (a fake UK buoy network) registered in obsarea shows up in the finder and the
     automatic list with no other change;
  5) the playbook freeze uses the race's picks (no station-list fetch), still live-probed; with no
     picks it is the automatic list, unchanged; `picked: []` freezes none;
  6) the picks survive a re-ingest draft and another tab's save; only the stations route changes
     them, and `picked: null` goes back to automatic;
  7) the routes: find, lookup, check, save.
"""
import json
import os
import re
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["GRIB_CACHE"] = os.path.join(_tmp, "gribcache")
os.environ.pop("ANTHROPIC_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, main, obsarea, synthesis  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  — ' + str(extra)) if extra else ''}")


AWC = [   # (id, lat, lon, site, siteType)
    ("KFLL", 26.079, -80.162, "Ft Lauderdale-Hollywood Intl", ["METAR", "TAF"]),
    ("KFXE", 26.199, -80.178, "Ft Lauderdale Exec", ["METAR", "TAF"]),
    ("KPBI", 26.683, -80.096, "West Palm Beach Intl", ["METAR", "TAF"]),
    ("KMIA", 25.795, -80.290, "Miami Intl", ["METAR", "TAF"]),
    ("KEYW", 24.556, -81.760, "Key West Intl", ["METAR", "TAF"]),     # far: only by id
]
NDBC_XML = """<?xml version="1.0" encoding="utf-8"?><stations>
<station id="lkwf1" lat="26.612" lon="-80.034" name="Lake Worth" type="fixed" met="y"/>
<station id="FWYF1" lat="25.591" lon="-80.097" name="Fowey Rocks" type="fixed" met="y"/>
<station id="SMKF1" lat="24.627" lon="-81.110" name="Sombrero Key" type="fixed" met="y"/>
</stations>"""
FETCHED = []


def fake_get(url, timeout=20):
    FETCHED.append(url)
    if "aviationweather" in url and "ids=" in url:
        sid = re.search(r"ids=([^&]+)", url).group(1)
        return json.dumps([{"icaoId": i, "lat": la, "lon": lo, "site": site, "siteType": t}
                           for i, la, lo, site, t in AWC if i == sid])
    if "aviationweather" in url:
        s, w, n, e = (float(x) for x in re.search(r"bbox=([^&]+)", url).group(1).split(","))
        return json.dumps([{"id": i, "icaoId": i, "lat": la, "lon": lo, "site": site, "siteType": t}
                           for i, la, lo, site, t in AWC if s <= la <= n and w <= lo <= e])
    if "activestations" in url:
        return NDBC_XML
    raise AssertionError("unexpected fetch " + url)


obsarea._get = fake_get
PROBED = []
synthesis._probe_metars = lambda ids, timeout=12: (PROBED.extend(ids) or set(ids) - {"KFXE"})
synthesis._probe_ndbc = lambda sid, timeout=8: (PROBED.append(sid) or True)


def reset():
    obsarea.clear_cache()
    FETCHED.clear()
    PROBED.clear()


FLL = (26.6, 25.6, -80.6, -79.6)
RACE = {"race_id": "fll-race", "name": "FLL test",
        "courses": [{"id": "c1", "name": "Ocean",
                     "start": {"lat": 26.10, "lon": -80.08},
                     "marks": [{"name": "Sea buoy", "lat": 26.30, "lon": -80.02, "rounding": "port"}],
                     "finish": {"points": [{"lat": 26.10, "lon": -80.08}]}}]}

print("1) the finder: stations around the course, automatic picks marked")
reset()
got = obsarea.finder(FLL)
rows = {s["id"]: s for s in got["stations"]}
auto = [s["id"] for s in obsarea.find(FLL)["stations"]]
check("lists the area's airports and buoys", {"KFLL", "KFXE", "KPBI", "KMIA", "LKWF1", "FWYF1"}
      <= set(rows), sorted(rows))
check("auto = what find() picks", sorted(s["id"] for s in got["stations"] if s["auto"]) == sorted(auto),
      auto)
check("each row: id, kind, name, distance, source", all(
    {"id", "kind", "name", "lat", "lon", "dist_nm", "source", "auto"} <= set(s) for s in got["stations"]))
check("nearest first", [s["dist_nm"] for s in got["stations"]] ==
      sorted(s["dist_nm"] for s in got["stations"]))
check("no course: a plain note", obsarea.finder(None)["notes"] == ["No marks with a position yet."])
reset()
gl = obsarea.finder((46.35, 42.5, -85.1, -81.3))      # Bayview: curated ones are the auto picks
check("Great Lakes: curated stations listed and ticked by default",
      any(s["source"] == "curated" and s["auto"] for s in gl["stations"]))

print("2) add by id")
reset()
check("curated id, no fetch", (obsarea.lookup("kapn") or {}).get("source") == "curated" and FETCHED == [])
k = obsarea.lookup("KEYW", near=(26.1, -80.1))
check("METAR by id (anywhere), with distance", k and k["kind"] == "metar" and k["dist_nm"] > 60, k)
s = obsarea.lookup("smkf1")
check("NDBC by id", s and s["id"] == "SMKF1" and s["kind"] == "ndbc", s)
check("unknown / bad id → None", obsarea.lookup("ZZZZ") is None and obsarea.lookup("") is None
      and obsarea.lookup("x" * 40) is None)

print("3) picks are cleaned")
blk = obsarea.clean_picks([
    {"id": "kfll", "kind": "metar", "lat": 26.079, "lon": -80.162, "name": "FLL"},
    {"id": "KFLL", "kind": "metar", "lat": 26.079, "lon": -80.162},          # duplicate
    {"id": "X1", "kind": "made-up", "lat": 1, "lon": 1},                      # unknown kind
    {"id": "<b>", "kind": "metar", "lat": 1, "lon": 1},                       # bad id
    {"id": "Y1", "kind": "ndbc", "lat": 99, "lon": 1},                        # out of range
    "junk"])
check("one good row, upper-case id", blk == {"picked": [
    {"id": "KFLL", "kind": "metar", "lat": 26.079, "lon": -80.162, "name": "FLL"}]}, blk)
check("null = automatic", obsarea.clean_picks(None) is None)
check("capped", len(obsarea.clean_picks([{"id": f"S{i}", "kind": "ndbc", "lat": 1, "lon": 1}
                                         for i in range(50)])["picked"]) == obsarea.MAX_PICKS)
check("picks(): none → None, [] → []", obsarea.picks({}) is None
      and obsarea.picks({"stations": {"picked": []}}) == [])

print("4) a new source plugs in through the registry")


class UkBuoys(obsarea.StationSource):
    name, kind, label = "ukbuoy", "ukbuoy", "UK buoys"
    coverage = (49.0, 61.0, -12.0, 3.0)
    coverage_label = "UK waters only"
    LIST = [{"id": "62103", "kind": "ukbuoy", "lat": 49.9, "lon": -2.9, "name": "Channel Lightship"}]

    def find(self, box):
        return [st for st in self.LIST if obsarea._in_box(st, box)]


CHANNEL = (50.82, 49.6, -3.5, -0.25)
reset()
obsarea.register(UkBuoys())
try:
    f = obsarea.finder(CHANNEL)
    check("shows in the finder", any(s["id"] == "62103" and s["kind"] == "ukbuoy" for s in f["stations"]),
          f["stations"])
    check("and in the automatic list", "62103" in [s["id"] for s in obsarea.find(CHANNEL)["stations"]])
    check("add by id finds it", (obsarea.lookup("62103") or {}).get("source") == "ukbuoy")
    check("its kind can be picked", obsarea.clean_pick(UkBuoys.LIST[0]) is not None)
    check("not here elsewhere, plainly", "UK buoys: not here (UK waters only)." in obsarea.finder(FLL)["notes"])
finally:
    obsarea.SOURCES.pop("ukbuoy")

print("5) the playbook freeze uses the race's picks")
from app import optimizer  # noqa: E402
real_bbox = optimizer.course_bbox
optimizer.course_bbox = lambda d, c=None, pad=0.5: FLL
reset()
autos = [s["id"] for s in synthesis._buoy_stations({}, "c1")]
check("no picks: the automatic list (live ones), as before",
      autos == [i for i in auto if i != "KFXE"], autos)
reset()
picked = dict(RACE, stations={"picked": [
    {"id": "KEYW", "kind": "metar", "lat": 24.556, "lon": -81.76, "name": "Key West"},
    {"id": "KFXE", "kind": "metar", "lat": 26.199, "lon": -80.178, "name": "Exec"},
    {"id": "SMKF1", "kind": "ndbc", "lat": 24.627, "lon": -81.11, "name": "Sombrero Key"}]})
frozen = synthesis._buoy_stations(picked, "c1", promises={"KEYW": [[0, 9.0, 80]]})
check("picks frozen, dark ones dropped, no station-list fetch",
      [s["id"] for s in frozen] == ["KEYW", "SMKF1"] and FETCHED == [], (frozen, FETCHED))
check("promise attached, shore flag kept", frozen[0]["promise"] == [[0, 9.0, 80]]
      and frozen[0]["shore"] and not frozen[1]["shore"])
check("picks: no automatic-search notes", synthesis._station_notes(picked, "c1") == [])
check("picked: [] freezes none",
      synthesis._buoy_stations(dict(RACE, stations={"picked": []}), "c1") is None)
check("playbook promises sample the same stations",
      [s["id"] for s in obsarea.race_stations(picked, FLL, 0.6)["stations"]] == ["KEYW", "KFXE", "SMKF1"])
optimizer.course_bbox = real_bbox

print("6) the picks survive saves; only the stations route changes them")


def disk():
    with open(os.path.join(_tmp, "fll-race.json")) as fh:
        return json.load(fh)


main._write_race(json.loads(json.dumps(RACE)))
check("a new race has no picks", "stations" not in disk())
d = json.loads(json.dumps(RACE))
d["stations"] = {"picked": [picked["stations"]["picked"][0]]}
main._write_race(d)
check("an ordinary save can't set them", "stations" not in disk())
d = json.loads(json.dumps(picked))
main._write_race(d, stations=True)
check("the stations route sets them", disk()["stations"] == picked["stations"])
main._write_race({"race_id": "fll-race", "name": "re-read", "courses": RACE["courses"]}, draft=True)
check("a re-ingest draft keeps them", disk()["stations"] == picked["stations"] and disk()["name"] == "re-read")
main._write_race(dict(json.loads(json.dumps(RACE)), stations=None))
check("another tab's save (old copy) keeps them", disk()["stations"] == picked["stations"])

print("7) the routes")
reset()
client = TestClient(main.app)
TEAM = {"Authorization": "Bearer " + auth.issue_token()}
r = client.post("/api/races/fll-race/stations/find", headers=TEAM)
j = r.json()
check("find: the area's stations + the saved picks", r.status_code == 200 and
      any(s["id"] == "KFLL" for s in j["stations"]) and [s["id"] for s in j["picked"]] ==
      ["KEYW", "KFXE", "SMKF1"], j)
check("find: unknown race 404", client.post("/api/races/nope/stations/find", headers=TEAM).status_code == 404)
r = client.post("/api/races/fll-race/stations/lookup", json={"id": "keyw"}, headers=TEAM)
check("lookup: found, with distance", r.status_code == 200 and r.json()["station"]["id"] == "KEYW"
      and r.json()["station"]["dist_nm"] > 0, r.json())
r = client.post("/api/races/fll-race/stations/lookup", json={"id": "ZZZZ"}, headers=TEAM)
check("lookup: not found says so", r.status_code == 404 and r.json()["detail"] == "No station with that id.")
r = client.post("/api/races/fll-race/stations/check", headers=TEAM,
                json={"stations": [{"id": "KFLL", "kind": "metar"}, {"id": "KFXE", "kind": "metar"},
                                   {"id": "FWYF1", "kind": "ndbc"}]})
check("check: live per station", r.json() == {"live": {"KFLL": True, "KFXE": False, "FWYF1": True}},
      r.json())
r = client.post("/api/races/fll-race/stations", headers=TEAM,
                json={"picked": [{"id": "kfll", "kind": "metar", "lat": 26.079, "lon": -80.162, "name": "FLL"},
                                 {"id": "bad", "kind": "nope", "lat": 0, "lon": 0}]})
check("save: cleaned picks stored", r.json()["stations"] == {"picked": [
    {"id": "KFLL", "kind": "metar", "lat": 26.079, "lon": -80.162, "name": "FLL"}]}
    and disk()["stations"] == r.json()["stations"], r.json())
check("save keeps the rest of the race", disk()["name"] == "FLL test" and disk()["courses"] == RACE["courses"])
before = disk().get("stations")
many = [{"id": f"K{i:03d}", "kind": "metar", "lat": 26.0, "lon": -80.0, "name": str(i)} for i in range(21)]
r = client.post("/api/races/fll-race/stations", headers=TEAM, json={"picked": many})
check("21 picks refused, not trimmed; nothing saved", r.status_code == 422
      and "20" in r.json()["detail"] and disk().get("stations") == before, r.text)
two_kinds = many[:20] + [dict(many[0], kind="ndbc")]            # 20 ids, 21 (kind, id) picks
r = client.post("/api/races/fll-race/stations", headers=TEAM, json={"picked": two_kinds})
check("21 picks across two kinds refused too", r.status_code == 422 and disk().get("stations") == before, r.text)
r = client.post("/api/races/fll-race/stations", headers=TEAM, json={"picked": {}})
check("a non-list is refused, picks kept", r.status_code == 422 and disk().get("stations") == before, r.text)
r = client.post("/api/races/fll-race/stations", headers=TEAM, json={"picked": many[:20]})
check("20 picks saved", r.status_code == 200 and len(r.json()["stations"]["picked"]) == 20, r.text)
r = client.post("/api/races/fll-race/stations", headers=TEAM, json={"picked": None})
check("picked: null → automatic", r.json()["stations"] is None and "stations" not in disk())
check("unauthenticated refused", client.post("/api/races/fll-race/stations", json={"picked": []}).status_code == 401)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
