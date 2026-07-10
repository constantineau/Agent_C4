"""Corpus generator CLI — seeded, reproducible; split by LIBRARY (never scenario) so any future
training run can hold out whole libraries per §4.

    python3 -m copilot.eval.gen_corpus --libraries 30 --per-lib 8 --seed 7 \\
        --out /tmp/matcher_corpus.jsonl
"""

import argparse
import json
import random

from . import libgen, scengen


def generate(libraries=30, per_lib=8, seed=7):
    rng = random.Random(seed)
    rows = []
    for li in range(libraries):
        lib = libgen.make_library(rng, n_plays=rng.choice([5, 6, 7]), race_idx=li)
        for si in range(per_lib):
            n_armed = rng.choice([0, 1, 1, 2])          # some all-quiet scenarios: silence is a verdict
            sc = scengen.make_scenario(rng, lib, n_armed=n_armed,
                                       near_modes=scengen.sample_modes(rng))
            rows.append({"id": f"lib{li:02d}-sc{si:02d}", "library_idx": li,
                         "bundle": lib, **sc})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--libraries", type=int, default=30)
    ap.add_argument("--per-lib", type=int, default=8)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    rows = generate(args.libraries, args.per_lib, args.seed)
    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    n_armed = sum(len(r["oracle"]["armed"]) for r in rows)
    n_near = sum(len(r["oracle"]["near"]) for r in rows)
    print(f"wrote {len(rows)} examples ({args.libraries} libraries × {args.per_lib}) -> {args.out}")
    print(f"  armed instances: {n_armed} · near-miss instances: {n_near}")


if __name__ == "__main__":
    main()
