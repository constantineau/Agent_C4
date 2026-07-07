"""Tier-1 play matcher (Playbook v2 Phase D) — unit test. Stubs the signal reads + the frozen
bundle so it runs standalone; locks arm-slow/clear-fast, per-predicate logic, the crew sail-state
signals, and the payload shape.

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_matcher.py
"""
import time

from app import matcher

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


BUNDLE = {"race_id": "u", "schema": "c4.playbook/v2", "variants": [{"id": "middle"}], "plays": [
    {"id": "pace_behind_2h_1", "name": "2h behind at the gate", "category": "internal",
     "scenario": {"kind": "pace"}, "stakes_min": 63,
     "conditions": {"predicates": [
         {"signal": "time_behind_min", "op": ">=", "value": 104, "sustain_min": 45}]},
     "applicability": {"legs": [1]}, "response": {"type": "route"},
     "summary": "the pre-routed answer"},
    {"id": "gear_loss_a2", "name": "A2 out of service", "category": "internal",
     "scenario": {"kind": "gear_loss"}, "stakes_min": 276,
     "conditions": {"predicates": [
         {"signal": "sail_out_of_service", "op": "==", "value": "A2"}]},
     "response": {"type": "route"}, "summary": "re-planned without the A2"},
    {"id": "reef_r1_a3_slot", "name": "Reef 1 with the A3", "category": "internal",
     "scenario": {"kind": "sail_guidance"}, "stakes_min": 0,
     "conditions": {"predicates": [
         {"signal": "tws_kn", "op": ">=", "value": 16, "sustain_min": 10},
         {"signal": "hoisted_sail", "op": "==", "value": "A3"}]},
     "response": {"type": "guidance", "guidance": "tuck in reef 1 — open the slot"}},
    {"id": "low_maneuver", "name": "Low-maneuver", "category": "internal",
     "scenario": {"kind": "low_maneuver"}, "stakes_min": 318,
     "conditions": {"predicates": [
         {"signal": "fatigue_index", "op": ">=", "value": 60, "sustain_min": 30}]},
     "response": {"type": "route"}},
]}

SIGNALS = {}


class StubDS:
    state = {}

    def get_sail_state(self):
        return dict(self.state)

    def save_sail_state(self, blob):
        StubDS.state = dict(blob)

    def latest_value(self, path):
        v = SIGNALS.get("_tws_ms")
        return (v, time.time()) if v is not None else None


def stub(bundle=BUNDLE, **sig):
    matcher.deviation._load_playbook = lambda: bundle
    matcher.deviation.get_deviation = lambda route=None: {
        "available": sig.get("time_behind_min") is not None or sig.get("xte_nm") is not None,
        "time_behind_s": (sig.get("time_behind_min") or 0) * 60
        if sig.get("time_behind_min") is not None else None,
        "xte_nm": sig.get("xte_nm")}
    matcher.drift_mod.get_drift = lambda route=None: {"available": False}
    matcher.tactics.get_tactics = lambda route=None: {"available": False}
    matcher.fatigue.get_fatigue = lambda: ({"index": sig["fatigue"]} if "fatigue" in sig else {})
    matcher.datasource.active = lambda: StubDS()
    SIGNALS.clear()
    if sig.get("tws_kn") is not None:
        SIGNALS["_tws_ms"] = sig["tws_kn"] / 1.943844
    matcher.clear_state()


print("1) availability")
stub(bundle=None)
check("no playbook -> na", matcher.get_plays().get("available") is False)
stub(bundle={"race_id": "u", "variants": [{"id": "m"}]})
r = matcher.get_plays()
check("v1 bundle (no plays) -> na with a v2 hint", r.get("available") is False
      and "v2" in (r.get("note") or ""))

print("2) arm slow / clear fast (sustain discipline)")
matcher.SUSTAIN_SCALE = 1.0
stub(time_behind_min=120)
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "pace_behind_2h_1")
check("predicates true -> ARMING immediately (not armed)", p["status"] == "arming")
matcher._ST["pace_behind_2h_1"]["since"] = time.time() - 46 * 60      # rewind the hold clock
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "pace_behind_2h_1")
check("held past sustain -> ARMED", p["status"] == "armed")
check("armed list carries it", "pace_behind_2h_1" in r["armed"])
matcher.deviation.get_deviation = lambda route=None: {"available": True, "time_behind_s": 30 * 60,
                                                      "xte_nm": 0.4}
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "pace_behind_2h_1")
check("signal drops -> QUIET instantly (clear fast)", p["status"] == "quiet")
check("hold memory wiped on clear", "since" not in matcher._ST["pace_behind_2h_1"])

print("3) crew sail-state signals")
matcher.SUSTAIN_SCALE = 0.0
stub()
matcher.set_sail_state(out_of_service=["A2"])
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "gear_loss_a2")
check("crew declares the A2 blown -> gear play ARMED", p["status"] == "armed")
matcher.set_sail_state(out_of_service=[])
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "gear_loss_a2")
check("repaired/cleared -> quiet", p["status"] == "quiet")
stub(tws_kn=18.0)
matcher.set_sail_state(hoisted="A3")
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "reef_r1_a3_slot")
check("18 kn + A3 hoisted -> the slot-reef play arms", p["status"] == "armed")
check("guidance text rides on the armed play", "open the slot" in (p.get("guidance") or ""))
matcher.set_sail_state(hoisted="S2")
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "reef_r1_a3_slot")
check("same breeze, S2 hoisted -> quiet (AND semantics)", p["status"] == "quiet")

print("4) missing data never arms")
stub()          # no fatigue value
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "low_maneuver")
check("fatigue signal absent -> low-maneuver quiet", p["status"] == "quiet"
      and p["predicates"][0]["actual"] is None)

print("5) ordering + payload shape")
stub(tws_kn=18.0, time_behind_min=120)
matcher.set_sail_state(hoisted="A3", out_of_service=["A2"])
r = matcher.get_plays()
statuses = [x["status"] for x in r["plays"]]
check("armed sort first", statuses == sorted(statuses, key=lambda s: {"armed": 0, "arming": 1,
                                                                      "quiet": 2}[s]))
check("payload carries signals + sail state + grounding",
      "signals" in r and r["sail_state"].get("hoisted") == "A3" and "get_deviation" in r["based"])
check("no heavy route payloads ride along", all("route" not in x for x in r["plays"]))

print("RESULT:", "PASS" if ok else "FAIL")

print("6) corroborators — raise confidence, never gate (2026-07-08)")
CORR_BUNDLE = {"race_id": "u", "schema": "c4.playbook/v2", "variants": [{"id": "m"}], "plays": [
    {"id": "shift_right_20", "name": "Right shift", "category": "external",
     "scenario": {"kind": "rotation"}, "stakes_min": 300,
     "conditions": {
         "predicates": [{"signal": "time_behind_min", "op": ">=", "value": 60, "sustain_min": 0}],
         "corroborators": [{"signal": "upcourse_twd_shift_deg", "op": ">=", "value": 12,
                            "why": "buoy reads the shift"}]},
     "response": {"type": "route"}}]}
matcher.SUSTAIN_SCALE = 0.0
stub(bundle=CORR_BUNDLE, time_behind_min=90)
r = matcher.get_plays()
pl = r["plays"][0]
check("predicates alone ARM the play (dark buoy never blocks)",
      pl["status"] == "armed" and pl["corroborated"] is False)
# now the up-course signal agrees
matcher.gather = (lambda orig: (lambda route=None: {**orig(route),
                                                    "upcourse_twd_shift_deg": 15,
                                                    "_upcourse_name": "N Lake Huron buoy"}))(matcher.gather)
r = matcher.get_plays()
pl = r["plays"][0]
check("agreeing buoy -> corroborated + named",
      pl["status"] == "armed" and pl["corroborated"]
      and pl["corroborated_by"] == "up-course buoy N Lake Huron buoy")
check("corroborator rows ride with why", pl["corroborators"][0]["ok"]
      and "buoy" in pl["corroborators"][0]["why"])
# corroborator true but predicates false -> still quiet (it can't arm anything by itself)
matcher.deviation.get_deviation = lambda route=None: {"available": True, "time_behind_s": 0,
                                                      "xte_nm": 0.1}
r = matcher.get_plays()
check("corroborator alone can never arm", r["plays"][0]["status"] == "quiet")

print("RESULT-6:", "PASS" if ok else "FAIL")
