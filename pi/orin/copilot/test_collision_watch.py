"""Exit test for the collision watch as the crew actually gets it: drive the auto-coach timer and
read GET /coach (and POST /strategy) through the FastAPI app. Pure — stubbed engine facts and a
stubbed LLM, no live engine, no model. Covers GitHub #29:

  1. every closing contact is checked — a near-term contact behind a CPA-sorted far one is warned;
  2. the warning is published at once — never waits on the LLM, never phrased by it;
  3. the polled /strategy never blocks or stacks LLM calls;
  4. a watch-level contact leads the coach line over "now" items;
  5. engine lost / AIS read failing / no own fix raise a "collision watch blind" callout.

    cd pi/orin && python3 -m copilot.test_collision_watch
"""
import asyncio
import sys
import threading
import time

from fastapi.testclient import TestClient

from . import app as app_mod
from . import bench_copilot, coach as coach_mod, config, copilot, narrate as narrate_mod

client = TestClient(app_mod.app)        # no `with` → the lifespan (the real timer) never starts
COACH = coach_mod.COACH
ROUTE = "_test_cw"


def _check(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return bool(ok)


class StubEngine:
    """Every engine read the copilot makes, answered from `facts` (missing → the engine's own
    'not available' shape, not a transport error)."""
    facts: dict = {}
    base_url = "http://stub:8200"

    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        def read(*a, **k):
            return dict(StubEngine.facts.get(name) or {"available": False, "reason": "stub"})
        return read


class StubLLM:
    calls = 0
    gate = threading.Event()             # chat() blocks until set — a cold / busy model
    reply = "Coach line from the model."
    strategy = None

    def __init__(self, *a, **k):
        pass

    def chat(self, messages, **k):
        StubLLM.calls += 1
        StubLLM.gate.wait(10)
        if k.get("schema"):
            import json
            return {"content": json.dumps(StubLLM.strategy)}
        return {"content": StubLLM.reply}


def _ais(*targets, own_fix=True):
    return {"available": True, "own_fix": own_fix, "targets": list(targets)}


def _tgt(mmsi, name, cpa, tcpa, closing=True):
    return {"mmsi": mmsi, "name": name, "cpa_nm": cpa, "tcpa_min": tcpa, "bearing": 90,
            "range_nm": 2.0, "closing": closing}


_COND = {"available": True, "stale": False, "tws": 12.0}


def _fresh(facts):
    StubEngine.facts = facts
    narrate_mod.reset(ROUTE)
    COACH.route = ROUTE
    COACH.last, COACH.last_error = None, None
    COACH.history.clear()


async def _tick():
    await COACH.tick()
    return client.get("/coach").json()


async def _settle():
    for t in (COACH._phrase_task, COACH._brief_task):
        if t is not None:
            await t


async def scenarios() -> bool:
    ok = True
    config.BRIEFS_ENABLED = False

    print("\n== 1. every closing contact is checked (read via GET /coach) ==")
    config.USE_LLM = False
    # the engine sorts by CPA alone: FAR (0.1 nm, 50 min) first, NEAR (0.3 nm, 4 min) second
    _fresh({"conditions": _COND, "ais": _ais(_tgt("1", "FAR", 0.1, 50), _tgt("2", "NEAR", 0.3, 4))})
    st = await _tick()
    ok &= _check("NEAR (0.3 nm in 4 min) is warned though FAR sorts first",
                 st["active"] and st["active"][0]["headline"] == "Collision risk: NEAR")
    _fresh({"conditions": _COND, "ais": _ais(_tgt("3", "SLOW", 1.0, 20), _tgt("4", "TUG", 0.4, 9))})
    st = await _tick()
    ok &= _check("an act-level contact beats a watch-level one listed ahead of it",
                 st["active"][0]["headline"] == "Collision risk: TUG")
    _fresh({"conditions": _COND, "ais": _ais(_tgt("5", "A", 0.4, 11), _tgt("6", "B", 0.45, 3))})
    st = await _tick()
    ok &= _check("two act-level contacts → the soonest (TCPA) one", st["active"][0]["headline"] == "Collision risk: B")

    print("\n== 2. published at once; safety never goes through the LLM ==")
    config.USE_LLM = True
    StubLLM.calls = 0
    StubLLM.gate.clear()                 # the model is stuck: any chat() call would hang
    _fresh({"conditions": _COND, "ais": _ais(_tgt("7", "FERRY", 0.2, 6))})
    t0 = time.monotonic()
    st = await _tick()
    ok &= _check("tick returns without waiting on the stuck model", time.monotonic() - t0 < 3)
    ok &= _check("/coach carries the warning straight away",
                 st["new"] and st["new"][0]["headline"] == "Collision risk: FERRY")
    ok &= _check("/coach line is the callout's own text",
                 st["spoken"].startswith("Collision risk: FERRY — CPA 0.2 nm / 6.0 min")
                 and st["narration_mode"] == "deterministic")
    await _settle()
    ok &= _check("the LLM was never asked to phrase a safety callout", StubLLM.calls == 0)

    # a non-safety callout IS phrased — but in the background, after the deterministic line is up
    _fresh({"conditions": _COND, "ais": _ais(),
            "navigator": {"available": True, "next_mark": {"name": "Windward", "eta_min": 8.0}}})
    st = await _tick()
    ok &= _check("rounding callout published with the deterministic line while the model is busy",
                 st["new"] and st["narration_mode"] == "deterministic" and st["spoken"].startswith("Windward"))
    StubLLM.gate.set()
    await _settle()
    st = client.get("/coach").json()
    ok &= _check("the model's phrasing replaces it once it lands",
                 st["spoken"] == StubLLM.reply and st["narration_mode"] == "llm"
                 and st["history"][0]["spoken"] == StubLLM.reply)
    config.USE_LLM = False

    print("\n== 4. watch-level traffic leads the coach line ==")
    _fresh({"conditions": _COND, "ais": _ais(_tgt("8", "BARGE", 1.2, 25)),
            "fatigue": {"available": True, "level": "rotate_now", "index": 82},
            "navigator": {"available": True, "next_mark": {"name": "Windward", "eta_min": 4.0}}})
    st = await _tick()
    ok &= _check("'Traffic closing' is first in active and new, ahead of two 'now' items",
                 st["active"][0]["headline"] == "Traffic closing: BARGE"
                 and st["new"][0]["headline"] == "Traffic closing: BARGE"
                 and sum(c["urgency"] == "now" for c in st["active"]) == 2)
    ok &= _check("…and opens the coach line", st["spoken"].startswith("Traffic closing: BARGE"))

    print("\n== 5. a blind collision watch is never 'all clear' ==")
    eng_url, eng_fb = config.ENGINE_URL, config.ENGINE_URL_FALLBACK
    orig_engine = copilot.EngineClient
    copilot.EngineClient = _RealEngine
    config.ENGINE_URL, config.ENGINE_URL_FALLBACK = "http://127.0.0.1:9", ""   # nothing listens there
    try:
        _fresh({})
        st = await _tick()
        ok &= _check("engine lost: first tick holds (one dropped read doesn't bark) but last_error says so",
                     st["active"] == [] and "collision watch blind" in (st["last_error"] or ""))
        st = await _tick()
        ok &= _check("engine lost: second tick → 'Collision watch blind: engine lost', urgency now",
                     st["active"] and st["active"][0]["headline"] == "Collision watch blind: engine lost"
                     and st["active"][0]["urgency"] == "now" and st["spoken"].startswith("Collision watch blind"))
    finally:
        copilot.EngineClient = orig_engine
        config.ENGINE_URL, config.ENGINE_URL_FALLBACK = eng_url, eng_fb

    _fresh({"conditions": _COND, "ais": {"available": False, "error": "timed out", "path": "/ais"}})
    await _tick()
    st = await _tick()
    ok &= _check("AIS read failing (engine up) → 'Collision watch blind: AIS read failing'",
                 st["active"] and st["active"][0]["headline"] == "Collision watch blind: AIS read failing"
                 and st["last_error"] is None)
    _fresh({"conditions": _COND, "ais": _ais(_tgt("9", "X", None, None, closing=None), own_fix=False)})
    await _tick()
    st = await _tick()
    ok &= _check("no own fix → 'Collision watch blind: no own GPS fix'",
                 st["active"] and st["active"][0]["headline"] == "Collision watch blind: no own GPS fix")
    _fresh({"conditions": _COND, "ais": _ais()})
    await _tick()
    st = await _tick()
    ok &= _check("own fix + empty sea → genuinely quiet", st["active"] == [] and st["last_error"] is None)
    return ok


_RealEngine = copilot.EngineClient


def test_strategy_cache() -> bool:
    """POST /strategy (the iPad's 15-s poll) must answer at once, run at most one LLM pass at a
    time, reuse the narrative while the engine's call is unchanged, and drop it when it changes."""
    print("\n== 3. polled /strategy: cached, single-flight, never blocks ==")
    ok = True
    rec = {"action": "Hold: Middle start", "vs_playbook": "on-plan", "target_variant": "middle",
           "rationale": "no decisive signal", "grounded_in": ["get_selector"], "urgency": "monitor",
           "confidence": "med"}
    digest = {"available": True, "mode": "deterministic", "assessment": "Hold: Middle start",
              "picture": [{"signal": "shift", "read": "osc ±4°", "grounded_in": ["get_tactics"]}],
              "recommendation": rec}
    StubEngine.facts = {"strategy": digest}
    StubLLM.strategy = {"assessment": "Signals are quiet — hold the middle.",
                        "recommendation": {"action": "x", "rationale": "shift is just oscillating",
                                           "grounded_in": ["get_tactics"], "urgency": "monitor",
                                           "confidence": "med"}}
    StubLLM.calls = 0
    StubLLM.gate.clear()
    config.USE_LLM = True
    copilot.reset_strategy_cache()

    t0 = time.monotonic()
    rs = [client.post("/strategy", json={"route": ROUTE}).json() for _ in range(5)]
    ok &= _check("5 rapid polls answer at once with the engine digest while the model is busy",
                 time.monotonic() - t0 < 3 and all(r["mode"] == "deterministic" for r in rs))
    ok &= _check("…and start exactly ONE LLM pass", StubLLM.calls == 1)
    StubLLM.gate.set()
    for _ in range(100):
        if not copilot._STRAT_LOCK.locked():
            break
        time.sleep(0.05)
    r = client.post("/strategy", json={"route": ROUTE}).json()
    ok &= _check("next poll carries the LLM narrative on the fresh digest",
                 r["mode"] == "llm" and r["assessment"].startswith("Signals are quiet")
                 and r["recommendation"]["action"] == "Hold: Middle start"
                 and r["recommendation"]["rationale"] == "shift is just oscillating")
    ok &= _check("still one LLM pass (served from cache)", StubLLM.calls == 1)
    StubEngine.facts = {"strategy": {**digest, "recommendation": {**rec, "action": "Switch → Right",
                                                                  "target_variant": "right"}}}
    r = client.post("/strategy", json={"route": ROUTE}).json()
    ok &= _check("engine's call changed → the old narrative is dropped",
                 r["mode"] == "deterministic" and r["recommendation"]["action"] == "Switch → Right")
    config.USE_LLM = False
    return ok


def _play(pid):
    return {"id": pid, "conditions": {"narrative": f"{pid} narrative"}}


def _load_playbook(*pids):
    r = client.post("/playbook/load", json={"race_id": "t", "variants": [{"id": "middle"}],
                                            "plays": [_play(p) for p in pids]}).json()
    assert r.get("loaded"), r


def _wait_idle():
    for _ in range(100):
        if not copilot._STRAT_LOCK.locked():
            return
        time.sleep(0.05)


def test_strategy_cache_plays() -> bool:
    """The cached LLM play grades never carry stale statuses or stale play ids: statuses always come
    from the fresh digest, a play that cleared is dropped, and a playbook reload empties the cache
    (an LLM pass in flight across the reload can't refill it)."""
    print("\n== 3b. polled /strategy: play matches always match the fresh digest ==")
    import os
    import tempfile
    ok = True
    tmp = tempfile.mkdtemp()
    orig_path = config.PLAYBOOK_PATH
    config.PLAYBOOK_PATH = os.path.join(tmp, "playbook.json")
    rec = {"action": "Hold: Middle start", "vs_playbook": "on-plan", "target_variant": "middle",
           "rationale": "no decisive signal", "grounded_in": ["get_selector"], "urgency": "monitor"}

    def digest(statuses):
        return {"available": True, "mode": "deterministic", "assessment": "Hold: Middle start",
                "picture": [{"signal": "shift", "read": "osc ±4°", "grounded_in": ["get_tactics"]}],
                "recommendation": rec,
                "plays": {"detail": [{"id": k, "status": v} for k, v in statuses.items()]}}

    def poll():
        return client.post("/strategy", json={"route": ROUTE}).json()

    try:
        _load_playbook("P1", "P2")
        config.USE_LLM = True
        StubLLM.strategy = {"assessment": "Pressure play is on.",
                            "recommendation": {"action": "x", "rationale": "the left is paying",
                                               "grounded_in": ["get_tactics"]},
                            "play_matches": [{"play_id": "P1", "match": "strong", "why": "breeze left"},
                                             {"play_id": "P2", "match": "partial", "why": "maybe"}]}
        StubLLM.gate.set()
        StubEngine.facts = {"strategy": digest({"P1": "armed", "P2": "arming"})}
        poll()
        _wait_idle()
        r = poll()
        ok &= _check("cached pass shows both graded plays with their live status",
                     r["mode"] == "llm" and [(m["play_id"], m["status"]) for m in r.get("play_matches", [])]
                     == [("P1", "armed"), ("P2", "arming")])

        # P1 clears; the engine still says Hold, so the narrative is reused — but not P1's grade
        StubEngine.facts = {"strategy": digest({"P1": "quiet", "P2": "arming"})}
        r = poll()
        ok &= _check("a play that cleared (rec still Hold) is NOT shown armed",
                     r["mode"] == "llm" and [m["play_id"] for m in r.get("play_matches", [])] == ["P2"])
        StubEngine.facts = {"strategy": digest({"P2": "armed"})}
        r = poll()
        ok &= _check("a play whose status changed since the grade is dropped", not r.get("play_matches"))

        # new homework: the cache is emptied, and the next poll is the plain fresh digest
        StubEngine.facts = {"strategy": digest({"P1": "armed", "P2": "arming"})}
        _load_playbook("P3")
        r = poll()
        ok &= _check("playbook reload → no cached narrative, no stale play ids",
                     r["mode"] == "deterministic" and not r.get("play_matches"))
        _wait_idle()
        r = poll()
        ok &= _check("after the reload, matches only ever name plays in the new playbook",
                     all(m["play_id"] == "P3" for m in r.get("play_matches", [])))

        # a pass in flight across a reload can't refill the cache with the old playbook's grades
        copilot.reset_strategy_cache()
        StubLLM.gate.clear()
        _load_playbook("P1", "P2")
        poll()                                   # starts a pass graded against P1/P2
        _load_playbook("P3")
        StubLLM.gate.set()
        _wait_idle()
        ok &= _check("an LLM pass in flight across a reload does not refill the cache",
                     ROUTE not in copilot._STRAT_CACHE)
    finally:
        config.PLAYBOOK_PATH = orig_path
        config.USE_LLM = False
        copilot.reset_strategy_cache()
    return ok


def main():
    orig = (copilot.EngineClient, copilot.LLMClient, config.USE_LLM, config.BRIEFS_ENABLED)
    copilot.EngineClient, copilot.LLMClient = StubEngine, StubLLM
    try:
        ok = asyncio.run(scenarios())
        ok &= test_strategy_cache()
        ok &= test_strategy_cache_plays()
    finally:
        copilot.EngineClient, copilot.LLMClient, config.USE_LLM, config.BRIEFS_ENABLED = orig
    # the bench's pure callout/coach checks (incl. the watch-level ranking) ride along in CI
    for t in (bench_copilot.test_narrate_logic, bench_copilot.test_safety_callout,
              bench_copilot.test_coach_logic, bench_copilot.test_strategy_synthesis):
        ok &= t()
    print("\n" + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
