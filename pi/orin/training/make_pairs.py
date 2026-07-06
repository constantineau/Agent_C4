"""Rankings → DPO preference pairs → data/pref.jsonl (+ the held-out eval list).

Turns expert rankings into best-vs-worse preference pairs in the shape a DPO trainer wants — each
carries the EXACT copilot strategy prompt (system + engine-picture seed) so train == inference, and
`chosen`/`rejected` are the assistant JSON outputs (`{assessment, recommendation}`) of the two
candidates. Two Plan §3 details:

  * HELD-OUT SPLIT — a deterministic ~EVAL_HOLDOUT_FRAC of snapshots (by content hash) NEVER produce
    pairs; they're the expert blind-A/B eval set (Plan §5). Written to data/eval_holdout.txt.
  * CALIBRATION DEMOTION — a candidate a sailor flagged `too_high`/`too_low` is demoted below an
    adjacent `right`-flagged one, so miscalibration loses its pair even when the call was otherwise
    fine (that's how "calibration" rides in the same pairs).

Runs fine with zero rankings (emits 0 pairs) — it just reports coverage. This is Phase-0 scaffolding;
the actual DPO run is Phase 2 on a rented GPU.

    python3 -m training.make_pairs
"""
import hashlib
import json

from . import config, schema
from .labeling import store

from copilot import copilot as cp
from copilot import playbook as playbook_mod


def _is_holdout(snapshot_id: str) -> bool:
    """Deterministic hash bucket → stable holdout membership regardless of label order."""
    h = int(hashlib.sha1(("holdout:" + snapshot_id).encode()).hexdigest()[:8], 16)
    return (h % 1000) / 1000.0 < config.EVAL_HOLDOUT_FRAC


def _prompt_for(digest: dict) -> list[dict]:
    pb = playbook_mod.load()
    system = cp._strategy_prompt(pb)
    seed = ("STRATEGIC PICTURE (engine-computed facts — reuse these, invent nothing):\n"
            + json.dumps({"assessment": digest.get("assessment"), "picture": digest.get("picture"),
                          "concordance": digest.get("concordance"),
                          "recommendation": digest.get("recommendation")}, separators=(",", ":")))
    return [{"role": "system", "content": system}, {"role": "user", "content": seed}]


def _assistant_output(cand: dict) -> str:
    rec = cand.get("recommendation") or {}
    return json.dumps({"assessment": cand.get("assessment", ""),
                       "recommendation": {k: rec.get(k) for k in
                                          ("action", "vs_playbook", "rationale", "grounded_in",
                                           "urgency", "confidence") if rec.get(k) is not None}},
                      separators=(",", ":"))


def _apply_calibration_demotion(order: list[str], calib: dict[str, str]) -> list[str]:
    """Stable bubble: if candidate A is directly above B, A is miscalibrated and B is 'right',
    swap them — the well-calibrated call wins the adjacent pair."""
    out = list(order)
    swapped = True
    passes = 0
    while swapped and passes < len(out):
        swapped = False
        passes += 1
        for i in range(len(out) - 1):
            a, b = out[i], out[i + 1]
            a_bad = calib.get(a) in ("too_high", "too_low")
            b_ok = calib.get(b) == "right"
            if a_bad and b_ok:
                out[i], out[i + 1] = b, a
                swapped = True
    return out


def build_pairs():
    snaps = {s["snapshot_id"]: s for s in schema.read_jsonl(config.SNAPSHOTS)}
    cands_by_snap = {}
    for c in schema.read_jsonl(config.CANDIDATES):
        cands_by_snap.setdefault(c["snapshot_id"], {})[c["candidate_id"]] = c

    rankings = store.all_rankings()
    pairs, holdout, used_snaps, skipped_holdout = [], set(), set(), set()

    for r in rankings:
        sid = r["snapshot_id"]
        if sid not in snaps or sid not in cands_by_snap:
            continue
        if _is_holdout(sid):
            holdout.add(sid)
            skipped_holdout.add(sid)
            continue
        order = _apply_calibration_demotion(r["order"], r.get("calibration") or {})
        cmap = cands_by_snap[sid]
        prompt = _prompt_for(snaps[sid]["digest"])
        # best-vs-each-worse: every earlier-ranked candidate is `chosen` over each later one.
        for i in range(len(order)):
            for j in range(i + 1, len(order)):
                ci, cj = cmap.get(order[i]), cmap.get(order[j])
                if not ci or not cj:
                    continue
                pairs.append({
                    "snapshot_id": sid, "labeler_id": r["labeler_id"],
                    "prompt": prompt,
                    "chosen": _assistant_output(ci), "rejected": _assistant_output(cj),
                    "chosen_origin": ci["origin"], "rejected_origin": cj["origin"],
                })
        used_snaps.add(sid)

    return pairs, holdout, used_snaps


def main():
    pairs, holdout, used_snaps = build_pairs()
    schema.write_jsonl(config.PAIRS, pairs)

    holdout_path = config.PAIRS.replace("pref.jsonl", "eval_holdout.txt")
    # Full deterministic holdout membership across the WHOLE corpus (not just labeled snaps),
    # so the split is fixed up front — labeling more never moves a snapshot in/out of eval.
    all_holdout = sorted(s["snapshot_id"] for s in schema.read_jsonl(config.SNAPSHOTS)
                         if _is_holdout(s["snapshot_id"]))
    with open(holdout_path, "w") as f:
        f.write("\n".join(all_holdout) + ("\n" if all_holdout else ""))

    print(f"preference pairs: {len(pairs)} → {config.PAIRS}")
    print(f"  from {len(used_snaps)} labeled train snapshots")
    print(f"  held-out eval snapshots (never trained): {len(all_holdout)} "
          f"({len(holdout)} of them already labeled) → {holdout_path}")
    if not pairs:
        print("  (no rankings yet — run the labeling app and collect some, then re-run this)")


if __name__ == "__main__":
    main()
