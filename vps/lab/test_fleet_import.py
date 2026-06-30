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


def test_website_source():
    # the regatta-website path extracts the entry list via Opus → stub the LLM call, then ORC-enrich
    _fake_net()
    fleetimport._llm_entry_list = lambda text: [
        {"boat": "Il Mostro", "sail": "USA 60", "owner": "Puma", "class": "VOR70"},
        {"boat": "Windquest", "sail": "USA 12", "division": "I"},
        {"boat": "", "sail": "skip me"},                 # no name → dropped
    ]
    r = fleetimport.roster_from_text("<pasted entry list page text>")
    assert r["ok"] and r["count"] == 2 and r["entries"][0]["source"] == "website", r
    out = fleetimport.import_fleet({"race_id": "bayview-mackinac-2026"}, source="website",
                                   country="USA", course_id="cove_island", text="page text")
    assert out["ok"] and out["count"] == 2 and out["matched"] == 2, out
    print(f"PASS website_source: extracted {r['count']} boats from page text, {out['matched']} ORC-matched")


def test_yachtscoring_parse():
    # YachtScoring's public boats API (paginated) — owner & split are dicts; sail = prefix+number
    page1 = json.dumps({"count": 2, "rows": [
        {"name": "Think blue", "sailPrefix": "usa", "sailNumber": "200", "design": "Ten",
         "owner": {"firstName": "Gary", "lastName": "Disbrow"},
         "split": {"splitDivision": "PHRF", "splitClassName": "PHRF G"}},
        {"name": "Killing Me Softly", "sailPrefix": "USA", "sailNumber": "40037", "design": "Farr 40",
         "owner": {"firstName": "Ryan", "lastName": "Quinn"}, "split": {"splitClassName": "PHRF A"}},
    ]}).encode()
    fleetimport._get = lambda url: page1
    for u in ("https://yachtscoring.com/emenu/50579",
              "https://www.yachtscoring.com/event_entry_list.cfm?eID=50579"):
        assert fleetimport._YS_EVENT_RE.search(u).group(1) == "50579", u
    r = fleetimport.roster_from_url("https://yachtscoring.com/emenu/50579")
    assert r["ok"] and r["count"] == 2, r
    e0 = r["entries"][0]
    assert e0["boat"] == "Think blue" and e0["sail"] == "USA 200" and e0["owner"] == "Gary Disbrow" \
        and e0["division"] == "PHRF G" and e0["source"] == "yachtscoring", e0
    print("PASS yachtscoring: event-id parsed, boats API mapped (sail/owner/division from nested JSON)")


def test_website_empty():
    fleetimport._llm_entry_list = lambda text: []        # JS-rendered page → nothing extracted
    r = fleetimport.roster_from_text("nav menu only, no list")
    assert not r["ok"] and "JS-rendered" in r["note"], r
    print("PASS website_empty: no boats → graceful 'paste/upload' note")


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
    test_website_source(); test_yachtscoring_parse(); test_website_empty()
    if "--live" in sys.argv:
        _live()
    print("\nALL FLEET-IMPORT TESTS PASSED")
