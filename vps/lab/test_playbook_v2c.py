"""Playbook v2 Phase C — internal plays (docs/PLAYBOOK_V2.md §3, §9).

Locks the Phase-C machinery deterministically (constant wind, no network, no LLM):
  - gear-loss routing: exclude_sails drops the sail's curve AND rebuilds the envelope as the
    max over the REMAINING sails (route slower/equal, excluded sail never in the sail plan);
  - pace routing: from_mark routes only the REMAINING course from an intermediate mark;
  - low-maneuver: maneuver_prune_mult biases the search away from tacks (fewer or equal
    maneuvers, still reaches the mark, ETA cost stays honest);
  - INTERNAL_DETECT predicates: pace percentile-framed off the venue's fleet-normal stats,
    gear-loss crew-armed, sail-guidance TWS-threshold, low-maneuver fatigue, rejoin XTE;
  - sail-guidance crossover scan: a nominal leg's sail near a crossover yields a guidance play
    with the boundary TWS + the change-to sail, grounded in the frozen boat model;
  - rejoin-vs-continue tabulation: representative off-track positions on a long leg produce
    per-side rows with honest continue/rejoin minutes (continuing wins on a homogeneous reach).
"""
import math
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Resolve the seed dir wherever the test runs: the repo (vps/lab/../db/seed) OR docker-cp'd to /srv.
# Only override the env when the seed is actually found (a bad guess zeroes out the polars).
for _seed in (os.path.join(HERE, "..", "db", "seed"), "/srv"):
    if os.path.exists(os.path.join(_seed, "polars_sr33.sql")):
        os.environ["POLARS_FILE"] = os.path.join(_seed, "polars_sr33.sql")
        os.environ["SAIL_POLARS_FILE"] = os.path.join(_seed, "sr33_sail_polars.json")
        os.environ["CROSSOVERS_FILE"] = os.path.join(_seed, "sr33_crossovers.json")
        break

from app import optimizer as OPT       # noqa: E402
from app import playbook as PB         # noqa: E402
from app import scenarios as SCEN      # noqa: E402
from app import synthesis as SYN       # noqa: E402
from app import polars as POL          # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class WF:
    """Constant-wind field stub good enough for optimize_course (loaded/status included)."""
    loaded = True

    def __init__(self, tws=12.0, twd=0.0):
        self.tws, self.twd = tws, twd

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}

    def status(self):
        return {"models": []}


# ---- 1) INTERNAL_DETECT predicates -------------------------------------------------------------
print("1) INTERNAL_DETECT predicates (percentile-framed, locked input #3)")
vs = {"behind_median_min": 149, "behind_p90_min": 384, "xte_median_nm": 3.5, "xte_p90_nm": 6.0}
p = SCEN.pace_predicates({"delay_h": 2}, {"venue_stats": vs})
check("pace 2h-behind arms above fleet-normal (0.7×median=104)",
      p == [{"signal": "time_behind_min", "op": ">=", "value": 104, "sustain_min": 45}])
p = SCEN.pace_predicates({"delay_h": 4}, {"venue_stats": vs})
check("pace 4h-behind (deep) arms near the venue p90 (min(384, 240)=240)",
      p[0]["value"] == 240 and p[0]["op"] == ">=")
p = SCEN.pace_predicates({"delay_h": 4}, {"venue_stats": {"behind_p90_min": 200}})
check("deep play caps at the venue p90 when p90 < the scenario delay", p[0]["value"] == 200)
p = SCEN.pace_predicates({"delay_h": -2}, {})
check("ahead-of-plan play keys on NEGATIVE time-behind",
      p[0]["op"] == "<=" and p[0]["value"] == -60)
p = SCEN.pace_predicates({"delay_h": 2}, {})
check("no venue stats -> conservative defaults (0.7×150=105)", p[0]["value"] == 105)
p = SCEN.gear_loss_predicates({"sail": "A2"}, {})
check("gear-loss is crew-armed (sail_out_of_service == A2)",
      p == [{"signal": "sail_out_of_service", "op": "==", "value": "A2"}])
p = SCEN.low_maneuver_predicates({}, {})
check("low-maneuver keys on the fatigue index (>=60, sustained)",
      p[0]["signal"] == "fatigue_index" and p[0]["value"] == 60)
p = SCEN.rejoin_predicates({"consider_nm": 3.5}, {"venue_stats": vs})
check("rejoin opens at the venue consider band (xte >= 3.5 nm)",
      p[0]["signal"] == "xte_nm" and p[0]["value"] == 3.5)
p = SCEN.rejoin_predicates({}, {})
check("rejoin default threshold 3.5 nm with no stats", p[0]["value"] == 3.5)
p = SCEN.sail_guidance_predicates({"tws_threshold": 14.0, "direction": "over", "hoisted": "J1"}, {})
check("sail-guidance = TWS threshold + hoisted sail",
      p[0] == {"signal": "tws_kn", "op": ">=", "value": 14.0, "sustain_min": 10}
      and p[1] == {"signal": "hoisted_sail", "op": "==", "value": "J1"})

# ---- 2) gear-loss: exclude_sails rebuilds the envelope -----------------------------------------
print("2) gear-loss routing (exclude_sails: curve dropped + envelope rebuilt)")
# dead run ~18 nm: start due north of finish, wind FROM north -> S2 is the nominal run sail
run_def = {"courses": [{"id": "x", "start": {"lat": 44.3, "lon": -82.0},
                        "finish": {"points": [{"lat": 44.0, "lon": -82.0}]}}]}
base = OPT.optimize_course(run_def, "x", 0, WF(), avoid=False, emit_exploration=False,
                           resolution="fast", time_budget_s=30)
noS2 = OPT.optimize_course(run_def, "x", 0, WF(), avoid=False, emit_exploration=False,
                           resolution="fast", time_budget_s=30, exclude_sails=["S2"])
b_sails = {s["sail"] for s in (base.get("sail_plan") or [])}
n_sails = {s["sail"] for s in (noS2.get("sail_plan") or [])}
print(f"     baseline {base.get('total_minutes')} min sails={sorted(b_sails)}; "
      f"no-S2 {noS2.get('total_minutes')} min sails={sorted(n_sails)}")
check("baseline run flies the S2", "S2" in b_sails)
check("excluded route never flies the S2", "S2" not in n_sails)
check("excluded route still routes + reaches", noS2.get("available")
      and OPT._hav_nm(noS2["path"][-1]["lat"], noS2["path"][-1]["lon"], 44.0, -82.0) < 0.5)
check("losing the run kite never makes the boat faster",
      noS2["total_minutes"] >= base["total_minutes"] - 1)

# ---- 3) pace: from_mark routes the REMAINING course --------------------------------------------
print("3) pace routing (from_mark)")
three = {"courses": [{"id": "x", "start": {"lat": 44.0, "lon": -82.0},
                      "marks": [{"name": "M1", "type": "buoy", "lat": 44.0, "lon": -81.7,
                                 "rounding": "none"}],
                      "finish": {"points": [{"lat": 44.2, "lon": -81.7}]}}]}
full = OPT.optimize_course(three, "x", 0, WF(), avoid=False, emit_exploration=False,
                           resolution="fast", time_budget_s=30)
rest = OPT.optimize_course(three, "x", 7200, WF(), avoid=False, emit_exploration=False,
                           resolution="fast", time_budget_s=30, from_mark=1)
p0 = rest["path"][0]
print(f"     full legs={len(full['legs'])}; from_mark=1 legs={len(rest['legs'])} "
      f"start=({p0['lat']},{p0['lon']}) t0={rest['start_epoch']}")
check("full course has 2 legs", len(full["legs"]) == 2)
check("from_mark=1 routes only the remainder (1 leg)", len(rest["legs"]) == 1)
check("remainder starts AT the intermediate mark",
      OPT._hav_nm(p0["lat"], p0["lon"], 44.0, -81.7) < 0.1)
check("remainder starts at the delayed epoch", rest["start_epoch"] == 7200)

# ---- 4) low-maneuver: maneuver_prune_mult ------------------------------------------------------
print("4) low-maneuver routing (maneuver_prune_mult)")
P = POL.polars_stw()
# dead upwind ~10 nm — free-tacking territory; the biased search must tack no MORE than baseline
leg_b = OPT.route_leg(WF(), P, 44.0, -82.0, 0.0, 44.17, -82.0)
leg_l = OPT.route_leg(WF(), P, 44.0, -82.0, 0.0, 44.17, -82.0, maneuver_prune_mult=5.0)
d_b = OPT._hav_nm(leg_b["path"][-1]["lat"], leg_b["path"][-1]["lon"], 44.17, -82.0)
d_l = OPT._hav_nm(leg_l["path"][-1]["lat"], leg_l["path"][-1]["lon"], 44.17, -82.0)
print(f"     baseline tacks={leg_b['tacks']} eta={leg_b['eta']/3600:.2f}h; "
      f"x5 tacks={leg_l['tacks']} eta={leg_l['eta']/3600:.2f}h")
check("both reach the mark", d_b < 0.3 and d_l < 0.3)
check("×5 prune bias tacks no more than baseline", leg_l["tacks"] <= leg_b["tacks"])
check("upwind still requires at least one tack (not a wall)", leg_l["tacks"] >= 1)

# ---- 5) sail-guidance crossover scan -----------------------------------------------------------
print("5) sail-guidance crossover scan (synthesis._sail_guidance_plays)")
jibs = [{"sail": "J1", "tws_max": 14}, {"sail": "J2", "tws_min": 14, "tws_max": 20},
        {"sail": "J3", "tws_min": 20}]
consensus = {"legs": [{"sail": "J1", "point_of_sail": "upwind", "wind": {"tws": 12.0}}]}
plays = SYN._sail_guidance_plays(consensus, jibs)
over = next((x for x in plays if x["params"].get("direction") == "over"), None)
print(f"     {len(plays)} guidance seeds: {[x['id'] for x in plays]}")
check("a building breeze crosses J1 -> J2", over is not None
      and over["params"]["change_to"] == "J2" and abs(over["params"]["tws_threshold"] - 14) <= 1)
check("guidance names the change + threshold",
      over is not None and "J2" in over["guidance"] and "guidance" in over and over.get("route") is None)
check("no crossover -> no play (dead-calm leg skipped)",
      SYN._sail_guidance_plays({"legs": [{"sail": None, "wind": {"tws": 8}}]}, jibs) == [])

# ---- 6) rejoin-vs-continue tabulation ----------------------------------------------------------
print("6) rejoin-vs-continue tabulation (_rejoin_tab)")
# synthetic nominal: a straight due-east 40 nm reach at 8 kn (constant wind from the north)
import app.geo as GEO
_saved_bfc = GEO.build_for_course
GEO.build_for_course = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network in tests"))
try:
    lat0, lon0 = 44.0, -82.0
    deg_per_nm = 1.0 / (60.0 * math.cos(math.radians(lat0)))
    total_nm, kn = 40.0, 8.0
    n = 21
    path = [{"lat": lat0, "lon": round(lon0 + (total_nm * i / (n - 1)) * deg_per_nm, 5),
             "t": (total_nm * i / (n - 1)) / kn * 3600.0} for i in range(n)]
    eta = total_nm / kn * 3600.0
    syn_consensus = {"path": path, "legs": [{"to": "Finish", "eta_epoch": eta, "direct_nm": total_nm}],
                     "start_epoch": 0}
    marks = [(1, "Start", lat0, lon0), (2, "Finish", lat0, path[-1]["lon"])]
    log = []
    rows = PB._rejoin_tab({}, "x", WF(), syn_consensus, None, None, marks, 6.0, log=log)
    for line in log:
        print("     ", line)
    sides = {r["side"] for r in rows}
    check("both sides tabulated", sides == {"left", "right"})
    check("honest positive minutes", all(r["continue_min"] > 0 and r["rejoin_min"] > 0 for r in rows))
    check("on a homogeneous reach, rejoining a straight line never beats continuing",
          all(r["verdict"] in ("continue", "even") for r in rows))
    check("rows carry the offset + leg identity",
          all(r["off_nm"] == 6.0 and r["to"] == "Finish" for r in rows))
    # a short leg (offset comparable to the leg) is skipped
    short = {"path": path[:6], "legs": [{"to": "Finish", "eta_epoch": eta / 4, "direct_nm": 10.0}],
             "start_epoch": 0}
    marks_s = [(1, "Start", lat0, lon0), (2, "Finish", lat0, path[5]["lon"])]
    check("short legs are skipped (offset ~ leg length)",
          PB._rejoin_tab({}, "x", WF(), short, None, None, marks_s, 6.0) == [])
finally:
    GEO.build_for_course = _saved_bfc

# ---- 7) plays assemble through _build_plays (guidance response + table passthrough) ------------
print("7) _build_plays assembly (no LLM)")
_saved_key = SYN.API_KEY
SYN.API_KEY = None
try:
    entry = {"id": "rejoin_vs_continue", "name": "Off the line — rejoin or continue?",
             "kind": "rejoin", "category": "internal",
             "params": {"off_nm": 6.0, "consider_nm": 3.5, "commit_nm": 6.0},
             "narrative_seed": "seed text", "divergence": {"delta_eta_min": 25, "xte_mean_nm": 6.0},
             "guidance": "the tabulated call", "table": [{"leg": 0, "to": "Finish", "side": "left",
                                                          "off_nm": 6.0, "continue_min": 100,
                                                          "rejoin_min": 125, "delta_min": 25,
                                                          "verdict": "continue"}],
             "total_hours": None, "favored_side": None}
    lm = {"id": "low_maneuver", "name": "Low-maneuver", "kind": "low_maneuver",
          "category": "internal", "params": {"prune_mult": 4.0, "maneuvers": 5,
                                             "nominal_maneuvers": 9},
          "narrative_seed": "tired crew", "divergence": {"delta_eta_min": 12, "xte_mean_nm": 1.1},
          "total_hours": 40.0, "favored_side": "middle",
          "route": {"legs": [], "path": path[:3], "total_sailed_nm": 300.0, "total_tacks": 3,
                    "sail_plan": []}}
    fake_pb = {"v2": {"scenario_routes": [entry, lm], "robustness": [], "corridor": {},
                      "pos_profile": {}},
               "consensus": {"legs": []}}
    built, _v2, note = SYN._build_plays(fake_pb, {"name": "Test"}, "x", venue_stats=vs)
    by_id = {x["id"]: x for x in built}
    rj, lo = by_id.get("rejoin_vs_continue"), by_id.get("low_maneuver")
    check("both internal plays built", rj is not None and lo is not None)
    check("rejoin play is a GUIDANCE response with the table attached",
          rj["response"]["type"] == "guidance" and rj["response"]["guidance"] == "the tabulated call"
          and len(rj.get("table") or []) == 1)
    check("rejoin predicates resolved from the venue stats",
          rj["conditions"]["predicates"][0]["signal"] == "xte_nm"
          and rj["conditions"]["predicates"][0]["value"] == 3.5)
    check("low-maneuver play is a ROUTE response keyed on fatigue",
          lo["response"]["type"] == "route"
          and lo["conditions"]["predicates"][0]["signal"] == "fatigue_index")
    check("internal plays sort first", built[0]["category"] == "internal")
finally:
    SYN.API_KEY = _saved_key

print("RESULT:", "PASS" if ok else "FAIL")
