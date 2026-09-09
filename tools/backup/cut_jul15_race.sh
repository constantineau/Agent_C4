#!/bin/bash
# Keep the Jul 15 2026 race as prototype data for the debrief, then let the 8.5 G salvage go.
#
# `recovered/archive-jul1517-recovered.db` is 46.4 M rows of Jul 15 20:35 -> Jul 17 21:13 — the
# delivery to Port Huron, which Cole ruled out of Postgres, WITH the Jul 15 race buried in it
# (session 1: 22:44:17 -> Jul 16 00:02:27Z, derived 22:39 -> 00:07). This cuts the race out at
# full resolution so the big file can be deleted while the race stays here and reaches the
# debrief, which reads Postgres and not this disk.
set -euo pipefail

SRC=/home/constantineau/backups/c4-boat-pull-2026-08-30/recovered/archive-jul1517-recovered.db
OUT=/home/constantineau/backups/race-data/archive-jul15-race.db
FROM='2026-07-15T22:30:00'
TO='2026-07-16T00:20:00'      # the derived window (22:39 -> 00:07) with room either side

mkdir -p "$(dirname "$OUT")"
rm -f "$OUT" "$OUT-wal" "$OUT-shm"

python3 - "$SRC" "$OUT" "$FROM" "$TO" <<'PY'
import sqlite3, sys
src, out, t0, t1 = sys.argv[1:5]
c = sqlite3.connect(out)
c.executescript("""
CREATE TABLE IF NOT EXISTS readings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    time      TEXT NOT NULL, boat_id TEXT NOT NULL, source TEXT NOT NULL,
    path      TEXT NOT NULL, value REAL, str_value TEXT);
CREATE INDEX IF NOT EXISTS readings_time_idx      ON readings(time);
CREATE INDEX IF NOT EXISTS readings_path_time_idx ON readings(path, time);
CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT);
""")
c.execute("ATTACH DATABASE ? AS s", (f"file:{src}?immutable=1",))
c.execute("INSERT INTO readings (time, boat_id, source, path, value, str_value) "
          "SELECT time, boat_id, source, path, value, str_value FROM s.readings "
          "WHERE time >= ? AND time <= ? ORDER BY time", (t0, t1))
c.commit()
n, a, b = c.execute("SELECT count(*), min(time), max(time) FROM readings").fetchone()
print(f"cut {n:,} rows  {a} -> {b}")
# The cut must be able to stand in for the source over this window, so compare against it.
m = c.execute("SELECT count(*) FROM s.readings WHERE time >= ? AND time <= ?", (t0, t1)).fetchone()[0]
assert n == m, f"row count mismatch: cut {n} vs source {m}"
print(f"row count matches the source over the window ({m:,})")
c.execute("DETACH DATABASE s")
c.close()
PY

ls -lh "$OUT"
echo "DONE"
