"""Automated fleet-roster ingestion — YB entry-list parse + ORC handicap match.

Deterministic + offline: monkeypatches the network with fixture RaceSetup + ORC-dump bytes and asserts
the entry-list parse, the sail-then-name ORC match, and that the ToT column comes from the race
definition (#311): per course, race-wide, or unknown → no rating and a plain note, never a guess.
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
     "TMF_Offshore": 1.10, "TMF_Inshore": 1.20, "XX_RACE_A_TOT": 0.95, "XX_RACE_B_TOT": 0.97},
    {"SailNo": "USA 99", "YachtName": "Windquest", "Class": "Santa Cruz 52", "GPH": 560.0,
     "TMF_Offshore": 1.05, "TMF_Inshore": 1.15, "XX_RACE_A_TOT": 1.02},
]}).encode("utf-8-sig")


def _race(**scoring):
    """A synthetic ORC race whose scoring block carries the given rating fields."""
    sc = {"system": "ORC", "method": "Time-on-Time", "rating_system": "ORC"}
    sc.update(scoring)
    return {"race_id": "synthetic-race-2027", "rules_profile": {"scoring": sc},
            "divisions": [{"id": "A", "course_ref": "long"}, {"id": "B", "course_ref": "short"}],
            "courses": [{"id": "long"}, {"id": "short"}],
            "tracker": {"provider": "yb", "race": "demo2026"}}


_PER_COURSE = [{"course": "long", "column": "XX_RACE_A_TOT", "column_tod": ""},
               {"course": "short", "column": "XX_RACE_B_TOT", "column_tod": ""}]


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
    d = _race(course_columns=_PER_COURSE)
    entries = fleetimport.roster_from_yb({"tracker": {"provider": "yb", "race": "demo2026"}})["entries"]
    en = fleetimport.enrich_from_orc(entries, country="USA", definition=d, course_id="long")
    assert en["ok"] and en["matched"] == 2, en          # Il Mostro by sail, Windquest by name
    bysail = next(e for e in entries if e["boat"] == "Il Mostro")
    byname = next(e for e in entries if e["boat"] == "Windquest")
    unmatched = next(e for e in entries if e["boat"] == "Unrated Joe")
    # the course's column from the definition, NOT the generic TMF_Offshore
    assert en["tot_col"] == "XX_RACE_A_TOT" and "note" not in en, en
    assert bysail["rating"] == 0.95 and bysail["orc_gph"] == 500.0, bysail
    assert byname["rating"] == 1.02 and byname["cls"] == "Sy 52", byname  # name match; YB class kept (not clobbered)
    assert byname["orc_match"]["by"] == "name", byname
    assert unmatched.get("rating") is None and unmatched.get("orc_match") is None, unmatched
    print(f"PASS orc_match: 2/3 matched (sail+name), course column used (rating {bysail['rating']}), "
          "1 unmatched left for hand entry")


def test_course_column_follows_course():
    _fake_net()
    entries = fleetimport.roster_from_yb({"tracker": {"provider": "yb", "race": "demo2026"}})["entries"]
    en = fleetimport.enrich_from_orc(entries, country="USA", definition=_race(course_columns=_PER_COURSE),
                                     course_id="short")
    assert en["tot_col"] == "XX_RACE_B_TOT", en
    windquest = next(e for e in entries if e["boat"] == "Windquest")
    assert windquest.get("rating") is None and windquest["orc_gph"] == 560.0, windquest   # cert lacks it
    assert en["note"] == "1 boat lacks XX_RACE_B_TOT on the cert.", en
    # no course asked → our division's course
    d = dict(_race(course_columns=_PER_COURSE), own={"division": "B"})
    en = fleetimport.enrich_from_orc([{"boat": "Il Mostro", "sail": "USA 60"}], country="USA", definition=d)
    assert en["tot_col"] == "XX_RACE_B_TOT" and en["entries"][0]["rating"] == 0.97, en
    print("PASS course_column: per-course column follows the course / our division; a cert without it "
          "gets no rating and a note")


def test_reenrich_clears_a_rating_off_another_column():
    # #311 review: a rating read off column A must not survive a switch to a column the cert lacks
    _fake_net()
    roster = [{"boat": "Il Mostro", "sail": "USA 60", "rating": 1.10},
              {"boat": "Windquest", "sail": "USA 99", "rating": 1.05}]
    en = fleetimport.enrich_from_orc(roster, country="USA", definition=_race(rating_column="XX_RACE_B_TOT"))
    mostro, wq = en["entries"]
    assert mostro["rating"] == 0.97, mostro                       # cert has B → the B number
    assert wq["rating"] is None and wq["orc_match"]["col"] == "XX_RACE_B_TOT", wq   # lacks B → cleared
    # and switching to no column at all clears them too
    en = fleetimport.enrich_from_orc(roster, country="USA", definition=_race())
    assert all(e["rating"] is None for e in en["entries"]), en["entries"]
    # an unmatched boat keeps a hand-entered rating (no cert to replace it)
    hand = [{"boat": "Nobody", "sail": "USA 1", "rating": 0.9}]
    fleetimport.enrich_from_orc(hand, country="USA", definition=_race(rating_column="XX_RACE_B_TOT"))
    assert hand[0]["rating"] == 0.9, hand
    print("PASS reenrich_clears: a matched boat's rating is always the selected column's, or empty")


def test_tod_column_imported_separately():
    # round 2: a ToD race's own allowance column (620) is imported apart from GPH (500)
    _fake_net()
    _ORC_TOD = json.dumps({"rms": [
        {"SailNo": "USA 60", "YachtName": "Il Mostro", "GPH": 500.0, "XX_TOT": 1.1, "XX_TOD": 620.0},
    ]}).encode("utf-8-sig")
    fleetimport._get = lambda url: _ORC_TOD
    d = _race(rating_column="XX_TOT", rating_column_tod="XX_TOD")
    en = fleetimport.enrich_from_orc([{"boat": "Il Mostro", "sail": "USA 60"}], country="USA", definition=d)
    e = en["entries"][0]
    assert e["orc_gph"] == 500.0 and e["tod_allowance"] == 620.0 and e["tod_col"] == "XX_TOD", e
    assert e["rating"] == 1.1 and e["rating_col"] == "XX_TOT" and en["tod_col"] == "XX_TOD", e
    c = fleetimport.candidates("Il Mostro", "USA 60", "USA", d)
    assert c["candidates"][0]["tod_allowance"] == 620.0 and c["tod_col"] == "XX_TOD", c
    # no ToD column named → no allowance (the engine then uses GPH)
    en = fleetimport.enrich_from_orc([{"boat": "Il Mostro", "sail": "USA 60"}], country="USA",
                                     definition=_race(rating_column="XX_TOT"))
    assert en["entries"][0]["tod_allowance"] is None and "tod_col" not in en["entries"][0], en
    # and the onboard blob carries it
    from shared import race_def
    blob = race_def.fleet_blob(dict(d, fleet=[e]))
    assert blob["fleet"][0]["tod_allowance"] == 620.0 and blob["fleet"][0]["orc_gph"] == 500.0, blob
    print("PASS tod_column: the race's ToD allowance is imported and shipped apart from GPH")


def test_switch_clears_imported_numbers_even_when_unmatched():
    # round 2: A → B clears an imported A rating on a boat the new lookup misses (its dump failed),
    # but keeps a number typed by hand
    def _get(url):
        if "CountryId=CAN" in url:
            raise OSError("CAN dump down")
        return _ORC
    fleetimport._get = _get
    fleetimport._orc_cache.clear()
    roster = [
        {"boat": "Maple", "sail": "CAN 1", "rating": 1.3, "rating_col": "XX_RACE_A_TOT",
         "tod_allowance": 610.0, "tod_col": "XX_A_TOD"},              # imported off A, now unmatched
        {"boat": "Typed", "sail": "CAN 2", "rating": 0.88},            # typed by hand: kept
        {"boat": "Il Mostro", "sail": "USA 60", "rating": 0.95, "rating_col": "XX_RACE_A_TOT"},
    ]
    en = fleetimport.enrich_from_orc(roster, country="USA,CAN",
                                     definition=_race(rating_column="XX_RACE_B_TOT"))
    maple, typed, mostro = en["entries"]
    assert maple["rating"] is None and "rating_col" not in maple, maple
    assert maple["tod_allowance"] is None and "tod_col" not in maple, maple
    assert typed["rating"] == 0.88, typed
    assert mostro["rating"] == 0.97 and mostro["rating_col"] == "XX_RACE_B_TOT", mostro
    assert en["stale_cleared"] == 1, en
    # even when EVERY dump fails, the stale import goes (and the count says so)
    fleetimport._get = lambda url: (_ for _ in ()).throw(OSError("down"))
    fleetimport._orc_cache.clear()
    r2 = [{"boat": "Maple", "sail": "CAN 1", "rating": 1.3, "rating_col": "XX_RACE_A_TOT"}]
    out = fleetimport.pack_with_orc(r2, "orc", "CAN", _race(rating_column="XX_RACE_B_TOT"), None)
    assert out["orc_error"] and out["stale_cleared"] == 1 and r2[0]["rating"] is None, out
    # same column again → nothing cleared
    fleetimport._get = lambda url: _ORC
    fleetimport._orc_cache.clear()
    r3 = [{"boat": "Nobody", "sail": "USA 1", "rating": 1.0, "rating_col": "XX_RACE_B_TOT"}]
    en = fleetimport.enrich_from_orc(r3, country="USA", definition=_race(rating_column="XX_RACE_B_TOT"))
    assert r3[0]["rating"] == 1.0 and en["stale_cleared"] == 0, en
    print("PASS switch_clears: imported numbers off another column go, matched or not; typed ones stay")


def test_own_boat_follows_the_column():
    # round 2: a column switch re-reads our boat too, or clears it and asks again
    _fake_net()
    d = _race(rating_column="TMF_Inshore")
    d["own"] = {"boat": "Il Mostro", "rating": 1.10, "rating_col": "TMF_Offshore", "division": "A"}
    out = fleetimport.pack_with_orc([{"boat": "Windquest", "sail": "USA 99"}], "orc", "USA", d, None)
    assert out["own"]["rating"] == 1.20 and out["own"]["rating_col"] == "TMF_Inshore", out["own"]
    assert out["own"]["division"] == "A" and "own_note" not in out, out
    assert d["own"]["rating"] == 1.10                                   # the saved race not mutated
    # not on any cert: an old number for another column is cleared, with the plain ask
    d["own"] = {"boat": "Our Boat", "rating": 1.10, "rating_col": "TMF_Offshore"}
    out = fleetimport.pack_with_orc([], "orc", "USA", d, None)
    assert out["own"]["rating"] is None and out["own_note"] == "Re-enter our rating for this column", out
    # typed for this very column → kept, no note
    d["own"] = {"boat": "Our Boat", "rating": 0.9, "rating_col": "TMF_Inshore"}
    out = fleetimport.pack_with_orc([], "orc", "USA", d, None)
    assert out["own"]["rating"] == 0.9 and "own_note" not in out, out
    # on a cert that lacks the column → empty, ask again
    d2 = _race(rating_column="XX_RACE_B_TOT")
    d2["own"] = {"boat": "Windquest", "rating": 1.0, "rating_col": "XX_RACE_A_TOT"}
    out = fleetimport.pack_with_orc([], "orc", "USA", d2, None)
    assert out["own"]["rating"] is None and out["own_note"] == "Re-enter our rating for this column", out
    # no own boat set → nothing about it
    assert "own" not in fleetimport.pack_with_orc([], "orc", "USA", _race(rating_column="X"), None)
    print("PASS own_boat: our rating follows the column (re-read, or cleared with 'Re-enter our rating')")


def test_missing_column_no_guess():
    _fake_net()
    entries = fleetimport.roster_from_yb({"tracker": {"provider": "yb", "race": "demo2026"}})["entries"]
    # an ORC race whose definition names no column → no rating at all, and the roster says so
    en = fleetimport.enrich_from_orc(entries, country="USA", definition=_race())
    bysail = next(e for e in entries if e["boat"] == "Il Mostro")
    assert bysail.get("rating") is None and bysail["orc_gph"] == 500.0, bysail   # NOT TMF_Offshore
    assert en["tot_col"] is None and en["note"] == "Rating column unknown: pick one", en
    assert "TMF_Offshore" in en["orc_columns"] and "GPH" not in en["orc_columns"], en
    # the same through the import (what the Fleet tab shows)
    out = fleetimport.import_fleet(_race(), source="both", country="USA")
    assert out["rating_note"] == "Rating column unknown: pick one", out
    assert all(e.get("rating") is None for e in out["entries"]), out
    # a race with no definition at all is no different
    en = fleetimport.enrich_from_orc([{"boat": "Il Mostro", "sail": "USA 60"}], country="USA")
    assert en["entries"][0].get("rating") is None and en["note"] == "Rating column unknown: pick one"
    print("PASS missing_column: no column named → ratings empty, 'Rating column unknown: pick one'")


def test_non_us_orc_race():
    # an inshore ORC race outside North America: race-wide column, countries from the definition
    # (venue country + entrants' sail prefixes), no USA/CAN anywhere
    certs = {
        "FRA": {"rms": [{"SailNo": "FRA 4242", "YachtName": "Bleu", "Class": "JPK 1080", "GPH": 640.0,
                         "TMF_Offshore": 0.71, "TMF_Inshore": 0.69}]},
        "GBR": {"rms": [{"SailNo": "GBR 1234", "YachtName": "Red Kite", "Class": "J/109", "GPH": 650.0,
                         "TMF_Offshore": 0.70, "TMF_Inshore": 0.68}]},
        "IRL": {"rms": [{"SailNo": "IRL 7", "YachtName": "Green", "Class": "X-35", "GPH": 660.0,
                         "TMF_Offshore": 0.67, "TMF_Inshore": 0.66}]},
    }
    asked = []

    def _get(url):
        cc = url.split("CountryId=")[1].split("&")[0]
        asked.append(cc)
        if cc not in certs:
            raise OSError("no such authority")
        return json.dumps(certs[cc]).encode("utf-8-sig")
    fleetimport._get = _get
    fleetimport._orc_cache.clear()
    d = {"race_id": "synthetic-inshore-2027", "venue_country": "GBR",
         "rules_profile": {"scoring": {"system": "ORC", "method": "Time-on-Time",
                                       "rating_system": "ORC", "rating_column": "TMF_Inshore"}}}
    entries = [{"boat": "Red Kite", "sail": "GBR 1234"}, {"boat": "Bleu", "sail": "FRA-4242"},
               {"boat": "Bleu Deux", "sail": "FRA 99"}, {"boat": "Green", "sail": "IRL 7"}]
    out = fleetimport.pack_with_orc(entries, "website", None, d, None)
    assert asked == ["GBR", "FRA", "IRL"], asked          # venue first, then prefixes by count
    assert out["matched"] == 3 and out["tot_col"] == "TMF_Inshore", out
    assert out["orc_country"] == "GBR,FRA,IRL" and "rating_note" not in out, out
    assert next(e for e in entries if e["boat"] == "Bleu")["rating"] == 0.69
    # a list typed in the Fleet tab still wins
    asked.clear()
    fleetimport._orc_cache.clear()
    fleetimport.enrich_from_orc([dict(entries[0])], country="IRL", definition=d)
    assert asked == ["IRL"], asked
    # nothing known anywhere → say so, fetch nothing
    asked.clear()
    en = fleetimport.enrich_from_orc([{"boat": "X", "sail": "12"}], definition=_race())
    assert not en["ok"] and en["note"] == "No ORC country known. Enter one." and asked == [], en
    print("PASS non_us_orc: inshore column from the definition, countries GBR,FRA,IRL from venue + sails")


def test_other_rating_system_has_no_feed():
    _fake_net()
    d = {"race_id": "synthetic-phrf", "rules_profile": {"scoring": {"system": "PHRF",
                                                                    "method": "Time-on-Distance"}}}
    out = fleetimport.pack_with_orc([{"boat": "Il Mostro", "sail": "USA 60"}], "website", "USA", d, None)
    assert out["rating_note"] == "No PHRF ratings feed yet. Enter ratings by hand.", out
    assert "matched" not in out and out["entries"][0].get("rating") is None, out
    c = fleetimport.candidates("Il Mostro", "USA 60", "USA", d)
    assert c["candidates"] == [] and "PHRF" in c["note"], c
    od = {"rules_profile": {"scoring": {"rating_system": "one-design"}}}
    assert fleetimport.pack_with_orc([], "website", "USA", od, None)["rating_note"] == \
        "One-design: no ratings needed."
    print("PASS other_system: PHRF / one-design get a plain note, never ORC numbers")


def test_candidates_use_race_column():
    _fake_net()
    r = fleetimport.candidates("Il Mostro", "USA 60", "USA", _race(rating_column="TMF_Inshore"))
    assert r["candidates"][0]["rating"] == 1.20 and r["tot_col"] == "TMF_Inshore", r
    r = fleetimport.candidates("Il Mostro", "USA 60", "USA", _race())
    assert r["candidates"][0]["rating"] is None and r["note"] == "Rating column unknown: pick one", r
    print("PASS candidates: the find button reads the race's column too")


def test_set_fields_cli():
    import os
    import tempfile
    from app import ratings
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "race.json")
        with open(p, "w") as f:
            json.dump({"race_id": "r", "rules_profile": {"scoring": {"system": "ORC"}}}, f)
        assert ratings._main(["set", p, "--system", "orc", "--course", "long=XX_RACE_A_TOT/XX_A_TOD",
                              "--countries", "gbr, fra", "--dry-run"]) == 0
        with open(p) as f:
            assert "rating_system" not in json.load(f)["rules_profile"]["scoring"]   # dry run
        ratings._main(["set", p, "--system", "orc", "--course", "long=XX_RACE_A_TOT/XX_A_TOD",
                       "--countries", "gbr, fra"])
        with open(p) as f:
            sc = json.load(f)["rules_profile"]["scoring"]
    assert sc == {"system": "ORC", "rating_system": "ORC", "rating_countries": ["GBR", "FRA"],
                  "course_columns": [{"course": "long", "column": "XX_RACE_A_TOT",
                                      "column_tod": "XX_A_TOD"}]}, sc
    print("PASS set_fields: the migration CLI writes the rating fields (and a dry run writes nothing)")


def test_import_fleet_both():
    _fake_net()
    d = _race(course_columns=_PER_COURSE)
    out = fleetimport.import_fleet(d, source="both", country="USA", course_id="long")
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
    assert not r["ok"] and "paste it" in r["note"], r
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
    test_roster_from_yb(); test_orc_match_and_race_col(); test_course_column_follows_course()
    test_reenrich_clears_a_rating_off_another_column()
    test_tod_column_imported_separately(); test_switch_clears_imported_numbers_even_when_unmatched()
    test_own_boat_follows_the_column()
    test_missing_column_no_guess(); test_non_us_orc_race(); test_other_rating_system_has_no_feed()
    test_candidates_use_race_column(); test_set_fields_cli(); test_import_fleet_both()
    test_website_source(); test_yachtscoring_parse(); test_regatta_network_parse()
    test_preserve_seeded_mmsi(); test_website_empty(); test_iframe_follow()
    if "--live" in sys.argv:
        _live()
    print("\nALL FLEET-IMPORT TESTS PASSED")
