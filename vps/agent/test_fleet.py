"""Handicap-aware fleet tactics — unit test for matching, course-progress, and the ORC
corrected-time delta. Stubs the data source so it runs standalone (no DB / no live AIS).

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_fleet.py
  or inside the engine container:  docker ... exec -w /srv engine python test_fleet.py
"""
import time

from app import fleet, ais, tracker

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

# --- public tracker source ------------------------------------------------------------------
print("--- tracker ---")

# (0) tracker.positions: aged + confidence-reduced fixes from the bench 'sample' provider
tracker._reset_cache()
tk = tracker.positions({"provider": "sample", "delay_min": 15})
check("tracker available with positions", tk["available"] and len(tk["positions"]) == 3)
fix0 = tk["positions"][0]
check("each fix is aged (age_s ~ delay)", 800 < fix0["age_s"] < 1200)   # 15 min ≈ 900 s
check("delayed fix confidence is reduced (<1)", 0.1 <= fix0["confidence"] < 1.0)
check("confidence decays to a floor when stale", tracker._age_conf(3 * 3600) == 0.1)
check("a fresh fix is full confidence", abs(tracker._age_conf(0) - 1.0) < 1e-9)

# (1) over-the-horizon: a roster boat on the tracker but NOT on our AIS → an aged tracker row.
#     'Il Mostro' is in the sample feed but absent from TARGETS; add it to the roster.
ROSTER_TK = ROSTER + [{"boat": "Il Mostro", "division": "I", "orc_gph": 590.0, "mmsi": None}]
BLOB_TK = {"fleet": ROSTER_TK,
           "scoring": {"system": "ORC", "method": "Single-Number Time-on-Time (ToT)"},
           "own": {"boat": "C4 SR33", "orc_gph": 630.0},
           "tracker": {"permitted": True, "provider": "sample", "delay_min": 15}}
fleet.datasource.active = lambda: FakeSource(BLOB_TK, TARGETS, MARKS)
tracker._reset_cache()
rtk = fleet.get_fleet()
byb = {r["boat"]: r for r in rtk["fleet"]}
check("tracker status reports permitted + available", rtk["tracker"] and rtk["tracker"]["permitted"] and rtk["tracker"]["available"])
check("over-the-horizon roster boat added from tracker", "Il Mostro" in byb)
if "Il Mostro" in byb:
    im = byb["Il Mostro"]
    check("the tracker row is sourced=tracker + carries an age", im["source"] == "tracker" and im.get("age_s", 0) > 0)
    check("the tracker row confidence is reduced vs a live AIS match", im["confidence"] < byb["Defiance"]["confidence"])
check("count split: live AIS vs tracker", rtk["count_ais"] == 2 and rtk["count_tracker"] >= 1)
check("Defiance/Windquest stay live AIS (source=ais)", byb["Defiance"]["source"] == "ais" and byb["Windquest"]["source"] == "ais")

# (2) permission gate: tracker present but NOT permitted → no tracker rows + a gap note
BLOB_NP = {**BLOB_TK, "tracker": {"permitted": False, "provider": "sample", "delay_min": 15}}
fleet.datasource.active = lambda: FakeSource(BLOB_NP, TARGETS, MARKS)
tracker._reset_cache()
rnp = fleet.get_fleet()
check("not-permitted tracker → 0 tracker rows", rnp["count_tracker"] == 0)
check("not-permitted tracker → withheld gap note", any("not permitted" in g.lower() for g in (rnp.get("gaps") or [])))
check("not-permitted tracker status flags permitted False", rnp["tracker"] and rnp["tracker"]["permitted"] is False)

# (3) identity enrichment: an unmatched AIS target sitting ON a roster boat's tracker fix gets resolved.
#     Place an MMSI-less, name-less AIS target right at Il Mostro's sample-feed position (45.35,-82.70).
TARGETS_ID = TARGETS + [{"mmsi": "555555555", "name": None, "lat": 45.35, "lon": -82.70, "sog": 8.3, "cog": 15, "time": 0}]
fleet.datasource.active = lambda: FakeSource(BLOB_TK, TARGETS_ID, MARKS)
tracker._reset_cache()
rid = fleet.get_fleet()
bid = {r["boat"]: r for r in rid["fleet"]}
check("unknown AIS target identity-resolved to Il Mostro via tracker", "Il Mostro" in bid and bid["Il Mostro"]["matched_by"] == "tracker_position")
check("position-resolved row is a LIVE AIS source (not the delayed tracker)", "Il Mostro" in bid and bid["Il Mostro"]["source"] == "ais")
check("the resolved target is no longer unmatched traffic", all(t.get("mmsi") != "555555555" for t in rid["traffic"]))

# --- YB provider (bycmack.com/tracking) -----------------------------------------------------
# Real-shaped GetPositions payload (trimmed from the live bayviewmack2025 feed, 2026-06-28).
print("--- tracker: yb provider ---")
_YB_PAYLOAD = {"raceUrl": "bayviewmack2025", "teams": [
    {"serial": 6012, "name": "Epic", "positions": [
        {"latitude": 45.85141, "longitude": -84.60559, "sogKnots": 0.0, "cog": 0,
         "gpsAtMillis": 1752501601000, "dtfNm": 0.0}]},
    {"serial": 5015, "name": "Titan", "positions": [
        # an OLDER fix first + the latest second → exercises the max-gpsAtMillis pick
        {"latitude": 45.0, "longitude": -83.0, "sogKnots": 6.0, "cog": 10, "gpsAtMillis": 1752519000000},
        {"latitude": 45.85149, "longitude": -84.60572, "sogKnots": 0.4, "cog": 76,
         "gpsAtMillis": 1752519450000, "dtfNm": 0.0}]},
    {"serial": 6150, "name": "Unplugged", "positions": []},   # no positions → skipped
]}
yfx = tracker._provider_yb(_YB_PAYLOAD)
check("yb parses one fix per team-with-positions (empty positions skipped)", len(yfx) == 2)
epic = next((f for f in yfx if f["name"] == "Epic"), None)
check("yb fix carries name + lat/lon", epic and abs(epic["lat"] - 45.85141) < 1e-6 and abs(epic["lon"] + 84.60559) < 1e-6)
check("yb gpsAtMillis converted to epoch seconds", epic and abs(epic["time"] - 1752501601.0) < 1e-3)
titan = next((f for f in yfx if f["name"] == "Titan"), None)
check("yb picks the LATEST position per boat (max gpsAtMillis)", titan and abs(titan["lat"] - 45.85149) < 1e-6 and titan["cog"] == 76)
check("yb sog/cog pass through (knots/deg)", titan and titan["sog"] == 0.4)
check("yb dormant payload (error, no teams) → no fixes", tracker._provider_yb({"at": 1, "error": "Unexpected Error"}) == [])

# url building: race id → GetPositions endpoint; explicit url wins; host override honored.
check("yb url built from race id", tracker._yb_url({"race": "bayviewmack2026"}) ==
      "https://cf.yb.tl/API3/Race/bayviewmack2026/GetPositions?t=0")
check("yb url honors host override", tracker._yb_url({"race": "x", "host": "yb.tl"}) ==
      "https://yb.tl/API3/Race/x/GetPositions?t=0")
check("yb explicit url wins over race", tracker._yb_url({"race": "x", "url": "http://e/p"}) == "http://e/p")
check("yb with neither race nor url → no url", tracker._yb_url({}) is None)

# positions(): a yb config with no race id degrades gracefully (no network, clear reason).
tracker._reset_cache()
ytk = tracker.positions({"provider": "yb"})
check("yb with no race id → unavailable + reason (graceful)", ytk["available"] is False and "race id" in (ytk["error"] or ""))

print("RESULT:", "PASS" if ok else "FAIL")


print("S) whole-fleet STANDINGS — rank, DR, division markers (2026-07-08)")
from app import fleet as F

roster = [
    {"boat": "Il Mostro", "division": "I", "rating": 1.10, "orc_gph": 560.0},
    {"boat": "Windquest", "division": "I", "rating": 0.98, "orc_gph": 640.0},
    {"boat": "Defiance", "division": "B", "rating": 0.95, "orc_gph": 660.0},
    {"boat": "Ghost", "division": "B", "rating": 0.94, "orc_gph": 665.0},   # no fix anywhere
]
own_cfg = {"boat": "C4", "division": "B", "rating": 0.96, "orc_gph": 650.0}
rows = [
    # live AIS: Il Mostro well up the course, fast (they will beat us on corrected: big lead)
    {"boat": "Il Mostro", "source": "ais", "dtf_nm": 40.0, "sog": 8.0,
     "corrected_delta_s": -1800, "confidence": 0.9},
    # tracker fix, 30 min old, 2 nm behind us on the water at the fix
    {"boat": "Windquest", "source": "tracker", "age_s": 1800, "dtf_nm": 62.0, "sog": 6.0,
     "corrected_delta_s": 400, "confidence": 0.5},
    # tracker fix with NO sog → DR falls back to the handicap-scaled own pace
    {"boat": "Defiance", "source": "tracker", "age_s": 3600, "dtf_nm": 61.0, "sog": None,
     "corrected_delta_s": 300, "confidence": 0.5},
]
standings, own_rank = F._standings(roster, rows, own_cfg, own_dtf=60.0, own_sog=6.5,
                                   method="tot", is_tod=False, own_tot=0.96, own_alw=None)
by = {r["boat"]: r for r in standings}
check("every roster boat + our own boat present", len(standings) == 5 and "C4" in by)
ranked = [r for r in standings if r.get("rank") is not None]
deltas = [r["corrected_delta_s"] for r in ranked]
check("ranked ascending by corrected delta, us included at 0",
      deltas == sorted(deltas) and by["C4"]["corrected_delta_s"] == 0
      and by["Il Mostro"]["rank"] < by["C4"]["rank"] < by["Windquest"]["rank"])
check("our division marked (B), others not",
      by["C4"]["our_division"] and by["Defiance"]["our_division"]
      and not by["Il Mostro"]["our_division"])
check("division rank computed within B", own_rank["division_of"] >= 2
      and by["C4"].get("division_rank") is not None)
check("DR advanced the aged Windquest fix (dtf shrank, dr_nm recorded)",
      by["Windquest"]["dtf_nm"] < 62.0 and by["Windquest"]["dr_nm"] > 2.5)
check("DR with no SOG uses the handicap-scaled own pace (Defiance advanced)",
      by["Defiance"]["dtf_nm"] < 61.0 and by["Defiance"].get("dr_nm") is not None)
check("DR recomputed the corrected delta", by["Windquest"]["corrected_delta_s"] != 400)
check("no-fix boat unranked at the bottom with a note",
      by["Ghost"].get("rank") is None and "no fix" in (by["Ghost"].get("note") or ""))
check("own_rank summary sane", own_rank["of"] == 4 and own_rank["unranked"] == 1)
check("live AIS row is NOT dead-reckoned", by["Il Mostro"]["dtf_nm"] == 40.0
      and "dr_nm" not in by["Il Mostro"])
print("RESULT-S:", "PASS" if ok else "FAIL")
import sys; sys.exit(0 if ok else 1)
