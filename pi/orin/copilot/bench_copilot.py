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

from . import brief as brief_mod
from . import config, copilot, narrate as narrate_mod
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
    "get_tactics": {"available": True, "persistent": True, "favored_side": "left",
                    "oscillation_deg": 12, "recommendation": "work the left"},
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="also exercise the LLM tool-loop")
    ap.add_argument("--question", default="What should the crew be thinking about right now?")
    ap.add_argument("--route", default=None)
    args = ap.parse_args()

    # The callout engine is pure — exercise it first, with no live engine needed.
    overall = test_narrate_logic()

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
