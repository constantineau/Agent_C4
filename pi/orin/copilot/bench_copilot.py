#!/usr/bin/env python3
"""Exit test / bench for the copilot decision-support layer.

Runs the brief pipeline against a LIVE engine and checks the guardrails actually hold:
  - a brief is always produced (engine-only path with no LLM),
  - every factor/recommendation is grounded in a tool the run actually used,
  - the standing disclaimer is present,
  - with --llm, the bounded tool-loop runs against the local /v1 endpoint and its output
    survives validation (or cleanly falls back to deterministic).

Pure stdlib + the copilot package (no FastAPI needed). Run from `pi/orin/`:
    python3 -m copilot.bench_copilot                 # deterministic (engine only)
    python3 -m copilot.bench_copilot --llm           # full loop against the Orin LLM
    ENGINE_URL=http://127.0.0.1:8200 LLM_BASE_URL=http://127.0.0.1:11434/v1 \
        python3 -m copilot.bench_copilot --llm
"""
import argparse
import json
import sys

from . import adherence as adherence_mod
from . import brief as brief_mod
from . import config, copilot, narrate as narrate_mod, playbook as playbook_mod
from .engine_client import EngineClient


def _check(name, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return ok


# A synthetic snapshot (the shape copilot.gather() returns) that trips several callout types at
# once — deterministic, so the callout engine can be exercised with NO live engine.
_SYNTH_SNAPSHOT = {
    "_route": "_bench", "_hoisted": "A2",
    "get_conditions": {"available": True, "tws": 12.0, "twa": 60, "stale": False},
    "get_navigator": {
        "available": True,
        "next_mark": {"name": "Windward", "eta_min": 8.0},
        "leg": {"type": "beat"},
        "layline_call": "On the starboard layline — tack now.",
        "next_rounding": {"exit_mark": "Leeward", "exit_course_deg": 200.0,
                          "exit_twa_deg": 150.0, "exit_leg_type": "run", "maneuver": "bear away"},
    },
    "get_tactics": {"available": True, "favored_side": "left",
                    "wind": {"persistent": True, "oscillation_deg": 12, "trend": "backing",
                             "shift_deg": -8, "now": 250, "mean_12min": 258},
                    "recommendation": "work the left"},
    "get_sail_advice": {"available": True, "optimal_sail": "A2", "wrong_sail": False,
                        "hoisted_sail": "A2"},
    "get_fatigue": {"available": True, "level": "rotate_now", "index": 82},
}


def _grounded(callouts):
    return all(c.get("grounded_in") for c in callouts)


def test_narrate_logic() -> bool:
    """Pure-function exit test for the proactive callout engine — no engine/LLM needed, fully
    deterministic. Verifies: every callout is grounded; raise-slow (a need-2 category isn't voiced
    until its second poll); clear-fast / speak-once (a voiced callout is not re-voiced); priority
    sorting; and the deterministic spoken line."""
    ok = True
    route = "_bench_narrate"
    narrate_mod.reset(route)
    print("\n== narration callout engine (pure, synthetic snapshot) ==")

    # Stateless evaluate: the four trip-wires fire and all are grounded.
    cos = narrate_mod.evaluate(_SYNTH_SNAPSHOT, playbook=None, engine=None)
    cats = {c["category"] for c in cos}
    ok &= _check("evaluate trips rounding+layline+shift+fatigue",
                 {"rounding", "layline", "shift", "fatigue"} <= cats)
    ok &= _check("every evaluated callout is grounded", _grounded(cos))

    # Poll 1: the need-1 categories (rounding/layline/fatigue) confirm + voice immediately; the
    # need-2 shift is not active yet.
    s1 = narrate_mod.step(route, _SYNTH_SNAPSHOT, None, None)
    new1 = {c["category"] for c in s1["new"]}
    ok &= _check("poll1 voices the immediate (need-1) callouts",
                 new1 == {"rounding", "layline", "fatigue"})
    ok &= _check("poll1 active is priority-sorted (rotate_now first)",
                 bool(s1["active"]) and s1["active"][0]["category"] == "fatigue")
    ok &= _check("poll1 new callouts all grounded", _grounded(s1["new"]))

    # Poll 2: the persistent (need-2) shift now crosses its threshold and is voiced once; the
    # already-voiced callouts are still active but NOT re-voiced.
    s2 = narrate_mod.step(route, _SYNTH_SNAPSHOT, None, None)
    ok &= _check("poll2 voices the persistence-gated shift",
                 [c["category"] for c in s2["new"]] == ["shift"])
    ok &= _check("poll2 active holds all four", len(s2["active"]) == 4)

    # Poll 3: nothing new — speak-once holds while conditions are unchanged.
    s3 = narrate_mod.step(route, _SYNTH_SNAPSHOT, None, None)
    ok &= _check("poll3 voices nothing (speak-once)", s3["new"] == [])

    # Clear-fast: an empty situation drops everything from active immediately.
    s4 = narrate_mod.step(route, {"get_conditions": {"available": True, "stale": False}}, None, None)
    ok &= _check("clear-fast: active empties when callouts go away", s4["active"] == [])

    # The deterministic spoken line: top callouts' own grounded text, no model.
    txt, mode = narrate_mod.narrate(s1["new"], llm=None)
    ok &= _check("deterministic narration returns text", bool(txt) and mode == "deterministic")
    empty_txt, empty_mode = narrate_mod.narrate([], llm=None)
    ok &= _check("no callouts → empty narration", empty_txt == "" and empty_mode == "none")
    print(f"  spoken (deterministic): {txt!r}")
    narrate_mod.reset(route)
    return ok


def _audit_narration(n: dict) -> bool:
    ok = True
    print(f"\n--- narration (mode={n.get('narration_mode')}, "
          f"active={len(n.get('active', []))}, new={len(n.get('new', []))}) " + "-" * 20)
    for c in n.get("active", []):
        print(f"  [{c['urgency']}] {c['category']}: {c['headline']} <- {c['grounded_in']}")
    print("  spoken:", repr(n.get("spoken")))
    print("-" * 60)
    # Same guardrail as a brief: every surfaced callout must be grounded in an engine fact/playbook.
    ok &= _check("every active/new callout is grounded",
                 _grounded(n.get("active", []) + n.get("new", [])))
    ok &= _check("narration_mode is honest",
                 n.get("narration_mode") in ("llm", "deterministic", "none"))
    return ok


def _audit_brief(b: dict, allow_engine_only=True) -> bool:
    ok = True
    print(f"\n--- brief (engine_only={b.get('engine_only')}, confidence={b.get('confidence')}) "
          + "-" * 20)
    print("situation:", b.get("situation"))
    for f in b.get("factors", []):
        print(f"  factor: {f['factor']} <- {f['grounded_in']} ({f['confidence']})")
    for r in b.get("recommendations", []):
        print(f"  rec[{r['urgency']}]: {r['action']} <- {r['grounded_in']} ({r['confidence']})")
    for c in b.get("caveats", []):
        print("  caveat:", c)
    print("  sources_used:", b.get("sources_used"))
    print("-" * 60)

    ok &= _check("has disclaimer", bool(b.get("disclaimer")))
    used = set(b.get("sources_used", []))
    # Grounding guardrail: nothing cites a source outside what was used.
    grounded_ok = True
    for it in b.get("factors", []) + b.get("recommendations", []):
        refs = [r for r in it.get("grounded_in", []) if not r.startswith("playbook")]
        if any(r not in used for r in refs) or not it.get("grounded_in"):
            grounded_ok = False
    ok &= _check("every factor/rec is grounded in a used source", grounded_ok)
    ok &= _check("produced at least one factor or recommendation",
                 bool(b.get("factors") or b.get("recommendations")))
    return ok


# A minimal frozen playbook bundle (the c4.playbook/v1 shape the Lab signs) for the pure adherence
# test — recommends LEFT, with a Right variant whose trigger is the branch the crew watches for.
_PLAYBOOK_BUNDLE = {
    "schema": "c4.playbook/v1", "race_id": "_bench_race",
    "headline": "Gameplan: start on the Left (52% of forecasts agree); branch right on a persistent veer.",
    "recommended": "left", "agreement": 0.52, "decision_spread_min": 18, "first_beat_rhumb_deg": 10.0,
    "variants": [
        {"id": "left", "name": "Left", "share": 0.52,
         "what_flips_it": "the breeze backs early; if it veers right instead, the right side pays"},
        {"id": "middle", "name": "Middle", "share": 0.28,
         "what_flips_it": "the breeze stays steady near the rhumb"},
        {"id": "right", "name": "Right", "share": 0.20,
         "what_flips_it": "the breeze veers and holds right of ~020° for two-plus oscillation cycles"},
    ],
}


def _tac(favored, persistent, osc, shift, trend):
    return {"get_tactics": {"available": True, "favored_side": favored,
            "wind": {"persistent": persistent, "oscillation_deg": osc, "shift_deg": shift, "trend": trend}}}


def test_adherence_logic() -> bool:
    """Pure-function exit test for the playbook-adherence tile — no engine/LLM. Verifies the five
    states map correctly onto the frozen variants and stay grounded: no-playbook → na; oscillating →
    on-plan; persistent shift confirming the recommended side → on-plan; persistent shift to a
    DIFFERENT side → branch/ACT naming the right variant; an oscillating lean toward another side →
    early-warning WATCH."""
    ok = True
    print("\n== playbook-adherence (pure, synthetic playbook + tactics) ==")
    pb = playbook_mod.Playbook(_PLAYBOOK_BUNDLE)

    no_pb = adherence_mod.evaluate(playbook_mod.Playbook(None), {})
    ok &= _check("no playbook → na", no_pb["status"] == "na" and no_pb["available"] is False)

    pre = adherence_mod.evaluate(pb, {"get_tactics": {"available": False}})
    ok &= _check("no live tactics → hold the recommended start (ok, 'Left')",
                 pre["status"] == "ok" and pre["value"] == "Left" and "holding" in pre["sub"])

    osc = adherence_mod.evaluate(pb, _tac("either", False, 10, 1, "steady"))
    ok &= _check("oscillating, no lean → on plan (ok)",
                 osc["status"] == "ok" and osc["value"] == "On plan: Left")

    confirm = adherence_mod.evaluate(pb, _tac("left", True, 8, -12, "backing"))
    ok &= _check("persistent shift confirms recommended side → on plan (ok)",
                 confirm["status"] == "ok" and "confirms" in confirm["sub"])

    flip = adherence_mod.evaluate(pb, _tac("right", True, 8, 14, "veering"))
    ok &= _check("persistent shift to a different side → branch (act, 'Switch → Right')",
                 flip["status"] == "act" and flip["value"] == "Switch → Right"
                 and "playbook:right" in flip["based"])
    ok &= _check("branch why carries the right variant's flip trigger",
                 "veers" in flip["why"])

    lean = adherence_mod.evaluate(pb, _tac("either", False, 12, 7, "veering"))
    ok &= _check("oscillating lean toward a non-recommended side → early-warning (watch)",
                 lean["status"] == "watch" and "right lean" in lean["sub"])

    # Grounding + tile-shape invariants on every live state.
    for name, r in (("oscillating", osc), ("confirm", confirm), ("flip", flip), ("lean", lean)):
        ok &= _check(f"{name}: grounded in the playbook + tactics",
                     any(b.startswith("playbook:") for b in r["based"]) and "get_tactics" in r["based"])
        ok &= _check(f"{name}: carries a variant table (rows)", bool(r.get("rows")))
    print(f"  flip value/sub: {flip['value']!r} / {flip['sub']!r}")
    return ok


def test_coach_logic() -> bool:
    """Pure exit test for the auto-coach timer — no engine/LLM. Stub make_narration with a scripted
    sequence and drive Coach.tick() directly: the held state captures the last result; a tick with a
    NEW callout logs a spoken line to history; a tick with nothing new doesn't grow history; an
    exception is caught into last_error and the loop keeps counting ticks (best-effort survival)."""
    import asyncio

    from . import coach as coach_mod
    ok = True
    print("\n== auto-coach timer (pure, stubbed narration) ==")

    seq = []

    def fake_make_narration(route=None, hoisted=None, use_llm=None):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    orig_mn, orig_rn = copilot.make_narration, copilot.reset_narration
    copilot.make_narration = fake_make_narration
    copilot.reset_narration = lambda route=None: None
    try:
        c = coach_mod._Coach()
        c.route = "_bench_coach"

        seq.append({"active": [{"headline": "Windward in ~8 min"}],
                    "new": [{"headline": "Windward in ~8 min"}],
                    "spoken": "Windward mark in about 8 minutes.", "narration_mode": "deterministic",
                    "_meta": {"playbook_loaded": True}})
        asyncio.run(c.tick())
        st = c.state()
        ok &= _check("tick1 holds the latest spoken line", st["spoken"].startswith("Windward"))
        ok &= _check("tick1 logged one history entry", len(st["history"]) == 1)
        ok &= _check("tick1 active populated + tick counted", len(st["active"]) == 1 and st["ticks"] == 1)
        ok &= _check("tick1 surfaces playbook_loaded", st["playbook_loaded"] is True)

        seq.append({"active": [{"headline": "Windward in ~8 min"}], "new": [], "spoken": "",
                    "narration_mode": "none", "_meta": {}})
        asyncio.run(c.tick())
        st = c.state()
        ok &= _check("tick2 (nothing new) does not grow history", len(st["history"]) == 1)
        ok &= _check("tick2 clears last_error + counts", st["last_error"] is None and st["ticks"] == 2)

        seq.append(RuntimeError("engine down"))
        asyncio.run(c.tick())
        st = c.state()
        ok &= _check("tick3 error captured, loop survives",
                     bool(st["last_error"]) and "engine down" in st["last_error"] and st["ticks"] == 3)
    finally:
        copilot.make_narration, copilot.reset_narration = orig_mn, orig_rn
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="also exercise the LLM tool-loop")
    ap.add_argument("--question", default="What should the crew be thinking about right now?")
    ap.add_argument("--route", default=None)
    args = ap.parse_args()

    # The callout + adherence engines are pure — exercise them first, with no live engine needed.
    overall = test_narrate_logic()
    overall &= test_adherence_logic()
    overall &= test_coach_logic()

    print(f"\n>> engine: {config.ENGINE_URL}")
    engine = EngineClient()
    if not _check("engine reachable", engine.reachable()):
        print("!! engine not reachable — start the Pi engine (:8200) first; "
              "narration logic test above still ran.", file=sys.stderr)
        sys.exit(0 if overall else 2)

    print("\n== deterministic (engine-only) brief ==")
    det = copilot.make_brief(question=args.question, route=args.route, use_llm=False)
    overall &= _check("deterministic brief is engine_only", det.get("engine_only") is True)
    overall &= _audit_brief(det)

    print("\n== narration against the live engine (deterministic phrasing) ==")
    narrate_mod.reset(args.route or config.DEFAULT_ROUTE)
    live_narr = copilot.make_narration(route=args.route, use_llm=False)
    overall &= _check("live narration falls back to deterministic phrasing",
                      live_narr.get("narration_mode") in ("deterministic", "none"))
    overall &= _audit_narration(live_narr)

    if args.llm:
        print(f"\n== LLM brief ==  (model {config.LLM_MODEL} @ {config.LLM_BASE_URL})")
        llm_b = copilot.make_brief(question=args.question, route=args.route, use_llm=True)
        meta = llm_b.get("_meta", {})
        if meta.get("llm_used"):
            print("  (LLM path used)")
        else:
            print(f"  (fell back to deterministic — {meta.get('llm_error','?')})")
        overall &= _audit_brief(llm_b)
        # The grounding validator itself, exercised directly with a poisoned brief:
        print("\n== validator rejects ungrounded content ==")
        poisoned = brief_mod.new_brief()
        poisoned["factors"] = [{"factor": "made up", "detail": "x", "grounded_in": [], "confidence": "high"}]
        poisoned["recommendations"] = [{"action": "tack now", "rationale": "vibes",
                                        "grounded_in": ["get_tactics"], "urgency": "now", "confidence": "high"}]
        cleaned = brief_mod.validate(poisoned, allowed_sources={"get_tactics"})
        overall &= _check("ungrounded factor dropped", len(cleaned["factors"]) == 0)
        overall &= _check("grounded rec kept", len(cleaned["recommendations"]) == 1)

        print(f"\n== LLM narration ==  (model {config.LLM_MODEL} @ {config.LLM_BASE_URL})")
        narrate_mod.reset(args.route or config.DEFAULT_ROUTE)
        llm_narr = copilot.make_narration(route=args.route, use_llm=True)
        if llm_narr.get("narration_mode") == "llm":
            print("  (LLM phrased the callouts)")
        else:
            print(f"  (fell back to deterministic phrasing — mode {llm_narr.get('narration_mode')})")
        overall &= _audit_narration(llm_narr)

    print("\n" + ("PASS: decision-support layer holds its guardrails." if overall else
                  "FAIL: see above."))
    sys.exit(0 if overall else 2)


if __name__ == "__main__":
    main()
