#!/usr/bin/env python3
"""Score a Race Rewind timeline for FALSE ALARMS and FLAPPING, not for correctness at a moment.

Two lessons from this project made this file:

  * a value that is right every other poll passes every point-in-time test — the bank tile
    flipped `warn`/`danger` 17 times in 40 minutes and no test noticed, because each individual
    reading was defensible;
  * every check the boat has was tuned against Jul 18 2026, a race with a flat house bank, a
    kicked compass and a retirement. **Nothing had ever been scored against a healthy race**, so
    the false-alarm rate was a claim rather than a measurement.

Point it at the Jul 15 timeline (all instruments alive, no failures) and every non-`ok` frame is
a false alarm by construction. Point it at Jul 18 and the same numbers are the detections. The
interesting output is the diff between the two.

    python3 tools/replay/score_stability.py <timeline-dir> [<timeline-dir> ...]
"""
import json
import os
import sys
from collections import defaultdict

# (label, endpoint, path-into-the-response). Each must resolve to a status string.
TILES = [
    ("power/bank",        "/power",           ("status",)),
    ("health overall",    "/health/sensors",  ("status",)),
    ("health/heading",    "/health/sensors",  ("heading", "status")),
    ("health/attitude",   "/health/sensors",  ("attitude", "status")),
    ("deviation",         "/deviation",       ("status",)),
    ("selector",          "/selector",        ("status",)),
]
GOOD = {"ok", "unknown", "na", None}      # `unknown` is honest silence, not an alarm


def dig(obj, path):
    for k in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def score(dir_path):
    frames = [json.loads(l) for l in open(os.path.join(dir_path, "frames.jsonl"))]
    print(f"\n=== {os.path.basename(dir_path)} — {len(frames)} frames "
          f"{frames[0]['t']} -> {frames[-1]['t']}")
    print(f"{'tile':18} {'frames':>7} {'not-ok':>7} {'%':>6} {'flips':>6}  states")
    rows = []
    for label, ep, path in TILES:
        seq = []
        for f in frames:
            v = dig((f.get("data") or {}).get(ep), path)
            if v is not None:
                seq.append(v)
        if not seq:
            continue
        bad = sum(1 for v in seq if v not in GOOD)
        flips = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
        counts = defaultdict(int)
        for v in seq:
            counts[v] += 1
        states = " ".join(f"{k}:{n}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        print(f"{label:18} {len(seq):>7} {bad:>7} {100 * bad / len(seq):>5.1f}% {flips:>6}  {states}")
        rows.append((label, len(seq), bad, flips))
    # An endpoint that errored is not a passing tile — count those separately or a broken
    # endpoint reads as a quiet one.
    errs = defaultdict(int)
    for f in frames:
        for ep, body in (f.get("data") or {}).items():
            if isinstance(body, dict) and ("detail" in body or body.get("error")):
                errs[ep] += 1
    print("endpoint errors:", dict(errs) or "none")
    return rows


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for d in sys.argv[1:]:
        score(d)
