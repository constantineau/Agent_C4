"""Handicap-aware fleet tactics — unit test for matching, course-progress, and the ORC
corrected-time delta. Stubs the data source so it runs standalone (no DB / no live AIS).

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_fleet.py
  or inside the engine container:  docker ... exec -w /srv engine python test_fleet.py
"""
from app import fleet, ais

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

# --- a fake data source: own ship + raw AIS + roster + course marks --------------------------
class FakeSource:
    def __init__(self, blob, targets, marks):
        self._blob, self._targets, self._marks = blob, targets, marks
    def get_fleet(self): return self._blob
    def ais_targets(self, max_age_min): return self._targets
    def marks(self, route): return self._marks
    def latest_value(self, path): return None        # own ship comes from _own_ship stub

# Straight 10 nm course due north: Start (44.00,-82.0) → Finish (44.1667,-82.0)
MARKS = [{"seq": 1, "name": "Start", "lat": 44.0, "lon": -82.0},
         {"seq": 2, "name": "Finish", "lat": 44.0 + 10.0/60.0, "lon": -82.0}]

ROSTER = [
    {"boat": "Defiance", "division": "I", "orc_gph": 600.0, "mmsi": "366000001"},  # faster (low GPH)
    {"boat": "Windquest", "division": "I", "orc_gph": 660.0, "mmsi": None},         # slower, name-match
]
BLOB = {"fleet": ROSTER, "scoring": {"system": "ORC", "method": "Single-Number Time-on-Time (ToT)"},
        "own": {"boat": "C4 SR33", "orc_gph": 630.0}}

# own ship 2 nm up the course (8 nm to finish), 7 kn straight up
OWN = {"lat": 44.0 + 2.0/60.0, "lon": -82.0, "sog": 7.0, "cog": 0.0}
ais._own_ship = lambda: OWN

# targets: Defiance 4 nm up (ahead on water, 6 nm to go), Windquest 1 nm up (behind), an unknown laker
TARGETS = [
    {"mmsi": "366000001", "name": "DEFIANCE", "lat": 44.0 + 4.0/60.0, "lon": -82.0, "sog": 7.5, "cog": 0.0, "time": 0},
    {"mmsi": "999999999", "name": "Windquest", "lat": 44.0 + 1.0/60.0, "lon": -82.0, "sog": 6.5, "cog": 0.0, "time": 0},
    {"mmsi": "111111111", "name": "Algoma Spirit", "lat": 44.05, "lon": -82.02, "sog": 12.0, "cog": 90.0, "time": 0},
]

fleet.datasource.active = lambda: FakeSource(BLOB, TARGETS, MARKS)

res = fleet.get_fleet()
print("scoring:", res["scoring_method"], "| matched:", res["count_matched"], "| traffic:", res["count_traffic"])
for r in res["fleet"]:
    print(f"   {r['boat']:10s} by={r['matched_by']:5s} dtf={r.get('dtf_nm')} lead={r.get('on_water_lead_nm')} "
          f"corr={r.get('corrected_delta_s')}s tag={r['tag']} conf={r['confidence']}")

check("available", res["available"] is True)
check("2 competitors matched (Defiance by MMSI, Windquest by name)", res["count_matched"] == 2)
check("the laker is unmatched traffic", res["count_traffic"] == 1 and res["traffic"][0]["name"] == "Algoma Spirit")

byboat = {r["boat"]: r for r in res["fleet"]}
check("Defiance matched by mmsi", byboat["Defiance"]["matched_by"] == "mmsi")
check("Windquest matched by name (mmsi mismatch)", byboat["Windquest"]["matched_by"] == "name")

# own dtf = 8.0, Defiance dtf = 6.0, Windquest dtf = 9.0
check("own distance-to-finish ≈ 8 nm", abs(res["own"]["dtf_nm"] - 8.0) < 0.05)
check("Defiance dtf ≈ 6 nm", abs(byboat["Defiance"]["dtf_nm"] - 6.0) < 0.05)
check("Defiance is ahead on the water (+lead)", byboat["Defiance"]["on_water_lead_nm"] > 0)
check("Windquest is behind on the water (−lead)", byboat["Windquest"]["on_water_lead_nm"] < 0)

# corrected-time: Defiance is closer to the finish AND lower GPH (faster, smaller ToT coeff)
#   → projected to beat us → corrected_delta_s < 0
check("Defiance projected to beat us (corrected < 0)", byboat["Defiance"]["corrected_delta_s"] < 0)
check("Windquest projected behind us (corrected > 0)", byboat["Windquest"]["corrected_delta_s"] > 0)
check("Defiance tagged ahead_corrected (it's beating us)", byboat["Defiance"]["tag"] == "ahead_corrected")
check("Windquest tagged behind_corrected", byboat["Windquest"]["tag"] == "behind_corrected")

# leverage: both dead on the rhumb here → ~0
check("leverage ~0 on the rhumb", abs(byboat["Defiance"].get("leverage_nm") or 0) < 0.05)

# --- no roster loaded → graceful unavailable ------------------------------------------------
fleet.datasource.active = lambda: FakeSource({}, TARGETS, MARKS)
empty = fleet.get_fleet()
check("no roster → available False with a note", empty["available"] is False and "roster" in empty["note"].lower())

# --- ToD scoring path -----------------------------------------------------------------------
BLOB_TOD = {"fleet": ROSTER, "scoring": {"system": "ORC", "method": "Time-on-Distance (ToD)"},
            "own": {"boat": "C4 SR33", "orc_gph": 630.0}}
fleet.datasource.active = lambda: FakeSource(BLOB_TOD, TARGETS, MARKS)
tod = fleet.get_fleet()
check("ToD method reported", "Distance" in tod["scoring_method"])
check("ToD still produces a corrected delta", tod["fleet"][0].get("corrected_delta_s") is not None)

print("RESULT:", "PASS" if ok else "FAIL")
import sys; sys.exit(0 if ok else 1)
