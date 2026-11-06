"""Strategy synthesis (Tier-1 deterministic) — unit test for the cross-signal picture + concordance +
recommendation. Stubs the underlying trigger reads (via the selector) + the fleet read so it runs
standalone, no engine / DB / network.

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_strategy.py
"""
from app import strategy

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

BUNDLE = {"race_id": "u", "recommended": "middle", "headline": "Cove Island split",
          "variants": [{"id": "middle", "name": "Middle start"},
                       {"id": "left", "name": "Left start", "what_flips_it": "breeze backs left of ~190°"}]}

def stub(bundle=BUNDLE, tac=None, dev=None, dft=None, fleet=None):
    sel = strategy.selector_mod
    # each scenario is a fresh boat: the selector's settle/confirm state is per-process and
    # every scenario runs within the same second, so it would carry the last verdict over.
    sel._SETTLE.clear(); sel._CONFIRM.clear()
    sel.deviation._load_playbook = lambda: bundle
    sel.tactics.get_tactics = lambda route=None: (tac or {"available": False})
    sel.deviation.get_deviation = lambda route=None: (dev or {"available": False, "status": "na"})
    sel.drift_mod.get_drift = lambda route=None: (dft or {"available": False, "status": "na"})
    strategy.fleet_mod.get_fleet = lambda max_range_nm=40.0: (fleet or {"available": False, "fleet": [], "traffic": []})

def wind(persistent, favored, trend="steady", osc=8):
    # tactics always carries now/mean_12min when available; the shift row needs them (#46)
    return {"available": True, "favored_side": favored,
            "wind": {"persistent": persistent, "trend": trend, "oscillation_deg": osc,
                     "now": 220.0 if persistent else 212.0, "mean_12min": 210.0}}

def grounded(items):
    return all(it.get("grounded_in") for it in items)

# --- 1. CONCORDANT switch: shift-left + drift-backed(→left) + deviation-left + fleet-left → STRONG ---
print("concordant → strong, consolidate:")
stub(tac=wind(True, "left", "backing"),
     dev={"available": True, "status": "watch", "xte_status": "watch", "xte_side": "left", "xte_nm": 0.6, "time_behind_s": 0, "variant": "middle"},
     dft={"available": True, "status": "act", "drift_dir": "backed", "drift_twd_deg": 22, "drift_tws_kn": 1.5},
     fleet={"available": True, "fleet": [
         {"boat": "Rival A", "tag": "rival", "leverage_nm": -0.8, "confidence": 0.6, "corrected_delta_s": -40}], "traffic": []})
r = strategy.get_strategy_signals()
print("  ", r["assessment"], "|", r["concordance"]["strength"], "| conf", r["confidence"])
check("available", r["available"])
check("concordance strong", r["concordance"]["strength"] == "strong")
check("lean left", r["concordance"]["lean"] == "left")
check("recommendation is a switch", r["recommendation"]["action"].startswith("Switch"))
check("assessment says agree left", "agree left" in r["assessment"].lower())
check("every picture item grounded", grounded(r["picture"]))
check("recommendation grounded", bool(r["recommendation"]["grounded_in"]))
check("picture has a concordance row", any(p["signal"] == "concordance" for p in r["picture"]))

# --- 2. DISCORDANT: shift-left but forecast veered(→right) + fleet-right → SPLIT, caution -----------
print("discordant → split, hold & confirm:")
stub(tac=wind(True, "left", "backing"),
     dft={"available": True, "status": "act", "drift_dir": "veered", "drift_twd_deg": 20, "drift_tws_kn": 0},
     fleet={"available": True, "fleet": [
         {"boat": "Rival B", "tag": "ahead_corrected", "leverage_nm": 1.1, "confidence": 0.7, "corrected_delta_s": -30}], "traffic": []})
r = strategy.get_strategy_signals()
print("  ", r["assessment"], "|", r["concordance"]["strength"])
check("concordance split", r["concordance"]["strength"] == "split")
check("assessment flags the split", "split" in r["assessment"].lower())
check("recommendation urges confirmation", "confirm" in r["recommendation"]["rationale"].lower())
check("split lowers confidence (< high)", r["confidence"] in ("med", "low"))

# --- 3. HOLD with a rival beating us on corrected → nudge to press, urgency soon ---------------------
print("hold + rival threat:")
stub(tac=wind(False, "either", "steady", osc=10),
     fleet={"available": True, "fleet": [
         {"boat": "Rival C", "tag": "rival", "leverage_nm": 0.4, "confidence": 0.5, "corrected_delta_s": -55}], "traffic": []})
r = strategy.get_strategy_signals()
print("  ", r["assessment"], "| urg", r["recommendation"]["urgency"])
check("recommendation holds", r["recommendation"]["action"].startswith("Hold"))
check("rival nudge in rationale", "rival" in r["recommendation"]["rationale"].lower())
check("urgency raised to soon", r["recommendation"]["urgency"] == "soon")
check("fleet read in the picture", any(p["signal"] == "fleet" for p in r["picture"]))

# --- 4. NO PLAYBOOK but a fleet roster loaded → still available, no-plan, low confidence -------------
print("no playbook, fleet only:")
stub(bundle=None,
     fleet={"available": True, "fleet": [
         {"boat": "Rival D", "tag": "ahead_corrected", "leverage_nm": -0.5, "confidence": 0.6, "corrected_delta_s": -20}], "traffic": []})
r = strategy.get_strategy_signals()
print("  ", r["assessment"], "| vs_playbook", r["recommendation"]["vs_playbook"])
check("available (reasons from fleet)", r["available"])
check("vs_playbook = no-plan", r["recommendation"]["vs_playbook"] == "no-plan")
check("confidence low", r["confidence"] == "low")
check("caveat flags no gameplan", any("no gameplan" in c.lower() for c in r["caveats"]))

# --- 4b. NO PLAYBOOK but a persistent tactical shift → strip still shows favoured-side (Tactics-tile
#         fallback: the strip must not go blind without a gameplan) ------------------------------------
print("no playbook, persistent shift (Tactics fallback):")
stub(bundle=None, tac=wind(True, "right", "veering"),
     fleet={"available": False, "fleet": [], "traffic": []})
r = strategy.get_strategy_signals()
print("  ", r["assessment"], "| rec:", r["recommendation"]["action"])
check("available from the tactical read alone", r["available"] is True)
check("picture carries the shift read grounded in get_tactics",
      any(p["signal"] == "shift" and "get_tactics" in (p.get("grounded_in") or []) for p in r["picture"]))
check("favoured side named in the assessment", "right" in r["assessment"].lower())
check("recommendation is no-plan (nothing to branch within)", r["recommendation"]["vs_playbook"] == "no-plan")
check("recommendation grounded in get_tactics", "get_tactics" in r["recommendation"]["grounded_in"])
check("no re-route suggested (no-plan ≠ off-book departure)", "reoptimize" not in r
      and "reroute_suggest" not in r)

# oscillating, no playbook → sail-your-phase read (still available, not blind)
stub(bundle=None, tac=wind(False, "either", "steady", osc=10),
     fleet={"available": False, "fleet": [], "traffic": []})
r = strategy.get_strategy_signals()
check("oscillating no-playbook still available", r["available"] is True)
check("oscillating rec says play the shifts", "shifts" in r["recommendation"]["action"].lower())

# --- 5. NOTHING aboard → not available ---------------------------------------------------------------
print("nothing aboard:")
stub(bundle=None, tac={"available": False}, fleet={"available": False, "fleet": [], "traffic": []})
r = strategy.get_strategy_signals()
check("not available", r["available"] is False)
check("still returns a disclaimer + caveat", bool(r["caveats"]) and "disclaimer" in r)

# --- 5b. OFF-BOOK recommendation → SUGGESTS a re-route, never computes one (#125) -----------------
print("off-book → a quiet re-route suggestion, nothing computed:")
# a persistent shift favouring a side with NO variant aboard drives the selector off_script → off-book.
BUNDLE_ONESIDED = {"race_id": "u", "recommended": "middle", "headline": "one-sided",
                   "variants": [{"id": "middle", "name": "Middle start"}]}
stub(bundle=BUNDLE_ONESIDED, tac=wind(True, "right", "veering"))
# trip-wire: ANY route computation from the synthesis fails the test (button only)
from app import reoptimize as _reopt_mod, orinroute as _orin_mod  # noqa: E402
_ro_calls = {"n": 0}
def _tripwire(*a, **k):
    _ro_calls["n"] += 1
    raise AssertionError("a route was computed without the button")
_reopt_mod._compute = _tripwire
_reopt_mod.compute = _tripwire
_reopt_mod.run = _tripwire
_orin_mod.run = _tripwire
r = strategy.get_strategy_signals()
print("  ", r["recommendation"]["action"], "| suggest", r.get("reroute_suggest"))
check("recommendation departs the playbook", r["recommendation"]["vs_playbook"] == "departs")
check("no route computed (no re-optimizer call)", _ro_calls["n"] == 0)
check("no route offer attached", "reoptimize" not in r and "reoptimize" not in r["recommendation"])
check("rationale doesn't claim a route is ready", "route ready" not in r["recommendation"]["rationale"].lower())
sug = r.get("reroute_suggest") or {}
check("suggestion with a short plain reason", sug.get("reason") == "Off the playbook. The right is paying."
      and len(sug["reason"].split()) <= 12)

# an ON-PLAN hold: no suggestion, no computation
stub(tac=wind(False, "either", "steady", osc=10))
r = strategy.get_strategy_signals()
check("on-plan hold: no suggestion, nothing computed", "reroute_suggest" not in r and _ro_calls["n"] == 0)

print("suggestion reasons:")
S = strategy._reroute_suggestion
check("off-book with no side", S({"vs_playbook": "departs"}, {}, {}, None)["reason"] == "Off the playbook.")
check("our wind right of the plan", S({"vs_playbook": "on-plan"}, {}, {},
      {"status": "act", "obs_twd": 5, "plan_twd": 350, "obs_tws_kn": 12, "plan_tws_kn": 12})["reason"]
      == "Wind shifted 15° right of the plan.")
check("our wind left of the plan", "left" in S({}, {}, {}, {"status": "act", "obs_twd": 330,
                                                          "plan_twd": 350})["reason"])
check("more wind than the plan", S({}, {}, {}, {"status": "act", "obs_twd": 350, "plan_twd": 352,
                                               "obs_tws_kn": 17, "plan_tws_kn": 12})["reason"]
      == "Wind 5 kts more than the plan.")
check("forecast moved (never veered/backed)", S({}, {}, {"drift": {"status": "act", "dir": "veered",
                                                                  "deg": 28}}, None)["reason"]
      == "Forecast shifted 28° right since the plan.")
check("a watch-level gap stays quiet", S({}, {}, {"drift": {"status": "watch", "dir": "right", "deg": 12}},
                                          {"status": "watch", "obs_twd": 10, "plan_twd": 350}) is None)

# --- 6. _fleet_lean directly: confidence-weighted side of the threats -------------------------------
print("fleet-lean math:")
side, strength, n = strategy._fleet_lean({"available": True, "fleet": [
    {"tag": "rival", "leverage_nm": -1.0, "confidence": 1.0},
    {"tag": "ahead_corrected", "leverage_nm": -0.6, "confidence": 1.0},
    {"tag": "behind_corrected", "leverage_nm": 3.0, "confidence": 1.0}]})   # behind boats ignored
check("threats lean left", side == "left")
check("n_threats excludes the behind boat", n == 2)
side2, _, _ = strategy._fleet_lean({"available": False})
check("no fleet → no lean", side2 is None)

# --- 7. Rival side is relative to US, not to the course (#160) -------------------------------------
# leverage_nm is each boat's cross-track from the course line. We're 5 nm right, the rival 2 nm
# right: the rival is 3 nm to our LEFT.
print("rival side relative to us (#160):")
def fleet_read(flt):
    rows = [p for p in strategy._picture({}, flt, {"strength": "none"}) if p["signal"] == "fleet"]
    return rows[0]["read"] if rows else ""
rival = {"boat": "Rival", "tag": "rival", "leverage_nm": 2.0, "on_water_lead_nm": 0.5,
         "confidence": 0.6, "corrected_delta_s": -120}
rd = fleet_read({"available": True, "own": {"leverage_nm": 5.0}, "fleet": [rival]})
print("  ", rd)
check("rival 3 nm to our left", "3.0 nm to our left" in rd)
check("not 'to our right'", "to our right" not in rd)
rd = fleet_read({"available": True, "own": {"leverage_nm": 2.0}, "fleet": [rival]})
print("  ", rd)
check("same offset → no side claimed", "to our" not in rd and "ahead" in rd)
rd = fleet_read({"available": True, "own": {"leverage_nm": None}, "fleet": [rival]})
print("  ", rd)
check("own offset unknown → says 'of the course', not 'to our'",
      "2.0 nm right of the course" in rd and "to our" not in rd)

# Leverage is measured against each boat's NEAREST leg, so across a turn the two offsets can belong to
# different legs and their difference means nothing. Course (0,0)→(0,10)→(10,10): we're at (1,9),
# +1 nm right of the north leg; the rival at (9,11) is −1 nm (left) of the east leg. Subtracting says
# "2.0 nm to our left", but the rival is ~8 nm to our RIGHT. Different legs → claim no side.
print("rival on a different leg (#160 turn):")
from app import fleet as _fleet
_pts = [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0)]
_, own_lev, own_leg = _fleet._course_progress(1.0, 9.0, _pts)
_, riv_lev, riv_leg = _fleet._course_progress(9.0, 11.0, _pts)
check("repro geometry: own on leg 0, rival on leg 1", (own_leg, riv_leg) == (0, 1))
rival_turn = {**rival, "leverage_nm": riv_lev, "leg_index": riv_leg}
rd = fleet_read({"available": True, "own": {"leverage_nm": own_lev, "leg_index": own_leg},
                 "fleet": [rival_turn]})
print("  ", rd)
check("different legs → no side claimed", "to our" not in rd and "of the course" not in rd and "ahead" in rd)
rd = fleet_read({"available": True, "own": {"leverage_nm": 5.0, "leg_index": 1},
                 "fleet": [{**rival, "leg_index": 1}]})
check("same leg → side still given", "3.0 nm to our left" in rd)

# Behind a leg's start the clamped projection lands on the endpoint, so "cross-track" became the
# distance to the mark and the side could flip. Course (0,0)→(0,10): we're at (1,-5), the rival at
# (2,-1) — the rival is 1 nm to our RIGHT. Leverage must be the offset from the leg's LINE.
print("rival beyond a leg endpoint (#160):")
_pts1 = [(0.0, 0.0), (0.0, 10.0)]
_, own_lev, own_leg = _fleet._course_progress(1.0, -5.0, _pts1)
_, riv_lev, riv_leg = _fleet._course_progress(2.0, -1.0, _pts1)
rd = fleet_read({"available": True, "own": {"leverage_nm": own_lev, "leg_index": own_leg},
                 "fleet": [{**rival, "leverage_nm": riv_lev, "leg_index": riv_leg}]})
print("  ", rd)
check("behind the start: rival 1.0 nm to our right", "1.0 nm to our right" in rd)

# --- 8. one boat, a clock that moves: a settling change reaches the strategy output (#40) ---------
print("continuous boat: settling reaches /strategy, built from fresh signals (#40):")
_sel = strategy.selector_mod
_real_time = _sel.time.time
CLOCK = [1_800_000_000.0]
_sel.time.time = lambda: CLOCK[0]
try:
    stub(tac=wind(False, "either", osc=10))
    r = strategy.get_strategy_signals()
    check("t=0: hold, nothing settling", r["recommendation"]["action"].startswith("Hold")
          and "settling" not in r)
    # same boat (no stub reset): a lasting left shift and a forecast that agrees
    sel_ = strategy.selector_mod
    sel_.tactics.get_tactics = lambda route=None: wind(True, "left", "backing")
    sel_.drift_mod.get_drift = lambda route=None: {"available": True, "status": "act",
                                                   "drift_dir": "backed", "drift_twd_deg": 22}
    CLOCK[0] += 30
    r = strategy.get_strategy_signals()
    print("  ", r["recommendation"]["action"], "| settling", r.get("settling"), "|", r["concordance"]["note"])
    check("the card's hold stands while the switch settles", r["recommendation"]["action"].startswith("Hold"))
    check("…and the strategy output says what it is moving to",
          (r.get("settling") or {}).get("to_action") == "switch")
    check("concordance is built from the fresh shift (lean left)", r["concordance"]["lean"] == "left")
    check("the forecast row is in the picture (fresh drift)",
          any(p["signal"] == "forecast" for p in r["picture"]))
    CLOCK[0] += _sel.SETTLE_S
    r = strategy.get_strategy_signals()
    check("after the settle: switch, nothing settling",
          r["recommendation"]["action"].startswith("Switch") and "settling" not in r)
finally:
    _sel.time.time = _real_time

# --- 9. time behind alone doesn't vote a side in the concordance (#43) ------------------------------
print("time behind alone doesn't vote a side (#43):")
stub(tac=wind(True, "left", "backing"),
     dev={"available": True, "status": "act", "xte_status": "ok", "xte_side": "right", "xte_nm": 0.02,
          "time_behind_s": 400, "variant": "middle"})
r = strategy.get_strategy_signals()
print("  ", r["concordance"]["note"])
check("not split by a boat that is merely behind", r["concordance"]["strength"] != "split")
check("the picture still says we're behind", any(p["signal"] == "deviation" and "behind" in p["read"]
                                                 for p in r["picture"]))

# --- 10. no wind data with a gameplan aboard: no all-clear, no 'steady near 0°' (#46) ---------------
print("no wind data, gameplan aboard (#46):")
stub()
r = strategy.get_strategy_signals()
print("  ", r["assessment"], "|", [p["read"] for p in r["picture"]])
check("not a Hold recommendation", not r["recommendation"]["action"].startswith("Hold"))
check("says there's no wind data", "No wind data" in r["assessment"])
check("no shift row describing 0°", not any(p["signal"] == "shift" for p in r["picture"]))
check("not in no-gameplan mode", r["recommendation"]["vs_playbook"] == "on-plan")

print("\nRESULT:", "ALL GREEN" if ok else "FAILURES ABOVE")
import sys; sys.exit(0 if ok else 1)
