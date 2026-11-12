"""Fleet import works on the roster ON SCREEN, not just the last save (issue #85).

The Fleet tab sends its draft roster (unsaved edits included) with an import. 'Enrich from ORC' must
enrich that draft — a boat added by hand and not yet saved has to survive — and a re-import must carry
MMSIs seeded in the draft over. With no draft sent, the saved roster is used as before.
Offline: the network is faked with the same fixtures as test_fleet_import.py.
Run in-container: python test_fleet_import_draft.py
"""
import asyncio
import json
import os
import sys
import tempfile

os.environ["INGESTED_DIR"] = tempfile.mkdtemp()

from app import fleetimport, main  # noqa: E402

_ORC = json.dumps({"rms": [
    {"SailNo": "USA 60", "YachtName": "Il Mostro", "Class": "VOR70", "GPH": 500.0, "TMF_Offshore": 1.10},
    {"SailNo": "USA 5", "YachtName": "Hand Added", "Class": "J/111", "GPH": 610.0, "TMF_Offshore": 0.98},
]}).encode("utf-8-sig")
_SETUP = json.dumps({"teams": [
    {"name": "Il Mostro", "sail": "USA 60", "model": "VOR70"},
    {"name": "Hand Added", "sail": "USA 5", "model": "J/111"},
]}).encode("utf-8")

STORED = {"race_id": "t-race", "tracker": {"provider": "yb", "race": "demo2026"},
          "fleet": [{"boat": "Il Mostro", "sail": "USA 60"}]}


def _setup():
    fleetimport._get = lambda url: _SETUP if "RaceSetup" in url else _ORC
    fleetimport._orc_cache.clear()
    main.store.get_race = lambda rid: json.loads(json.dumps(STORED)) if rid == "t-race" else None


def _import(body):
    return asyncio.run(main.fleet_import(body))


def test_with_draft_fleet():
    assert main._with_draft_fleet(STORED, None) is STORED
    assert main._with_draft_fleet(STORED, "not json") is STORED
    assert main._with_draft_fleet(STORED, {"boat": "x"}) is STORED
    d = main._with_draft_fleet(STORED, json.dumps([{"boat": "A"}, "junk"]))
    assert d["fleet"] == [{"boat": "A"}] and STORED["fleet"][0]["boat"] == "Il Mostro"


def test_orc_enrich_uses_the_draft():
    _setup()
    draft = [{"boat": "Il Mostro", "sail": "USA 60"}, {"boat": "Hand Added", "sail": "USA 5"}]
    r = _import({"race_id": "t-race", "source": "orc", "country": "USA", "fleet": draft})
    assert r["ok"] and r["count"] == 2, r
    added = next(e for e in r["entries"] if e["boat"] == "Hand Added")
    assert added["orc_gph"] == 610.0, added


def test_orc_enrich_without_draft_uses_saved():
    _setup()
    r = _import({"race_id": "t-race", "source": "orc", "country": "USA"})
    assert r["ok"] and r["count"] == 1, r


def test_reimport_keeps_mmsi_seeded_in_the_draft():
    _setup()
    draft = [{"boat": "Hand Added", "sail": "USA 5", "mmsi": "366555555"}]
    r = _import({"race_id": "t-race", "source": "both", "country": "USA", "fleet": draft})
    assert r["ok"] and r.get("seeded_mmsi") == 1, r
    assert next(e for e in r["entries"] if e["boat"] == "Hand Added")["mmsi"] == "366555555"


def test_yb_only_reimport_keeps_seeded_mmsi():
    _setup()
    draft = [{"boat": "Il Mostro", "sail": "USA 60", "mmsi": "366111111"}]
    r = _import({"race_id": "t-race", "source": "yb", "fleet": draft})
    assert r["ok"] and r.get("seeded_mmsi") == 1, r
    assert next(e for e in r["entries"] if e["boat"] == "Il Mostro")["mmsi"] == "366111111"


def test_reread_uses_the_unsaved_rating_column():
    # #311: pick a column on screen, don't save, re-read → the new column is the one read
    _setup()
    draft = [{"boat": "Il Mostro", "sail": "USA 60", "rating": 1.10}]
    saved_sc = {"rating_system": "ORC", "rating_column": "TMF_Offshore"}
    stored = dict(STORED, rules_profile={"scoring": saved_sc})
    main.store.get_race = lambda rid: json.loads(json.dumps(stored)) if rid == "t-race" else None
    r = _import({"race_id": "t-race", "source": "orc", "country": "USA", "fleet": draft,
                 "scoring": {"rating_system": "ORC", "rating_column": "TMF_Inshore"}})
    assert r["tot_col"] == "TMF_Inshore", r
    e = r["entries"][0]
    assert e["rating"] is None and e["orc_match"]["col"] == "TMF_Inshore", e   # cert has no inshore
    # no draft scoring sent → the saved column, as before
    r = _import({"race_id": "t-race", "source": "orc", "country": "USA", "fleet": draft})
    assert r["tot_col"] == "TMF_Offshore" and r["entries"][0]["rating"] == 1.10, r
    # the draft own division picks the course for a per-course column
    stored["divisions"] = [{"id": "A", "course_ref": "long"}, {"id": "B", "course_ref": "short"}]
    per = {"rating_system": "ORC", "course_columns": [
        {"course": "long", "column": "TMF_Offshore", "column_tod": ""},
        {"course": "short", "column": "TMF_Inshore", "column_tod": ""}]}
    r = _import({"race_id": "t-race", "source": "orc", "country": "USA", "fleet": draft,
                 "scoring": per, "own": {"division": "A"}})
    assert r["tot_col"] == "TMF_Offshore", r
    # the upload form sends them as JSON strings; junk keeps the saved values
    d = main._with_draft_rating(stored, json.dumps({"rating_column": "X"}), "not json")
    assert d["rules_profile"]["scoring"] == {"rating_column": "X"} and "own" not in d, d
    assert main._with_draft_rating(stored, None, None) is stored
    assert stored["rules_profile"]["scoring"] is saved_sc                      # stored not mutated


def test_candidates_use_the_unsaved_rating_column():
    _setup()
    main.store.get_race = lambda rid: json.loads(json.dumps(
        dict(STORED, rules_profile={"scoring": {"rating_system": "ORC"}}))) if rid == "t-race" else None
    r = main.fleet_orc_candidates({"race_id": "t-race", "boat": "Il Mostro", "sail": "USA 60",
                                   "country": "USA",
                                   "scoring": {"rating_system": "ORC", "rating_column": "TMF_Offshore"}})
    assert r["tot_col"] == "TMF_Offshore" and r["candidates"][0]["rating"] == 1.10, r


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1; print("ok", name)
    print(f"{n} passed")
    sys.exit(0)
