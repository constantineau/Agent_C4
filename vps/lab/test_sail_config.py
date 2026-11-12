"""Boat sail-config overlays — Code 0 (light-air reacher) + mainsail reef points.

Neither is in the ORC cert (it rates only J1/A3/S1 of the carried sails), so — like the J2/J3 change-downs — they are
crew-band LABEL overlays: routing SPEED stays the rated envelope; the bands set the sail CALL.
Locked here:
  - optimal_sail: the C0 takes the jib slot inside its {tws_max, twa_min, twa_max} band, nowhere
    else; jib change-downs and kite bands unaffected; disabled config is inert;
  - reef_for: depower reef at r1_tws_kn any point of sail; the A3-slot reef at the LOWER
    r1_a3_slot_tws_kn only with the A3 up;
  - crossovers_specialized: the C0 carves its TWA range out of the jib zone on light rows only;
  - routing: a light-air reach leg carries sail C0 with NO phantom peel (shares the J1 curve);
    a breeze leg carries the reef decoration (leg.reef/reef_why);
  - plays: the sail-guidance scan emits a J1→C0 play when the breeze drops into the band, and the
    reef plays fire near their thresholds (the A3-slot play predicated on hoisted A3);
  - the _POS_TWA vocabulary fix (legs say beat/reach/run, not upwind/reaching/downwind).
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

from app import optimizer as OPT       # noqa: E402
from app import sailplan               # noqa: E402
from app import scenarios as SCEN      # noqa: E402
from app import synthesis as SYN       # noqa: E402
import polar_fixture  # noqa: E402
polar_fixture.install()     # bare optimizer/sail-plan calls route on sr33's own polar (#303)

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


CFG = {"code0": {"enabled": True, "tws_max": 9, "twa_min": 55, "twa_max": 110},
       "main_reefs": {"r1_tws_kn": 20, "r1_a3_slot_tws_kn": 16}}
JIBS = [{"sail": "J1", "tws_max": 14}, {"sail": "J2", "tws_min": 14, "tws_max": 20},
        {"sail": "J3", "tws_min": 20}]

# ---- 1) optimal_sail with the C0 band ------------------------------------------------------------
print("1) optimal_sail — C0 takes the jib slot in-band only")
check("light reach in-band -> C0", sailplan.optimal_sail(6, 80, JIBS, CFG) == "C0")
check("same angle, heavier air -> not C0", sailplan.optimal_sail(12, 80, JIBS, CFG) != "C0")
check("light but too tight (40 deg) -> the jib", sailplan.optimal_sail(6, 40, JIBS, CFG) == "J1")
check("light but deep (150 deg) -> a kite", sailplan.optimal_sail(6, 150, JIBS, CFG) in ("A3", "S1"))
check("disabled band is inert",
      sailplan.optimal_sail(6, 80, JIBS, {"code0": {"enabled": False, "tws_max": 9,
                                                    "twa_min": 55, "twa_max": 110}}) == "J1")
check("no config unchanged", sailplan.optimal_sail(6, 80, JIBS) == "J1")
check("jib change-downs unaffected in a breeze", sailplan.optimal_sail(22, 40, JIBS, CFG) == "J3")

# ---- 2) reef_for ---------------------------------------------------------------------------------
print("2) reef_for — depower + the A3 slot")
mr = CFG["main_reefs"]
r = sailplan.reef_for(22, "beat", "J3", mr)
check("22 kn beat -> reef 1 (depower)", r and r["reef"] == "R1" and "depower" in r["why"])
r = sailplan.reef_for(17, "run", "A3", mr)
check("17 kn under the A3 -> reef 1 (open the slot)", r and "slot" in r["why"])
check("17 kn under the S1 -> no reef (below depower threshold)",
      sailplan.reef_for(17, "run", "S1", mr) is None)
check("12 kn -> no reef", sailplan.reef_for(12, "beat", "J1", mr) is None)
check("no thresholds -> no reef", sailplan.reef_for(30, "beat", "J3", {}) is None)

# ---- 3) crossover-chart carve --------------------------------------------------------------------
print("3) crossovers_specialized — the C0 carves the jib zone on light rows")
rows = sailplan.crossovers_specialized(JIBS, CFG)
light = rows.get("6") or rows.get("6.0") or []
heavy = rows.get("16") or rows.get("16.0") or []
c0z = [z for z in light if z.get("short") == "C0"]
print(f"     6 kn row: {[(z['short'], z['twa_min'], z['twa_max']) for z in light]}")
check("6 kn row has a C0 zone", len(c0z) == 1)
check("C0 zone clipped to the band",
      c0z and c0z[0]["twa_min"] >= 55 and c0z[0]["twa_max"] <= 110)
check("16 kn row has no C0", not any(z.get("short") == "C0" for z in heavy))
check("no config -> raw rows", not any(z.get("short") == "C0"
      for z in (sailplan.crossovers_specialized([], None).get("6") or [])))

# ---- 4) routing — C0 leg label, no phantom peel; reef decoration --------------------------------
print("4) routing — leg labels + no phantom peels")


class WF:
    loaded = True

    def __init__(self, tws, twd=0.0):
        self.tws, self.twd = tws, twd

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}

    def status(self):
        return {"models": []}


# ~13 nm due-east reach at 44N, wind FROM north (twa ~90) in 6 kn -> the C0 band
reach = {"courses": [{"id": "x", "start": {"lat": 44.0, "lon": -82.0},
                      "finish": {"points": [{"lat": 44.0, "lon": -81.7}]}}]}
r = OPT.optimize_course(reach, "x", 0, WF(6.0), avoid=False, emit_exploration=False,
                        resolution="fast", time_budget_s=30, jib_crossovers=JIBS, sail_config=CFG)
plan = [s["sail"] for s in (r.get("sail_plan") or [])]
print(f"     6 kn reach: leg sail={r['legs'][0].get('sail')} plan={plan} peels={r.get('total_peels')}")
check("light reach leg calls the C0", r["legs"][0].get("sail") == "C0")
check("sail plan carries the C0", "C0" in plan)
check("C0 relabel is free (no phantom peel)", (r.get("total_peels") or 0) == 0)
r2 = OPT.optimize_course(reach, "x", 0, WF(6.0), avoid=False, emit_exploration=False,
                         resolution="fast", time_budget_s=30, jib_crossovers=JIBS, sail_config=CFG)
r3 = OPT.optimize_course(reach, "x", 0, WF(6.0), avoid=False, emit_exploration=False,
                         resolution="fast", time_budget_s=30, jib_crossovers=JIBS)
print(f"     overlay ETA delta: {abs((r2.get('total_minutes') or 0) - (r3.get('total_minutes') or 0)):.1f} min")
check("label overlay leaves the routed time essentially unchanged (<5 min — peel-accounting only)",
      abs((r2.get("total_minutes") or 0) - (r3.get("total_minutes") or 0)) < 5.0)
# 22 kn beat -> J3 + reef 1
beat = {"courses": [{"id": "x", "start": {"lat": 44.0, "lon": -82.0},
                     "finish": {"points": [{"lat": 44.2, "lon": -82.0}]}}]}
rb = OPT.optimize_course(beat, "x", 0, WF(22.0), avoid=False, emit_exploration=False,
                         resolution="fast", time_budget_s=30, jib_crossovers=JIBS, sail_config=CFG)
lg = rb["legs"][0]
print(f"     22 kn beat: sail={lg.get('sail')} reef={lg.get('reef')} ({lg.get('reef_why')})")
check("22 kn beat carries J3 + reef 1", lg.get("sail") == "J3" and lg.get("reef") == "R1")
check("reef why says depower", "depower" in (lg.get("reef_why") or ""))
lgt = r["legs"][0]
check("6 kn leg carries no reef", lgt.get("reef") is None)

# ---- 5) plays — C0 crossover + reef guidance -----------------------------------------------------
print("5) guidance plays — sail the config, alert the team")
cons = {"legs": [{"sail": "A3", "point_of_sail": "reach", "wind": {"tws": 10.5}}]}
plays = SYN._sail_guidance_plays(cons, JIBS, CFG)
c0p = next((p for p in plays if p["params"].get("change_to") == "C0"), None)
print(f"     sail plays: {[p['id'] for p in plays]}")
check("breeze dying under the A3 -> set the Code 0 (an A3->C0 play)", c0p is not None
      and c0p["params"]["direction"] == "under" and c0p["params"]["tws_threshold"] <= 9.5)
cons_r = {"legs": [{"sail": "J1", "point_of_sail": "beat", "wind": {"tws": 17.0}},
                   {"sail": "A3", "point_of_sail": "run", "wind": {"tws": 14.0}}]}
rp = SYN._reef_guidance_plays(cons_r, CFG)
ids = [p["id"] for p in rp]
print(f"     reef plays: {ids}")
check("depower reef play fires near its threshold", "reef_r1_depower" in ids)
check("A3-slot reef play fires with the kite up", "reef_r1_a3_slot" in ids)
slot = next(p for p in rp if p["id"] == "reef_r1_a3_slot")
preds = SCEN.INTERNAL_DETECT["sail_guidance"](slot["params"], {})
check("slot play predicates = TWS >= threshold AND hoisted A3",
      any(x["signal"] == "tws_kn" and x["value"] == 16.0 for x in preds)
      and any(x["signal"] == "hoisted_sail" and x["value"] == "A3" for x in preds))
check("no reef thresholds -> no reef plays", SYN._reef_guidance_plays(cons_r, {}) == [])

# ---- 6) the _POS_TWA vocabulary fix --------------------------------------------------------------
print("6) point-of-sail vocabulary")
check("legs' beat/reach/run map to real scan angles",
      SYN._POS_TWA["beat"] == 45.0 and SYN._POS_TWA["reach"] == 100.0
      and SYN._POS_TWA["run"] == 150.0)

# ---- 7) another boat's own sail names (#305) -------------------------------------------------------
print("7) the boat's own sail names, by family")
check("an A2 opens the slot like the A3 (any asym)",
      (sailplan.reef_for(17, "run", "A2", mr) or {}).get("why") == "open the slot between the A2 and the main")
check("a symmetric S2 gets no slot reef", sailplan.reef_for(17, "run", "S2", mr) is None)
gx = [{"sail": "G1", "tws_max": 12}, {"sail": "G2", "tws_min": 12, "tws_max": 18}, {"sail": "G3", "tws_min": 18}]
check("genoa change-downs pick by TWS", [sailplan.jib_for_tws(t, gx, "G1") for t in (8, 15, 22)]
      == ["G1", "G2", "G3"])
check("no change-downs: the rated headsail itself", sailplan.jib_for_tws(15, [], "G1") == "G1")
check("a genoa change-down and a Code 0 read the rated headsail's curve",
      OPT._sail_polar_key("G2", ["G1", "A2"]) == "G1" and OPT._sail_polar_key("C0", ["G1"]) == "G1"
      and OPT._sail_polar_key("A2", ["G1", "A2"]) == "A2")
check("sr33 unchanged: J2/J3/C0 read the J1 curve",
      all(OPT._sail_polar_key(s, ["J1", "A3", "S1"]) == "J1" for s in ("J2", "J3", "C0")))
from app import playbook as PB   # noqa: E402
check("gear-loss plays for every kite in the plan, asym first",
      PB._gear_sails({"G1", "S2", "A2", "C0"}) == ["A2", "S2"]
      and PB._gear_sails({"J1", "A3", "S1"}) == ["A3", "S1"])
cons_a2 = {"legs": [{"wind": {"tws": 15}, "sail": "A2", "twa": 140}]}
a2 = next((p for p in SYN._reef_guidance_plays(cons_a2, {"main_reefs": mr})
           if p["id"] == "reef_r1_a3_slot"), None)
check("the slot-reef play names the boat's own asym",
      a2 is not None and a2["name"] == "Reef 1 with the A2 — open the slot"
      and a2["params"].get("hoisted") == "A2")

# ---- 8) the boat's own Code 0 name and sail_families reach the sail plan (Codex on #334) ----------
print("8) a custom Code 0 name and the boat's sail_families")
from app import boats as BOATS   # noqa: E402
RB = {"boat_id": "rb", "draft_m": 2.0, "sail_inventory": ["J1", "J2", "J3", "REACHER", "BIGRED", "S1"],
      "sail_families": {"REACHER": "code0", "BIGRED": "asym"}, "code0": CFG["code0"],
      "main_reefs": mr}
RCFG = BOATS.sail_config(RB)
check("the sail config carries the Code 0's own name and the family map",
      RCFG.get("code0_sail") == "REACHER" and RCFG.get("sail_families", {}).get("BIGRED") == "asym")
check("sr33's sail config is unchanged (no extra keys)",
      set(BOATS.sail_config({"sail_inventory": ["J1", "C0", "A3"], "code0": CFG["code0"],
                             "main_reefs": mr})) == {"code0", "main_reefs"})
check("in the band the plan calls REACHER, never a C0 the boat doesn't carry",
      sailplan.optimal_sail(6, 80, JIBS, RCFG) == "REACHER")
rrows = sailplan.crossovers_specialized(JIBS, RCFG)
shorts = {z.get("short") for zs in rrows.values() for z in zs}
check("the crossover chart carves REACHER, no C0", "REACHER" in shorts and "C0" not in shorts)
check("the slot reef fires for BIGRED (asym by the family map)",
      (sailplan.reef_for(17, "run", "BIGRED", mr, RCFG) or {}).get("reef") == "R1"
      and sailplan.reef_for(17, "run", "BIGRED", mr) is None)
check("a named headsail is a headsail by the family map",
      sailplan._is_headsail("HEAVY", {"sail_families": {"HEAVY": "jib"}})
      and not sailplan._is_headsail("HEAVY"))
check("REACHER reads the rated headsail's curve, BIGRED its own",
      OPT._sail_polar_key("REACHER", ["J1", "A3"], RCFG) == "J1"
      and OPT._sail_polar_key("BIGRED", ["J1", "BIGRED"], RCFG) == "BIGRED")
check("gear-loss plays use the family map", PB._gear_sails({"J1", "BIGRED", "S1"}, RCFG) == ["BIGRED", "S1"])

# ---- 9) holding speed of a named change-down reads the rated headsail (Codex round 2 on #334) -----
print("9) a named change-down's holding speed")
SPG = {"G1": [[10, 40, 5.0], [10, 60, 6.0], [10, 90, 6.5], [14, 40, 5.5], [14, 60, 6.5], [14, 90, 7.0]]}
DOMG = OPT._sail_domains(SPG)
HCFG = {"sail_families": {"HEAVY": "jib"}}
check("HEAVY holds on the G1 curve with the family map",
      OPT._sail_speed(SPG, DOMG, "HEAVY", 12, 60, HCFG) > 5.5
      and OPT._sail_speed(SPG, DOMG, "HEAVY", 12, 60) == 0.0)
check("the synthesis' play pricing reads it too",
      (SYN._sail_speed(SPG, "HEAVY", 12, 60, HCFG) or 0) > 5.5 and SYN._sail_speed(SPG, "HEAVY", 12, 60) is None)

print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
