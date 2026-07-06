"""End-to-end offline smoke test for the Phase-0 pipeline — no engine, no LLM, no API key.

Exercises the whole flywheel on a temp DB: snapshots → candidates → simulated rankings (two
labelers, with overlap + calibration flags) → preference pairs → eval/engine-audit report. Asserts
the pieces connect and produce sane numbers. Safe to run anytime — uses a throwaway DB + temp
outputs, never touches real labels.

    python3 -m training.smoke
"""
import os
import tempfile

from . import config, schema


def main():
    tmp = tempfile.mkdtemp(prefix="c4lora-smoke-")
    config.PREF_DB = os.path.join(tmp, "labels.sqlite")
    config.PAIRS = os.path.join(tmp, "pref.jsonl")
    config.EVAL_REPORT = os.path.join(tmp, "eval_report.json")
    config.SNAPSHOTS = os.path.join(tmp, "snapshots.jsonl")
    config.CANDIDATES = os.path.join(tmp, "candidates.jsonl")

    # 1) snapshots (synthetic) + 2) candidates (offline: deterministic + perturbed)
    from . import gen_snapshots, gen_candidates
    snaps = gen_snapshots.build_synthetic()
    schema.write_jsonl(config.SNAPSHOTS, snaps)
    cands = []
    for s in snaps:
        cands.extend(gen_candidates.generate_for(s, llm=None))
    schema.write_jsonl(config.CANDIDATES, cands)
    assert len(snaps) >= 40, f"expected a real corpus, got {len(snaps)}"
    assert len(cands) >= 2 * len(snaps) - 5, "expected ~2 offline candidates per snapshot"
    print(f"1-2) {len(snaps)} snapshots, {len(cands)} candidates")

    # 3) simulate two labelers ranking. A: engine-first (deterministic > perturbed). B: same, but
    #    flips ~1-in-6 (so agreement is realistic, not a trivial 1.0), and flags the perturbed
    #    (overconfident) candidate's calibration. Both rank the first ~half → overlap.
    from .labeling import store
    cands_by_snap: dict[str, list[dict]] = {}
    for c in cands:
        cands_by_snap.setdefault(c["snapshot_id"], []).append(c)

    la, lb = store.upsert_labeler("Skipper Ann"), store.upsert_labeler("Tactician Bob")
    overlap_cut = len(snaps) // 2
    for i, s in enumerate(snaps):
        sid = s["snapshot_id"]
        cs = cands_by_snap[sid]
        det = next((c["candidate_id"] for c in cs if c["origin"] == "deterministic"), None)
        pert = next((c["candidate_id"] for c in cs if c["origin"] == "perturbed"), None)
        if not det or not pert:
            continue
        # Labeler A — engine-first, flags the perturbed call as miscalibrated.
        store.record_ranking(sid, la, [det, pert], {pert: "too_high"}, notes="", elapsed_ms=8000)
        # Labeler B — overlaps on the first half; disagrees on every 6th to make tau < 1.
        if i < overlap_cut:
            order = [pert, det] if i % 6 == 0 else [det, pert]
            store.record_ranking(sid, lb, order, {pert: "too_high"}, notes="", elapsed_ms=9000)
    print(f"3) simulated rankings: {store.stats()}")

    # 4) preference pairs
    from . import make_pairs
    pairs, holdout, used = make_pairs.build_pairs()
    schema.write_jsonl(config.PAIRS, pairs)
    assert pairs, "expected preference pairs from the simulated rankings"
    # Train pairs must exclude held-out snapshots.
    assert all(p["snapshot_id"] not in holdout for p in pairs), "holdout leaked into training pairs"
    det_chosen = sum(1 for p in pairs if p["chosen_origin"] == "deterministic")
    print(f"4) {len(pairs)} pairs from {len(used)} train snapshots; "
          f"{det_chosen}/{len(pairs)} chose the engine candidate; {len(holdout)} eval snaps held out")
    assert det_chosen > len(pairs) * 0.7, "engine candidate should win the large majority"

    # 5) eval + engine audit
    from . import eval_judgment
    rep = eval_judgment.report()
    ag = rep["inter_rater_agreement"]
    assert ag["double_labeled_snapshots"] > 0, "expected overlap for agreement"
    assert ag["top1_agreement"] is not None and 0.5 < ag["top1_agreement"] < 1.0, \
        f"agreement should be realistic, got {ag['top1_agreement']}"
    assert rep["engine_audit"]["judged_snapshots"] > 0
    assert rep["calibration"]["overall"]["too_high"] > 0, "calibration flags should be recorded"
    print(f"5) inter-rater top-1 agreement {ag['top1_agreement']} (tau {ag['mean_kendall_tau']}); "
          f"engine beaten {rep['engine_audit']['beats_rate']}; "
          f"calib {rep['calibration']['overall']}")

    print("\nSMOKE OK — snapshots → candidates → rankings → pairs → eval all connect.")


if __name__ == "__main__":
    main()
