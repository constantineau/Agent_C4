"""Pluggable snapshot selection + the labeling queue policy.

Phase 0 is RANDOM/coverage-first: cover every snapshot once, then add ~OVERLAP_FRAC double-labels
for inter-rater agreement, then stop. The `select_active_pool` seam is where ACTIVE LEARNING drops
in later (Plan §6) — surface the snapshots where candidates are closest / the model is least certain,
so at scale sailors label the informative cases, not thousands of easy ones. Kept behind one function
so nothing downstream changes when that swap happens.
"""
from . import config, schema


def load_snapshots() -> list[dict]:
    return schema.read_jsonl(config.SNAPSHOTS)


def load_candidates_by_snapshot() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in schema.read_jsonl(config.CANDIDATES):
        out.setdefault(c["snapshot_id"], []).append(c)
    return out


def select_active_pool(snapshots: list[dict]) -> list[str]:
    """Which snapshots are eligible to label, in priority order. Phase 0: all, corpus order.
    ACTIVE-LEARNING SEAM — replace the body to rank by candidate disagreement / model uncertainty."""
    return [s["snapshot_id"] for s in snapshots]


def next_snapshot_for(labeler_id: str, store) -> str | None:
    """Coverage-first queue: full single coverage → ~OVERLAP_FRAC double coverage → done.

    `store` is the labeling.store module (passed in to avoid an import cycle)."""
    snaps = load_snapshots()
    pool = select_active_pool(snaps)
    done = store.snapshots_done_by(labeler_id)
    counts = store.ranking_counts()

    remaining = [sid for sid in pool if sid not in done]
    if not remaining:
        return None

    # 1) never-labeled first — get one ranking on everything before doubling up.
    singles = [sid for sid in remaining if counts.get(sid, 0) == 0]
    if singles:
        return singles[0]

    # 2) add overlap up to the target, drawing from snapshots that have exactly one ranking.
    target_doubles = round(config.OVERLAP_FRAC * len(pool))
    current_doubles = sum(1 for sid in pool if counts.get(sid, 0) >= 2)
    if current_doubles < target_doubles:
        ones = [sid for sid in remaining if counts.get(sid, 0) == 1]
        if ones:
            return ones[0]

    # 3) enough coverage + overlap — this labeler is done (or only over-covered snapshots remain).
    return None


def progress(store) -> dict:
    snaps = load_snapshots()
    total = len(snaps)
    counts = store.ranking_counts()
    covered = sum(1 for s in snaps if counts.get(s["snapshot_id"], 0) >= 1)
    doubled = sum(1 for s in snaps if counts.get(s["snapshot_id"], 0) >= 2)
    return {"snapshots": total, "covered": covered, "double_labeled": doubled,
            "overlap_target": round(config.OVERLAP_FRAC * total)}
