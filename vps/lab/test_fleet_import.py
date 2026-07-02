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


_RN_HTML = (
    '<html><body>'
    '<tbody class="results" data-fleet="ORC Spin 1">'
    '  <tr><td>Pos</td><td>Sail</td><td>Boat</td><td>Rating</td><td>Skipper</td><td>Yacht Club</td></tr>'
    '  <tr><td>1</td><td>USA 103</td><td>Creative</td><td>0</td><td>Ed Sanford</td><td>SDYC</td><td>1</td></tr>'
    '  <tr><td>2</td><td>46604</td><td>Stellah</td><td>0</td><td>Steven Zeis&nbsp;&nbsp;&nbsp; Gabrielle Bunn</td><td>CRA</td></tr>'
    '</tbody>'
    '<tbody class="results" data-fleet="PHRF Cruising Class">'
    '  <tr><td>1</td><td>61181</td><td>Komet</td><td>135</td><td>William Mason</td><td>SWYC</td></tr>'
    '</tbody></body></html>'
).encode("utf-8")


def test_regatta_network_parse():
    # Regatta Network results page: per-division <tbody data-fleet=…>, cols Pos|Sail|Boat|Rating|Skipper|YC
    fleetimport._get = lambda url: _RN_HTML
    assert fleetimport._RN_EVENT_RE.search(
        "https://www.regattanetwork.com/clubmgmt/applet_regatta_results.php?regatta_id=30524").group(1) == "30524"
    r = fleetimport.roster_from_url("https://www.regattanetwork.com/clubmgmt/applet_regatta_results.php?regatta_id=30524")
    assert r["ok"] and r["count"] == 3, r                 # header row skipped, 3 boats across 2 divisions
    e0 = r["entries"][0]
    assert e0["boat"] == "Creative" and e0["sail"] == "USA 103" and e0["owner"] == "Ed Sanford" \
        and e0["division"] == "ORC Spin 1" and e0["source"] == "regatta_network", e0
    # multi-line skipper (skipper + crew) collapses to the skipper
    assert next(e for e in r["entries"] if e["boat"] == "Stellah")["owner"] == "Steven Zeis"
    assert next(e for e in r["entries"] if e["boat"] == "Komet")["division"] == "PHRF Cruising Class"
    print(f"PASS regatta_network: {r['count']} boats across 2 divisions (data-fleet), header row skipped")


def test_preserve_seeded_mmsi():
    _fake_net()
    # a saved roster where the crew SEEDED MMSIs by hand (one by sail#, one only by boat name)
    definition = {"race_id": "bayview-mackinac-2026", "fleet": [
        {"boat": "Il Mostro", "sail": "USA 60", "mmsi": "366111111"},
        {"boat": "Windquest", "sail": "", "mmsi": "366222222"},
    ]}
    # a fresh entry-list import (no MMSI, as entry lists never carry one)
    fresh = [
        {"boat": "Il Mostro", "sail": "USA 60", "mmsi": None, "source": "yb"},
        {"boat": "Windquest", "sail": "USA 12", "mmsi": None, "source": "yb"},
        {"boat": "New Boat", "sail": "USA 5", "mmsi": None, "source": "yb"},
    ]
    out = fleetimport.pack_with_orc(fresh, "yb", "USA", definition, "cove_island")
    assert out["seeded_mmsi"] == 2, out
    bysail = next(e for e in out["entries"] if e["boat"] == "Il Mostro")
    byname = next(e for e in out["entries"] if e["boat"] == "Windquest")
    fresh_boat = next(e for e in out["entries"] if e["boat"] == "New Boat")
    assert bysail["mmsi"] == "366111111", bysail          # carried over by sail #
    assert byname["mmsi"] == "366222222", byname          # carried over by boat name (sail # changed)
    assert fresh_boat["mmsi"] in (None, ""), fresh_boat   # no prior → still needs seeding
    # an incoming entry that ALREADY has an MMSI is not overwritten
    fresh2 = [{"boat": "Il Mostro", "sail": "USA 60", "mmsi": "999999999", "source": "yb"}]
    out2 = fleetimport.pack_with_orc(fresh2, "yb", "USA", definition, None)
    assert next(e for e in out2["entries"] if e["boat"] == "Il Mostro")["mmsi"] == "999999999", out2
    print("PASS preserve_seeded_mmsi: seeded MMSIs carried across a re-import (by sail# + by name), "
          "incoming MMSI not clobbered")


def test_website_empty():
    fleetimport._llm_entry_list = lambda text: []        # JS-rendered page → nothing extracted
    r = fleetimport.roster_from_text("nav menu only, no list")
    assert not r["ok"] and "JS-rendered" in r["note"], r
    print("PASS website_empty: no boats → graceful 'paste/upload' note")


def test_iframe_follow():
    # regatta sites often embed the entry list in an <iframe> from a separate app (e.g. bycmack's
    # current-entries → cf.bycmack.com/entries.cfm). text_from_url must follow it + skip analytics.
    from app import extract
    saved = extract.fetch
    OUTER = ('<html><body><div>page chrome, no list here</div>'
             '<iframe id="advanced_iframe" src="https://cf.example.com/entries.cfm" width="100%"></iframe>'
             '<iframe src="https://doubleclick.net/ad"></iframe></body></html>')
    INNER = '<table><tr><td>Yacht Name</td><td>Sail Number</td></tr><tr><td>C4</td><td>CAN 100</td></tr></table>'
    def fake(url, timeout=30):
        if "doubleclick" in url:
            raise AssertionError("analytics iframe must be skipped, not fetched")
        if "entries.cfm" in url:
            return ("text/html", INNER.encode())
        return ("text/html", OUTER.encode())
    extract.fetch = fake
    try:
        _label, text = extract.text_from_url("https://example.com/current-entries/")
        assert "Yacht Name" in text and "C4" in text and "CAN 100" in text, text[:200]
        # follow_iframes=False leaves the outer page alone (unchanged NOR/SI behavior)
        _l2, t2 = extract.text_from_url("https://example.com/current-entries/", follow_iframes=False)
        assert "C4" not in t2, t2[:200]
    finally:
        extract.fetch = saved
    print("PASS iframe_follow: entry list inside an <iframe> is followed + appended; analytics skipped; opt-out works")


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
    test_website_source(); test_yachtscoring_parse(); test_regatta_network_parse()
    test_preserve_seeded_mmsi(); test_website_empty(); test_iframe_follow()
    if "--live" in sys.argv:
        _live()
    print("\nALL FLEET-IMPORT TESTS PASSED")
