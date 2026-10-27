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

print("with a live WAL writer, the engine's reopen leaves the fresh archive's sidecars alone:")
# The engine's connection is mode=ro (and aboard the volume is mounted read-only into it), so its
# close cannot checkpoint or delete `-wal`/`-shm`. Checked here with real WAL files: the engine
# closes its old connection AFTER the archiver has moved the file and started the fresh one.
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "archive.db")

    def wal_writer(path):
        c = sqlite3.connect(path)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS readings (id INTEGER PRIMARY KEY, time TEXT, "
                  "path TEXT)")
        c.commit()
        return c

    w_old = wal_writer(db)
    w_old.executemany("INSERT INTO readings (time, path) VALUES (?, ?)", [("t", "p")] * 40)
    w_old.commit()
    ds.ARCHIVE_DB = db
    ds.ARCHIVE_RECHECK_S = 0.0
    src = source()
    check("engine reads the old WAL archive", count(src) == 40)
    # the archiver's rotation: close, move db + sidecars, open fresh, write
    w_old.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.rename(db + suffix, os.path.join(d, "archive.corrupt-x.db" + suffix))
    w_new = wal_writer(db)
    w_new.executemany("INSERT INTO readings (time, path) VALUES (?, ?)", [("t", "p")] * 9)
    w_new.commit()
    check("(setup) the fresh archive has its -wal", os.path.exists(db + "-wal"))
    check("engine reopens onto the fresh archive (closing its old connection)", count(src) == 9)
    check("the fresh -wal is still there after the engine closed its old connection",
          os.path.exists(db + "-wal") and os.path.exists(db + "-shm"))
    w_new.executemany("INSERT INTO readings (time, path) VALUES (?, ?)", [("t", "p")] * 3)
    w_new.commit()
    r = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    check("a separate reader sees every fresh row",
          r.execute("SELECT count(*) FROM readings").fetchone()[0] == 12)
    r.close()
    check("and so does the engine", count(src) == 12)
    w_new.close()

print("a rotation landing between the connect and the identity stat is caught:")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "archive.db")
    make_archive(db, 5)
    ds.ARCHIVE_DB = db
    ds.ARCHIVE_RECHECK_S = 1e9          # no periodic recheck: the open itself must get it right
    real_ident = ds._file_identity
    calls = {"n": 0}

    def rotate_after_connect(path):
        calls["n"] += 1
        if calls["n"] == 2:             # the stat AFTER the first connect: rotate right before it
            os.rename(db, os.path.join(d, "archive.corrupt-20261027T160000.db"))
            make_archive(db, 8)
        return real_ident(path)

    ds._file_identity = rotate_after_connect
    try:
        src = source()
        n = count(src)
    finally:
        ds._file_identity = real_ident
    check("the open noticed the move and retried onto the new file", n == 8 and calls["n"] >= 4)
    check("the cached identity is the NEW file's", src._archive_local.ident == real_ident(db))
    check("and it stays on the new file", count(src) == 8)

    calls["n"] = 0

    def always_moving(path):
        calls["n"] += 1
        return (0, calls["n"])          # a file that never holds still

    ds._file_identity = always_moving
    try:
        src = source()
        count(src)
        ident = src._archive_local.ident
    finally:
        ds._file_identity = real_ident
    check("if it never settles, the identity is left unknown (so the next recheck reopens)",
          ident is None)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
