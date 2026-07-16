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
    sel.deviation._load_playbook = lambda: bundle
    sel.tactics.get_tactics = lambda route=None: (tac or {"available": False})
    sel.deviation.get_deviation = lambda route=None: (dev or {"available": False, "status": "na"})
    sel.drift_mod.get_drift = lambda route=None: (dft or {"available": False, "status": "na"})
    strategy.fleet_mod.get_fleet = lambda max_range_nm=40.0: (fleet or {"available": False, "fleet": [], "traffic": []})

def wind(persistent, favored, trend="steady", osc=8):
    return {"available": True, "favored_side": favored,
            "wind": {"persistent": persistent, "trend": trend, "oscillation_deg": osc}}

def grounded(items):
    return all(it.get("grounded_in") for it in items)

# --- 1. CONCORDANT switch: shift-left + drift-backed(→left) + deviation-left + fleet-left → STRONG ---
print("concordant → strong, consolidate:")
stub(tac=wind(True, "left", "backing"),
     dev={"available": True, "status": "watch", "xte_side": "left", "xte_nm": 0.6, "time_behind_s": 0, "variant": "middle"},
     dft={"available": True, "status": "act", "drift_dir": "backed", "drift_twd_deg": 22, "drift_tws_kn": 1.5},
     fleet={"available": True, "fleet": [
         {"boat": "Rival A", "tag": "rival", "leverage_nm": -0.8, "confidence": 0.6, "corrected_delta_s": -40}], "traffic": []})
r = strategy.get_strategy_signals()
print("  ", r["assessment"], "|", r["concordance"]["strength"], "| conf", r["confidence"])
check("available", r["available"])
check("concordance strong", r["concordance"]["strength"] == "strong")
check("lean left", r["concordance"]["lean"] == "left")
check("recommendation is a switch", r["recommendation"]["action"].startswith("Switch"))
check("assessment says converging left", "converging left" in r["assessment"].lower())
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
check("no re-route chained (no-plan ≠ off-book departure)", "reoptimize" not in r)

# oscillating, no playbook → sail-your-phase read (still available, not blind)
stub(bundle=None, tac=wind(False, "either", "steady", osc=10),
     fleet={"available": False, "fleet": [], "traffic": []})
r = strategy.get_strategy_signals()
check("oscillating no-playbook still available", r["available"] is True)
check("oscillating rec mentions phase/oscillating", "phase" in r["recommendation"]["action"].lower() or "oscillat" in r["recommendation"]["action"].lower())

# --- 5. NOTHING aboard → not available ---------------------------------------------------------------
print("nothing aboard:")
stub(bundle=None, tac={"available": False}, fleet={"available": False, "fleet": [], "traffic": []})
r = strategy.get_strategy_signals()
check("not available", r["available"] is False)
check("still returns a disclaimer + caveat", bool(r["caveats"]) and "disclaimer" in r)

# --- 5b. OFF-BOOK recommendation → chains the onboard re-route offer (Phase 3) ----------------------
print("off-book → re-route offer chained:")
# a persistent shift favouring a side with NO variant aboard drives the selector off_script → off-book.
BUNDLE_ONESIDED = {"race_id": "u", "recommended": "middle", "headline": "one-sided",
                   "variants": [{"id": "middle", "name": "Middle start"}]}
stub(bundle=BUNDLE_ONESIDED, tac=wind(True, "right", "veering"))
# stub the heavy re-optimizer so the test stays standalone (no nav/routing/engine).
_ro_calls = {"n": 0}
def _fake_reopt(route=None):
    _ro_calls["n"] += 1
    return {"available": True, "off_playbook": True, "eta_min": 254, "tacks": 9, "sailed_nm": 46.2,
            "sail_plan": ["J1", "A3", "S1"], "path": [{"lat": 45.0, "lon": -83.0}] * 500,
            "legs": [{"mark": "Finish"}], "vs_playbook": {"available": True, "max_divergence_nm": 2.4}}
strategy.reoptimize_mod.get_reoptimize = _fake_reopt
r = strategy.get_strategy_signals()
print("  ", r["recommendation"]["action"], "| vs_playbook", r["recommendation"]["vs_playbook"])
check("recommendation departs the playbook", r["recommendation"]["vs_playbook"] == "departs")
check("re-route offer attached", (r.get("reoptimize") or {}).get("available") is True)
check("offer is compact (no heavy path array)", "path" not in (r.get("reoptimize") or {}))
check("offer keeps eta/tacks/sail_plan", r["reoptimize"].get("eta_min") == 254 and r["reoptimize"].get("tacks") == 9)
check("rec flagged reoptimize ready", r["recommendation"].get("reoptimize") == "ready")
check("rationale mentions the re-route ETA", "re-route is ready" in r["recommendation"]["rationale"])
check("re-optimizer was actually called", _ro_calls["n"] == 1)

# an ON-PLAN hold must NOT run the heavy re-optimizer.
_ro_calls["n"] = 0
stub(tac=wind(False, "either", "steady", osc=10))
r = strategy.get_strategy_signals()
check("on-plan hold does not chain a re-route", "reoptimize" not in r and _ro_calls["n"] == 0)

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

print("\nRESULT:", "ALL GREEN" if ok else "FAILURES ABOVE")
import sys; sys.exit(0 if ok else 1)
