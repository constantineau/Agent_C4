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

print("\n" + ("ALL CLEAN — no issues found" if not issues else f"{len(issues)} ISSUE(S) FOUND:"))
for i in issues: print("  -", i)
sys.exit(1 if issues else 0)
