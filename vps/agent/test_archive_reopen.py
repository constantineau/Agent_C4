"""Onboard engine: follow the archive when the archiver rotates it (#32).

The archiver moves a corrupt archive.db aside and starts a fresh file at the same path. The
engine keeps one read-only connection per thread; before this fix a connection opened before the
rotation kept reading the OLD inode forever — history stale or failing while recording worked.

Run:  python3 vps/agent/test_archive_reopen.py
"""
import os
import sqlite3
import sys
import tempfile
import threading

_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, os.path.dirname(os.path.dirname(_here))):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("ONBOARD_LIVE_WS", "false")
from app import datasource_onboard as ds   # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def make_archive(path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, time TEXT, path TEXT)")
    c.executemany("INSERT INTO readings (time, path) VALUES (?, ?)",
                  [("2026-10-27T12:00:00", "p")] * rows)
    c.commit()
    c.close()


def source():
    src = object.__new__(ds.OnboardSource)
    src._archive_local = threading.local()
    return src


def count(src):
    return src._archive.execute("SELECT count(*) FROM readings").fetchone()[0]


print("the engine's archive connection follows a rotation:")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "archive.db")
    make_archive(db, 1)
    ds.ARCHIVE_DB = db
    ds.ARCHIVE_RECHECK_S = 0.0

    src = source()
    check("reads the original archive", count(src) == 1)
    ds._AIS_SRC_CACHE[db] = {"ais-source"}

    os.rename(db, os.path.join(d, "archive.corrupt-20261027T120000.db"))
    check("mid-rotation (no file at the path): keeps the old connection, no crash",
          count(src) == 1)
    make_archive(db, 3)
    check("after the rotation it reads the FRESH archive", count(src) == 3)
    check("the AIS-source memo from the old file is dropped", db not in ds._AIS_SRC_CACHE)

    # The control: without the recheck, the cached connection would still be on the old inode.
    ds.ARCHIVE_RECHECK_S = 1e9
    stale = source()
    check("(control) a fresh source reads the current file", count(stale) == 3)
    os.rename(db, os.path.join(d, "archive.corrupt-20261027T130000.db"))
    make_archive(db, 7)
    check("(control) with no recheck it stays on the old inode — the bug this fixes",
          count(stale) == 3)
    ds.ARCHIVE_RECHECK_S = 0.0
    check("and with the recheck it moves to the new file", count(stale) == 7)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
