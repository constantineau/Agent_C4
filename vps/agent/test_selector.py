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

def stub(bundle=BUNDLE, tac=None, dev=None, dft=None, reset=True):
    # a fresh scenario is a fresh boat: clear BOTH pieces of carried state, the downwind
    # confirmation clock and the settle latch that steadies the card. `reset=False` changes the
    # conditions on the SAME boat — which is what the settling tests below are about.
    if reset:
        selector._CONFIRM.clear()
        selector._SETTLE.clear()
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

# --- downwind pivot hygiene (PLAYBOOK_V2 locked input #5; 2025 known-answer backtest) ------------
# Downwind, the decisive persistent-shift SWITCH must CONFIRM (default 60 min) before it fires;
# upwind keeps fire-on-persistent. The 2025 replay showed 13/15 wrong-side calls were short-lived
# downwind excursions — this timer is what kills them.
print("downwind confirmation:")
B_RIGHT = {"race_id": "u", "recommended": "right",
           "variants": [{"id": "right", "name": "Right"}, {"id": "left", "name": "Left"}]}
def wind_pos(pos):
    w = wind(True, "left")
    w["point_of_sail"] = pos
    return w
T0 = 1_800_000_000.0
selector._CONFIRM.clear()
stub(bundle=B_RIGHT, tac=wind_pos("upwind"))
check("upwind: immediate switch (unchanged)", selector.get_selector(now=T0)["action"] == "switch")
selector._CONFIRM.clear()
stub(bundle=B_RIGHT, tac=wind_pos("downwind"))
r = selector.get_selector(now=T0)
check("downwind t=0: hold-watch, confirming", r["action"] == "hold" and r["status"] == "watch"
      and r["confirming"]["favored"] == "left")
# relative to the bar, not a hard-coded 30/61 min — the bar is a tuned number and moved on
# 2026-09-17; assertions that pin it in two places rot the moment it does
BAR = selector.SWITCH_CONFIRM_DOWNWIND_S
check("downwind, halfway to the bar: still confirming, and says how far",
      selector.get_selector(now=T0 + BAR / 2)["confirming"]["held_s"] == round(BAR / 2))
check("downwind sustained past the bar: switch fires",
      selector.get_selector(now=T0 + BAR + 60)["action"] == "switch")
# Dropout handling (Cole, 2026-09-17: "forgive dropouts and lower the bar"). The clock used to
# clear-fast on ANY dropout; the detector feeding it chatters, so a short lapse is noise.
# NOTE: these re-stubs MUST pass reset=False. They did not when `stub()` first learned to clear
# state, and the old clear-fast assertion went on passing while testing nothing at all.
# They drive `_decide` rather than `get_selector`: this is the confirmation CLOCK under test, and
# the card's settle wrapper would otherwise stand between the test and it.
selector._CONFIRM.clear()
stub(bundle=B_RIGHT, tac=wind_pos("downwind"))
selector._decide(None, now=T0)                              # clock starts
stub(bundle=B_RIGHT, tac=wind(False, "either"), reset=False)
selector._decide(None, now=T0 + 60)                         # a 1-minute dropout...
stub(bundle=B_RIGHT, tac=wind_pos("downwind"), reset=False)
check("a dropout INSIDE the grace does not reset the clock — it keeps running through it",
      selector._decide(None, now=T0 + 120)["confirming"]["held_s"] == 120)
stub(bundle=B_RIGHT, tac=wind(False, "either"), reset=False)
selector._decide(None, now=T0 + 180)
selector._decide(None, now=T0 + 180 + selector.SWITCH_GRACE_S + 30)   # ...outlives the grace
stub(bundle=B_RIGHT, tac=wind_pos("downwind"), reset=False)
check("a dropout that OUTLIVES the grace does reset it",
      selector._decide(None, now=T0 + 240 + selector.SWITCH_GRACE_S)["confirming"]["held_s"] == 0)
# Swapping sides is not a flicker. BUNDLE recommends "middle", so BOTH left and right are
# decisive here — with B_RIGHT, "favours right" is simply the recommended side and therefore not
# decisive at all, i.e. an ordinary (forgiven) lapse.
def wind_side(side, pos="downwind"):
    w = wind(True, side)
    w["point_of_sail"] = pos
    return w

selector._CONFIRM.clear()
stub(tac=wind_side("left"))
selector._decide(None, now=T0)
stub(tac=wind_side("right"), reset=False)                  # the other side takes over
selector._decide(None, now=T0 + 60)
stub(tac=wind_side("left"), reset=False)
check("a shift that swaps sides drops the old clock outright — that is not a flicker",
      selector._decide(None, now=T0 + 120)["confirming"]["held_s"] == 0)
selector._CONFIRM.clear()

# --- na paths -----------------------------------------------------------------------------------
print("na:")
stub(bundle={})
check("no playbook → na", selector.get_selector()["action"] == "na")

# --- the card has to be steady enough to read (2026-09-17) ---------------------------------------
# Measured on the Jul 18 2026 full-race timeline: this tile changed state 40 times in 9 hours, in
# 20 excursions, TEN of them a minute or less. The cause is one layer up — tactics' persistence
# test is a bare threshold and its quantity sat within +/-10% of that threshold for 13% of the
# race, flipping 74 times. A recommendation that appears and withdraws inside one tack teaches the
# crew to stop reading the card.
print("settling — a verdict has to hold before the crew sees it:")
S = selector.SETTLE_S

stub(tac=wind(False, "either"), reset=False)
base = selector.get_selector(now=T0)
check("the first read is shown immediately — nothing to settle against",
      base["status"] == "ok" and "settling" not in base)

stub(tac=wind(True, "left", "backing"), reset=False)                    # a switch call appears...
r = selector.get_selector(now=T0 + 30)
check("a change does NOT reach the card on its first frame",
      r["status"] == "ok" and r["action"] == "hold")
check("...but the card SAYS a change is firming up, and to what",
      r["settling"]["to_action"] == "switch" and r["settling"]["need_s"] == S
      and r["settling"]["held_s"] == 0)
stub(tac=wind(False, "either"), reset=False)                            # ...and withdraws inside a minute
r = selector.get_selector(now=T0 + 60)
check("a one-minute excursion never reaches the card at all",
      r["status"] == "ok" and r["action"] == "hold" and "settling" not in r)

selector._SETTLE.clear()
stub(tac=wind(False, "either"), reset=False)
selector.get_selector(now=T0)
stub(tac=wind(True, "left", "backing"), reset=False)
selector.get_selector(now=T0 + 30)
check("halfway through, the wait is reported honestly",
      selector.get_selector(now=T0 + 30 + S / 2)["settling"]["held_s"] == round(S / 2))
check("a change that HOLDS for the settle does reach the card",
      selector.get_selector(now=T0 + 30 + S)["action"] == "switch")

selector._SETTLE.clear()
stub(tac=wind(True, "left", "backing"), reset=False)
selector.get_selector(now=T0)
stub(tac=wind(False, "either"), reset=False)
check("de-escalation settles too — most of Jul 18's churn was an alarm withdrawing",
      selector.get_selector(now=T0 + 30)["action"] == "switch")
check("...and clears once it holds",
      selector.get_selector(now=T0 + 30 + S)["action"] == "hold")

selector._SETTLE.clear()
stub(tac=wind(False, "either"), reset=False)
selector.get_selector(now=T0)
stub(tac=wind(True, "left", "backing"), reset=False)
selector.get_selector(now=T0 + 30)
stub(tac=wind(True, "right"), reset=False)                              # a DIFFERENT change part-way through
r = selector.get_selector(now=T0 + 30 + S * 0.9)
check("a different change starts its own clock rather than inheriting the first one's",
      r["settling"]["held_s"] == 0 and r["settling"]["to_action"] == "off_script")

selector._SETTLE.clear()
stub(bundle={})
check("an `na` read is passed straight through, never a held stale verdict",
      selector.get_selector(now=T0)["action"] == "na")
selector._SETTLE.clear()

print("\n", "ALL PASS" if ok else "FAILURES ABOVE")
raise SystemExit(0 if ok else 1)
