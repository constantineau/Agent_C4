"""Corpus generator CLI — seeded, reproducible; split by LIBRARY (never scenario) so any future
training run can hold out whole libraries per §4.

    PYTHONPATH=../../vps/agent:../.. python3 -m copilot.eval.gen_corpus --libraries 30 \\
        --per-lib 8 --seed 7 --out /tmp/matcher_corpus.jsonl    # dev VM; copy the file to the Orin

The default split is "eval": held-out phrasings for every play family plus held-out families the
training split never generates (libgen.py), so the score measures matching, not template recall.
Seeds are rendered by the engine's own strategy code (scengen.py), so generate where vps/agent
imports.
"""

import argparse
import json
import random

from . import libgen, scengen


def generate(libraries=30, per_lib=8, seed=7, split="eval", blind_frac=None):
    """Rows of labeled examples. `blind_frac` (training only): that share of examples is marked
    `regime: "blind"` and built from the near-miss modes a blind view can decide; the rest are
    `"hinted"`. Without it (the eval) every example carries both views (`regime: "both"`)."""
    rng = random.Random(seed)
    rows = []
    for li in range(libraries):
        lib = libgen.make_library(rng, n_plays=rng.choice([5, 6, 7]), race_idx=li, split=split)
        for si in range(per_lib):
            n_armed = rng.choice([0, 1, 1, 2])          # some all-quiet scenarios: silence is a verdict
            regime = "both"
            modes = scengen.MODES
            if blind_frac is not None:
                regime = "blind" if rng.random() < blind_frac else "hinted"
                if regime == "blind":
                    modes = scengen.BLIND_MODES
            sc = scengen.make_scenario(rng, lib, n_armed=n_armed,
                                       near_modes=scengen.sample_modes(rng, modes))
            rows.append({"id": f"{split}-lib{li:02d}-sc{si:02d}", "library_idx": li,
                         "split": split, "regime": regime, "bundle": lib, **sc})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--libraries", type=int, default=30)
    ap.add_argument("--per-lib", type=int, default=8)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--split", choices=libgen.SPLITS, default="eval")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    rows = generate(args.libraries, args.per_lib, args.seed, args.split)
    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    n_armed = sum(len(r["oracle"]["armed"]) for r in rows)
    n_near = sum(len(r["oracle"]["near"]) for r in rows)
    n_blind = sum(len(r["blind_oracle"]["armed"]) for r in rows)
    held = libgen.heldout_keys()
    n_held = sum(1 for r in rows for p in r["bundle"]["plays"] if p.get("family") in held)
    print(f"wrote {len(rows)} examples ({args.libraries} libraries × {args.per_lib}, "
          f"split {args.split}) -> {args.out}")
    print(f"  armed instances: {n_armed} (blind-scorable {n_blind}) · near-miss instances: "
          f"{n_near} · held-out-family play slots: {n_held}")


if __name__ == "__main__":
    main()
