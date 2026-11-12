"""Issue-hunting regression for the homework-loading + fleet-roster -> strategy path.
Stubs the datasource so we control roster / AIS / course, then drives the REAL
fleet.get_fleet + strategy.get_strategy_signals across valid + adversarial cases.
Locks the fix for the non-numeric-rating crash (a messy handicap must not 500 the
fleet + strategy endpoints).

Run:  python3 vps/agent/test_fleet_load.py
"""
import os
import sys
_HERE = os.path.dirname(os.path.abspath(__file__))          # vps/agent
_ROOT = os.path.dirname(os.path.dirname(_HERE))              # repo root (for `shared`)
sys.path[:0] = [_HERE, _ROOT]
from app import fleet, strategy, ais  # noqa

issues, notes = [], []
def ISSUE(m): issues.append(m); print("  !! ISSUE:", m)
def OK(m): print("  [ok]", m)

# course: due-north leg, start (45.00,-83.00) → finish (45.30,-83.00)
MARKS = [{"lat": 45.00, "lon": -83.00}, {"lat": 45.30, "lon": -83.00}]
OWN = {"lat": 45.05, "lon": -83.00, "sog": 6.0, "cog": 0}

class Src:
    def __init__(s, blob, targets, marks=MARKS): s.b, s.t, s.m = blob, targets, marks
    def get_fleet(s): return s.b
    def ais_targets(s, w): return s.t
    def marks(s, route): return s.m

def setup(blob, targets, marks=MARKS, own=OWN):
    src = Src(blob, targets, marks)
    fleet.datasource.active = lambda: src
    fleet.ais._own_ship = lambda: dict(own)
    fleet._active_route = lambda: "test"

def tgt(mmsi, lat, lon, sog=6.0, cog=0, name=None):
    return {"mmsi": mmsi, "lat": lat, "lon": lon, "sog": sog, "cog": cog, "name": name}

# ---------------------------------------------------------------------------
print("\n== 1. valid ToT roster, AIS matches by MMSI, moving ==")
try:
    blob = {"scoring": {"method": "Time-on-Time"}, "own": {"rating": 1.000},
            "fleet": [{"boat": "Defiance", "mmsi": "111", "rating": 1.050, "division": "ORC A"},
                      {"boat": "Windquest", "mmsi": "222", "rating": 0.980}]}
    setup(blob, [tgt("111", 45.10, -83.02, 6.4), tgt("222", 45.08, -83.00, 5.8)])
    f = fleet.get_fleet()
    OK(f"available={f['available']} matched={f['count_matched']} ais={f['count_ais']}")
    for r in f["fleet"]:
        print(f"     {r['boat']}: matched_by={r['matched_by']} corrected_delta_s={r.get('corrected_delta_s')} "
              f"leverage_nm={r.get('leverage_nm')} tag={r['tag']} conf={r['confidence']}")
    if f["count_matched"] != 2: ISSUE("expected 2 matched")
    if not all(r.get("corrected_delta_s") is not None for r in f["fleet"]): ISSUE("corrected_delta missing on a moving matched boat")
    if not all(r.get("leverage_nm") is not None for r in f["fleet"]): ISSUE("leverage missing with a course loaded")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

print("\n== 2. ToD (allowance = GPH) scoring ==")
try:
    blob = {"scoring": {"method": "Time-on-Distance"}, "own": {"orc_gph": 600.0},
            "fleet": [{"boat": "Fast", "mmsi": "111", "orc_gph": 580.0}]}
    setup(blob, [tgt("111", 45.12, -83.01, 7.0)])
    f = fleet.get_fleet()
    r = f["fleet"][0]
    OK(f"ToD method={f['scoring_method']} corrected_delta_s={r.get('corrected_delta_s')} basis={r.get('corrected_basis')}")
    if r.get("corrected_delta_s") is None: ISSUE("ToD corrected delta not computed with gph on both boats")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

print("\n== 3. entry with NO rating/gph (unknown handicap) ==")
try:
    blob = {"scoring": {"method": "Time-on-Time"}, "own": {"rating": 1.0},
            "fleet": [{"boat": "Mystery", "mmsi": "111"}]}   # no rating
    setup(blob, [tgt("111", 45.10, -83.01, 6.0)])
    f = fleet.get_fleet()
    r = f["fleet"][0]
    OK(f"no-rating boat: corrected_delta_s={r.get('corrected_delta_s')} tag={r['tag']} conf={r['confidence']}")
    if r.get("corrected_delta_s") is not None: ISSUE("corrected delta computed without a handicap?!")
    if r["confidence"] >= 0.95: ISSUE("confidence not reduced for unknown handicap")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

print("\n== 4. ADVERSARIAL: non-numeric rating from an extracted roster ==")
for bad in ["1.05 GPH", "DNC", "", "n/a", None]:
    try:
        blob = {"scoring": {"method": "Time-on-Time"}, "own": {"rating": 1.0},
                "fleet": [{"boat": "Bad", "mmsi": "111", "rating": bad}]}
        setup(blob, [tgt("111", 45.10, -83.01, 6.0)])
        f = fleet.get_fleet()
        OK(f"rating={bad!r} → survived, matched={f['count_matched']}")
    except Exception as e:
        ISSUE(f"rating={bad!r} CRASHED get_fleet: {type(e).__name__}: {e}")

print("\n== 4b. ADVERSARIAL: non-numeric gph (ToD) ==")
for bad in ["580s", "fast", ""]:
    try:
        blob = {"scoring": {"method": "ToD"}, "own": {"orc_gph": 600.0},
                "fleet": [{"boat": "Bad", "mmsi": "111", "orc_gph": bad}]}
        setup(blob, [tgt("111", 45.10, -83.01, 6.0)])
        fleet.get_fleet()
        OK(f"gph={bad!r} → survived")
    except Exception as e:
        ISSUE(f"gph={bad!r} CRASHED get_fleet: {type(e).__name__}: {e}")

print("\n== 5. no course loaded (marks < 2) ==")
try:
    blob = {"scoring": {"method": "ToT"}, "own": {"rating": 1.0},
            "fleet": [{"boat": "X", "mmsi": "111", "rating": 1.0}]}
    setup(blob, [tgt("111", 45.10, -83.01, 6.0)], marks=[{"lat": 45.0, "lon": -83.0}])
    f = fleet.get_fleet()
    OK(f"no course: available={f['available']} leverage={f['fleet'][0].get('leverage_nm')} gaps={f.get('gaps')}")
    if not f["available"]: ISSUE("get_fleet should still be available without a course")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

print("\n== 6. own has no GPS fix ==")
try:
    blob = {"scoring": {"method": "ToT"}, "own": {"rating": 1.0},
            "fleet": [{"boat": "X", "mmsi": "111", "rating": 1.0}]}
    setup(blob, [tgt("111", 45.10, -83.01, 6.0)], own={"sog": None})
    f = fleet.get_fleet()
    OK(f"no own fix: available={f['available']} own.fix={f['own']['fix']} corrected={f['fleet'][0].get('corrected_delta_s')}")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

print("\n== 7. empty roster → not available ==")
try:
    setup({"fleet": []}, [tgt("111", 45.1, -83.0)])
    f = fleet.get_fleet()
    OK(f"empty roster: available={f['available']} note={f['note'][:40]}")
    if f["available"]: ISSUE("empty roster should be unavailable")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
print("\n== 8. strategy._fleet_lean over REAL get_fleet output (left/right/split/tie) ==")
def lean_case(name, positions):
    blob = {"scoring": {"method": "ToT"}, "own": {"rating": 1.0},
            "fleet": [{"boat": f"R{i}", "mmsi": str(100+i), "rating": 1.02} for i in range(len(positions))]}
    setup(blob, [tgt(str(100+i), lat, lon, 6.5) for i, (lat, lon) in enumerate(positions)])
    f = fleet.get_fleet()
    # force them to read as rivals so _fleet_lean counts them
    for r in f["fleet"]:
        r["tag"] = "rival"
    side, strength, n = strategy._fleet_lean(f)
    levs = [r.get("leverage_nm") for r in f["fleet"]]
    print(f"  {name}: leverages={levs} → lean={side} strength={round(strength,2)} n={n}")
    return side
# east (higher lon) should be one side, west the other; verify consistency + no crash
try:
    e = lean_case("all-east", [(45.10, -82.98), (45.12, -82.97)])
    w = lean_case("all-west", [(45.10, -83.02), (45.12, -83.03)])
    lean_case("split", [(45.10, -82.98), (45.10, -83.02)])
    lean_case("on-line (tie)", [(45.10, -83.00), (45.11, -83.00)])
    if e and w and e == w: ISSUE("east and west rosters produced the SAME lean — sign bug")
    else: OK(f"east lean={e} vs west lean={w} (opposite as expected)")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
print("\n== 9. fleet_blob (load-endpoint parser) on a RaceDefinition ==")
try:
    from shared.race_def import fleet_blob
    defn = {"fleet": [{"boat": "A", "mmsi": "111", "rating": 1.05, "division": "ORC A", "sail": "USA 1"},
                      {"boat": "B", "orc_gph": 600}],
            "rules_profile": {"scoring": {"method": "Time-on-Time"}, "tracker_permitted": True},
            "tracker": {"provider": "yb", "race": "bayviewmack2026"}}
    b = fleet_blob(defn, own={"boat": "C4", "rating": 1.0})
    OK(f"fleet_blob: roster={len(b['fleet'])} scoring={b['scoring']} tracker.permitted={b['tracker'].get('permitted')}")
    if len(b["fleet"]) != 2: ISSUE("fleet_blob dropped an entry")
    if not b["tracker"].get("permitted"): ISSUE("tracker_permitted=True not carried into blob")
    # empty definition
    b2 = fleet_blob({}, None)
    OK(f"empty def → roster={len(b2['fleet'])} (graceful)")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
print("\n== 10. #183-1: no own rating withholds corrected gaps (no ToT 1.0 guess) ==")
try:
    blob = {"scoring": {"method": "Time-on-Time"}, "own": {"boat": "C4"},
            "fleet": [{"boat": "Defiance", "mmsi": "111", "rating": 1.050}]}
    setup(blob, [tgt("111", 45.10, -83.02, 6.4)])
    f = fleet.get_fleet()
    r = f["fleet"][0]
    if r.get("corrected_delta_s") is not None: ISSUE(f"corrected delta shown with no own rating: {r.get('corrected_delta_s')}")
    else: OK("no corrected delta without our rating")
    if not any("own rating missing" in g for g in (f.get("gaps") or [])): ISSUE(f"no gap note: {f.get('gaps')}")
    else: OK("gap note says own rating missing")
    if f["own_rank"]["overall"] is not None: ISSUE(f"own boat still ranked: {f['own_rank']}")
    else: OK("own boat not ranked either")
    if r.get("dtf_nm") is None: ISSUE("positions dropped too (only corrected gaps should go)")
    # with our rating back, the same picture scores again
    blob["own"]["rating"] = 1.0
    f = fleet.get_fleet()
    if f["fleet"][0].get("corrected_delta_s") is None: ISSUE("rating present but no corrected delta")
    else: OK("own rating present -> corrected delta back")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

print("\n== 11. #240: unsupported scoring is not scored as ToT ==")
try:
    for meth in ("Performance Curve Scoring", "One-Design", "ToT or ToD per SI"):
        blob = {"scoring": {"method": meth}, "own": {"rating": 1.0, "orc_gph": 600.0},
                "fleet": [{"boat": "Defiance", "mmsi": "111", "rating": 1.050, "orc_gph": 580.0}]}
        setup(blob, [tgt("111", 45.10, -83.02, 6.4)])
        f = fleet.get_fleet()
        if f["fleet"][0].get("corrected_delta_s") is not None: ISSUE(f"{meth!r} scored anyway")
        elif not any("not supported" in g for g in (f.get("gaps") or [])): ISSUE(f"{meth!r}: no gap note {f.get('gaps')}")
        else: OK(f"{meth!r}: no corrected gaps, gap note")
    for meth, tod in (("Single-Number Time-on-Time (ToT)", False), ("ToD", True), ("time on distance", True)):
        blob = {"scoring": {"method": meth}, "own": {"rating": 1.0, "orc_gph": 600.0},
                "fleet": [{"boat": "Defiance", "mmsi": "111", "rating": 1.050, "orc_gph": 580.0}]}
        setup(blob, [tgt("111", 45.10, -83.02, 6.4)])
        f = fleet.get_fleet()
        if f["fleet"][0].get("corrected_delta_s") is None: ISSUE(f"{meth!r} should score")
        else: OK(f"{meth!r} scores")
    # no method but a system that names one; no method at all keeps ToT with a note
    blob = {"scoring": {"system": "ORC"}, "own": {"rating": 1.0},
            "fleet": [{"boat": "Defiance", "mmsi": "111", "rating": 1.050}]}
    setup(blob, [tgt("111", 45.10, -83.02, 6.4)])
    f = fleet.get_fleet()
    if f["fleet"][0].get("corrected_delta_s") is None or not any("using Time-on-Time" in g for g in (f.get("gaps") or [])):
        ISSUE(f"system-only scoring: {f.get('gaps')}")
    else: OK("no method set -> ToT with a gap note")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

print("\n== 12. #183-7: a tracker fix with no time is not brand new ==")
try:
    from app import tracker
    tracker._reset_cache()
    _orig_fetch = tracker._fetch
    tracker._fetch = lambda cfg, centre=None: ([{"name": "Defiance", "lat": 45.20, "lon": -83.00, "sog": 6.0,
                                    "cog": 0, "time": None}], None)
    tk = tracker.positions({"provider": "x", "url": "u"}, now=1_000_000.0)
    fx = tk["positions"][0]
    if fx["age_s"] is not None and fx["age_s"] < 60: ISSUE(f"no-time fix aged {fx['age_s']} s (looks fresh)")
    else: OK(f"age unknown (age_s={fx['age_s']}), confidence {fx['confidence']}")
    if fx["confidence"] > 0.2: ISSUE(f"no-time fix confidence {fx['confidence']}")
    # through the fleet: shown, not scored
    tracker._reset_cache()
    blob = {"scoring": {"method": "ToT"}, "own": {"rating": 1.0},
            "fleet": [{"boat": "Defiance", "rating": 1.05}],
            "tracker": {"provider": "x", "url": "u", "permitted": True}}
    setup(blob, [])
    f = fleet.get_fleet()
    rows = [r for r in f["fleet"] if r["boat"] == "Defiance"]
    if not rows: ISSUE("no-time tracker boat vanished")
    elif rows[0].get("corrected_delta_s") is not None: ISSUE("scored off a fix of unknown age")
    elif not rows[0].get("age_unknown"): ISSUE("row not flagged age_unknown")
    else: OK("shown, flagged, not scored")
    tracker._fetch = _orig_fetch
    tracker._reset_cache()
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

print("\n== 13. #311: the race's rating fields reach the in-race model ==")
try:
    row = [{"boat": "Defiance", "mmsi": "111", "rating": 1.050, "orc_gph": 580.0}]
    own = {"rating": 1.0, "orc_gph": 600.0}
    # a PHRF race (rating_system) is never scored with ORC math, even with a ToT method text
    blob = {"scoring": {"method": "Time-on-Time", "rating_system": "PHRF"}, "own": own, "fleet": row}
    setup(blob, [tgt("111", 45.10, -83.02, 6.4)])
    f = fleet.get_fleet()
    if f["fleet"][0].get("corrected_delta_s") is not None: ISSUE("PHRF race scored with ORC math")
    elif "PHRF scoring not supported, no corrected gaps" not in (f.get("gaps") or []): ISSUE(f"PHRF gap: {f.get('gaps')}")
    else: OK("PHRF race: no corrected gaps, plain note")
    # ORC with no column: still scores, but says the numbers may not be the race's
    blob = {"scoring": {"method": "Time-on-Time", "rating_system": "ORC"}, "own": own, "fleet": row}
    setup(blob, [tgt("111", 45.10, -83.02, 6.4)])
    f = fleet.get_fleet()
    if "rating column unknown, gaps may be off" not in (f.get("gaps") or []): ISSUE(f"no column gap: {f.get('gaps')}")
    else: OK("ORC, no column: gap note")
    # ORC with a race-wide or per-course column: no such note
    for sc in ({"rating_column": "TMF_Inshore"},
               {"course_columns": [{"course": "a", "column": "X_A"}, {"course": "b", "column": "X_B"}]}):
        blob = {"scoring": dict({"method": "Time-on-Time", "rating_system": "ORC"}, **sc), "own": own, "fleet": row}
        setup(blob, [tgt("111", 45.10, -83.02, 6.4)])
        f = fleet.get_fleet()
        if any("rating column" in g for g in (f.get("gaps") or [])): ISSUE(f"column set but flagged: {sc}")
        elif f["fleet"][0].get("corrected_delta_s") is None: ISSUE(f"column set but not scored: {sc}")
        else: OK(f"column set ({list(sc)[0]}): scored, no note")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

print("\n== 14. #311: a race's own ToD allowance column beats GPH ==")
try:
    if fleet._allowance_s_per_nm({"orc_gph": 600.0, "tod_allowance": 620.0}) != 620.0: ISSUE("tod_allowance ignored")
    elif fleet._allowance_s_per_nm({"orc_gph": 600.0, "tod_allowance": None}) != 600.0: ISSUE("no GPH fallback")
    else: OK("allowance = tod_allowance, else GPH")
    def _delta(them, us, tod_col="XX_TOD"):
        blob = {"scoring": {"method": "Time-on-Distance", "rating_system": "ORC", "rating_column": "XX_TOT",
                            "rating_column_tod": tod_col},
                "own": us, "fleet": [dict({"boat": "Fast", "mmsi": "111"}, **them)]}
        setup(blob, [tgt("111", 45.10, -83.00, 6.0)])
        return fleet.get_fleet()["fleet"][0].get("corrected_delta_s")
    gph_only = _delta({"orc_gph": 580.0}, {"orc_gph": 600.0}, tod_col="")      # no ToD column: GPH
    with_col = _delta({"orc_gph": 580.0, "tod_allowance": 560.0}, {"orc_gph": 600.0, "tod_allowance": 620.0})
    if gph_only is None or with_col is None: ISSUE(f"ToD not scored: {gph_only} {with_col}")
    elif gph_only == with_col: ISSUE("tod_allowance made no difference to the corrected gap")
    else: OK(f"corrected gap uses the race's ToD column ({gph_only} s on GPH vs {with_col} s)")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

print("\n== 15. #311: with a named column, a missing number is 'no rating', never a GPH stand-in ==")
try:
    own = {"rating": 1.0, "orc_gph": 600.0}
    # ToT race on column B; Defiance's B rating was cleared, only GPH left
    blob = {"scoring": {"method": "Time-on-Time", "rating_system": "ORC", "rating_column": "COL_B"},
            "own": own, "fleet": [{"boat": "Defiance", "mmsi": "111", "orc_gph": 500.0},
                                  {"boat": "Rated", "mmsi": "222", "rating": 1.05, "orc_gph": 580.0}]}
    setup(blob, [tgt("111", 45.10, -83.02, 6.4), tgt("222", 45.09, -83.01, 6.2)])
    f = fleet.get_fleet()
    rows = {r["boat"]: r for r in f["fleet"]}
    st = {r["boat"]: r for r in f["standings"]}
    if rows["Defiance"].get("corrected_delta_s") is not None: ISSUE("GPH-derived ToT used on a named column")
    elif not rows["Defiance"].get("no_rating") or not st["Defiance"].get("no_rating"): ISSUE(f"not shown as no rating: {rows['Defiance']}")
    elif st["Defiance"].get("rank") is not None: ISSUE("a boat with no rating was ranked")
    elif rows["Rated"].get("corrected_delta_s") is None: ISSUE("a rated boat lost its gap")
    elif "1 boat with no rating for the race's column, no corrected time for them" not in (f.get("gaps") or []):
        ISSUE(f"no gap note: {f.get('gaps')}")
    else: OK("named ToT column: unrated boat shown as no rating, not ranked, gap note")
    # our own rating cleared too → no corrected gaps at all (never 600/GPH)
    blob["own"] = {"orc_gph": 600.0}
    setup(blob, [tgt("222", 45.09, -83.01, 6.2)])
    f = fleet.get_fleet()
    if f["fleet"][0].get("corrected_delta_s") is not None: ISSUE("own ToT made up from GPH")
    elif "own rating missing, no corrected gaps" not in (f.get("gaps") or []): ISSUE(f"own gap: {f.get('gaps')}")
    else: OK("named column, own rating missing: no corrected gaps")
    # ToD race with a named ToD column: a missing allowance is not replaced by GPH
    blob = {"scoring": {"method": "Time-on-Distance", "rating_system": "ORC", "rating_column": "T",
                        "rating_column_tod": "D"},
            "own": {"orc_gph": 600.0, "tod_allowance": 620.0},
            "fleet": [{"boat": "NoAlw", "mmsi": "111", "orc_gph": 580.0}]}
    setup(blob, [tgt("111", 45.10, -83.00, 6.0)])
    f = fleet.get_fleet()
    if f["fleet"][0].get("corrected_delta_s") is not None: ISSUE("GPH used for a missing ToD allowance")
    elif not f["fleet"][0].get("no_rating"): ISSUE("ToD: not shown as no rating")
    else: OK("named ToD column: missing allowance = no rating")
    # no column named (old races): GPH-derived numbers still work as before
    blob = {"scoring": {"method": "Time-on-Time"}, "own": {"orc_gph": 600.0},
            "fleet": [{"boat": "G", "mmsi": "111", "orc_gph": 580.0}]}
    setup(blob, [tgt("111", 45.10, -83.02, 6.4)])
    f = fleet.get_fleet()
    if f["fleet"][0].get("corrected_delta_s") is None: ISSUE("no column named: GPH fallback lost")
    else: OK("no column named: GPH fallback unchanged")
except Exception as e:
    ISSUE(f"crash: {type(e).__name__}: {e}")

print("\n" + ("ALL CLEAN — no issues found" if not issues else f"{len(issues)} ISSUE(S) FOUND:"))
for i in issues: print("  -", i)
sys.exit(1 if issues else 0)
