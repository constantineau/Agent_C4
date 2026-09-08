"""Materialise cloud spool rows into a `readings`-schema SQLite file for the replay rig.

WHY THIS EXISTS. The Jul 18 race is recorded at two resolutions and the rig could only read one
of them. The Pi's full-res archive covers `17:03:31 -> 20:40:30Z` (~18,000 rows/h, 5-28 Hz) and
stops there, because the archiver hit SQLite corruption at the bottom of the flat-bank curve. The
rest of the race — including the decision to retire, and the kicked GPS at 22:58Z — survived only
as 15-s uplink aggregates that went to the cloud and now sit in `telemetry_raw`. So
`tools/replay/` replayed the first 3 h 37 m of a ~7 h race and went dark for the part where
Time-to-Mark and playbook relevance mattered most.

The gap is not resolution, it is COVERAGE, and it matters most for the checks written on
2026-09-07 for the kick itself: `sensor_health.attitude_plausible` (roll stepped to 133 deg at
22:58Z) and `sensor_health.heading_bias` (the compass read a quarter turn out for seven hours
after it was re-seated at 23:21Z). Both events are **after** the archive ends, so both checks
have only ever been exercised against a hand-typed fixture — never against the real recording,
through the real engine, on the real console. This file is what closes that.

WHAT IT PRODUCES. A standalone SQLite file with the archiver's exact `readings` schema and
indexes, so `ReplaySource` can ATTACH it and read a UNION of the two without the engine knowing.
Timestamps are written in the archiver's exact format (`2026-07-18T20:40:30.027Z` - millisecond
precision, trailing Z, fixed width) because every archive read compares `time` LEXICOGRAPHICALLY:
a differently-formatted string would sort into the wrong place and silently change what a frame
sees. A `spool_meta` table records the window, the row count and the source database, so nobody
has to guess later what this file is or how to regenerate it.

WHAT IT IS NOT. It is not a substitute for the archive and must not be merged into one: the two
have different sample rates and mixing them without saying so would let a 24-s aggregate be read
as a 5-Hz measurement. Keep them as separate files, attached at read time and labelled.

Usage (the rig's ephemeral venv, plus `psycopg[binary]`):

    python tools/replay/spool_to_sqlite.py \
        --dsn "postgresql://sr33:sr33-dev@localhost:5433/sr33_dev" \
        --since 2026-07-18T20:40:30Z --until 2026-07-19T02:10:00Z \
        --out /home/constantineau/backups/replay-jul18/spool-jul18-to0210.db

Then build a timeline across the whole race:

    python tools/replay/harness.py \
        --spool /home/constantineau/backups/replay-jul18/spool-jul18-to0210.db \
        --start 2026-07-18T17:03:31Z --end 2026-07-19T02:09:00Z --step 30 --out <dir>

⚠️ **CHOOSE `--until` BY THE FAULT, NOT BY THE FINISH.** The first version of this file stopped
at `00:09:36Z` — the end of racing, and a defensible line. The compass misalignment it exists to
replay begins at **23:56:10Z**, so that window held thirteen minutes of a seven-hour fault, all
of it inside the transition where a sliding-window check correctly reports `unknown`. The result
was a P0 raised against working code (see `docs/V2_BACKLOG.md`). Postgres holds `telemetry_raw`
well past the finish; when a window is cut, cut it where the evidence ends.
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

BOAT_ID = os.environ.get("BOAT_ID", "sr33")

# Mirrors pi/archiver/archiver.py. Kept verbatim rather than imported: the archiver's DDL lives
# inside a larger schema string with backfill bookkeeping this file has no business creating,
# and a drifting copy would be caught immediately by ReplaySource failing to read it.
SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    time      TEXT NOT NULL,
    boat_id   TEXT NOT NULL,
    source    TEXT NOT NULL,
    path      TEXT NOT NULL,
    value     REAL,
    str_value TEXT
);
CREATE TABLE IF NOT EXISTS spool_meta (
    key TEXT PRIMARY KEY,
    val TEXT
);
"""
# Created AFTER the insert, which is much faster than maintaining them per row.
INDEXES = """
CREATE INDEX IF NOT EXISTS readings_time_idx      ON readings(time);
CREATE INDEX IF NOT EXISTS readings_path_time_idx ON readings(path, time);
"""


def _iso_ms(dt):
    """The archiver's on-disk format: millisecond precision, trailing Z, fixed 24 chars.

    Fixed width is the load-bearing part. `datasource_onboard` and `tools/replay/source.py`
    bound their window reads with string comparison (`time > ?` / `time <= ?`), which is only
    correct while every row is the same shape."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def db_count_empty(path):
    """Rows carrying neither a number nor a string — i.e. rows the export dropped the content
    of. The first version of this tool produced 4,736 of them by not selecting `str_value`, and
    nothing would have noticed: the rig would simply have shown those paths as absent."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return db.execute("SELECT count(*) FROM readings "
                          "WHERE value IS NULL AND str_value IS NULL").fetchone()[0]
    finally:
        db.close()


def export(dsn, since, until, out, boat_id=BOAT_ID, batch=20000):
    import psycopg

    tmp = out + ".tmp"
    for p in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)

    n, first, last, paths, sources, nonnum = 0, None, None, set(), set(), 0
    with psycopg.connect(dsn) as pg:
        # Server-side cursor: this window is ~33k rows, but the same tool pointed at a
        # multi-hour delivery would be millions and must not be materialised in the client.
        with pg.cursor(name="spool_export") as cur:
            cur.itersize = batch
            # `str_value` is not optional. `telemetry_raw` carries it alongside `value` for the
            # paths that are not numbers — GNSS fix quality, `navigation.datetime`, the course
            # calculator's method, the Fusion's on/off — and the first version of this exporter
            # selected only `value`, which turned 4,736 of 33,282 rows into rows with no content
            # at all. "Collect everything" has to survive the export too.
            cur.execute(
                "SELECT time, source, path, value, str_value FROM telemetry_raw "
                "WHERE boat_id = %s AND time >= %s AND time < %s ORDER BY time",
                (boat_id, since, until),
            )
            buf = []
            for (t, source, path, value, str_value) in cur:
                iso = _iso_ms(t)
                if first is None:
                    first = iso
                last = iso
                paths.add(path)
                sources.add(source)
                num = None if value is None else float(value)
                txt = None if str_value is None else str(str_value)
                if num is None and txt is not None:
                    nonnum += 1
                buf.append((iso, boat_id, source, path, num, txt))
                if len(buf) >= batch:
                    db.executemany(
                        "INSERT INTO readings (time, boat_id, source, path, value, str_value) "
                        "VALUES (?,?,?,?,?,?)", buf)
                    n += len(buf)
                    buf.clear()
                    print(f"[spool] {n:,} rows…", flush=True)
            if buf:
                db.executemany(
                    "INSERT INTO readings (time, boat_id, source, path, value, str_value) "
                    "VALUES (?,?,?,?,?,?)", buf)
                n += len(buf)

    print(f"[spool] indexing {n:,} rows…", flush=True)
    db.executescript(INDEXES)
    meta = {
        "generated_by": "tools/replay/spool_to_sqlite.py",
        "source_db": dsn.rsplit("/", 1)[-1],
        "source_table": "telemetry_raw",
        "boat_id": boat_id,
        "window_since": since.isoformat(),
        "window_until": until.isoformat(),
        "rows": str(n),
        "first_time": first or "",
        "last_time": last or "",
        "paths": str(len(paths)),
        "sources": ",".join(sorted(sources)),
        "note": "Uplink aggregates (~15-30 s per path), NOT full-res. Do not merge into the "
                "Pi archive; ATTACH it alongside so the two rates stay distinguishable.",
    }
    db.executemany("INSERT OR REPLACE INTO spool_meta (key, val) VALUES (?,?)",
                   sorted(meta.items()))
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp, out)

    size = os.path.getsize(out) / 1e6
    print(f"[spool] {n:,} rows, {len(paths)} paths, {len(sources)} sources -> {out} "
          f"({size:.1f} MB)")
    print(f"[spool] span {first} -> {last}")
    if nonnum:
        print(f"[spool] {nonnum:,} non-numeric values kept in str_value")
    empty = db_count_empty(out)
    if empty:
        print(f"[spool] WARNING: {empty:,} rows have neither value nor str_value — "
              f"something was dropped in the export, not in the boat's data")
    if not n:
        print("[spool] WARNING: no rows in that window — check --since/--until and the boat_id")
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dsn", default=os.environ.get(
        "DATABASE_URL", "postgresql://sr33:sr33-dev@localhost:5433/sr33_dev"))
    ap.add_argument("--since", required=True, help="inclusive, ISO8601 (e.g. 2026-07-18T20:40:30Z)")
    ap.add_argument("--until", required=True, help="exclusive, ISO8601")
    ap.add_argument("--out", required=True)
    ap.add_argument("--boat-id", default=BOAT_ID)
    a = ap.parse_args()
    n = export(a.dsn, _parse(a.since), _parse(a.until), a.out, boat_id=a.boat_id)
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
