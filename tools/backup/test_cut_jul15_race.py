"""cut_jul15_race.sh must write a sync_state table the archiver's backfill can use (#39).

The cut is meant to stand in for the archive over the race window, so `backfill.py` must be able
to run on it. backfill reads and writes `sync_state (k, v)`, the archiver's own columns; the cut
used to create `(key, value)`, so the first backfill query failed. This runs the real script on a
tiny source archive and checks the columns against the archiver's schema and backfill's queries.

Run: python3 tools/backup/test_cut_jul15_race.py
"""
import os
import re
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


with tempfile.TemporaryDirectory() as tmp:
    src, out = os.path.join(tmp, "src.db"), os.path.join(tmp, "out", "cut.db")
    s = sqlite3.connect(src)
    s.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, "
              "boat_id TEXT NOT NULL, source TEXT NOT NULL, path TEXT NOT NULL, value REAL, "
              "str_value TEXT)")
    s.executemany("INSERT INTO readings (time, boat_id, source, path, value) VALUES (?,?,?,?,?)", [
        ("2026-07-15T22:00:00", "sr33", "gps", "p", 1.0),     # before the window
        ("2026-07-15T23:00:00", "sr33", "gps", "p", 2.0),     # in it
        ("2026-07-15T23:30:00", "sr33", "gps", "p", 3.0),     # in it
        ("2026-07-16T01:00:00", "sr33", "gps", "p", 4.0),     # after it
    ])
    s.commit()
    s.close()

    r = subprocess.run(["bash", os.path.join(HERE, "cut_jul15_race.sh")],
                       env={**os.environ, "SRC": src, "OUT": out},
                       capture_output=True, text=True)
    check(f"script runs (rc {r.returncode})", r.returncode == 0)
    if r.returncode:
        print(r.stdout[-800:], r.stderr[-800:])
        sys.exit(1)

    c = sqlite3.connect(out)
    check("cut keeps only the race window (2 rows)",
          c.execute("SELECT count(*) FROM readings").fetchone()[0] == 2)

    # The archiver's own sync_state columns, read from its schema so the two can't drift apart.
    src_text = open(os.path.join(REPO, "pi", "archiver", "archiver.py")).read()
    m = re.search(r"CREATE TABLE IF NOT EXISTS sync_state \((.*?)\);", src_text, re.S)
    ref = sqlite3.connect(":memory:")
    ref.execute(f"CREATE TABLE sync_state ({m.group(1)})")
    check("sync_state columns match the archiver's",
          columns(c, "sync_state") == columns(ref, "sync_state"))

    # backfill.py's cursor queries, as it runs them.
    try:
        c.execute("INSERT INTO sync_state (k, v) VALUES (?, ?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", ("backfill_last_id", "2"))
        got = c.execute("SELECT v FROM sync_state WHERE k=?", ("backfill_last_id",)).fetchone()
        check("backfill's cursor write + read work on the cut", got == ("2",))
    except sqlite3.OperationalError as e:
        check(f"backfill's cursor write + read work on the cut ({e})", False)

sys.exit(0 if ok else 1)
