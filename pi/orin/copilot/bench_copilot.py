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
from . import config, copilot
from .engine_client import EngineClient


def _check(name, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
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

    print(f">> engine: {config.ENGINE_URL}")
    engine = EngineClient()
    if not _check("engine reachable", engine.reachable()):
        print("!! engine not reachable — start the Pi engine (:8200) first", file=sys.stderr)
        sys.exit(1)

    overall = True

    print("\n== deterministic (engine-only) brief ==")
    det = copilot.make_brief(question=args.question, route=args.route, use_llm=False)
    overall &= _check("deterministic brief is engine_only", det.get("engine_only") is True)
    overall &= _audit_brief(det)

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

    print("\n" + ("PASS: decision-support layer holds its guardrails." if overall else
                  "FAIL: see above."))
    sys.exit(0 if overall else 2)


if __name__ == "__main__":
    main()
