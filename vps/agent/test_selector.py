"""Branch selector — unit test for the unified HOLD / SWITCH / OFF-SCRIPT decision + graceful
degradation + signal concordance. Stubs the three trigger reads so it runs standalone.

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_selector.py
"""
from app import selector

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

# bundle: recommended middle; a LEFT variant exists, NO right variant (for the off-script case)
BUNDLE = {"race_id": "u", "recommended": "middle",
          "variants": [{"id": "middle", "name": "Middle start"},
                       {"id": "left", "name": "Left start", "what_flips_it": "breeze backs left of ~190°"}]}

def stub(bundle=BUNDLE, tac=None, dev=None, dft=None):
    selector.deviation._load_playbook = lambda: bundle
    selector.tactics.get_tactics = lambda route=None: (tac or {"available": False})
    selector.deviation.get_deviation = lambda route=None: (dev or {"available": False, "status": "na"})
    selector.drift_mod.get_drift = lambda route=None: (dft or {"available": False, "status": "na"})

def wind(persistent, favored, trend="steady", osc=8):
    return {"available": True, "favored_side": favored,
            "wind": {"persistent": persistent, "trend": trend, "oscillation_deg": osc}}

# --- persistent shift favours LEFT (≠ recommended middle), left variant exists → SWITCH ---------
print("switch to a pre-authored branch:")
stub(tac=wind(True, "left", "backing"))
r = selector.get_selector()
print("  ", r["action"], "|", r["value"], "| tier", r["tier"], "| conf", r["confidence"], "| by", r["driven_by"])
check("action switch", r["action"] == "switch")
check("target = left variant", r["target_variant"] == "left")
check("tier 1 (pre-authored)", r["tier"] == 1)
check("status act", r["status"] == "act")
check("driven by the shift", "get_tactics" in r["driven_by"])

# --- + concurring drift (backed → favours left) + deviation working left → higher confidence -----
print("reinforced by drift + deviation:")
stub(tac=wind(True, "left", "backing"),
     dev={"available": True, "status": "watch", "xte_side": "left", "xte_nm": 0.6, "variant": "middle"},
     dft={"available": True, "status": "act", "drift_dir": "backed", "drift_twd_deg": 22})
r = selector.get_selector()
print("  ", r["action"], "| conf", r["confidence"], "| by", r["driven_by"])
check("all three signals drive it", set(["get_tactics", "get_drift", "get_deviation"]) <= set(r["driven_by"]))
check("confidence higher than the bare switch (>0.75)", r["confidence"] > 0.75)
check("why cites the reinforcement", "Reinforced" in r["why"])

# --- persistent shift favours RIGHT, but NO right variant aboard → OFF-SCRIPT (tier 2) -----------
print("off-script (no branch for the favoured side):")
stub(tac=wind(True, "right", "veering"))
r = selector.get_selector()
print("  ", r["action"], "| tier", r["tier"], "|", r["value"])
check("action off_script", r["action"] == "off_script")
check("tier 2", r["tier"] == 2)
check("no target variant", r["target_variant"] is None)
check("consider mentions sailing your own", "your own" in r["consider"])

# --- persistent shift CONFIRMS the recommended side → HOLD confirmed -----------------------------
print("hold confirmed:")
stub(tac=wind(True, "middle"))          # favored == recommended 'middle' → but favored must be left/right
# middle isn't left/right, so this falls through to default hold; test the true-confirm with a
# bundle whose recommended is 'left' and the shift favours left:
stub(bundle={**BUNDLE, "recommended": "left"}, tac=wind(True, "left"))
r = selector.get_selector()
check("favoured side == recommended → hold, ok", r["action"] == "hold" and r["status"] == "ok")
check("value holds the recommended", "Hold" in r["value"] and "Left" in r["value"])

# --- drift act but NO persistent shift → HOLD + reassess (watch) ---------------------------------
print("forecast-drift reassess:")
stub(tac=wind(False, "either"), dft={"available": True, "status": "act", "drift_dir": "veered", "drift_twd_deg": 30})
r = selector.get_selector()
print("  ", r["action"], "|", r["status"], "|", r["value"])
check("hold but status watch (reassess)", r["action"] == "hold" and r["status"] == "watch")
check("driven by drift", r["driven_by"] == ["get_drift"])

# --- oscillating, nothing decisive → HOLD default -----------------------------------------------
print("default hold:")
stub(tac=wind(False, "either", osc=12))
r = selector.get_selector()
check("default hold, ok", r["action"] == "hold" and r["status"] == "ok")

# --- na paths -----------------------------------------------------------------------------------
print("na:")
stub(bundle={})
check("no playbook → na", selector.get_selector()["action"] == "na")

print("\n", "ALL PASS" if ok else "FAILURES ABOVE")
raise SystemExit(0 if ok else 1)
