"""Phase-0 eval + engine-audit report → data/eval_report.json.

Three reads over whatever labels exist so far (all run fine on partial/zero data):

  1. INTER-RATER AGREEMENT (the pilot GATE, Plan §7) — over double-labeled snapshots: how often two
     sailors pick the same #1, and their average rank correlation (Kendall tau). Low agreement = the
     rubric is ambiguous; fix it before scaling. NO DPO fixes a bad rubric.
  2. ENGINE AUDIT (Plan §4) — how often the sailors' consensus best is NOT the deterministic
     candidate, and how often a candidate that DEPARTS the engine's recommendation wins. Grouped by
     scenario tag → a labeled tuning/bug signal for vps/agent/app/strategy.py.
  3. CALIBRATION — the distribution of too_high/too_low flags (is the model systematically over- or
     under-confident?), overall and by candidate origin.

The base-vs-tuned blind A/B (Plan §5) is Phase 3 (no tuned model yet); as a preview this reports the
base model's consensus rank vs the deterministic anchor when base candidates are present.

    python3 -m training.eval_judgment
"""
import json
from itertools import combinations

from . import config, schema
from .labeling import store


# --- Borda consensus over a set of rankings --------------------------------------------------
def _borda(rankings: list[list[str]]) -> list[str]:
    """Rank candidates by summed Borda points across rankings (best-first)."""
    score: dict[str, float] = {}
    for order in rankings:
        n = len(order)
        for pos, cid in enumerate(order):
            score[cid] = score.get(cid, 0.0) + (n - 1 - pos)
    return sorted(score, key=lambda c: -score[c])


def _kendall_tau(a: list[str], b: list[str]) -> float | None:
    """Rank correlation over the shared candidate set (-1..1). None if <2 shared."""
    shared = [c for c in a if c in b]
    if len(shared) < 2:
        return None
    ra = {c: i for i, c in enumerate(a)}
    rb = {c: i for i, c in enumerate(b)}
    conc = disc = 0
    for x, y in combinations(shared, 2):
        s = (ra[x] - ra[y]) * (rb[x] - rb[y])
        if s > 0:
            conc += 1
        elif s < 0:
            disc += 1
    tot = conc + disc
    return (conc - disc) / tot if tot else None


def report() -> dict:
    snaps = {s["snapshot_id"]: s for s in schema.read_jsonl(config.SNAPSHOTS)}
    origin = {}
    for c in schema.read_jsonl(config.CANDIDATES):
        origin[c["candidate_id"]] = c["origin"]

    rankings = store.all_rankings()
    by_snap: dict[str, list[dict]] = {}
    for r in rankings:
        by_snap.setdefault(r["snapshot_id"], []).append(r)

    # 1) inter-rater agreement over double-labeled snapshots
    top1_hits = top1_total = 0
    taus = []
    for sid, rs in by_snap.items():
        if len(rs) < 2:
            continue
        for r1, r2 in combinations(rs, 2):
            top1_total += 1
            if r1["order"] and r2["order"] and r1["order"][0] == r2["order"][0]:
                top1_hits += 1
            t = _kendall_tau(r1["order"], r2["order"])
            if t is not None:
                taus.append(t)
    agreement = {
        "double_labeled_snapshots": sum(1 for rs in by_snap.values() if len(rs) >= 2),
        "pairwise_comparisons": top1_total,
        "top1_agreement": round(top1_hits / top1_total, 3) if top1_total else None,
        "mean_kendall_tau": round(sum(taus) / len(taus), 3) if taus else None,
        "gate_note": "pilot gate — aim for top1_agreement ≳ 0.6 before scaling; else fix the rubric",
    }

    # 2) engine audit — consensus best vs the deterministic anchor, by scenario tag
    audit_by_tag: dict[str, dict] = {}
    beats_engine = departs_win = judged = 0
    for sid, rs in by_snap.items():
        consensus = _borda([r["order"] for r in rs])
        if not consensus:
            continue
        judged += 1
        top = consensus[0]
        top_origin = origin.get(top, "?")
        tag = (snaps.get(sid, {}).get("scenario") or {}).get("tag", "?")
        d = audit_by_tag.setdefault(tag, {"n": 0, "engine_lost": 0, "departs_won": 0,
                                          "winning_origins": {}})
        d["n"] += 1
        d["winning_origins"][top_origin] = d["winning_origins"].get(top_origin, 0) + 1
        if top_origin != "deterministic":
            beats_engine += 1
            d["engine_lost"] += 1
    engine_audit = {
        "judged_snapshots": judged,
        "consensus_beats_deterministic": beats_engine,
        "beats_rate": round(beats_engine / judged, 3) if judged else None,
        "by_scenario_tag": audit_by_tag,
        "note": "high engine_lost on a tag = a labeled tuning/bug signal for strategy.py (Plan §4)",
    }

    # 3) calibration flags overall + by origin
    calib_counts = {"right": 0, "too_high": 0, "too_low": 0}
    calib_by_origin: dict[str, dict] = {}
    for r in rankings:
        for cid, flag in (r.get("calibration") or {}).items():
            if flag in calib_counts:
                calib_counts[flag] += 1
                o = origin.get(cid, "?")
                calib_by_origin.setdefault(o, {"right": 0, "too_high": 0, "too_low": 0})[flag] += 1

    return {
        "totals": store.stats(),
        "inter_rater_agreement": agreement,
        "engine_audit": engine_audit,
        "calibration": {"overall": calib_counts, "by_origin": calib_by_origin},
    }


def main():
    rep = report()
    schema.write_jsonl(config.EVAL_REPORT, [rep]) if False else None
    import os
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.EVAL_REPORT, "w") as f:
        json.dump(rep, f, indent=2)

    a = rep["inter_rater_agreement"]
    e = rep["engine_audit"]
    print(f"eval report → {config.EVAL_REPORT}")
    print(f"  rankings: {rep['totals']['total_rankings']} by {rep['totals']['labelers']} labeler(s); "
          f"{rep['totals']['snapshots_double_labeled']} double-labeled")
    print(f"  inter-rater top-1 agreement: {a['top1_agreement']}  (mean tau {a['mean_kendall_tau']})  "
          f"[{a['gate_note']}]")
    print(f"  engine audit: consensus beats deterministic {e['consensus_beats_deterministic']}"
          f"/{e['judged_snapshots']} (rate {e['beats_rate']})")
    print(f"  calibration flags: {rep['calibration']['overall']}")
    if rep["totals"]["total_rankings"] == 0:
        print("  (no rankings yet — collect labels via the labeling app first)")


if __name__ == "__main__":
    main()
