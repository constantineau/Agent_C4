"""Fleet-retro persistent archive — EVERYTHING a retro study gathers is kept for future use.

The user requirement (2026-07-06, docs/RETRO_STUDY.md §3): race data pulled for the fleet retro
studies — YB tracks/entries/results, ORC certs + converted polars, per-boat optimizer runs and
scores, and the GRIB files behind both the retro wind fields and the weather-model backtests — is
saved durably, not left in transient caches. Pure-stdlib `sqlite3` on the `lab_retro` volume
(`/srv/retro`), mirroring the `learning.py` / `modelskill` house pattern.

GRIB pinning: `pin_grib(path, ...)` COPIES a GRIB file into `/srv/retro/grib/` and indexes it by
sha256 (model/cycle/fhr/member/bbox/source-url/context). The NOMADS `lab_gribcache` volume stays a
cache; this store is the archive — evicting the cache can never lose a study's inputs. Pinning is
idempotent (same content → one archived copy, extra contexts recorded).
"""
import hashlib
import json
import os
import shutil
import sqlite3
import time

from . import atomicio

RETRO_DB = os.environ.get("RETRO_DB", "/srv/retro/retro.db")
RETRO_GRIB_DIR = os.environ.get("RETRO_GRIB_DIR", "/srv/retro/grib")


def _conn():
    os.makedirs(os.path.dirname(RETRO_DB), exist_ok=True)
    c = sqlite3.connect(RETRO_DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS races (
        race_id TEXT PRIMARY KEY,           -- e.g. 'bayviewmack2025' (the YB id)
        source TEXT,                        -- 'yb' | 'file' (an uploaded GPX/CSV)
        start_epoch REAL,
        setup_json TEXT,                    -- the full RaceSetup (course, bounds, teams meta)
        ingested_at REAL);
    CREATE TABLE IF NOT EXISTS entries (
        race_id TEXT, team_id INTEGER,      -- team_id = the YB team 'id'
        boat TEXT, sail TEXT, owner TEXT, model TEXT, division TEXT,
        tcf REAL, start_epoch REAL, finished_at REAL, status TEXT,
        PRIMARY KEY (race_id, team_id));
    CREATE TABLE IF NOT EXISTS tracks (
        race_id TEXT, team_id INTEGER,
        n_fixes INTEGER, t0 REAL, t1 REAL,
        fixes_json TEXT,                    -- [{lat,lon,t,sog?,cog?}] time-ascending
        PRIMARY KEY (race_id, team_id));
    CREATE TABLE IF NOT EXISTS results (
        race_id TEXT, team_id INTEGER,
        division TEXT,                      -- a boat ranks in several divisions (overall + class)
        elapsed_s REAL, corrected_s REAL, tcf REAL,
        rank_division INTEGER, finished INTEGER, status TEXT,
        PRIMARY KEY (race_id, team_id, division));
    CREATE TABLE IF NOT EXISTS certs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        country TEXT, refno TEXT, yacht TEXT, sail TEXT,
        cert_json TEXT, fetched_at REAL);
    CREATE UNIQUE INDEX IF NOT EXISTS certs_ref ON certs (country, refno);
    CREATE TABLE IF NOT EXISTS polars (
        race_id TEXT, team_id INTEGER,
        cert_id INTEGER,                    -- -> certs.id
        match_by TEXT, match_confidence REAL,
        polar_json TEXT,                    -- the converted optimizer-shaped grid
        PRIMARY KEY (race_id, team_id));
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id TEXT, team_id INTEGER, created_at REAL,
        kind TEXT,                          -- 'gun_forecast' | 'oracle' | ...
        config_json TEXT,                   -- start_epoch, course, models/cycles, resolution…
        windfield_json TEXT,                -- provenance (models, cycles, frames)
        result_json TEXT);                  -- the optimize result (route/legs/eta), heavy
    CREATE TABLE IF NOT EXISTS scores (
        race_id TEXT, team_id INTEGER, run_id INTEGER,
        created_at REAL, metrics_json TEXT,
        PRIMARY KEY (race_id, team_id, run_id));
    CREATE TABLE IF NOT EXISTS grib_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sha256 TEXT UNIQUE, bytes INTEGER, path TEXT,
        model TEXT, cycle TEXT, fhr INTEGER, member TEXT, bbox TEXT,
        source_url TEXT, pinned_at REAL);
    CREATE TABLE IF NOT EXISTS grib_contexts (
        sha256 TEXT, context TEXT, added_at REAL,
        PRIMARY KEY (sha256, context));
    """)
    return c


# ---------------------------------------------------------------- race ingest writes

def upsert_race(race_id, setup, start_epoch, source="yb"):
    with _conn() as c:
        c.execute("INSERT INTO races (race_id, source, start_epoch, setup_json, ingested_at) "
                  "VALUES (?,?,?,?,?) ON CONFLICT(race_id) DO UPDATE SET "
                  "source=excluded.source, start_epoch=excluded.start_epoch, "
                  "setup_json=excluded.setup_json, ingested_at=excluded.ingested_at",
                  (race_id, source, start_epoch, json.dumps(setup), time.time()))


def upsert_entry(race_id, team):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO entries (race_id, team_id, boat, sail, owner, model, "
                  "division, tcf, start_epoch, finished_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (race_id, team.get("id"), team.get("name"), team.get("sail"),
                   team.get("owner"), team.get("model"), team.get("division"),
                   team.get("tcf"), team.get("start"), team.get("finishedAt"),
                   team.get("status")))


def save_track(race_id, team_id, fixes):
    ts = [f["t"] for f in fixes] or [None, None]
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO tracks (race_id, team_id, n_fixes, t0, t1, fixes_json) "
                  "VALUES (?,?,?,?,?,?)",
                  (race_id, team_id, len(fixes), min(ts) if fixes else None,
                   max(ts) if fixes else None, json.dumps(fixes)))


def save_result(race_id, team_id, division, elapsed_s, corrected_s, tcf, rank_division,
                finished, status=""):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO results (race_id, team_id, division, elapsed_s, "
                  "corrected_s, tcf, rank_division, finished, status) VALUES (?,?,?,?,?,?,?,?,?)",
                  (race_id, team_id, division, elapsed_s, corrected_s, tcf, rank_division,
                   1 if finished else 0, status))


def replace_fleet(race_id, setup, start_epoch, source, teams, tracks, results) -> dict:
    """A re-import that REPLACES the race (a fleet file uploaded again, #310): its entries, tracks
    and results become exactly the new ones, in one transaction, so a boat or a rank the new file
    drops is gone. Runs and scores for the race are deleted (they were built from the old tracks;
    "Score the fleet" rebuilds them). A boat's matched polar is kept only when its id, name and
    sail are all unchanged; a removed or re-identified boat loses it. Ids must already be stable
    (`retro.stable_ids`). Returns how much analysis was cleared."""
    c = _conn()
    try:
        with c:
            old = {r["team_id"]: (r["boat"], r["sail"]) for r in c.execute(
                "SELECT team_id, boat, sail FROM entries WHERE race_id=?", (race_id,))}
            new = {t.get("id"): (t.get("name"), t.get("sail")) for t in teams}
            drop_polars = [tid for tid, ident in old.items() if new.get(tid) != ident]
            runs = c.execute("SELECT COUNT(*) FROM runs WHERE race_id=?", (race_id,)).fetchone()[0]
            for tbl in ("entries", "tracks", "results", "runs", "scores"):
                c.execute(f"DELETE FROM {tbl} WHERE race_id=?", (race_id,))
            c.executemany("DELETE FROM polars WHERE race_id=? AND team_id=?",
                          [(race_id, tid) for tid in drop_polars])
            c.execute("INSERT INTO races (race_id, source, start_epoch, setup_json, ingested_at) "
                      "VALUES (?,?,?,?,?) ON CONFLICT(race_id) DO UPDATE SET "
                      "source=excluded.source, start_epoch=excluded.start_epoch, "
                      "setup_json=excluded.setup_json, ingested_at=excluded.ingested_at",
                      (race_id, source, start_epoch, json.dumps(setup), time.time()))
            c.executemany(
                "INSERT INTO entries (race_id, team_id, boat, sail, owner, model, division, tcf, "
                "start_epoch, finished_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(race_id, t.get("id"), t.get("name"), t.get("sail"), t.get("owner"),
                  t.get("model"), t.get("division"), t.get("tcf"), t.get("start"),
                  t.get("finishedAt"), t.get("status")) for t in teams])
            c.executemany(
                "INSERT INTO tracks (race_id, team_id, n_fixes, t0, t1, fixes_json) "
                "VALUES (?,?,?,?,?,?)",
                [(race_id, tid, len(fx), min(f["t"] for f in fx) if fx else None,
                  max(f["t"] for f in fx) if fx else None, json.dumps(fx))
                 for tid, fx in tracks.items()])
            c.executemany(
                "INSERT INTO results (race_id, team_id, division, elapsed_s, corrected_s, tcf, "
                "rank_division, finished, status) VALUES (?,?,?,?,?,?,?,?,?)",
                [(race_id, r["team_id"], r["division"], r.get("elapsed"), r.get("corrected"),
                  r.get("tcf"), r.get("rank"), 1 if r.get("finished") else 0,
                  r.get("status") or "") for r in results])
    finally:
        c.close()
    return {"runs": runs, "polars": len(drop_polars)}


def save_cert(country, refno, yacht, sail, cert):
    with _conn() as c:
        c.execute("INSERT INTO certs (country, refno, yacht, sail, cert_json, fetched_at) "
                  "VALUES (?,?,?,?,?,?) ON CONFLICT(country, refno) DO UPDATE SET "
                  "yacht=excluded.yacht, sail=excluded.sail, cert_json=excluded.cert_json, "
                  "fetched_at=excluded.fetched_at",
                  (country, str(refno), yacht, sail, json.dumps(cert), time.time()))
        return c.execute("SELECT id FROM certs WHERE country=? AND refno=?",
                         (country, str(refno))).fetchone()[0]


def save_polar(race_id, team_id, cert_id, polar, match_by, match_confidence):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO polars (race_id, team_id, cert_id, match_by, "
                  "match_confidence, polar_json) VALUES (?,?,?,?,?,?)",
                  (race_id, team_id, cert_id, match_by, match_confidence, json.dumps(polar)))


_NO_STAMP = object()


def _guarded(stamp, race_id, sql, args):
    """Run one write, but only while the race's import is still `stamp` (its `ingested_at`), in
    the same locked transaction as the check (#345). Without a stamp it just writes. Returns the
    cursor, or None when the race was re-imported (or removed) since the stamp was read."""
    c = _conn()
    try:
        c.isolation_level = None
        c.execute("BEGIN IMMEDIATE")
        try:
            if stamp is not _NO_STAMP:
                row = c.execute("SELECT ingested_at FROM races WHERE race_id=?", (race_id,)).fetchone()
                if row is None or row[0] != stamp:
                    c.execute("ROLLBACK")
                    return None
            cur = c.execute(sql, args)
            c.execute("COMMIT")
            return cur
        except Exception:
            c.execute("ROLLBACK")
            raise
    finally:
        c.close()


def save_run(race_id, team_id, kind, config, windfield, result, stamp=_NO_STAMP):
    """The new run's id; None (nothing written) when `stamp` is given and the race was imported
    again since."""
    cur = _guarded(stamp, race_id,
                   "INSERT INTO runs (race_id, team_id, created_at, kind, config_json, "
                   "windfield_json, result_json) VALUES (?,?,?,?,?,?,?)",
                   (race_id, team_id, time.time(), kind, json.dumps(config),
                    json.dumps(windfield), json.dumps(result)))
    return cur.lastrowid if cur is not None else None


def save_score(race_id, team_id, run_id, metrics, stamp=_NO_STAMP):
    """True when written; False when `stamp` is given and the race was imported again since."""
    return _guarded(stamp, race_id,
                    "INSERT OR REPLACE INTO scores (race_id, team_id, run_id, created_at, "
                    "metrics_json) VALUES (?,?,?,?,?)",
                    (race_id, team_id, run_id, time.time(), json.dumps(metrics))) is not None


# ---------------------------------------------------------------- GRIB pinning

def pin_grib(path, model=None, cycle=None, fhr=None, member=None, bbox=None,
             source_url=None, context=None):
    """Copy a GRIB file into the durable archive + index it. Idempotent by content hash — a file
    already pinned just gains the new `context`. Returns the archived path (or None on a miss)."""
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    dest_dir = os.path.join(RETRO_GRIB_DIR, model or "misc")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{sha[:16]}_{os.path.basename(path)}")
    with _conn() as c:
        row = c.execute("SELECT path FROM grib_files WHERE sha256=?", (sha,)).fetchone()
        if row is None:
            if not os.path.exists(dest):
                atomicio.copy_file(path, dest)
            c.execute("INSERT OR IGNORE INTO grib_files (sha256, bytes, path, model, cycle, fhr, "
                      "member, bbox, source_url, pinned_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (sha, os.path.getsize(path), dest, model,
                       str(cycle) if cycle is not None else None, fhr, member,
                       json.dumps(bbox) if bbox is not None else None, source_url, time.time()))
        else:
            dest = row["path"]
        if context:
            c.execute("INSERT OR IGNORE INTO grib_contexts (sha256, context, added_at) "
                      "VALUES (?,?,?)", (sha, context, time.time()))
    return dest


# ---------------------------------------------------------------- reads

def _rows(q, args=()):
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def get_race(race_id):
    r = _rows("SELECT * FROM races WHERE race_id=?", (race_id,))
    if not r:
        return None
    out = r[0]
    out["setup"] = json.loads(out.pop("setup_json") or "{}")
    return out


def list_races():
    return _rows("SELECT race_id, source, start_epoch, ingested_at, "
                 "(SELECT COUNT(*) FROM entries e WHERE e.race_id=races.race_id) AS entries, "
                 "(SELECT COUNT(*) FROM tracks t WHERE t.race_id=races.race_id) AS tracks, "
                 "(SELECT COUNT(*) FROM polars p WHERE p.race_id=races.race_id) AS polars, "
                 "(SELECT COUNT(*) FROM runs r WHERE r.race_id=races.race_id) AS runs "
                 "FROM races ORDER BY start_epoch")


def get_entries(race_id):
    return _rows("SELECT * FROM entries WHERE race_id=? ORDER BY team_id", (race_id,))


def get_track(race_id, team_id):
    r = _rows("SELECT fixes_json FROM tracks WHERE race_id=? AND team_id=?", (race_id, team_id))
    return json.loads(r[0]["fixes_json"]) if r else None


def teams_with_tracks(race_id):
    return {r["team_id"] for r in _rows("SELECT team_id FROM tracks WHERE race_id=?", (race_id,))}


def get_results(race_id):
    return _rows("SELECT * FROM results WHERE race_id=? ORDER BY division, rank_division",
                 (race_id,))


def get_polar(race_id, team_id):
    r = _rows("SELECT polar_json FROM polars WHERE race_id=? AND team_id=?", (race_id, team_id))
    return json.loads(r[0]["polar_json"]) if r else None


def get_polars(race_id):
    return _rows("SELECT team_id, cert_id, match_by, match_confidence FROM polars "
                 "WHERE race_id=?", (race_id,))


def get_scores(race_id):
    return _rows("SELECT * FROM scores WHERE race_id=?", (race_id,))


def grib_stats():
    with _conn() as c:
        n, b = c.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM grib_files").fetchone()
        return {"files": n, "bytes": b}
