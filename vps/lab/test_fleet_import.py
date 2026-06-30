"""Automated fleet-roster ingestion — YB entry-list parse + ORC handicap match.

Deterministic + offline: monkeypatches the network with fixture RaceSetup + ORC-dump bytes and asserts
the entry-list parse, the sail-then-name ORC match, and race-specific ToT column selection (BAYMAC).
A separate LIVE check (network) hits the real YB + ORC endpoints — see __main__ --live.
Run in-container: docker cp ... && docker compose ... exec ... python test_fleet_import.py
"""
import json
import sys

from app import fleetimport

_SETUP = json.dumps({"teams": [
    {"name": "Il Mostro", "sail": "USA 60", "owner": "Puma", "model": "VOR70"},
    {"name": "Windquest", "sail": "USA 12", "model": "Sy 52", "captain": "Doug D."},
    {"name": "Unrated Joe", "sail": "USA 777", "model": "X"},
]}).encode("utf-8")

# ORC dump (note: real ORC serves a UTF-8 BOM → _orc_dump decodes utf-8-sig)
_ORC = json.dumps({"rms": [
    {"SailNo": "USA 60", "YachtName": "Il Mostro", "Class": "VOR70", "GPH": 500.0,
     "TMF_Offshore": 1.10, "US_BAYMAC_CV_TOT": 0.95},
    {"SailNo": "USA 99", "YachtName": "Windquest", "Class": "Santa Cruz 52", "GPH": 560.0,
     "TMF_Offshore": 1.05, "US_BAYMAC_CV_TOT": 1.02},
]}).encode("utf-8-sig")


def _fake_net():
    def _get(url):
        return _SETUP if "RaceSetup" in url else _ORC
    fleetimport._get = _get
    fleetimport._orc_cache.clear()


def test_roster_from_yb():
    _fake_net()
    d = {"tracker": {"provider": "yb", "race": "demo2026"}}
    r = fleetimport.roster_from_yb(d)
    assert r["ok"] and r["count"] == 3, r
    e0 = r["entries"][0]
    assert e0["boat"] == "Il Mostro" and e0["sail"] == "USA 60" and e0["cls"] == "VOR70" and e0["source"] == "yb", e0
    print(f"PASS roster_from_yb: {r['count']} entries with sail#/class/owner")


def test_orc_match_and_race_col():
    _fake_net()
    d = {"race_id": "bayview-mackinac-2026", "name": "Bayview Mackinac"}
    entries = fleetimport.roster_from_yb({"tracker": {"provider": "yb", "race": "demo2026"}})["entries"]
    en = fleetimport.enrich_from_orc(entries, country="USA", definition=d, course_id="cove_island")
    assert en["ok"] and en["matched"] == 2, en          # Il Mostro by sail, Windquest by name
    bysail = next(e for e in entries if e["boat"] == "Il Mostro")
    byname = next(e for e in entries if e["boat"] == "Windquest")
    unmatched = next(e for e in entries if e["boat"] == "Unrated Joe")
    # race-specific column used (US_BAYMAC_CV_TOT), NOT the generic TMF_Offshore
    assert bysail["rating"] == 0.95 and bysail["orc_gph"] == 500.0, bysail
    assert byname["rating"] == 1.02 and byname["cls"] == "Sy 52", byname  # name match; YB class kept (not clobbered)
    assert byname["orc_match"]["by"] == "name", byname
    assert unmatched.get("rating") is None and unmatched.get("orc_match") is None, unmatched
    print(f"PASS orc_match: 2/3 matched (sail+name), BAYMAC column used (rating {bysail['rating']}), "
          "1 unmatched left for hand entry")


def test_generic_col_fallback():
    _fake_net()
    entries = fleetimport.roster_from_yb({"tracker": {"provider": "yb", "race": "demo2026"}})["entries"]
    # a non-BAYMAC race → falls back to the generic offshore ToT (TMF_Offshore)
    en = fleetimport.enrich_from_orc(entries, country="USA", definition={"race_id": "some-other-race"})
    bysail = next(e for e in entries if e["boat"] == "Il Mostro")
    assert bysail["rating"] == 1.10, bysail             # TMF_Offshore, not the BAYMAC column
    print(f"PASS generic_col: non-BAYMAC race uses TMF_Offshore ({bysail['rating']})")


def test_import_fleet_both():
    _fake_net()
    d = {"race_id": "bayview-mackinac-2026", "tracker": {"provider": "yb", "race": "demo2026"}}
    out = fleetimport.import_fleet(d, source="both", country="USA", course_id="cove_island")
    assert out["ok"] and out["count"] == 3 and out["matched"] == 2, out
    assert out["unmatched"] == ["Unrated Joe"], out
    print(f"PASS import_fleet: {out['count']} boats, {out['matched']} ORC-matched, unmatched {out['unmatched']}")


def _live():
    import urllib.request
    # restore real network
    import importlib
    importlib.reload(fleetimport)
    d = {"race_id": "bayview-mackinac-2025", "name": "Bayview Mackinac",
         "tracker": {"provider": "yb", "race": "bayviewmack2025", "host": "cf.yb.tl"}}
    out = fleetimport.import_fleet(d, source="both", country="USA", course_id="cove_island")
    if not out.get("ok"):
        print("LIVE note:", out.get("note")); return
    print(f"LIVE bayviewmack2025: {out['count']} entries, {out.get('matched')}/{out.get('total')} "
          f"matched to {out.get('orc_certs')} USA ORC certs; sample:",
          [(e['boat'], e.get('rating'), e.get('orc_gph')) for e in out['entries'][:3]])


if __name__ == "__main__":
    test_roster_from_yb(); test_orc_match_and_race_col(); test_generic_col_fallback(); test_import_fleet_both()
    if "--live" in sys.argv:
        _live()
    print("\nALL FLEET-IMPORT TESTS PASSED")
