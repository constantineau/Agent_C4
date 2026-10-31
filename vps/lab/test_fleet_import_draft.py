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


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1; print("ok", name)
    print(f"{n} passed")
    sys.exit(0)
