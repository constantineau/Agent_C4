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

# --- 5. NOTHING aboard → not available ---------------------------------------------------------------
print("nothing aboard:")
stub(bundle=None, fleet={"available": False, "fleet": [], "traffic": []})
r = strategy.get_strategy_signals()
check("not available", r["available"] is False)
check("still returns a disclaimer + caveat", bool(r["caveats"]) and "disclaimer" in r)

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
