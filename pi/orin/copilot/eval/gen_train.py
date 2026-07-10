"""SFT training-corpus generator (MATCHER_LORA_PLAN §3) — the same generators as the eval corpus,
a DIFFERENT seed (libraries are disjoint by construction; the seed-7 eval corpus is the held-out
set and must never be trained on), targets rendered from the oracle labels.

Teaching signal per example (what the baseline measurably gets wrong):
  - oracle-ARMED plays  -> match "strong", ranked first, why quoting the play's own condition
    language against the live numbers (the §1 explanation contract);
  - "arming" plays      -> match "partial" (conditions present, sustain window not yet met) —
    gives `partial` a precise meaning and trains calibration monotonicity;
  - every other near-miss (threshold / wrong-leg / confounder) -> ABSENT from play_matches.
    Saying no-match to a near-miss is most of the value (§3.3), so the assistant target for a
    quiet scenario is an EMPTY play_matches — silence is a verdict.
A slice of examples renders the prompt with all-[quiet] status hints (the blind regime): the
compound/fuzzy cases the predicates can't see arrive exactly like that in production.

    python3 -m copilot.eval.gen_train --libraries 400 --per-lib 8 --seed 1001 \\
        --blind-frac 0.35 --out /tmp/matcher_train.jsonl
"""

import argparse
import json
import random

from .. import copilot, playbook as playbook_mod
from . import gen_corpus

_EVAL_SEED = 7   # gen_corpus default — refuse to generate training data from the held-out seed


def _fmt(v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return f"{v:g}"
    return str(v)


def _why(play, signals, partial=False):
    """The explanation contract: quote the condition, cite the live numbers, no invention."""
    bits = []
    for p in (play["conditions"].get("predicates") or []):
        actual = signals.get(p["signal"])
        if isinstance(actual, list):
            actual = "+".join(actual) if actual else "none"
        bits.append(f"{p['signal']} {_fmt(actual)} vs {p['op']} {_fmt(p['value'])}")
    why = f"Its condition holds now: {'; '.join(bits)}."
    if partial:
        why = (f"Conditions present ({'; '.join(bits)}) but the sustain window "
               f"isn't met yet — arming, not armed.")
    return why[:300]


def _target(ex, seed_payload):
    """The assistant turn: engine facts reused verbatim, play_matches from the oracle."""
    signals = ex["signals"]
    plays = {str(p["id"]): p for p in ex["bundle"]["plays"]}
    matches = [{"play_id": pid, "match": "strong", "why": _why(plays[pid], signals)}
               for pid in ex["oracle"]["armed"]]
    matches += [{"play_id": pid, "match": "partial", "why": _why(plays[pid], signals, partial=True)}
                for pid, mode in ex["oracle"]["near"].items() if mode == "sustain"]
    rec = dict(seed_payload["recommendation"])
    if matches:
        rec["rationale"] = (f"Pre-authored condition met — {plays[matches[0]['play_id']]['name']}: "
                            f"{plays[matches[0]['play_id']]['response']['guidance']}")
    rec["grounded_in"] = sorted({g for row in seed_payload["picture"]
                                 for g in row["grounded_in"]} | {"get_strategy"})
    return {"assessment": seed_payload["assessment"], "recommendation": rec,
            "play_matches": matches}


def generate(libraries=400, per_lib=8, seed=1001, blind_frac=0.35):
    if seed == _EVAL_SEED:
        raise SystemExit(f"seed {seed} is the held-out EVAL seed — pick another")
    rows = gen_corpus.generate(libraries=libraries, per_lib=per_lib, seed=seed)
    rng = random.Random(seed + 1)
    out = []
    for ex in rows:
        pb = playbook_mod.Playbook(ex["bundle"])
        blind = rng.random() < blind_frac
        smap = {pid: "quiet" for pid in pb.play_ids()} if blind else ex["status_map"]
        seed_payload = json.loads(ex["seed"].split("\n", 1)[1])
        out.append({"id": ex["id"], "blind": blind, "messages": [
            {"role": "system", "content": copilot._strategy_prompt(pb, smap)},
            {"role": "user", "content": ex["seed"]},
            {"role": "assistant", "content": json.dumps(_target(ex, seed_payload),
                                                        separators=(",", ":"))},
        ]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--libraries", type=int, default=400)
    ap.add_argument("--per-lib", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1001)
    ap.add_argument("--blind-frac", type=float, default=0.35)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    rows = generate(args.libraries, args.per_lib, args.seed, args.blind_frac)
    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    n_blind = sum(r["blind"] for r in rows)
    n_match = sum(1 for r in rows if json.loads(r["messages"][2]["content"])["play_matches"])
    print(f"wrote {len(rows)} SFT examples -> {args.out}")
    print(f"  blind-hint slice: {n_blind} · with >=1 match: {n_match} · "
          f"quiet (empty matches): {len(rows) - n_match}")


if __name__ == "__main__":
    main()
