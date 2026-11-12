"""Race tracker providers (#310, map #316).

Retro, debrief tracks and the fleet entry list read tracks through `app/trackers.py`, picked by
the race definition's `tracker.provider`. YB behaves as before. A race on a tracker with no
public feed gets the `file` provider: the fleet's tracks are uploaded as GPX or CSV, and the
debrief and the entry list then read them from the retro archive. An unknown tracker gets a
plain note.

No network: every YB fetch is stubbed, the retro archive is a temp SQLite file.
Run in the Lab image: python3 -m pytest -q vps/lab/test_tracker_providers.py
"""
import json
import os
import struct
import tempfile

import pytest

from app import fleetimport as fi
from app import retro, store, trackers
from app import retrostore as rs
from app import track as track_mod


@pytest.fixture(autouse=True)
def archive(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setattr(rs, "RETRO_DB", os.path.join(d, "retro.db"))
    return d


def _race(**over):
    d = {"race_id": "orc-na-2027", "timezone": "America/New_York",
         "division_starts": {"A": "2027-11-15T11:00:00"},
         "courses": [{"id": "wl", "name": "W/L"}],
         "tracker": {"provider": "file"}}
    d.update(over)
    return d


# ---- registry ------------------------------------------------------------------------------
def test_provider_names():
    for n in ("yb", "YB", "bycmack", "ybtracking", "yellowbrick"):
        assert trackers.get(n) is trackers.YB
    for n in ("file", "upload", "gpx", "csv"):
        assert trackers.get(n) is trackers.File
    assert trackers.get("tractrac") is None and trackers.get("") is None
    assert trackers.get({"tracker": {"provider": " Gpx "}}) is trackers.File


def test_js_names_match():
    """The Lab's trackerKind() lists the same names as the backend."""
    js = open(os.path.join(os.path.dirname(__file__), "web", "app.js")).read()
    assert f"const TRACKER_YB = {json.dumps(list(trackers.YB_NAMES))};" in js
    assert f"const TRACKER_FILE = {json.dumps(list(trackers.FILE_NAMES))};" in js


def test_unknown_tracker_gets_a_plain_note():
    d = _race(tracker={"provider": "tractrac", "url": "https://example.invalid"})
    assert trackers.entries(d) == {"ok": False, "note": trackers.unknown_note("tractrac")}
    r = trackers.boat_track(d, "Bravo")
    assert r["ok"] is False and "tractrac" in r["note"]
    assert trackers.entries(_race(tracker={}))["note"] == "No tracker set for this race."
    su = retro.setup_for(d)
    assert su["yb_id"] is None and not su["upload"] and "tractrac" in su["note"] and "YB" in su["note"]
    assert retro.ingest_race("x", provider="tractrac")["ok"] is False


def test_setup_for_file_race():
    su = retro.setup_for(_race())
    assert su["provider"] == "file" and su["upload"] is True and su["yb_id"] is None
    assert su["note"] == trackers.FILE_HINT


def test_setup_for_yb_race_unchanged():
    su = retro.setup_for(_race(tracker={"provider": "bycmack", "race": "demo2026"}))
    assert su["provider"] == "yb" and su["yb_id"] == "demo2026" and su["note"] is None


def test_validate_accepts_a_file_tracker():
    from shared import race_def
    _, warns = race_def.validate({"race_id": "x", "name": "x", "year": 2027,
                                  "tracker": {"provider": "file"}})
    assert not any("not fetchable" in w for w in warns)


# ---- YB through the provider: same archive as before ---------------------------------------
def _allpositions(blocks):
    """Encode YB AllPositions3: per block a base (newest fix) then newest-first deltas."""
    out = b"\x00\x00\x00\x00"
    for fixes in blocks:                       # fixes chronological [(lat, lon, t)]
        rev = list(reversed(fixes))
        la, lo, t = rev[0]
        out += struct.pack(">iiI", round(la * 1e5), round(lo * 1e5), 0)
        for (pla, plo, pt) in rev[1:]:
            out += struct.pack(">Hhhh", 0x8000 | (t - pt), round((pla - la) * 1e5),
                               round((plo - lo) * 1e5), 0)
            la, lo, t = pla, plo, pt
    return out


def _stub_yb(monkeypatch, leaderboard=True):
    setup = {"start": 1_800_000_000, "tags": [{"id": 7, "name": "ORC A", "lb": True, "sort": 1}],
             "teams": [{"id": 11, "name": "Bravo", "sail": "USA 1", "tags": [7], "tcf1": "1.01"},
                       {"id": 12, "name": "Charlie", "sail": "USA 2", "tags": [7], "tcf1": "0.99"}]}
    blocks = [[(26.10, -80.10, 0), (26.11, -80.09, 600), (26.12, -80.08, 1200)],
              [(26.10, -80.10, 0), (26.13, -80.12, 600), (26.15, -80.14, 1200)]]
    gp = {"teams": [{"name": "Bravo", "positions": [
                        {"latitude": 26.12, "longitude": -80.08, "gpsAtMillis": 1_800_003_600_000}]},
                    {"name": "Charlie", "positions": [
                        {"latitude": 26.15, "longitude": -80.14, "gpsAtMillis": 1_800_003_000_000}]}]}
    lb = {"tags": [{"id": 7, "teams": [
        {"id": 12, "elapsed": "3000", "cElapsed": "2970", "tcf": "0.99", "rankR": 1, "finished": True},
        {"id": 11, "elapsed": "3600", "cElapsed": "3636", "tcf": "1.01", "rankR": 2, "finished": True}]}]}
    asked = []

    def get(url, raw=False):
        asked.append(url)
        if "RaceSetup" in url:
            return setup
        if "AllPositions3" in url:
            return _allpositions(blocks)
        if "GetPositions" in url:
            return gp
        if "leaderboard" in url:
            if not leaderboard:
                raise OSError("down")
            return lb
        raise AssertionError(url)
    monkeypatch.setattr(track_mod, "_yb_get", get)
    return asked


def test_yb_retro_ingest(monkeypatch):
    asked = _stub_yb(monkeypatch)
    r = retro.ingest_race("demo2026")
    assert r == {"ok": True, "race_id": "demo2026", "teams": 2, "blocks": 2, "tracks": 2,
                 "results": 2, "divisions": 1, "unmatched_tracks": []}
    assert all(u.startswith("https://cf.yb.tl/") for u in asked)
    race = rs.get_race("demo2026")
    assert race["source"] == "yb" and race["start_epoch"] == 1_800_000_000
    ents = {e["team_id"]: e for e in rs.get_entries("demo2026")}
    assert ents[11]["division"] == '["ORC A"]' and ents[11]["tcf"] == 1.01
    fx = rs.get_track("demo2026", 11)
    assert fx[-1]["t"] == 1_800_003_600 and fx[0]["t"] == 1_800_003_600 - 1200
    assert abs(fx[-1]["lat"] - 26.12) < 1e-6
    res = {r["team_id"]: r for r in rs.get_results("demo2026")}
    assert res[12]["rank_division"] == 1 and res[11]["corrected_s"] == 3636.0


def test_yb_retro_ingest_without_leaderboard(monkeypatch):
    _stub_yb(monkeypatch, leaderboard=False)
    r = retro.ingest_race("demo2026")
    assert r["ok"] and r["tracks"] == 2 and r["results"] == 0 and r["divisions"] == 0
    assert "leaderboard unavailable" in r["note"]


def test_yb_boat_track_and_entries_go_through_the_provider(monkeypatch):
    seen = []
    monkeypatch.setattr(track_mod, "fetch_yb_track", lambda d, b=None: seen.append(("track", b)) or {"ok": True})
    monkeypatch.setattr(fi, "roster_from_yb", lambda d: seen.append(("entries",)) or {"ok": True, "entries": []})
    d = _race(tracker={"provider": "yb", "race": "demo2026"})
    assert trackers.boat_track(d, "Bravo")["ok"] and trackers.entries(d)["ok"]
    assert seen == [("track", "Bravo"), ("entries",)]


# ---- file provider -------------------------------------------------------------------------
GPX = b"""<?xml version="1.0"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1">
 <trk><name>Bravo</name><trkseg>
  <trkpt lat="26.10" lon="-80.10"><time>2027-11-15T16:00:00Z</time></trkpt>
  <trkpt lat="26.11" lon="-80.09"><time>2027-11-15T16:10:00Z</time></trkpt>
 </trkseg></trk>
 <trk><name>Charlie</name><trkseg>
  <trkpt lat="26.10" lon="-80.11"><time>2027-11-15T16:00:00Z</time></trkpt>
  <trkpt lat="26.12" lon="-80.12"><time>2027-11-15T16:10:00Z</time></trkpt>
  <trkpt lat="26.13" lon="-80.13"></trkpt>
 </trkseg></trk>
</gpx>"""

CSV = b"""Boat,Sail,Division,Rank,Time,Latitude,Longitude
Bravo,USA 1,A,2,2027-11-15T16:00:00Z,26.10,-80.10
Charlie,USA 2,A,1,2027-11-15T16:00:00Z,26.10,-80.11
Bravo,,,,2027-11-15T16:10:00Z,26.11,-80.09
Charlie,,,,2027-11-15T16:10:00Z,26.12,-80.12
"""


def test_gpx_fleet_file():
    f = trackers.parse_fleet_files([("fleet.gpx", GPX)], _race())
    assert f["ok"] and [t["name"] for t in f["teams"]] == ["Bravo", "Charlie"]
    assert len(f["tracks"][2]) == 2                       # the untimed fix is dropped
    gun = 1_826_294_400.0                                 # 11:00 New York = 16:00Z
    assert f["start"] == gun and f["results"] == [] and "no report" in f["results_note"]
    # a GPX names no division and this race has no roster: no start, never A's gun
    assert [t["start"] for t in f["teams"]] == [None, None]
    assert f["no_start"] == ["Bravo", "Charlie"]
    assert "No start for Bravo, Charlie. Not scored." in f["results_note"]


TWO_GUNS = {"A": "2027-11-15T10:00:00", "B": "2027-11-15T11:00:00"}


def test_gpx_divisions_come_from_the_roster():
    """A 10:00, B 11:00: Charlie (B, matched by sail) starts at 11:00, not A's 10:00."""
    d = _race(division_starts=TWO_GUNS,
              fleet=[{"boat": "Bravo", "sail": "USA 1", "division": "A"},
                     {"boat": "Chaz", "sail": "USA-2", "division": "b"}],    # other name, same sail
              own={})
    f = trackers.parse_fleet_files([("fleet.gpx", GPX), ("f.csv", b"boat,sail,time,lat,lon\nCharlie,USA 2,2027-11-15T16:00:00Z,26.1,-80.1\n")], d)
    by = {t["name"]: t for t in f["teams"]}
    assert by["Bravo"]["division"] == "A" and by["Bravo"]["start"] == 1_826_290_800.0      # 15:00Z
    assert by["Charlie"]["division"] == "B" and by["Charlie"]["start"] == 1_826_294_400.0  # 16:00Z
    assert f["no_start"] == []


def test_unmatched_boat_has_no_start():
    d = _race(division_starts=TWO_GUNS, fleet=[{"boat": "Bravo", "division": "A"}])
    f = trackers.parse_fleet_files([("fleet.gpx", GPX)], d)
    by = {t["name"]: t for t in f["teams"]}
    assert by["Bravo"]["start"] == 1_826_290_800.0 and by["Charlie"]["start"] is None
    assert f["no_start"] == ["Charlie"]
    # a CSV division with no gun is not swapped for another division's
    csv = b"boat,division,time,lat,lon\nDelta,C,2027-11-15T16:00:00Z,26.1,-80.1\nDelta,C,2027-11-15T16:10:00Z,26.2,-80.1\n"
    f = trackers.parse_fleet_files([("f.csv", csv)], d)
    assert f["teams"][0]["division"] == "C" and f["teams"][0]["start"] is None


def test_race_with_no_guns_has_no_start():
    f = trackers.parse_fleet_files([("fleet.csv", CSV)], _race(division_starts={}))
    assert f["start"] is None and all(t["start"] is None for t in f["teams"])
    assert f["no_start"] == ["Bravo", "Charlie"]


def _stub_run(monkeypatch, on_optimize=None):
    """run_fleet with the wind field, router and scoring stubbed: records the gun each boat got."""
    from app import optimizer
    import app.wind as wind_mod
    import app.wind.archive as archive_mod
    guns = []

    class WF:
        loaded = True
    monkeypatch.setattr(wind_mod, "build_windfield", lambda *a, **k: WF())
    monkeypatch.setattr(archive_mod, "gun_sources", lambda *a, **k: [])
    monkeypatch.setattr(optimizer, "course_bbox", lambda *a: None)
    monkeypatch.setattr(optimizer, "incomplete_reason", lambda *a: None)
    monkeypatch.setattr(retro.race_def, "course_to_marks",
                        lambda d, cid: ([("s", "", 26.1, -80.1), ("f", "", 26.2, -80.1)], [], cid))

    def optimize(d, cid, start, wf, **k):
        guns.append(start)
        if on_optimize:
            on_optimize()
        return {"path": [{"lat": 26.2, "lon": -80.1}], "total_hours": 1.0}
    monkeypatch.setattr(optimizer, "optimize_course", optimize)
    monkeypatch.setattr(track_mod, "score_track", lambda *a, **k: {"available": True})
    return guns


def _polars_for_all(rid="orc-na-2027"):
    for tid in _ids(rid).values():
        rs.save_polar(rid, tid, None, [[1, 2, 3]], "sail", 0.9)


def test_run_scores_each_boat_from_its_own_gun(monkeypatch):
    d = _race(division_starts=TWO_GUNS, fleet=[{"boat": "Bravo", "division": "A"},
                                               {"boat": "Charlie", "division": "B"}])
    monkeypatch.setattr(store, "get_race", lambda rid: d)
    retro.ingest_file("orc-na-2027", [("fleet.gpx", GPX)], d)
    _polars_for_all()
    guns = _stub_run(monkeypatch)
    r = retro.run_fleet("orc-na-2027", def_race_id="orc-na-2027")
    assert r["ok"] and r["ran"] == 2 and sorted(guns) == [1_826_290_800.0, 1_826_294_400.0]


def test_run_skips_a_boat_with_no_start(monkeypatch):
    d = _race(division_starts=TWO_GUNS, fleet=[{"boat": "Bravo", "division": "A"}])
    monkeypatch.setattr(store, "get_race", lambda rid: d)
    retro.ingest_file("orc-na-2027", [("fleet.gpx", GPX)], d)
    _polars_for_all()
    guns = _stub_run(monkeypatch)
    r = retro.run_fleet("orc-na-2027", def_race_id="orc-na-2027")
    assert r["ran"] == 1 and guns == [1_826_290_800.0]
    assert {"boat": "Charlie", "note": "No start for Charlie. Not scored."} in r["failed"]


def test_upload_refused_while_that_race_is_scoring(monkeypatch):
    monkeypatch.setattr(retro, "fleet_job_status", lambda: {"state": "running", "race_id": "orc-na-2027"})
    r = retro.ingest_file("orc-na-2027", [("fleet.gpx", GPX)], _race())
    assert r == {"ok": False, "busy": True, "note": "Scoring is running. Upload after it ends."}
    assert rs.get_race("orc-na-2027") is None
    # another race's job doesn't block this one
    monkeypatch.setattr(retro, "fleet_job_status", lambda: {"state": "running", "race_id": "other"})
    assert retro.ingest_file("orc-na-2027", [("fleet.gpx", GPX)], _race())["ok"]


def test_run_drops_its_writes_after_a_reimport(monkeypatch):
    """The file is uploaded again while the job optimizes boat 1: nothing of the old run lands."""
    d = _race(division_starts=TWO_GUNS, fleet=[{"boat": "Bravo", "division": "A"},
                                               {"boat": "Charlie", "division": "B"}])
    monkeypatch.setattr(store, "get_race", lambda rid: d)
    retro.ingest_file("orc-na-2027", [("fleet.gpx", GPX)], d)
    _polars_for_all()
    reimport = lambda: retro.ingest_file("orc-na-2027", [("fleet.gpx", GPX)], d)
    _stub_run(monkeypatch, on_optimize=reimport)
    r = retro.run_fleet("orc-na-2027", def_race_id="orc-na-2027")
    assert r["ok"] is False and r["stale"] and r["note"] == retro.STALE and r["ran"] == 0
    assert rs.get_scores("orc-na-2027") == []
    assert rs._rows("SELECT COUNT(*) AS n FROM runs WHERE race_id=?", ("orc-na-2027",))[0]["n"] == 0


def test_unstamped_writes_still_work():
    rid = "x"
    assert rs.save_run(rid, 1, "k", {}, {}, {}) is not None
    assert rs.save_run(rid, 1, "k", {}, {}, {}, stamp=123.0) is None      # no such race


def test_one_gpx_per_boat_takes_the_file_name():
    one = b'<gpx><trk><trkseg><trkpt lat="1" lon="2"><time>2027-01-01T00:00:00Z</time></trkpt>' \
          b'<trkpt lat="1.1" lon="2"><time>2027-01-01T00:01:00Z</time></trkpt></trkseg></trk></gpx>'
    f = trackers.parse_fleet_files([("Bravo.gpx", one), ("Delta.gpx", one)], _race(division_starts={}))
    assert [t["name"] for t in f["teams"]] == ["Bravo", "Delta"]
    assert f["start"] is None                             # never the first fix


def test_csv_fleet_file_with_ranks():
    f = trackers.parse_fleet_files([("fleet.csv", CSV)], _race())
    assert f["ok"] and {t["name"]: t["sail"] for t in f["teams"]} == {"Bravo": "USA 1", "Charlie": "USA 2"}
    assert all(t["start"] == 1_826_294_400.0 for t in f["teams"])      # division A's gun
    assert sorted((r["team_id"], r["rank"], r["division"]) for r in f["results"]) == [(1, 2, "A"), (2, 1, "A")]


def test_csv_local_times_need_a_zone():
    naive = b"boat,time,lat,lon\nBravo,2027-11-15 11:00:00,26.1,-80.1\nBravo,2027-11-15 11:10:00,26.2,-80.1\n"
    assert trackers.parse_fleet_files([("f.csv", naive)], _race(timezone=""))["note"] == trackers.NO_TIMES
    f = trackers.parse_fleet_files([("f.csv", naive)], _race())
    assert f["ok"] and f["tracks"][1][0]["t"] == 1_826_294_400.0


def test_bad_files_say_why():
    with pytest.raises(ValueError, match="boat, time, lat, lon"):
        trackers.parse_fleet_files([("f.csv", b"name,lat\nx,1\n")], _race())
    with pytest.raises(ValueError, match="not valid GPX"):
        trackers.parse_fleet_files([("f.gpx", b"<gpx><trk>")], _race())
    assert trackers.parse_fleet_files([], _race())["ok"] is False


def test_file_upload_feeds_retro_debrief_and_entry_list(monkeypatch):
    d = _race()
    r = retro.ingest_file("orc-na-2027", [("fleet.csv", CSV)], d)
    assert r["ok"] and r["tracks"] == 2 and r["results"] == 2 and r["divisions"] == 1
    assert rs.get_race("orc-na-2027")["source"] == "file"

    t = trackers.boat_track(d, "charlie")
    assert t["ok"] and t["source"] == "file" and t["boat"] == "Charlie" and t["n"] == 2
    miss = trackers.boat_track(d, "Nobody")
    assert miss["ok"] is False and "Bravo" in miss["boats"]

    e = fi.import_fleet(d, source="tracker")
    assert e["ok"] and [x["boat"] for x in e["entries"]] == ["Bravo", "Charlie"]
    assert e["entries"][0]["source"] == "file" and e["entries"][0]["division"] == "A"


def test_file_race_before_upload():
    d = _race()
    assert trackers.entries(d)["note"] == trackers.NO_FILE
    assert trackers.boat_track(d, "Bravo")["note"] == trackers.NO_FILE


def test_upload_endpoint(monkeypatch, archive):
    from fastapi.testclient import TestClient
    from app import auth, main
    monkeypatch.setattr(track_mod, "TRACK_DIR", archive)
    monkeypatch.setattr(store, "get_race", lambda rid: _race() if rid == "orc-na-2027" else None)
    c = TestClient(main.app)
    h = {"Authorization": "Bearer " + auth.issue_token()}
    r = c.post("/api/retro/upload?def_race_id=orc-na-2027", headers=h,
               files=[("files", ("fleet.gpx", GPX, "application/gpx+xml"))])
    assert r.status_code == 200 and r.json()["race_id"] == "orc-na-2027" and r.json()["tracks"] == 2
    r = c.post("/api/retro/upload?def_race_id=orc-na-2027", headers=h,
               files=[("files", ("f.csv", b"x,y\n1,2\n", "text/csv"))])
    assert r.status_code == 400 and "boat, time, lat, lon" in r.json()["detail"]
    r = c.post("/api/retro/upload?def_race_id=nope", headers=h,
               files=[("files", ("fleet.gpx", GPX, "application/gpx+xml"))])
    assert r.status_code == 404
    # the debrief "Get from tracker" reads our boat from the uploaded file
    r = c.post("/api/debrief/track/fetch", headers=h, json={"race_id": "orc-na-2027", "boat": "Bravo"})
    assert r.status_code == 200 and r.json()["ok"]


# ---- re-uploading a fleet file (Codex on #345) ---------------------------------------------
def _csv(*rows, header="boat,sail,division,rank,time,lat,lon"):
    out = header + "\n"
    for boat, sail, rank in rows:
        out += f"{boat},{sail},A,{rank},2027-11-15T16:00:00Z,26.10,-80.10\n"
        out += f"{boat},,,,2027-11-15T16:10:00Z,26.11,-80.09\n"
    return out.encode()


def _ids(rid="orc-na-2027"):
    return {e["boat"]: e["team_id"] for e in rs.get_entries(rid)}


def _analyse(rid="orc-na-2027"):
    """Stand-in for steps 2 and 3: a polar and a scored run per boat, tagged with its name."""
    for boat, tid in _ids(rid).items():
        rs.save_polar(rid, tid, None, [[boat]], "sail", 0.9)
        run = rs.save_run(rid, tid, "gun_forecast", {}, {}, {"boat": boat})
        rs.save_score(rid, tid, run, {"boat": boat})


def test_reupload_in_another_order_keeps_each_boats_id():
    d = _race()
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 1", 1), ("Charlie", "USA 2", 2)))], d)
    before = _ids()
    _analyse()
    r = retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Charlie", "USA 2", 2), ("Bravo", "USA 1", 1)))], d)
    assert _ids() == before
    for boat, tid in before.items():
        assert rs.get_polar("orc-na-2027", tid) == [[boat]]          # its own polar, not the other's
    assert rs.get_scores("orc-na-2027") == []                        # runs/scores rebuilt by a re-score
    assert r["cleared"] == {"runs": 2, "polars": 0} and retro.RESCORE in r["note"]


def test_reupload_matches_by_name_when_there_is_no_sail():
    d = _race()
    retro.ingest_file("orc-na-2027", [("fleet.gpx", GPX)], d)
    before = _ids()
    swapped = GPX.replace(b"<name>Bravo</name>", b"<name>TMP</name>") \
                 .replace(b"<name>Charlie</name>", b"<name>Bravo</name>") \
                 .replace(b"<name>TMP</name>", b"<name>Charlie</name>")
    retro.ingest_file("orc-na-2027", [("fleet.gpx", swapped)], d)
    assert _ids() == before


def test_reupload_without_ranks_drops_the_old_ranks():
    d = _race()
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 1", 1), ("Charlie", "USA 2", 2)))], d)
    assert len(rs.get_results("orc-na-2027")) == 2
    r = retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 1", ""), ("Charlie", "USA 2", "")))], d)
    assert r["results"] == 0 and rs.get_results("orc-na-2027") == []
    assert retro.report("orc-na-2027").get("divisions") in (None, [])


def test_reupload_without_a_boat_removes_it():
    d = _race()
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 1", 1), ("Charlie", "USA 2", 2)))], d)
    _analyse()
    charlie = _ids()["Charlie"]
    r = retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 1", 1)))], d)
    assert list(_ids()) == ["Bravo"] and r["cleared"]["polars"] == 1
    assert rs.get_track("orc-na-2027", charlie) is None and rs.get_polar("orc-na-2027", charlie) is None
    assert [e["boat"] for e in trackers.entries(d)["entries"]] == ["Bravo"]
    assert trackers.boat_track(d, "Charlie")["ok"] is False


def test_reupload_with_a_new_sail_drops_that_boats_polar():
    d = _race()
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 1", 1), ("Charlie", "USA 2", 2)))], d)
    _analyse()
    ids = _ids()
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 11", 1), ("Charlie", "USA 2", 2)))], d)
    assert _ids() == ids                                             # same boat, matched by name
    assert rs.get_polar("orc-na-2027", ids["Bravo"]) is None         # its cert match may be wrong now
    assert rs.get_polar("orc-na-2027", ids["Charlie"]) == [["Charlie"]]


def test_new_boats_get_fresh_ids():
    d = _race()
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 1", 1), ("Charlie", "USA 2", 2)))], d)
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 1", 1), ("Delta", "USA 3", 2)))], d)
    assert _ids() == {"Bravo": 1, "Delta": 3}                        # Charlie's id is not reused


# ---- Codex round 3 on #345 -----------------------------------------------------------------
def test_a_named_division_without_a_gun_never_takes_the_rosters():
    """CSV says Bravo is in B; the roster says A; only A has a gun → no start, not A's."""
    d = _race(division_starts={"A": "2027-11-15T10:00:00"},
              fleet=[{"boat": "Bravo", "sail": "USA 1", "division": "A"}])
    csv = (b"boat,sail,division,time,lat,lon\n"
           b"Bravo,USA 1,B,2027-11-15T16:00:00Z,26.1,-80.1\n"
           b"Bravo,USA 1,B,2027-11-15T16:10:00Z,26.2,-80.1\n")
    f = trackers.parse_fleet_files([("f.csv", csv)], d)
    t = f["teams"][0]
    assert t["division"] == "B" and t["start"] is None and f["no_start"] == ["Bravo"]
    assert "No start for Bravo. Not scored." in f["results_note"]
    # the file's spelling of a division with a gun still resolves to the gun's spelling
    f = trackers.parse_fleet_files([("f.csv", csv.replace(b",B,", b",a,"))], d)
    assert f["teams"][0]["division"] == "A" and f["teams"][0]["start"] == 1_826_290_800.0


def test_polar_match_racing_a_reupload_saves_nothing(monkeypatch):
    """Bravo is re-uploaded with a new sail while the ORC dumps load: its polar was cleared, and
    the pending matcher must not write the old cert's polar back onto Bravo's id."""
    d = _race()
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 1", 1), ("Charlie", "USA 2", 2)))], d)
    _analyse()
    bravo = _ids()["Bravo"]
    rows = [{"SailNo": "USA 1", "YachtName": "Bravo", "RefNo": "u1"},
            {"SailNo": "USA 2", "YachtName": "Charlie", "RefNo": "u2"}]

    def dump(cc):
        retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 11", 1), ("Charlie", "USA 2", 2)))], d)
        return {"by_sail": {fi._norm_sail(r["SailNo"]): r for r in rows},
                "by_name": {fi._norm(r["YachtName"]): r for r in rows}, "n": len(rows)}
    monkeypatch.setattr(fi, "_orc_dump", dump)
    monkeypatch.setattr(retro.orcpolar, "cert_polar", lambda rec: [["old cert"]])
    r = retro.match_polars("orc-na-2027", country="USA")
    assert r["ok"] is False and r["stale"] and r["note"] == retro.STALE_POLARS and r["matched"] == 0
    assert rs.get_polar("orc-na-2027", bravo) is None                # cleared by the re-upload, stays so
    assert rs.get_polar("orc-na-2027", _ids()["Charlie"]) == [["Charlie"]]


def test_polar_match_without_a_reimport_still_saves(monkeypatch):
    d = _race()
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 1", 1)))], d)
    rows = [{"SailNo": "USA 1", "YachtName": "Bravo", "RefNo": "u1"}]
    monkeypatch.setattr(fi, "_orc_dump", lambda cc: {"by_sail": {"usa1": rows[0]}, "by_name": {}, "n": 1})
    monkeypatch.setattr(retro.orcpolar, "cert_polar", lambda rec: [[1, 2, 3]])
    r = retro.match_polars("orc-na-2027", country="USA")
    assert r["ok"] and r["matched"] == 1 and rs.get_polar("orc-na-2027", _ids()["Bravo"]) == [[1, 2, 3]]


def test_yb_reingest_stops_a_pending_polar_match(monkeypatch):
    """A YB import is one transaction with a new stamp, like a file re-upload."""
    _stub_yb(monkeypatch)
    retro.ingest_race("demo2026")
    rows = [{"SailNo": "USA 1", "YachtName": "Bravo", "RefNo": "u1"}]

    def dump(cc):
        retro.ingest_race("demo2026")
        return {"by_sail": {"usa1": rows[0]}, "by_name": {}, "n": 1}
    monkeypatch.setattr(fi, "_orc_dump", dump)
    monkeypatch.setattr(retro.orcpolar, "cert_polar", lambda rec: [[1, 2, 3]])
    assert retro.match_polars("demo2026", country="USA")["stale"]


# ---- Codex minors on #345 ------------------------------------------------------------------
def test_sail_matches_are_reserved_before_name_matches():
    """Archive: Bravo/USA 1 = id 1. New file: Bravo now sails USA 3, and Renamed holds USA 1.
    Whatever the row order, USA 1 keeps id 1 (by sail) and Bravo/USA 3 is a new boat."""
    d = _race()
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 1", 1)))], d)
    assert _ids() == {"Bravo": 1}
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 3", 1), ("Renamed", "USA 1", 2)))], d)
    first = _ids()
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Bravo", "USA 1", 1)))], d)       # reset
    retro.ingest_file("orc-na-2027", [("f.csv", _csv(("Renamed", "USA 1", 2), ("Bravo", "USA 3", 1)))], d)
    assert first == _ids() and first["Renamed"] == 1 and first["Bravo"] != 1


def test_gpx_time_without_a_zone_uses_the_race_zone():
    naive = GPX.replace(b"Z</time>", b"</time>")
    f = trackers.parse_fleet_files([("fleet.gpx", naive)], _race())          # America/New_York
    assert f["tracks"][1][0]["t"] == 1_826_312_400.0                          # 16:00 New York = 21:00Z
    with pytest.raises(ValueError, match="GPX times have no zone"):
        trackers.parse_fleet_files([("fleet.gpx", naive)], _race(timezone=""))
    zoned = GPX.replace(b"Z</time>", b"+01:00</time>")
    f = trackers.parse_fleet_files([("fleet.gpx", zoned)], _race(timezone=""))
    assert f["tracks"][1][0]["t"] == 1_826_294_400.0 - 3600


def test_upload_over_the_limit_is_refused(monkeypatch, archive):
    from fastapi.testclient import TestClient
    from app import auth, main
    monkeypatch.setattr(store, "get_race", lambda rid: _race() if rid == "orc-na-2027" else None)
    monkeypatch.setattr(trackers, "MAX_FILE_BYTES", len(GPX) + 10)
    read = []
    from starlette.datastructures import UploadFile
    real = UploadFile.read

    async def spy(self, size=-1):
        read.append(size)
        return await real(self, size)
    monkeypatch.setattr(UploadFile, "read", spy)
    c = TestClient(main.app)
    h = {"Authorization": "Bearer " + auth.issue_token()}
    big = [("files", ("a.gpx", GPX, "application/gpx+xml")), ("files", ("b.gpx", GPX, "application/gpx+xml"))]
    r = c.post("/api/retro/upload?def_race_id=orc-na-2027", headers=h, files=big)
    assert r.status_code == 413 and r.json()["detail"] == "File too big."
    assert read == [len(GPX) + 11, 11]                 # never more than what's left + 1 byte
    assert rs.get_race("orc-na-2027") is None
    r = c.post("/api/retro/upload?def_race_id=orc-na-2027", headers=h, files=big[:1])
    assert r.status_code == 200
