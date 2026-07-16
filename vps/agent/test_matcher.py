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
    {"id": "gear_loss_s1", "name": "S1 out of service", "category": "internal",
     "scenario": {"kind": "gear_loss"}, "stakes_min": 276,
     "conditions": {"predicates": [
         {"signal": "sail_out_of_service", "op": "==", "value": "S1"}]},
     "response": {"type": "route"}, "summary": "re-planned without the S1"},
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
matcher.set_sail_state(out_of_service=["S1"])
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "gear_loss_s1")
check("crew declares the S1 blown -> gear play ARMED", p["status"] == "armed")
matcher.set_sail_state(out_of_service=[])
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "gear_loss_s1")
check("repaired/cleared -> quiet", p["status"] == "quiet")
stub(tws_kn=18.0)
matcher.set_sail_state(hoisted="A3")
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "reef_r1_a3_slot")
check("18 kn + A3 hoisted -> the slot-reef play arms", p["status"] == "armed")
check("guidance text rides on the armed play", "open the slot" in (p.get("guidance") or ""))
matcher.set_sail_state(hoisted="S1")
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "reef_r1_a3_slot")
check("same breeze, S1 hoisted -> quiet (AND semantics)", p["status"] == "quiet")

print("4) missing data never arms")
stub()          # no fatigue value
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "low_maneuver")
check("fatigue signal absent -> low-maneuver quiet", p["status"] == "quiet"
      and p["predicates"][0]["actual"] is None)

print("5) ordering + payload shape")
stub(tws_kn=18.0, time_behind_min=120)
matcher.set_sail_state(hoisted="A3", out_of_service=["S1"])
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

print("7) leg gate — a pace play arms only on its leg (2026-07-08)")
matcher.SUSTAIN_SCALE = 0.0
_nav_leg = [None]
matcher.navigator.get_navigator = lambda route=None: (
    {"available": True, "next_mark": {"index": _nav_leg[0]}} if _nav_leg[0] is not None
    else {"available": False})
stub(time_behind_min=120)          # pace play predicates true; applicability {"legs": [1]}
_nav_leg[0] = None
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "pace_behind_2h_1")
check("leg unknown -> FAIL OPEN (arms)", p["status"] == "armed" and p["applicable"] is True)
_nav_leg[0] = 1
matcher.clear_state()
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "pace_behind_2h_1")
check("on the applicable leg -> arms", p["status"] == "armed"
      and r["signals"]["current_leg"] == 1)
_nav_leg[0] = 2
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "pace_behind_2h_1")
check("rounded onto leg 2 -> gated quiet (clear fast)",
      p["status"] == "quiet" and p["applicable"] is False)
# advisory applicability never gates: same legs list, sail_guidance kind, gate advisory
ADV = {"race_id": "u", "schema": "c4.playbook/v2", "variants": [{"id": "m"}], "plays": [
    {"id": "sail_over_j1_j2", "name": "J1 past crossover", "category": "internal",
     "scenario": {"kind": "sail_guidance"}, "stakes_min": 0,
     "conditions": {"predicates": [{"signal": "tws_kn", "op": ">=", "value": 15}]},
     "applicability": {"legs": [1], "gate": "advisory"}, "response": {"type": "guidance"}}]}
stub(bundle=ADV, tws_kn=18.0)
_nav_leg[0] = 3
r = matcher.get_plays()
check("advisory applicability off its leg still arms (condition-driven)",
      r["plays"][0]["status"] == "armed")
print("RESULT-7:", "PASS" if ok else "FAIL")

print("8) polar_pct — windowed live % of polar (2026-07-08)")
SEA = {"race_id": "u", "schema": "c4.playbook/v2", "variants": [{"id": "m"}], "plays": [
    {"id": "sea_state_up", "name": "Rougher than forecast", "category": "external",
     "scenario": {"kind": "wave_heavy"}, "stakes_min": 60,
     "conditions": {"predicates": [{"signal": "polar_pct", "op": "<=", "value": 88,
                                    "sustain_min": 0}]},
     "response": {"type": "route"}}]}
_now = time.time()


class PolarDS(StubDS):
    pct = 0.80          # boat sailing at 80% of target

    def polars_stw(self):
        return [(8.0, 45.0, 6.0), (8.0, 90.0, 7.0), (12.0, 90.0, 8.0)]

    def series(self, path, minutes):
        n = 30
        if path == "navigation.speedThroughWater":
            return [(_now - i, 7.0 * PolarDS.pct / 1.943844) for i in range(n, 0, -1)]
        if path == "environment.wind.speedTrue":
            return [(_now - i, 8.0 / 1.943844) for i in range(n, 0, -1)]
        if path == "environment.wind.angleTrueWater":
            return [(_now - i, 90.0 / 57.29577951308232) for i in range(n, 0, -1)]
        return []


stub(bundle=SEA)
matcher.datasource.active = lambda: PolarDS()
matcher._POLAR_TABLE = None
r = matcher.get_plays()
check("windowed polar_pct computed (~80%)",
      r["signals"]["polar_pct"] is not None and abs(r["signals"]["polar_pct"] - 80.0) < 1.5)
check("sea-state play arms on underperformance", r["plays"][0]["status"] == "armed")
PolarDS.pct = 0.95
matcher.clear_state()
r = matcher.get_plays()
check("sailing at 95% -> quiet", r["plays"][0]["status"] == "quiet"
      and abs(r["signals"]["polar_pct"] - 95.0) < 1.5)


class ThinDS(PolarDS):
    def series(self, path, minutes):
        return []           # archive gap — fall back to the instantaneous read

    def latest_value(self, path):
        return {"navigation.speedThroughWater": 7.0 * 0.8 / 1.943844,
                "environment.wind.speedTrue": 8.0 / 1.943844,
                "environment.wind.angleTrueWater": 90.0 / 57.29577951308232}.get(path)


matcher.datasource.active = lambda: ThinDS()
matcher._POLAR_TABLE = None
r = matcher.get_plays()
check("thin archive -> instantaneous fallback (~80%)",
      r["signals"]["polar_pct"] is not None and abs(r["signals"]["polar_pct"] - 80.0) < 1.5)
matcher._POLAR_TABLE = None
print("RESULT-8:", "PASS" if ok else "FAIL")

print("9) sail CONFIGURATIONS — flying sets, reef, legacy compat (2026-07-08)")
COMBO = {"race_id": "u", "schema": "c4.playbook/v2", "variants": [{"id": "m"}], "plays": [
    {"id": "sail_c0_up", "name": "C0 past its ceiling", "category": "internal",
     "scenario": {"kind": "sail_guidance"}, "stakes_min": 0,
     "conditions": {"predicates": [{"signal": "hoisted_sail", "op": "==", "value": "C0"}]},
     "response": {"type": "guidance"}},
    {"id": "reef_in_check", "name": "Reef 1 declared", "category": "internal",
     "scenario": {"kind": "sail_guidance"}, "stakes_min": 0,
     "conditions": {"predicates": [{"signal": "reef", "op": "==", "value": "R1"}]},
     "response": {"type": "guidance"}},
]}
matcher.SUSTAIN_SCALE = 0.0
stub(bundle=COMBO)
st = matcher.set_sail_state(flying=["C0", "J2"])
check("flying SET stored + primary mirror", st["flying"] == ["C0", "J2"] and st["hoisted"] == "C0")
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "sail_c0_up")
check("membership: C0 among C0+J2 arms a hoisted==C0 play", p["status"] == "armed")
st = matcher.set_sail_state(flying=["C0", "J2"], reef="R1")
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "reef_in_check")
check("reef R1 declared -> reef play arms", p["status"] == "armed"
      and r["signals"]["reef"] == "R1")
st = matcher.set_sail_state(reef="")
check("reef shaken out (flying untouched)", st["reef"] is None and st["flying"] == ["C0", "J2"])
st = matcher.set_sail_state(flying=["A3", "SS"])
check("kite+staysail combo; primary = the kite", st["flying"] == ["A3", "SS"]
      and st["hoisted"] == "A3")
st = matcher.set_sail_state(hoisted="J1")          # legacy single-sail setter
check("legacy hoisted setter -> flying=[J1]", st["flying"] == ["J1"] and st["hoisted"] == "J1")
r = matcher.get_plays()
check("C0 doused -> the C0 play clears", next(x for x in r["plays"]
      if x["id"] == "sail_c0_up")["status"] == "quiet")
print("RESULT-9:", "PASS" if ok else "FAIL")

print("10) distance-to-trigger + watchlist (2026-07-09)")
matcher.navigator.get_navigator = lambda route=None: {"available": False}   # leg unknown again
matcher.SUSTAIN_SCALE = 1.0                                                 # real sustain clocks
StubDS.state = {}                                                           # no leftover sail state
# 94 min behind a 104-min pace predicate → quiet but ~0.9 close; on the watchlist w/ live numbers
stub(time_behind_min=94)
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "pace_behind_2h_1")
check("quiet near-threshold play reports closeness ~0.9",
      p["status"] == "quiet" and p["closeness"] is not None and 0.85 <= p["closeness"] <= 0.95)
check("nearest_gap carries the live number vs the threshold",
      p["nearest_gap"] and p["nearest_gap"]["signal"] == "time_behind_min"
      and p["nearest_gap"]["actual"] == 94 and p["nearest_gap"]["value"] == 104)
check("watchlist surfaces it", any(w["id"] == "pace_behind_2h_1" for w in r["watchlist"]))
# an unknowable play (fatigue signal absent) never fakes a closeness / a watchlist slot
p = next(x for x in r["plays"] if x["id"] == "low_maneuver")
check("unknowable signal -> closeness None, off the watchlist",
      p["closeness"] is None and not any(w["id"] == "low_maneuver" for w in r["watchlist"]))
# far from the threshold → below the 0.5 cut, off the watchlist
stub(time_behind_min=20)
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "pace_behind_2h_1")
check("far-off play (closeness < 0.5) stays off the watchlist",
      (p["closeness"] or 0) < 0.5 and not any(w["id"] == "pace_behind_2h_1"
                                              for w in r["watchlist"]))
# a holding predicate w/ sustain running → arming carries sustain_pct, closeness 1.0
stub(time_behind_min=120)
r = matcher.get_plays()
p = next(x for x in r["plays"] if x["id"] == "pace_behind_2h_1")
check("arming play: closeness 1.0 + sustain_pct present",
      p["status"] == "arming" and p["closeness"] == 1.0
      and isinstance(p["sustain_pct"], int) and 0 <= p["sustain_pct"] < 100)
check("arming play is not on the quiet watchlist",
      not any(w["id"] == "pace_behind_2h_1" for w in r["watchlist"]))
# == predicates are 1/0 — a gear-loss play never reads "almost"
p = next(x for x in r["plays"] if x["id"] == "gear_loss_s1")
check("discrete predicate: closeness 0, not 'almost'", p["closeness"] in (0.0, None)
      or p["closeness"] == 0)
print("RESULT-10:", "PASS" if ok else "FAIL")
import sys
sys.exit(0 if ok else 1)
