"""Monitor fleet + debrief GPX through the race's tracker (#310, map #316).

The Monitor's fleet map read YB only. It now asks the race's tracker provider
(`trackers.live`): YB fetches live positions as before, a fleet-file race shows each boat's last
position from the uploaded file (never called live), and any other tracker says plainly it has
no live positions. The debrief's single-boat GPX upload reads a time with no zone in the race's
time zone, and refuses plainly when the race has none (as the fleet file does since #347).

No network: YB is stubbed, the retro archive is a temp SQLite file.
Run in the Lab image: python3 -m pytest -q vps/lab/test_monitor_trackers.py
"""
import os
import tempfile
import time

import pytest

from app import monitor, retro, store, track, trackers
from app import retrostore as rs


@pytest.fixture(autouse=True)
def archive(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setattr(rs, "RETRO_DB", os.path.join(d, "retro.db"))
    return d


def _race(**over):
    d = {"race_id": "regatta-x", "timezone": "America/New_York",
         "division_starts": {"A": "2027-11-15T11:00:00"},
         "courses": [{"id": "wl", "name": "W/L"}],
         "tracker": {"provider": "file"}, "own": {"boat": "Bravo"}}
    d.update(over)
    return d


GPX = b"""<?xml version="1.0"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1">
 <trk><name>Bravo</name><trkseg>
  <trkpt lat="26.10" lon="-80.10"><time>2025-11-15T16:00:00Z</time></trkpt>
  <trkpt lat="26.11" lon="-80.09"><time>2025-11-15T16:10:00Z</time></trkpt>
 </trkseg></trk>
 <trk><name>Charlie</name><trkseg>
  <trkpt lat="26.10" lon="-80.11"><time>2025-11-15T16:00:00Z</time></trkpt>
  <trkpt lat="26.12" lon="-80.12"><time>2025-11-15T16:20:00Z</time></trkpt>
 </trkseg></trk>
</gpx>"""


def _no_http(monkeypatch):
    def boom(url, **kw):
        raise AssertionError(f"no fetch expected, got {url}")
    monkeypatch.setattr(monitor, "_http_json", boom)


# ---- Monitor fleet ---------------------------------------------------------------------------
def test_yb_race_still_fetches_live(monkeypatch):
    now_ms = time.time() * 1000
    asked = []

    def get(url, **kw):
        asked.append(url)
        return {"teams": [{"name": "Bravo", "positions": [
            {"latitude": 44.0, "longitude": -82.5, "gpsAtMillis": now_ms - 600_000, "sogKnots": 6.1}]}]}
    monkeypatch.setattr(monitor, "_http_json", get)
    f = monitor.fleet(_race(tracker={"provider": "yb", "race": "abc", "delay_min": 15}))
    assert asked == ["https://cf.yb.tl/API3/Race/abc/GetPositions?t=0"]
    assert [x["name"] for x in f["fixes"]] == ["Bravo"] and f["fixes"][0]["own"]
    assert f["label"] == "YB" and f["live"] is True and f["freshness"] == "fresh"


def test_yb_failures_say_why(monkeypatch):
    def fail(url, **kw):
        raise OSError("down")
    monkeypatch.setattr(monitor, "_http_json", fail)
    assert monitor.fleet(_race(tracker={"provider": "yb", "race": "abc"}))["reason"] == \
        "Tracker fetch failed: OSError"
    _no_http(monkeypatch)
    assert monitor.fleet(_race(tracker={"provider": "yb"}))["reason"] == "No YB race id set for this race."
    monkeypatch.setattr(monitor, "_http_json", lambda url, **kw: {"error": "x"})
    assert monitor.fleet(_race(tracker={"provider": "yb", "race": "abc"}))["reason"] == \
        "Race not live on the tracker yet."


def test_file_race_shows_last_positions_from_the_file(monkeypatch):
    _no_http(monkeypatch)
    d = _race()
    retro.ingest_file("regatta-x", [("fleet.gpx", GPX)], d)
    f = monitor.fleet(d)
    by = {x["name"]: x for x in f["fixes"]}
    assert set(by) == {"Bravo", "Charlie"}
    assert (by["Charlie"]["lat"], by["Charlie"]["lon"]) == (26.12, -80.12)        # its LAST fix
    assert by["Charlie"]["time"] == 1_763_223_600.0                                # 16:20Z
    assert by["Bravo"]["own"] and not by["Charlie"].get("own")
    assert f["label"] == "fleet file" and f["live"] is False and f["reason"] == ""
    assert f["freshness"] == "very_stale"            # an old file reads as not live


def test_file_race_with_no_file_says_so(monkeypatch):
    _no_http(monkeypatch)
    f = monitor.fleet(_race())
    assert f["fixes"] == [] and f["reason"] == trackers.NO_FILE_LIVE


def test_other_tracker_says_it_has_no_live_positions(monkeypatch):
    _no_http(monkeypatch)
    f = monitor.fleet(_race(tracker={"provider": "tractrac", "url": "https://example.invalid"}))
    assert f["fixes"] == [] and f["reason"] == trackers.no_live_note("tractrac")
    assert "No live positions from tractrac" in f["reason"]
    assert monitor.fleet(_race(tracker={}))["reason"] == "No tracker set for this race."


def test_monitor_endpoint_routes_through_the_provider(monkeypatch):
    from fastapi.testclient import TestClient
    from app import auth, main
    _no_http(monkeypatch)
    d = _race()
    retro.ingest_file("regatta-x", [("fleet.gpx", GPX)], d)
    monkeypatch.setattr(store, "get_race", lambda rid: d if rid == "regatta-x" else None)
    monkeypatch.setattr(monitor, "own", lambda: {"available": False, "reason": "test"})
    h = {"Authorization": "Bearer " + auth.issue_token()}
    r = TestClient(main.app).get("/api/monitor", params={"race_id": "regatta-x"}, headers=h)
    assert r.status_code == 200, r.text
    assert len(r.json()["fleet"]["fixes"]) == 2


# ---- debrief GPX: zoneless times ---------------------------------------------------------------
ONE = b"""<gpx><trk><trkseg>
 <trkpt lat="26.10" lon="-80.10"><time>2027-11-15T11:00:00</time></trkpt>
 <trkpt lat="26.11" lon="-80.09"><time>2027-11-15T11:10:00</time></trkpt>
</trkseg></trk></gpx>"""


def test_gpx_time_without_a_zone_uses_the_race_zone():
    t = track.parse_gpx(ONE, "America/New_York")
    assert t["fixes"][0]["t"] == 1_826_294_400.0                    # 11:00 New York = 16:00Z


def test_gpx_time_without_a_zone_and_no_race_zone_is_refused():
    with pytest.raises(ValueError, match="GPX times have no zone"):
        track.parse_gpx(ONE, "")
    with pytest.raises(ValueError, match="GPX times have no zone"):
        track.parse_gpx(ONE)


def test_gpx_zoned_times_ignore_the_race_zone():
    z = ONE.replace(b":00</time>", b":00Z</time>")
    assert track.parse_gpx(z, "")["fixes"][0]["t"] == 1_826_276_400.0       # 11:00Z
    assert track.parse_gpx(z, "Asia/Tokyo")["fixes"][0]["t"] == 1_826_276_400.0


def test_upload_reads_times_in_the_race_zone(monkeypatch):
    from fastapi.testclient import TestClient
    from app import auth, main
    saved = []
    monkeypatch.setattr(track, "save_track", lambda rid, trk: saved.append(trk) or {"n": trk["n"]})
    races = {"r-zone": _race(), "r-nozone": _race(timezone="")}
    monkeypatch.setattr(store, "get_race", lambda rid: races.get(rid))
    c = TestClient(main.app)
    h = {"Authorization": "Bearer " + auth.issue_token()}
    r = c.post("/api/debrief/track/upload", params={"race_id": "r-zone"}, headers=h,
               files={"file": ("ours.gpx", ONE, "application/gpx+xml")})
    assert r.status_code == 200, r.text
    assert saved[0]["fixes"][0]["t"] == 1_826_294_400.0
    r = c.post("/api/debrief/track/upload", params={"race_id": "r-nozone"}, headers=h,
               files={"file": ("ours.gpx", ONE, "application/gpx+xml")})
    assert r.status_code == 400 and r.json()["detail"] == track.GPX_NO_ZONE
