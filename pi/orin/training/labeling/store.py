"""Append-only preference store (sqlite, pure stdlib).

One `rankings` row per (snapshot, labeler): the full best→worst candidate order + per-candidate
calibration flags + optional notes. Keeping the FULL ranking (not just winner/loser pairs) is
deliberate — the reward model (Plan §6) trains on the richer signal, and make_pairs derives pairs
from it. Deliberate `overlap` (a snapshot ranked by ≥2 sailors) powers inter-rater agreement, the
Phase-0 pilot gate. `gold` rows carry a known-best candidate to score labeler reliability.

The store is storage only; the queue/selection policy lives in sampling.py so it stays pluggable.
"""
import os
import re
import sqlite3
import time

from .. import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS labelers (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rankings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id  TEXT NOT NULL,
    labeler_id   TEXT NOT NULL,
    order_json   TEXT NOT NULL,   -- best->worst list of candidate_ids
    calib_json   TEXT NOT NULL,   -- {candidate_id: "right|too_high|too_low"}
    notes        TEXT,
    elapsed_ms   INTEGER,
    created_at   REAL NOT NULL,
    UNIQUE(snapshot_id, labeler_id)     -- one ranking per sailor per snapshot (re-submit updates)
);
CREATE TABLE IF NOT EXISTS gold (
    snapshot_id       TEXT PRIMARY KEY,
    best_candidate_id TEXT NOT NULL,
    note              TEXT
);
CREATE INDEX IF NOT EXISTS idx_rank_snap ON rankings(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_rank_labeler ON rankings(labeler_id);
"""


def _conn():
    os.makedirs(os.path.dirname(config.PREF_DB), exist_ok=True)
    c = sqlite3.connect(config.PREF_DB)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "anon"


def upsert_labeler(name: str) -> str:
    lid = slug(name)
    with _conn() as c:
        row = c.execute("SELECT id FROM labelers WHERE id=?", (lid,)).fetchone()
        if not row:
            c.execute("INSERT INTO labelers (id, name, created_at) VALUES (?,?,?)",
                      (lid, name.strip(), time.time()))
    return lid


def record_ranking(snapshot_id: str, labeler_id: str, order: list, calib: dict,
                   notes: str = "", elapsed_ms: int | None = None) -> None:
    import json
    with _conn() as c:
        c.execute(
            """INSERT INTO rankings (snapshot_id, labeler_id, order_json, calib_json, notes,
                                     elapsed_ms, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(snapshot_id, labeler_id) DO UPDATE SET
                 order_json=excluded.order_json, calib_json=excluded.calib_json,
                 notes=excluded.notes, elapsed_ms=excluded.elapsed_ms, created_at=excluded.created_at""",
            (snapshot_id, labeler_id, json.dumps(order), json.dumps(calib), notes,
             elapsed_ms, time.time()))


def _row_to_ranking(r: sqlite3.Row) -> dict:
    import json
    return {"id": r["id"], "snapshot_id": r["snapshot_id"], "labeler_id": r["labeler_id"],
            "order": json.loads(r["order_json"]), "calibration": json.loads(r["calib_json"]),
            "notes": r["notes"], "elapsed_ms": r["elapsed_ms"], "created_at": r["created_at"]}


def all_rankings() -> list[dict]:
    with _conn() as c:
        return [_row_to_ranking(r) for r in
                c.execute("SELECT * FROM rankings ORDER BY created_at").fetchall()]


def rankings_for(snapshot_id: str) -> list[dict]:
    with _conn() as c:
        return [_row_to_ranking(r) for r in
                c.execute("SELECT * FROM rankings WHERE snapshot_id=? ORDER BY created_at",
                          (snapshot_id,)).fetchall()]


def snapshots_done_by(labeler_id: str) -> set[str]:
    with _conn() as c:
        return {r["snapshot_id"] for r in
                c.execute("SELECT snapshot_id FROM rankings WHERE labeler_id=?", (labeler_id,)).fetchall()}


def ranking_counts() -> dict[str, int]:
    """snapshot_id -> number of sailors who have ranked it (for coverage + overlap)."""
    with _conn() as c:
        return {r["snapshot_id"]: r["n"] for r in
                c.execute("SELECT snapshot_id, COUNT(*) n FROM rankings GROUP BY snapshot_id").fetchall()}


def labelers() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM labelers ORDER BY created_at").fetchall()]


# --- gold traps ------------------------------------------------------------------------------
def set_gold(snapshot_id: str, best_candidate_id: str, note: str = "") -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO gold (snapshot_id, best_candidate_id, note) VALUES (?,?,?)",
                  (snapshot_id, best_candidate_id, note))


def gold_map() -> dict[str, str]:
    with _conn() as c:
        return {r["snapshot_id"]: r["best_candidate_id"] for r in
                c.execute("SELECT snapshot_id, best_candidate_id FROM gold").fetchall()}


def stats() -> dict:
    counts = ranking_counts()
    n_snaps_covered = len(counts)
    n_overlap = sum(1 for v in counts.values() if v >= 2)
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM rankings").fetchone()["n"]
        n_labelers = c.execute("SELECT COUNT(*) n FROM labelers").fetchone()["n"]
    return {"total_rankings": total, "labelers": n_labelers,
            "snapshots_covered": n_snaps_covered, "snapshots_double_labeled": n_overlap}
