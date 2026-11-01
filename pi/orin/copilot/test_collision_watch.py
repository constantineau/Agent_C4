"""Exit test for the collision watch as the crew actually gets it: drive the auto-coach timer and
read GET /coach (and POST /strategy) through the FastAPI app. Pure — stubbed engine facts and a
stubbed LLM, no live engine, no model. Covers GitHub #29:

  1. every closing contact is checked — a near-term contact behind a CPA-sorted far one is warned;
  2. the warning is published at once — never waits on the LLM, never phrased by it;
  3. the polled /strategy is the engine digest — it never calls the LLM;
  4. a watch-level contact leads the coach line over "now" items;
  5. engine lost / AIS read failing / no own fix raise a "Collision watch OFF" callout;
  6. (2026-11-02 review) every dangerous contact is shown, by TCPA; our own MMSI (once set) never alarms
     but a real freighter next to it does; a stale fix, a missing AIS receiver and a tick that
     throws are each "Collision watch OFF" (no traffic heard is only a hint); unknown motion is
     called on range; the engine's dead-reckoned
     numbers flow through; the copilot service binds the cable + loopback only, allow-listed.

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
    ok &= _check("two act-level contacts → the soonest (TCPA) one first", st["active"][0]["headline"] == "Collision risk: B")
    ok &= _check("…and the other one is shown too, not hidden",
                 [a["headline"] for a in st["active"]] == ["Collision risk: B", "Collision risk: A"])

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
                     st["active"] == [] and "collision watch OFF" in (st["last_error"] or ""))
        st = await _tick()
        ok &= _check("engine lost: second tick → 'Collision watch OFF: engine lost', urgency now",
                     st["active"] and st["active"][0]["headline"] == "Collision watch OFF: engine lost"
                     and st["active"][0]["urgency"] == "now" and st["spoken"].startswith("Collision watch OFF"))
    finally:
        copilot.EngineClient = orig_engine
        config.ENGINE_URL, config.ENGINE_URL_FALLBACK = eng_url, eng_fb

    _fresh({"conditions": _COND, "ais": {"available": False, "error": "timed out", "path": "/ais"}})
    await _tick()
    st = await _tick()
    ok &= _check("AIS read failing (engine up) → 'Collision watch OFF: AIS read failing'",
                 st["active"] and st["active"][0]["headline"] == "Collision watch OFF: AIS read failing"
                 and st["last_error"] is None)
    _fresh({"conditions": _COND, "ais": _ais(_tgt("9", "X", None, None, closing=None), own_fix=False)})
    await _tick()
    st = await _tick()
    ok &= _check("no own fix → 'Collision watch OFF: no GPS fix'",
                 st["active"] and st["active"][0]["headline"] == "Collision watch OFF: no GPS fix")
    _fresh({"conditions": _COND, "ais": _ais()})
    await _tick()
    st = await _tick()
    ok &= _check("own fix + empty sea → genuinely quiet", st["active"] == [] and st["last_error"] is None)
    ok &= await review_scenarios()
    return ok


def _engine_ais(rows, own_sog_kn=6.0, fix_age=1.0, own_mmsi=None, rx_age=None):
    """The ENGINE's real /ais answer (vps/agent/app/ais.py) for own ship at 45N 83W heading north
    and these raw AIS rows — so the copilot is tested on what the engine actually sends."""
    import os

    from app import ais as ais_mod
    now = time.time()

    class Src:
        def latest_value(self, path):
            return {"navigation.position.latitude": 45.0, "navigation.position.longitude": -83.0,
                    "navigation.speedOverGround": own_sog_kn / ais_mod._MS_TO_KN,
                    "navigation.courseOverGroundTrue": 0.0}.get(path)

        def latest_epoch(self, path):
            return now - fix_age

        def ais_targets(self, max_age_min):
            return [dict(r, time=now - fix_age) for r in rows]     # heard as recently as our fix

        def get_fleet(self):
            return {}

        def ais_receiver_age_s(self):
            return rx_age

    ais_mod.datasource.active = lambda: Src()
    ais_mod.OWN_FIX_MAX_AGE_S = 30
    if own_mmsi:
        os.environ["OWN_MMSI"] = own_mmsi
    try:
        return dict(ais_mod.get_ais_targets(), available=True)
    finally:
        os.environ.pop("OWN_MMSI", None)


GHOST = "316053364"
_M = 1 / (60.0 * 1852.0)                 # 1 m of latitude in degrees


async def review_scenarios() -> bool:
    """The 2026-11-02 system review's collision items, through the timer and GET /coach."""
    ok = True
    print("\n== 6a. own AIS echo + a real freighter (engine → copilot) ==")
    # our echo 8 m astern, a hair faster on our course: as raw geometry it is "closing, CPA 0, ~1 min"
    ghost = {"mmsi": GHOST, "name": None, "lat": 45.0 - 8 * _M, "lon": -83.0, "sog": 6.2, "cog": 0.0}
    freighter = {"mmsi": "366999001", "name": "FREIGHTER", "lat": 45.0 + 2 / 60.0, "lon": -83.0,
                 "sog": 15.0, "cog": 180.0}
    _fresh({"conditions": _COND, "ais": _engine_ais([ghost, freighter])})
    st = await _tick()
    heads = [a["headline"] for a in st["active"] if a["category"] == "safety"]
    ok &= _check("own MMSI not set: the suspected echo still alarms (only a set MMSI is dropped)",
                 f"Collision risk: MMSI {GHOST}" in heads)
    ok &= _check("…and the freighter is shown too", "Collision risk: FREIGHTER" in heads)
    st = await _tick()
    ok &= _check("plus a quiet hint: 'Possible own-ship echo: MMSI 316053364' (monitor, not safety)",
                 any(a["headline"] == f"Possible own-ship echo: MMSI {GHOST}" and a["category"] == "data"
                     and a["urgency"] == "monitor" and a["detail"] == "Set own MMSI if it's us."
                     for a in st["active"]))
    _fresh({"conditions": _COND, "ais": _engine_ais([ghost, freighter], own_mmsi=GHOST)})
    await _tick()
    st = await _tick()
    ok &= _check("with OWN_MMSI set: only the freighter, no echo alarm and no hint",
                 [a["headline"] for a in st["active"]] == ["Collision risk: FREIGHTER"])
    # Codex on #191: we're northbound at 6 kn, a boat 40 m astern at 7 kn — contact in ~78 s
    over = {"mmsi": "366999009", "name": "OVERTAKER", "lat": 45.0 - 40 * _M, "lon": -83.0,
            "sog": 7.0, "cog": 0.0}
    _fresh({"conditions": _COND, "ais": _engine_ais([over])})
    st = await _tick()
    ok &= _check("a real boat 40 m astern overtaking at +1 kn → 'Collision risk: OVERTAKER', now",
                 st["active"] and st["active"][0]["headline"] == "Collision risk: OVERTAKER"
                 and st["active"][0]["urgency"] == "now")
    # the dock: GPS jitter makes us "move" 0.1 kn toward our echo 3 m off (the live alarm). Setting
    # OWN_MMSI once is what silences it.
    dock_ghost = dict(ghost, lat=45.0 + 3 * _M, sog=0.0, cog=0.0)
    _fresh({"conditions": _COND, "ais": _engine_ais([dock_ghost], own_sog_kn=0.1, own_mmsi=GHOST)})
    await _tick()
    st = await _tick()
    ok &= _check("dock with OWN_MMSI set: quiet", st["active"] == [])

    print("\n== 6b. every dangerous contact, by TCPA ==")
    _fresh({"conditions": _COND, "ais": _ais(_tgt("21", "FAR", 0.05, 11), _tgt("22", "TUG", 0.4, 3),
                                             _tgt("23", "FERRY", 1.2, 8), _tgt("24", "BARGE", 0.3, 7))})
    st = await _tick()
    heads = [a["headline"] for a in st["active"] if a["category"] == "safety"]
    ok &= _check("all four shown: act by TCPA (TUG 3, BARGE 7, FAR 11), then watch (FERRY)",
                 heads == ["Collision risk: TUG", "Collision risk: BARGE", "Collision risk: FAR",
                           "Traffic closing: FERRY"])
    ok &= _check("the coach line names two and says '+2 more contacts'",
                 st["spoken"].startswith("Collision risk: TUG") and "+2 more contacts" in st["spoken"])
    _fresh({"conditions": _COND, "ais": _ais(_tgt("25", "SHIP", 0.2, 6))})
    await _tick()
    StubEngine.facts["ais"] = _ais(_tgt("25", "SHIP", 0.1, 4))
    st = await _tick()
    ok &= _check("numbers in `active` are live (CPA 0.1 / 4 min on the next tick)",
                 st["active"][0]["detail"].startswith("CPA 0.1 nm / 4.0 min") and not st["new"])

    print("\n== 6c. unknown motion is called on range ==")
    unk = {"mmsi": "31", "name": "NOSPEED", "cpa_nm": None, "tcpa_min": None, "closing": None,
           "bearing": 45, "range_nm": 0.4, "motion": "unknown"}
    _fresh({"conditions": _COND, "ais": _ais(unk)})
    st = await _tick()
    ok &= _check("0.4 nm, no SOG/COG → 'Contact, motion unknown', now",
                 st["active"] and st["active"][0]["headline"] == "Contact, motion unknown: NOSPEED"
                 and st["active"][0]["urgency"] == "now" and "no speed/course" in st["active"][0]["detail"])
    _fresh({"conditions": _COND, "ais": _ais(dict(unk, range_nm=3.0))})
    st = await _tick()
    ok &= _check("3 nm with unknown motion → nothing (outside the watch range)", st["active"] == [])
    _fresh({"conditions": _COND, "ais": dict(_ais(unk), underway=False)})
    st = await _tick()
    ok &= _check("stopped (at the dock) → no unknown-motion callout", st["active"] == [])

    print("\n== 6d. each blind state is 'Collision watch OFF' ==")
    _fresh({"conditions": _COND, "ais": _engine_ais([freighter], fix_age=45)})
    await _tick()
    st = await _tick()
    ok &= _check("GPS fix 45 s old → 'Collision watch OFF: GPS fix stale', no freighter callout",
                 [a["headline"] for a in st["active"]] == ["Collision watch OFF: GPS fix stale"]
                 and "45 s" in st["active"][0]["detail"])
    _fresh({"conditions": _COND, "ais": _engine_ais([], own_sog_kn=6.0)})
    await _tick()
    st = await _tick()
    ok &= _check("under way, no AIS heard (open water) → a quiet 'No AIS heard for 15 min', not OFF",
                 [(a["headline"], a["detail"], a["category"], a["urgency"]) for a in st["active"]]
                 == [("No AIS heard for 15 min", "Check the AIS.", "data", "soon")])
    _fresh({"conditions": _COND, "ais": _engine_ais([], own_sog_kn=6.0, rx_age=20 * 60)})
    await _tick()
    st = await _tick()
    ok &= _check("the AIS transceiver gone from the bus → 'Collision watch OFF: AIS receiver missing'",
                 [a["headline"] for a in st["active"]] == ["Collision watch OFF: AIS receiver missing"])
    _fresh({"conditions": _COND, "ais": _engine_ais([], own_sog_kn=0.0)})
    await _tick()
    st = await _tick()
    ok &= _check("stopped, AIS silent → quiet (no dock alarm)", st["active"] == [])

    # a tick that throws: the held result must not stand in for a working watch
    _fresh({"conditions": _COND, "ais": _ais(_tgt("41", "OLDSHIP", 0.2, 5))})
    st = await _tick()
    before = st["updated_at"]
    real = copilot.step_narration

    def boom(*a, **k):
        raise RuntimeError("tick blew up")
    copilot.step_narration = boom
    try:
        await asyncio.sleep(0.01)
        st = await _tick()
        ok &= _check("tick exception → 'Collision watch OFF: copilot error' replaces the old callout",
                     [a["headline"] for a in st["active"]] == ["Collision watch OFF: copilot error"]
                     and st["new"] and st["updated_at"] > before and "RuntimeError" in st["last_error"])
        ok &= _check("…and it is the newest history line",
                     st["history"][0]["spoken"].startswith("Collision watch OFF: copilot error"))
        st = await _tick()
        ok &= _check("a second failing tick keeps it up but doesn't re-announce it",
                     st["active"][0]["headline"] == "Collision watch OFF: copilot error" and st["new"] == [])
    finally:
        copilot.step_narration = real
    st = await _tick()
    ok &= _check("ticks recover → the real contact is back and new again",
                 st["active"] and st["active"][0]["headline"] == "Collision risk: OLDSHIP" and st["new"])
    return ok


def test_service_binding() -> bool:
    """The copilot listens on the boat cable interface + loopback only and answers only allow-listed
    callers (copilot/serve.py), like pi/orin/reroute/service.py."""
    print("\n== 7. copilot service: cable + loopback, allow-listed callers ==")
    from . import serve
    ok = True
    ok &= _check("default bind = 10.10.10.2 + 127.0.0.1 (no 0.0.0.0)",
                 serve._addrs(config.COPILOT_BIND) == ["10.10.10.2", "127.0.0.1"])
    ok &= _check("default callers = the Pi's cable end + loopback",
                 set(serve._addrs(config.COPILOT_ALLOW_FROM)) == {"10.10.10.1", "127.0.0.1"})
    denied = TestClient(serve.AllowList(app_mod.app, {"10.10.10.1"}))     # TestClient's host is 'testclient'
    r = denied.get("/tools")
    ok &= _check("a caller not on the list → 403", r.status_code == 403)
    allowed = TestClient(serve.AllowList(app_mod.app, {"testclient"}))
    ok &= _check("a caller on the list → the app answers", allowed.get("/tools").status_code == 200)
    socks = []
    try:
        socks = serve.bind_sockets(["127.0.0.1", "10.10.10.2"], 0)
        names = [s.getsockname()[0] for s in socks]
        ok &= _check("binds loopback and the cable address (even with no cable: FREEBIND)",
                     names == ["127.0.0.1", "10.10.10.2"])
    except OSError as e:
        ok &= _check(f"binds loopback and the cable address ({e})", False)
    finally:
        for s in socks:
            s.close()
    return ok


_RealEngine = copilot.EngineClient


def test_strategy_default() -> bool:
    """POST /strategy as the iPad polls it (empty body, every ~15 s) is the fresh engine digest:
    no LLM call, answered at once, play statuses straight from the digest. The LLM narrative runs
    only on an explicit use_llm=true."""
    print("\n== 3. polled /strategy: the engine digest, never the LLM ==")
    ok = True
    rec = {"action": "Hold: Middle start", "vs_playbook": "on-plan", "target_variant": "middle",
           "rationale": "no decisive signal", "grounded_in": ["get_selector"], "urgency": "monitor"}

    def digest(statuses):
        return {"available": True, "mode": "deterministic", "assessment": "Hold: Middle start",
                "picture": [{"signal": "shift", "read": "osc ±4°", "grounded_in": ["get_tactics"]}],
                "recommendation": rec,
                "plays": {"detail": [{"id": k, "status": v} for k, v in statuses.items()]}}

    StubLLM.strategy = {"assessment": "From the model.",
                        "recommendation": {"action": "x", "rationale": "model rationale",
                                           "grounded_in": ["get_tactics"]}}
    StubLLM.calls = 0
    StubLLM.gate.clear()                 # a stuck model: any default-path LLM call would hang here
    config.USE_LLM = True
    try:
        StubEngine.facts = {"strategy": digest({"P1": "armed"})}
        t0 = time.monotonic()
        rs = [client.post("/strategy", json={}).json() for _ in range(5)]
        ok &= _check("5 polls answer at once with the engine digest",
                     time.monotonic() - t0 < 3
                     and all(r["mode"] == "deterministic" and r["assessment"] == "Hold: Middle start"
                             for r in rs))
        ok &= _check("…and never call the LLM", StubLLM.calls == 0)
        StubEngine.facts = {"strategy": digest({"P1": "quiet"})}
        r = client.post("/strategy", json={}).json()
        ok &= _check("play statuses come straight from the fresh digest",
                     r["plays"]["detail"] == [{"id": "P1", "status": "quiet"}] and "play_matches" not in r)
        StubLLM.gate.set()
        r = client.post("/strategy", json={"use_llm": True}).json()
        ok &= _check("explicit use_llm=true still runs the LLM narrative",
                     StubLLM.calls == 1 and r["mode"] == "llm" and r["assessment"] == "From the model.")
    finally:
        config.USE_LLM = False
    return ok


BENCH_PURE = (bench_copilot.test_narrate_logic, bench_copilot.test_safety_callout,
              bench_copilot.test_fleet_callout, bench_copilot.test_deviation_drift_callout,
              bench_copilot.test_adherence_logic, bench_copilot.test_coach_logic,
              bench_copilot.test_strategy_synthesis, bench_copilot.test_strategy_callout,
              bench_copilot.test_plays_callout, bench_copilot.test_play_match_filter,
              bench_copilot.test_briefs_logic)


def test_bench_list_in_step():
    """Every pure test_* in bench_copilot runs here, so a new one can't be left out of CI."""
    have = {n for n in dir(bench_copilot) if n.startswith("test_") and callable(getattr(bench_copilot, n))}
    run = {t.__name__ for t in BENCH_PURE}
    return _check(f"every bench_copilot test_* runs in CI (missing: {sorted(have - run)})", have <= run)


def main():
    orig = (copilot.EngineClient, copilot.LLMClient, config.USE_LLM, config.BRIEFS_ENABLED)
    copilot.EngineClient, copilot.LLMClient = StubEngine, StubLLM
    try:
        ok = asyncio.run(scenarios())
        ok &= test_strategy_default()
        ok &= test_service_binding()
    finally:
        copilot.EngineClient, copilot.LLMClient, config.USE_LLM, config.BRIEFS_ENABLED = orig
    # ALL of the bench's pure checks ride along in CI: the same list bench_copilot.main() runs
    # before it needs a live engine. Keep the two lists in step (test_bench_list_in_step below).
    for t in BENCH_PURE:
        ok &= t()
    ok &= test_bench_list_in_step()
    print("\n" + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
