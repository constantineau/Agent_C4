#!/usr/bin/env python3
"""Salvage readable rows out of a corrupt SQLite archive.

Walks `readings` by rowid in chunks; a chunk that raises 'database disk image is malformed'
is bisected down to single rows, so only the pages that are actually damaged are lost instead
of the whole table. The source is opened read-only + immutable and is never modified.

Generalised from the one-off `backups/c4-boat-pull-2026-08-30/salvage.py` (which had SRC/DST
and the rowid range hardcoded to the 2.1 GB Jul-18 archive, and would have merged a second
archive into that one's output). Three things it adds, all learned the hard way:

  --resume     picks up at max(id) already in the destination. `id` is the PRIMARY KEY and
               inserts are INSERT OR IGNORE, so re-running a range is idempotent; a 46 M-row
               salvage that dies at 80% should not start over.
  --min-free-g aborts before writing when the filesystem is nearly full. Local `/` is 96 G and
               these files are ~9 GB each; the drain guardian earned this check the hard way.
  --lost       lost rowid ranges are written incrementally, not only at the end, so a killed
               run still tells you what it could not read.

Run (unit, so it outlives the session — shell-backgrounded jobs get reaped with the shell):
  systemd-run --unit=c4-salvage --collect \
    /home/constantineau/Agent_C4/pi/archiver/tools/salvage.py \
      --src .../archive.corrupt-20260718.db --dst .../recovered/archive-jul1517.db
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

COLS = "id, time, boat_id, source, path, value, str_value"
CREATE = """CREATE TABLE IF NOT EXISTS readings(
    id INTEGER PRIMARY KEY, time TEXT, boat_id TEXT,
    source TEXT, path TEXT, value REAL, str_value TEXT)"""


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", required=True, help="corrupt archive to read (never modified)")
    p.add_argument("--dst", required=True, help="destination DB (created / appended to)")
    p.add_argument("--lost", help="lost-rowid ranges, TSV (default: <dst>.lost-rowids.txt)")
    p.add_argument("--lo", type=int, default=1, help="first rowid (default 1)")
    p.add_argument("--hi", type=int, default=0,
                   help="last rowid (default: max(id) from the source, else the file is probed)")
    p.add_argument("--chunk", type=int, default=10000, help="rowids per read (default 10000)")
    p.add_argument("--resume", action="store_true",
                   help="start at max(id) in --dst instead of --lo")
    p.add_argument("--min-free-g", type=float, default=6.0,
                   help="abort when the destination filesystem drops below this (default 6 G)")
    p.add_argument("--index", action="store_true",
                   help="build (time) and (path, time) indexes at the end")
    return p.parse_args(argv)


def free_g(path):
    return shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free / 1e9


def probe_hi(src):
    """Highest rowid, cheaply. max(id) walks the PK index and usually survives corruption;
    fall back to a descending scan, which stops at the first readable row."""
    for q in ("SELECT max(id) FROM readings",
              "SELECT id FROM readings ORDER BY id DESC LIMIT 1"):
        try:
            row = src.execute(q).fetchone()
            if row and row[0]:
                return int(row[0])
        except sqlite3.DatabaseError:
            continue
    raise SystemExit("could not determine the rowid range — pass --hi explicitly")


def main(argv=None):
    a = parse_args(argv)
    lost_path = a.lost or a.dst + ".lost-rowids.txt"

    if free_g(a.dst) < a.min_free_g:
        raise SystemExit(f"only {free_g(a.dst):.1f} G free at {a.dst} "
                         f"(--min-free-g {a.min_free_g}) — refusing to start")

    src = sqlite3.connect(f"file:{a.src}?mode=ro&immutable=1", uri=True)
    dst = sqlite3.connect(a.dst)
    dst.execute("PRAGMA journal_mode=OFF")     # the destination is disposable; speed over safety
    dst.execute("PRAGMA synchronous=OFF")
    dst.execute(CREATE)

    hi = a.hi or probe_hi(src)
    lo = a.lo
    if a.resume:
        row = dst.execute("SELECT max(id) FROM readings").fetchone()
        if row and row[0]:
            lo = max(lo, int(row[0]) + 1)
            print(f"resuming at rowid {lo:,}", flush=True)
    if lo > hi:
        print(f"nothing to do: lo {lo:,} > hi {hi:,}", flush=True)
        return 0
    print(f"salvaging {a.src}\n       -> {a.dst}\n"
          f"rowids {lo:,}..{hi:,}  chunk={a.chunk:,}  free={free_g(a.dst):.1f} G", flush=True)

    state = {"saved": 0, "lost": 0}
    lost_f = open(lost_path, "a")
    t0 = time.time()

    def fetch(x, y):
        """Rows in [x,y], or None when the range is unreadable."""
        try:
            return src.execute(
                f"SELECT {COLS} FROM readings WHERE id BETWEEN ? AND ?", (x, y)).fetchall()
        except sqlite3.DatabaseError:
            return None

    def salvage(x, y):
        """Recursively salvage [x,y], narrowing around corrupt pages."""
        rows = fetch(x, y)
        if rows is not None:
            if rows:
                dst.executemany("INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?,?)", rows)
                state["saved"] += len(rows)
            return
        if x == y:                      # a single unreadable row
            lost_f.write(f"{x}\t{y}\n")
            lost_f.flush()
            state["lost"] += 1
            return
        mid = (x + y) // 2
        salvage(x, mid)
        salvage(mid + 1, y)

    for start in range(lo, hi + 1, a.chunk):
        end = min(start + a.chunk - 1, hi)
        salvage(start, end)
        if (start // a.chunk) % 20 == 0:
            dst.commit()
            if free_g(a.dst) < a.min_free_g:
                print(f"STOPPING at rowid {end:,}: only {free_g(a.dst):.1f} G free. "
                      f"Free space and re-run with --resume.", flush=True)
                break
            pct = (end - lo) * 100 // max(hi - lo, 1)
            print(f"{pct:3d}%  rowid {end:>10,}  saved={state['saved']:>10,}  "
                  f"lost={state['lost']:>6,}  free={free_g(a.dst):.1f}G  "
                  f"{time.time() - t0:.0f}s", flush=True)

    dst.commit()
    if a.index:
        print("building indexes…", flush=True)
        dst.execute("CREATE INDEX IF NOT EXISTS r_time ON readings(time)")
        dst.execute("CREATE INDEX IF NOT EXISTS r_path_time ON readings(path, time)")
        dst.commit()
    lost_f.close()

    total = state["saved"] + state["lost"]
    print("=" * 64)
    print(f"salvaged : {state['saved']:,} rows")
    print(f"lost     : {state['lost']:,} rows  ({lost_path})")
    print(f"recovery : {state['saved'] * 100 / total:.4f}%" if total else "recovery : n/a")
    span = dst.execute("SELECT min(time), max(time), count(*) FROM readings").fetchone()
    print(f"time span: {span[0]}  ->  {span[1]}")
    print(f"dst rows : {span[2]:,}   file {os.path.getsize(a.dst) / 1e9:.2f} GB")
    print(f"elapsed  : {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.setrecursionlimit(10000)   # the bisect recurses ~log2(chunk) deep, but be generous
    raise SystemExit(main())
