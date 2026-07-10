"""Exit test for the eval harness itself: oracle lock-step vs the real matcher (where the engine
code is importable), generator/label consistency over a real corpus, metrics math, and the
production prompt path rendering. Pure — no engine, no LLM.

    cd pi/orin && python3 -m copilot.eval.test_eval          # (or from repo root, see below)
"""

import json
import os
import random
import sys

from . import gen_corpus, libgen, metrics, oracle, scengen
from .. import copilot, playbook as playbook_mod


def _check(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return bool(ok)


def test_lockstep():
    """oracle.pred_ok must match matcher._pred_ok on a semantics grid — skipped (honestly) when
    the engine stack isn't importable (the Orin venv)."""
    print("\n== oracle lock-step vs app.matcher ==")
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    sys.path[:0] = [root, os.path.join(root, "vps", "agent")]   # PYTHONPATH=vps/agent:. equivalent
    try:
        from app.matcher import _pred_ok as real
    except Exception as e:
        print(f"  [SKIP] engine matcher not importable here ({type(e).__name__}) — run on the VM")
        return True
    grid = [(op, a, v)
            for op in (">=", "<=", "==")
            for a in (None, 0, 14.3, 15.0, 15.7, -12, True, False, "C0", ["J1", "C0"], [], "15")
            for v in (15, -12, 0.0, True, False, "C0", "c0", "15")]
    bad = [(op, a, v) for op, a, v in grid if oracle.pred_ok(op, a, v) != real(op, a, v)]
    return _check(f"pred_ok agrees on {len(grid)} cases", not bad) or print("   drift:", bad[:5])


def test_generator_labels():
    """Every corpus label must be the literal oracle truth of its own snapshot, and every
    near-miss must fail for its declared reason."""
    print("\n== generator/label consistency (300 examples) ==")
    rows = gen_corpus.generate(libraries=25, per_lib=12, seed=11)
    ok = True
    n_by_mode = {}
    for r in rows:
        lib, sig, sus = r["bundle"], r["signals"], r["sustained"]
        smap = oracle.status_map(lib, sig, sus)
        ok &= smap == r["status_map"]
        ok &= sorted(p for p, s in smap.items() if s == "armed") == r["oracle"]["armed"]
        for pid, mode in r["oracle"]["near"].items():
            n_by_mode[mode] = n_by_mode.get(mode, 0) + 1
            play = next(p for p in lib["plays"] if p["id"] == pid)
            if mode == "sustain":
                ok &= smap[pid] == "arming"
            elif mode == "wrong_leg":
                ok &= smap[pid] == "quiet" and not oracle._applicable(play, sig)
            else:                                    # threshold / confounder stay quiet
                ok &= smap[pid] == "quiet"
        json.loads(r["seed"].split("\n", 1)[1])      # the seed payload must be valid JSON
    ok = _check("labels == oracle truth on every example", ok)
    ok &= _check(f"all four near-miss modes represented {n_by_mode}",
                 set(n_by_mode) == {"threshold", "sustain", "wrong_leg", "confounder"})
    armed_counts = [len(r["oracle"]["armed"]) for r in rows]
    ok &= _check("mix of quiet / single / multi-armed scenarios",
                 armed_counts.count(0) > 10 and any(c >= 2 for c in armed_counts))
    return ok


def test_prompt_path():
    """The production prompt must render the synthetic bundle: play ids + narratives present,
    digest non-empty, and the validator behaves on known/unknown ids."""
    print("\n== production prompt path on a synthetic bundle ==")
    rng = random.Random(3)
    lib = libgen.make_library(rng, n_plays=6)
    sc = scengen.make_scenario(rng, lib, n_armed=1, near_modes=("threshold",))
    pb = playbook_mod.Playbook(lib)
    prompt = copilot._strategy_prompt(pb, sc["status_map"])
    ok = _check("every play id + narrative in the prompt",
                all(p["id"] in prompt and p["conditions"]["narrative"][:40] in prompt
                    for p in lib["plays"]))
    ok &= _check("digest renders the variants", "variant left" in prompt)
    parsed = {"play_matches": [
        {"play_id": lib["plays"][0]["id"], "match": "strong", "why": "w"},
        {"play_id": "invented-play", "match": "strong", "why": "w"}]}
    filt = copilot._filter_play_matches(parsed, pb, sc["status_map"])
    ok &= _check("validator keeps known id, drops invented id",
                 len(filt) == 1 and filt[0]["play_id"] == lib["plays"][0]["id"])
    return ok


def test_metrics_math():
    print("\n== metrics math ==")
    exs = [{"oracle": {"armed": ["a"], "near": {"b": "threshold"}, "quiet": ["c"]},
            "bundle": {"plays": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}},
           {"oracle": {"armed": [], "near": {"c": "sustain"}, "quiet": ["a", "b"]},
            "bundle": {"plays": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}}]
    perfect = [{"parse_ok": True, "schema_ok": True,
                "raw": [{"play_id": "a", "match": "strong", "why": "w"}],
                "filtered": [{"play_id": "a", "match": "strong", "why": "w"}]},
               {"parse_ok": True, "schema_ok": True, "raw": [], "filtered": []}]
    m = metrics.score(exs, perfect)
    ok = _check("perfect predictions pass every gate", m["pass"]
                and m["armed_set"]["f1"] == 1.0 and m["near_miss_fp_rate"] == 0.0)
    fooled = [{"parse_ok": True, "schema_ok": True,
               "raw": [{"play_id": "b", "match": "strong", "why": "w"},
                       {"play_id": "ghost", "match": "strong", "why": "w"}],
               "filtered": [{"play_id": "b", "match": "strong", "why": "w"}]},
              {"parse_ok": False, "schema_ok": False, "raw": [], "filtered": []}]
    m2 = metrics.score(exs, fooled)
    ok &= _check("near-miss FP + grounding violation + schema failure all counted",
                 not m2["pass"] and m2["near_miss_fp_rate"] == 0.5
                 and m2["grounding_violation_rate"] == 0.5
                 and m2["reliability"]["schema_rate"] == 0.5)
    return ok


def main():
    ok = test_lockstep()
    ok &= test_generator_labels()
    ok &= test_prompt_path()
    ok &= test_metrics_math()
    print("\nALL PASS" if ok else "\nFAIL: see above")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
