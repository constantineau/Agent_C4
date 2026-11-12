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


def test_one_gpx_per_boat_takes_the_file_name():
    one = b'<gpx><trk><trkseg><trkpt lat="1" lon="2"><time>2027-01-01T00:00:00Z</time></trkpt>' \
          b'<trkpt lat="1.1" lon="2"><time>2027-01-01T00:01:00Z</time></trkpt></trkseg></trk></gpx>'
    f = trackers.parse_fleet_files([("Bravo.gpx", one), ("Delta.gpx", one)], _race(division_starts={}))
    assert [t["name"] for t in f["teams"]] == ["Bravo", "Delta"] and f["start"] == 1798761600.0


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
