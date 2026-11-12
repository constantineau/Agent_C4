"""Fleet retro has no built-in race (#310, map #316).

The retro routes the whole fleet on a race definition's course. Before #310 a run with no
definition silently used the Bayview course, the card started on a hard-coded YB race and the ORC
search was always USA + CAN. Now: no definition → a plain refusal; the course, the YB id and the
ORC countries come from the race the user picks. Bayview, when picked, still routes on its first
course and searches USA + CAN.

No network: ORC dumps and the retro archive are stubbed.
Run in the Lab image: python3 -m pytest -q vps/lab/test_retro_race_defaults.py
"""
import json
import os
import re

import pytest

from app import fleetimport as fi
from app import retro, store
from app import retrostore as rs

HERE = os.path.dirname(os.path.abspath(__file__))
BAYVIEW = json.load(open(os.path.join(HERE, "races", "bayview_mackinac_2026.json")))


def _channel(**over):
    d = {"race_id": "channel-race-2027", "venue_country": "GBR",
         "rules_profile": {"scoring": {"rating_system": "ORC"}},
         "own": {"division": "IRC 2"},
         "divisions": [{"id": "IRC 1", "course_ref": "long"}, {"id": "IRC 2", "course_ref": "short"}],
         "courses": [{"id": "long", "name": "Long"}, {"id": "short", "name": "Short"}],
         "tracker": {"provider": "yb", "race": "channel2027"}}
    d.update(over)
    return d


def test_run_without_a_race_refuses_plainly():
    assert retro.run_fleet("anyyb")["note"] == retro.NO_RACE
    assert retro.run_fleet("anyyb", def_race_id="")["ok"] is False


def test_start_without_a_race_starts_no_job(monkeypatch):
    started = []
    monkeypatch.setattr(retro.jobs, "start", lambda *a, **k: started.append(a) or {"ok": True})
    assert retro.start_fleet_job("anyyb", None, None, None, None, "auto")["note"] == retro.NO_RACE
    monkeypatch.setattr(store, "get_race", lambda rid: None)
    r = retro.start_fleet_job("anyyb", "no-such-race", None, None, None, "auto")
    assert r["ok"] is False and "library" in r["note"]
    assert started == []


def test_run_routes_on_the_picked_races_course(monkeypatch):
    """The course comes from the picked definition: our division's, else the first."""
    seen = {}
    monkeypatch.setattr(store, "get_race", lambda rid: _channel() if rid == "channel-race-2027" else None)
    monkeypatch.setattr(rs, "get_race", lambda rid: {"race_id": rid, "start_epoch": 0})

    def marks(d, cid):
        seen["course"] = cid
        return [], [], cid
    monkeypatch.setattr(retro.race_def, "course_to_marks", marks)
    r = retro.run_fleet("channel2027", def_race_id="channel-race-2027")
    assert seen["course"] == "short"          # own division IRC 2 sails "short"
    assert r["note"] == retro.NO_COURSE       # stub course has no marks → plain refusal


def test_default_course():
    assert retro.default_course(_channel()) == "short"
    assert retro.default_course(_channel(own={})) == "long"
    assert retro.default_course({"courses": []}) is None
    assert retro.default_course(BAYVIEW) == "cove_island"     # unchanged for Bayview


def test_setup_reads_the_race():
    su = retro.setup_for(_channel())
    assert su["yb_id"] == "channel2027" and su["note"] is None
    assert su["course_id"] == "short" and [c["id"] for c in su["courses"]] == ["long", "short"]
    assert su["countries"] == ["GBR"]
    bv = retro.setup_for(BAYVIEW)
    assert bv["course_id"] == "cove_island" and bv["countries"] == ["USA", "CAN"]


def test_setup_other_tracker_says_so():
    su = retro.setup_for(_channel(tracker={"provider": "tractrac", "url": "https://example.invalid"}))
    assert su["yb_id"] is None and "YB" in su["note"] and "tractrac" in su["note"]
    su = retro.setup_for(_channel(tracker={}))
    assert su["yb_id"] is None and su["note"] == "No YB race id in this race. Type one."


def _stub_polars(monkeypatch, entries, dumps):
    asked, saved = [], []

    def dump(cc):
        asked.append(cc)
        rows = dumps.get(cc, [])
        return {"by_sail": {fi._norm_sail(r["SailNo"]): r for r in rows},
                "by_name": {fi._norm(r["YachtName"]): r for r in rows}, "n": len(rows)}
    monkeypatch.setattr(fi, "_orc_dump", dump)
    monkeypatch.setattr(rs, "get_entries", lambda rid: entries)
    monkeypatch.setattr(rs, "save_cert", lambda cc, ref, *a: saved.append(cc) or len(saved))
    monkeypatch.setattr(rs, "save_polar", lambda *a: None)
    monkeypatch.setattr(retro.orcpolar, "cert_polar", lambda rec: [[1, 2, 3]])
    return asked, saved


def test_polars_search_the_races_countries(monkeypatch):
    entries = [{"team_id": 1, "boat": "Bravo", "sail": "GBR 5288"},
               {"team_id": 2, "boat": "Visitor", "sail": "FRA 12"}]
    dumps = {"GBR": [{"SailNo": "GBR 5288", "YachtName": "Bravo", "RefNo": "g1"}],
             "FRA": [{"SailNo": "FRA 12", "YachtName": "Visitor", "RefNo": "f1"}]}
    asked, saved = _stub_polars(monkeypatch, entries, dumps)
    monkeypatch.setattr(store, "get_race", lambda rid: _channel())
    r = retro.match_polars("channel2027", def_race_id="channel-race-2027")
    assert asked == ["GBR", "FRA"] and "USA" not in asked     # venue first, then sail prefixes
    assert r["matched"] == 2 and r["countries"] == ["GBR", "FRA"] and saved == ["GBR", "FRA"]


def test_polars_sail_match_beats_country_order(monkeypatch):
    """CAN 12 "Bravo": a USA boat also named Bravo must not win over the exact Canadian sail match."""
    entries = [{"team_id": 1, "boat": "Bravo", "sail": "CAN 12"},
               {"team_id": 2, "boat": "Solo", "sail": "XYZ 1"}]
    dumps = {"USA": [{"SailNo": "USA 99", "YachtName": "Bravo", "RefNo": "u1"},
                     {"SailNo": "USA 7", "YachtName": "Solo", "RefNo": "u2"}],
             "CAN": [{"SailNo": "CAN 12", "YachtName": "Bravo", "RefNo": "c1"},
                     {"SailNo": "CAN 8", "YachtName": "Solo", "RefNo": "c2"}]}
    asked, saved = _stub_polars(monkeypatch, entries, dumps)
    r = retro.match_polars("x", country="USA,CAN")
    assert r["matched"] == 2
    assert saved == ["CAN", "USA"]     # Bravo by its CAN sail; Solo by name, first country wins


def test_fleet_import_sail_match_beats_country_order(monkeypatch):
    """#341: the Gameplan fleet import (enrich_from_orc) as the retro above: CAN 12 "Bravo" must
    not take a USA "Bravo"'s cert; a sail number in any country beats a name in an earlier one."""
    _stub_polars(monkeypatch, [], {
        "USA": [{"SailNo": "USA 99", "YachtName": "Bravo", "RefNo": "u1"},
                {"SailNo": "USA 7", "YachtName": "Solo", "RefNo": "u2"}],
        "CAN": [{"SailNo": "CAN 12", "YachtName": "Bravo", "RefNo": "c1"},
                {"SailNo": "CAN 8", "YachtName": "Solo", "RefNo": "c2"}]})
    entries = [{"boat": "Bravo", "sail": "CAN 12"}, {"boat": "Solo", "sail": "XYZ 1"}]
    r = fi.enrich_from_orc(entries, country="USA,CAN")
    assert r["matched"] == 2
    got = [(e["orc_match"]["by"], e["orc_match"]["country"], e["orc_match"]["sail"])
           for e in r["entries"]]
    assert got == [("sail", "CAN", "CAN 12"), ("name", "USA", "USA 7")], got


def test_polars_without_a_race_use_sail_numbers(monkeypatch):
    asked, _ = _stub_polars(monkeypatch, [{"team_id": 1, "boat": "B", "sail": "AUS 9"}], {})
    r = retro.match_polars("x")
    assert asked == ["AUS"] and r["ok"] and r["matched"] == 0
    asked, _ = _stub_polars(monkeypatch, [{"team_id": 1, "boat": "B", "sail": ""}], {})
    assert retro.match_polars("x")["note"] == fi.COUNTRY_UNKNOWN and asked == []


def test_polars_typed_country_wins(monkeypatch):
    asked, _ = _stub_polars(monkeypatch, [{"team_id": 1, "boat": "B", "sail": "AUS 9"}], {})
    retro.match_polars("x", country="NZL,AUS")
    assert asked == ["NZL", "AUS"]


def test_orc_dump_has_no_country_default():
    with pytest.raises(ValueError):
        fi._orc_dump("")


def test_no_race_names_in_retro_code():
    for rel in ("app/retro.py", "app/pbbacktest.py", "web/app.js"):
        src = open(os.path.join(HERE, rel)).read()
        assert not re.search(r"bayviewmack20\d\d|bayview-mackinac-20\d\d|cove_island", src), rel
    js = open(os.path.join(HERE, "web", "app.js")).read()
    assert '["USA", "CAN"]' not in js
    main = open(os.path.join(HERE, "app", "main.py")).read()
    assert '"bayview-mackinac-2026"' not in main and '"cove_island"' not in main
