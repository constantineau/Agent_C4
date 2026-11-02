"""Archiver: disk guard, RAM cap, and a write path that never drops a batch (#32, #33).

The boat's archive corrupted on 2026-09-02 and the archiver crash-looped for eight weeks. A
fresh archive grows ~7 GB/day at the dock, with 69 GB free and an open race session that stops
the prune from freeing anything. Before full-res recording restarts, these must hold:

  1. Disk guard: above the high mark it stops writing full-res, says so, and resumes only
     below the low mark. SQLite's free pages count as free (a prune never shrinks the file).
  2. Once a write has raised corruption, the file is rotated without asking quick_check again
     (quick_check passed on the boat's file while count(*) failed).
  3. A failed rotation requeues the batch instead of dropping it.
  4. A failed executemany is rolled back, so a retry does not double-insert.
  5. The retry buffer is capped, dropping the oldest rows with a loud count.
  6. Startup: corruption on the row count rotates too (the boat's exact crash line).

Run:  python3 pi/archiver/test_disk_guard.py
"""
import asyncio
import errno
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.modules.setdefault("websockets", type(sys)("websockets"))
import archiver  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


ROW = ("2026-10-27T12:00:00.000Z", "sr33", "n2k-socketcan.15", "navigation.speedOverGround",
       0.1, None)
MB = 1024 * 1024


GB = 1024 * MB


def fake_statvfs(used_pct, total=115 * GB, frsize=4096):
    def f(_path):
        blocks = total // frsize
        return SimpleNamespace(f_blocks=blocks, f_frsize=frsize,
                               f_bavail=int(blocks * (100 - used_pct) / 100))
    return f


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def flush(buf, state, path, guard=None):
    async def go():
        await archiver._flush_once(buf, asyncio.get_running_loop(), state, path, guard)
    asyncio.run(go())


# --- 1. the hysteresis switch ---------------------------------------------------------------
print("disk guard: stop at the high mark, resume only below the low mark:")
MEM = sqlite3.connect(":memory:")      # an archive with no free pages: credit known, = 0
level = {"pct": 50.0}
clock = Clock()
g = archiver.DiskGuard("/x", high=85, low=80, every_s=30, nag_s=600,
                       statvfs=lambda p: fake_statvfs(level["pct"])(p), clock=clock)
check("50% -> recording", g.allow(MEM) is True and not g.paused)
level["pct"] = 86.0
check("86% but inside the check interval -> not measured yet", g.allow(MEM) is True)
clock.t = 31
check("86% after the interval -> STOPPED", g.allow(MEM) is False and g.paused)
check("...and the measured level is kept for the status", abs(g.pct - 86.0) < 0.1)
level["pct"] = 82.0
clock.t = 62
check("82% (between the marks) -> still stopped (no flapping at the line)", g.allow(MEM) is False)
level["pct"] = 79.0
clock.t = 93
check("79% -> RESUMED", g.allow(MEM) is True and not g.paused)


def broken_statvfs(_p):
    raise OSError("no such device")


# --- the hard floor: real free space, no free-page credit ------------------------------------
print("hard floor: a physically full card stops recording whatever the free-page credit says:")
real_usage = archiver.disk_usage
fs = {"raw": 99.0, "credit": 25.0}


def usage_with_credit(path, conn=None, statvfs=None):
    """99% of the filesystem really used, but 25 points of it are archive free pages."""
    total = 115 * GB
    return fs["raw"] - fs["credit"], fs["raw"], int(total * (100 - fs["raw"]) / 100)


archiver.disk_usage = usage_with_credit
try:
    c = Clock()
    g = archiver.DiskGuard("/x", high=85, low=80, every_s=0, hard_pct=95, min_free_mb=2048,
                           clock=c)
    check("99% real use, 74% credited -> STOPPED (the credit cannot hide a full card)",
          g.allow() is False and g.reason == "hard_floor")
    fs.update(raw=94.0, credit=25.0)
    check("94% real (inside the 2-point margin) -> still stopped", g.allow() is False)
    fs.update(raw=90.0, credit=25.0)
    check("90% real, 65% credited -> resumed", g.allow() is True)
    fs.update(raw=50.0, credit=0.0)
    g2 = archiver.DiskGuard("/x", high=85, low=80, every_s=0, hard_pct=95,
                            min_free_mb=60 * 1024, clock=c)
    check("under the minimum free bytes (57 GB free < 60 GB) -> stopped, even at 50%",
          g2.allow() is False and g2.reason == "hard_floor")
    fs.update(raw=86.0, credit=0.0)
    g3 = archiver.DiskGuard("/x", high=85, low=80, every_s=0, hard_pct=95, min_free_mb=2048,
                            clock=c)
    check("the ordinary high mark still says why", g3.allow() is False
          and g3.reason == "high_mark")
finally:
    archiver.disk_usage = real_usage

g2 = archiver.DiskGuard("/x", high=85, low=80, every_s=0, statvfs=broken_statvfs, clock=clock)
check("an unreadable disk does not stop the recorder", g2.allow() is True)
g3 = archiver.DiskGuard("/x", high=85, low=90, statvfs=fake_statvfs(10), clock=clock)
check("a low mark above the high mark is corrected below it", g3.low < g3.high)

# --- free pages count as free ----------------------------------------------------------------
print("disk use credits SQLite's free pages (a prune frees pages, not disk):")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    conn = archiver.open_db(db)
    archiver.write_rows(conn, [ROW] * 20000)
    conn.execute("DELETE FROM readings")
    conn.commit()
    free = conn.execute("PRAGMA freelist_count").fetchone()[0] * \
        conn.execute("PRAGMA page_size").fetchone()[0]
    sv = fake_statvfs(90, total=100 * MB)
    raw = archiver.disk_used_pct(db, None, sv)
    credited = archiver.disk_used_pct(db, conn, sv)
    check("the deleted rows left free pages", free > 0)
    check("raw df-style use is 90%", abs(raw - 90) < 0.1)
    check("credited use is lower by exactly the free pages",
          abs((raw - credited) - 100.0 * free / (100 * MB)) < 0.01)
    conn.close()

# --- the guard in the flush path --------------------------------------------------------------
print("over the limit, the flusher writes nothing, counts it, and reports it:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    conn = archiver.open_db(db)
    state = {"conn": conn}
    g = archiver.DiskGuard(db.parent, high=85, low=80, every_s=0, statvfs=fake_statvfs(95),
                           clock=Clock())
    buf = [ROW] * 7
    flush(buf, state, db, g)
    check("no rows written", conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 0)
    check("the buffer is emptied, not hoarded in RAM", buf == [])
    check("the 7 rows are counted as not recorded", g.dropped == 7)
    status_path = Path(d) / "archiver-status.json"
    doc = archiver.write_status(state, g, path=status_path)
    on_disk = json.loads(status_path.read_text())
    check("status file: recording false, reason disk_full",
          on_disk["recording"] is False and on_disk["reason"] == "disk_full")
    check("status file: carries the level, the marks and the count",
          on_disk["disk_used_pct"] == 95.0 and on_disk["disk_high_pct"] == 85
          and on_disk["rows_not_recorded_disk_full"] == 7)
    check("status file: carries the real use and free bytes too",
          on_disk["disk_really_used_pct"] == 95.0 and on_disk["disk_free_bytes"] > 0
          and on_disk["stop_cause"] in ("high_mark", "hard_floor"))
    check("no temp file left behind", not (Path(d) / "archiver-status.json.tmp").exists())
    g.statvfs = fake_statvfs(50)
    buf = [ROW] * 3
    flush(buf, state, db, g)
    check("below the low mark it records again",
          conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 3)
    check("status: recording again", archiver.write_status(state, g, path=status_path)
          ["recording"] is True)
    conn.close()

# --- 4. rollback before re-raise ------------------------------------------------------------
print("a failed batch is rolled back (no double insert on retry):")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    conn = archiver.open_db(db)
    bad = [ROW, (None,) + ROW[1:]]           # row 2 of 2 violates NOT NULL
    try:
        archiver.write_rows(conn, bad)
        raised = False
    except sqlite3.DatabaseError:
        raised = True
    check("the bad batch raised", raised)
    archiver.write_rows(conn, [ROW, ROW])
    n = conn.execute("SELECT count(*) FROM readings").fetchone()[0]
    check(f"only the good batch is in the archive (got {n}, want 2)", n == 2)
    conn.close()


# --- 2. corruption on write rotates without re-asking quick_check ---------------------------
class Corrupt:
    """A connection whose writes say 'malformed' — the boat's file, which passed quick_check."""

    def executemany(self, *a):
        raise sqlite3.DatabaseError("database disk image is malformed")

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


print("corruption on write rotates unconditionally (quick_check would have said ok):")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    archiver.open_db(db).close()                 # the file on disk the write path will rotate
    real_ok, real_resilient = archiver.integrity_ok, archiver.open_db_resilient
    archiver.integrity_ok = lambda c: True       # quick_check passes, as it did aboard
    archiver.open_db_resilient = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("re-gated on quick_check"))
    try:
        state = {"conn": Corrupt()}
        buf = [ROW] * 5
        flush(buf, state, db)
    finally:
        archiver.integrity_ok, archiver.open_db_resilient = real_ok, real_resilient
    check("the file was rotated aside", len(list(Path(d).glob("archive.corrupt-*.db"))) == 1)
    check("the batch landed in the fresh archive",
          state["conn"].execute("SELECT count(*) FROM readings").fetchone()[0] == 5)
    check("nothing left waiting", buf == [] and not state.get("rotate_pending"))
    check("the rotation is counted for the status", state.get("rotations") == 1)
    state["conn"].close()

# --- 3. a failed rotation requeues ----------------------------------------------------------
print("a failed rotation requeues the batch, and the next flush finishes the job:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    archiver.open_db(db).close()
    real_reopen = archiver.reopen_fresh

    def failing_reopen(conn, path):
        raise OSError("read-only file system")

    archiver.reopen_fresh = failing_reopen
    try:
        state = {"conn": Corrupt()}
        buf = [("2026-10-27T12:00:0%d.000Z" % i,) + ROW[1:] for i in range(4)]
        flush(buf, state, db)
    finally:
        archiver.reopen_fresh = real_reopen
    check("the rows are back in the buffer, in order",
          [r[0] for r in buf] == ["2026-10-27T12:00:0%d.000Z" % i for i in range(4)])
    check("the rotation is still owed", state.get("rotate_pending") is True)
    check("the file is still there (not rotated yet)", db.exists() and
          not list(Path(d).glob("archive.corrupt-*.db")))
    buf.append(("2026-10-27T12:00:09.000Z",) + ROW[1:])
    flush(buf, state, db)
    check("next flush rotates", len(list(Path(d).glob("archive.corrupt-*.db"))) == 1)
    got = [r[0] for r in state["conn"].execute("SELECT time FROM readings ORDER BY id")]
    check("...and writes every row, old ones first", len(got) == 5 and got[-1].endswith("09.000Z")
          and got[0].endswith("00.000Z"))
    state["conn"].close()


# --- 5. the RAM cap -------------------------------------------------------------------------
class Locked(Corrupt):
    def executemany(self, *a):
        raise sqlite3.OperationalError("database is locked")


print("the retry buffer is capped, oldest dropped, loudly counted:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    real_cap = archiver.MAX_BUFFER_ROWS
    archiver.MAX_BUFFER_ROWS = 10
    try:
        state = {"conn": Locked()}
        buf = [("t%02d" % i,) + ROW[1:] for i in range(25)]
        flush(buf, state, db)
    finally:
        archiver.MAX_BUFFER_ROWS = real_cap
    check("the buffer holds the cap", len(buf) == 10)
    check("the newest rows are the ones kept", buf[0][0] == "t15" and buf[-1][0] == "t24")
    check("the drop is counted", state.get("buffer_dropped") == 15)
    check("a lock is not corruption: no rotation owed", not state.get("rotate_pending"))
    check("status reports the drop",
          archiver.write_status(state, None, path=Path(d) / "s.json")
          ["rows_dropped_buffer_full"] == 15)


# --- 7. recovery comes before the guard (Codex round 2) ---------------------------------------
class NoPragmas(Corrupt):
    """The corrupt archive's connection, as it is mid-recovery: nothing on it works."""

    def execute(self, *a):
        raise sqlite3.ProgrammingError("Cannot operate on a closed database.")


print("unknown free-page credit is not 'full':")
c = Clock()
g = archiver.DiskGuard("/x", high=85, low=80, every_s=0, hard_pct=95, min_free_mb=2048,
                       statvfs=fake_statvfs(90), clock=c)
check("90% real, credit unreadable -> keeps recording (only the hard floor may stop it)",
      g.allow(NoPragmas()) is True and not g.paused)
check("...and the same with no connection at all", g.allow(None) is True)
g.statvfs = fake_statvfs(96)
check("96% real, credit unreadable -> the hard floor still stops it",
      g.allow(None) is False and g.reason == "hard_floor")
g.statvfs = fake_statvfs(90)
check("a stopped guard does not resume on an unknown credit", g.allow(None) is False)

print("a pending rotation runs BEFORE the guard; a failed one keeps the rows:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    archiver.open_db(db).close()
    g = archiver.DiskGuard(db.parent, high=85, low=80, every_s=0, hard_pct=95,
                           min_free_mb=2048, statvfs=fake_statvfs(90), clock=Clock())
    real_rotate = archiver.rotate_corrupt
    calls = {"n": 0}

    def flaky_rotate(path, stamp=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("device busy")          # transient
        return real_rotate(path, stamp)

    archiver.rotate_corrupt = flaky_rotate
    try:
        state = {"conn": NoPragmas(), "rotate_pending": True}
        buf = [ROW] * 6
        flush(buf, state, db, g)
        check("rotation failed: all 6 rows still waiting", len(buf) == 6)
        check("...none counted as not-recorded by the guard", g.dropped == 0)
        check("...the rotation is still owed", state.get("rotate_pending") is True)
        check("...and the handle is explicitly None (closed before the move, never reused)",
              state["conn"] is None)
        flush(buf, state, db, g)
        check("next flush: rotated first", calls["n"] == 2
              and len(list(Path(d).glob("archive.corrupt-*.db"))) == 1
              and not state.get("rotate_pending"))
        check("...then the guard judged the REAL archive (90%, no free pages) and stopped",
              g.paused and g.pct is not None)
    finally:
        archiver.rotate_corrupt = real_rotate
        if isinstance(state["conn"], sqlite3.Connection):
            state["conn"].close()

    # and below the marks, the same sequence writes everything
    archiver.open_db(db).close()
    g = archiver.DiskGuard(db.parent, high=85, low=80, every_s=0, hard_pct=95,
                           min_free_mb=2048, statvfs=fake_statvfs(50), clock=Clock())
    calls["n"] = 0
    archiver.rotate_corrupt = flaky_rotate
    try:
        state = {"conn": NoPragmas(), "rotate_pending": True}
        buf = [ROW] * 6
        flush(buf, state, db, g)
        flush(buf, state, db, g)
    finally:
        archiver.rotate_corrupt = real_rotate
    check("at 50%: after the retried rotation all 6 rows are in the fresh archive",
          state["conn"].execute("SELECT count(*) FROM readings").fetchone()[0] == 6 and buf == [])
    state["conn"].close()

print("the pruner skips while recovery is pending:")
with tempfile.TemporaryDirectory() as d:
    real_prune = archiver.prune
    pruned = []
    archiver.prune = lambda conn, *a, **k: pruned.append(conn)
    real_every = archiver.PRUNE_EVERY_S
    archiver.PRUNE_EVERY_S = 0.01

    async def one_prune(state):
        t = asyncio.create_task(archiver.pruner(state, asyncio.get_running_loop()))
        await asyncio.sleep(0.005)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(one_prune({"conn": NoPragmas(), "rotate_pending": True}))
        check("no prune on a file that just reported corruption", pruned == [])
        asyncio.run(one_prune({"conn": None}))
        check("no prune through a missing connection", pruned == [])

        def corrupt_prune(conn, *a, **k):
            raise sqlite3.DatabaseError("database disk image is malformed")

        archiver.prune = corrupt_prune
        st = {"conn": object()}
        asyncio.run(one_prune(st))
        check("a prune that hits corruption marks the file for rotation",
              st.get("rotate_pending") is True)
    finally:
        archiver.prune, archiver.PRUNE_EVERY_S = real_prune, real_every


# --- 8. rotation with REAL WAL connections (no fakes) ----------------------------------------
# reopen_fresh closes the old connection BEFORE moving the file: a WAL connection's close
# cleans up `archive.db-wal`/`-shm` by name, and after the move those names are the fresh
# archive's. These run real sqlite3 WAL connections in a temp dir.
print("rotation with real WAL connections: the fresh archive's WAL survives, readers see it:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    old = archiver.open_db(db)
    old.execute("PRAGMA wal_autocheckpoint=0")       # keep the old rows IN the -wal
    archiver.write_rows(old, [ROW] * 2000)
    check("(setup) the old archive has a non-empty -wal", Path(str(db) + "-wal").stat().st_size > 0)
    new, rotated = archiver.reopen_fresh(old, db)
    archiver.write_rows(new, [("2026-10-27T13:00:00.000Z",) + ROW[1:]] * 300)
    check("the fresh archive's -wal exists after the rotation",
          Path(str(db) + "-wal").exists())
    reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    check("a separate reader sees all 300 fresh rows",
          reader.execute("SELECT count(*) FROM readings").fetchone()[0] == 300)
    reader.close()
    archiver.write_rows(new, [ROW] * 10)              # the writer still works, too
    reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    check("...and the next 10 as well",
          reader.execute("SELECT count(*) FROM readings").fetchone()[0] == 310)
    reader.close()
    kept = sqlite3.connect(f"file:{rotated}?mode=ro", uri=True)
    check("the moved-aside archive kept all 2000 of its rows (its WAL was folded in on close)",
          kept.execute("SELECT count(*) FROM readings").fetchone()[0] == 2000)
    kept.close()
    new.close()

print("...and through the flusher, on a file that really is corrupt:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    conn = archiver.open_db(db)
    archiver.write_rows(conn, [ROW] * 4000)
    # Corrupt the main file under the live connection, the way the card does: fold the WAL in,
    # then scribble over the b-tree pages.
    w = sqlite3.connect(db)
    w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    w.close()
    with open(db, "r+b") as f:
        f.seek(4096)
        f.write(os.urandom(64 * 1024))
        f.flush()
        os.fsync(f.fileno())
    state = {"conn": conn}
    fresh = [("2026-10-27T14:00:%02d.000Z" % (i % 60),) + ROW[1:] for i in range(250)]
    buf = list(fresh)
    flush(buf, state, db)
    if state.get("rotations") != 1:          # the write may need a second try to hit a bad page
        flush(buf, state, db)
    check("the write hit the corruption and the file was rotated",
          state.get("rotations") == 1 and len(list(Path(d).glob("archive.corrupt-*.db"))) == 1)
    check("the fresh archive's -wal exists", Path(str(db) + "-wal").exists())
    reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    got = reader.execute("SELECT count(*) FROM readings WHERE time LIKE '2026-10-27T14:%'"
                         ).fetchone()[0]
    reader.close()
    check(f"a separate reader sees every requeued row in the fresh archive ({got}/250)",
          got == 250 and buf == [])
    state["conn"].close()


# --- 9. a rotation that fails halfway is finished, never split (Codex round 4) --------------
import pathlib  # noqa: E402

print("partial rotation: the db moves, the -wal rename fails once; the retry finishes it:")


def wal_archive_with_reader(db, rows):
    """A WAL archive whose rows are still in its -wal, with a second connection open (the
    engine, aboard) so closing the writer does not fold the WAL in and delete it."""
    w = archiver.open_db(db)
    w.execute("PRAGMA wal_autocheckpoint=0")
    archiver.write_rows(w, [ROW] * rows)
    r = sqlite3.connect(db)
    r.execute("SELECT count(*) FROM readings").fetchone()
    return w, r


with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    w, r = wal_archive_with_reader(db, 1500)
    real_rename = pathlib.Path.rename
    failed = {"n": 0}

    def rename_fails_on_wal_once(self, target):
        if str(self).endswith("-wal") and failed["n"] == 0:
            failed["n"] += 1
            raise OSError("I/O error")
        return real_rename(self, target)

    pathlib.Path.rename = rename_fails_on_wal_once
    try:
        try:
            archiver.reopen_fresh(w, db)
            first_raised = False
        except OSError:
            first_raised = True
    finally:
        pathlib.Path.rename = real_rename
    marker = archiver._rotation_marker(db)
    check("the first attempt failed on the -wal", first_raised and failed["n"] == 1)
    check("...after moving the db (the split state)",
          not db.exists() and Path(str(db) + "-wal").exists())
    check("...and the marker names where the rest must go", marker.exists())
    dest = db.with_name(marker.read_text().strip())
    new, rotated = archiver.reopen_fresh(None, db)
    check("the retry finished the SAME rotation", rotated == dest and not marker.exists())
    check("the rotated db and its -wal sit together", dest.exists()
          and Path(str(dest) + "-wal").exists())
    check("the fresh archive started empty: no foreign WAL replayed into it",
          new.execute("SELECT count(*) FROM readings").fetchone()[0] == 0)
    r.close()
    kept = sqlite3.connect(dest)
    check("the rotated archive + its -wal still hold all 1500 rows",
          kept.execute("SELECT count(*) FROM readings").fetchone()[0] == 1500)
    kept.close()
    new.close()

print("a process restart mid-rotation: startup finishes it before opening anything:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    w, r = wal_archive_with_reader(db, 700)
    w.close()
    dest = db.with_name("archive.corrupt-20261027T150000.db")
    archiver._rotation_marker(db).write_text(dest.name)
    db.rename(dest)                                     # ...and then the process died
    r.close()                                           # (the reader lets go of the -wal later)
    leftover = Path(str(db) + "-wal").exists()
    conn, rotated = archiver.open_db_resilient(db)
    check("(setup) the -wal was left at the original name", leftover)
    check("startup finished the recorded rotation", rotated == dest
          and not archiver._rotation_marker(db).exists())
    check("the fresh archive is empty",
          conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 0)
    kept = sqlite3.connect(dest)
    check("the rotated archive has its rows back with its -wal",
          kept.execute("SELECT count(*) FROM readings").fetchone()[0] == 700)
    kept.close()
    conn.close()

print("an orphaned -wal with no database and no rotation in progress is moved aside:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    w, r = wal_archive_with_reader(db, 300)
    w.close()
    db.rename(Path(d) / "moved-by-hand.db")             # someone moved only the db
    r.close()
    orphan = Path(str(db) + "-wal").exists()
    conn, rotated = archiver.open_db_resilient(db)
    check("(setup) a -wal sat at the path with no db", orphan)
    check("it was moved aside under a corrupt- name, not reused",
          rotated is not None and Path(str(rotated) + "-wal").exists())
    check("the fresh archive is empty",
          conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 0)
    conn.close()


# --- 10. the rotation journal is durable, in the right order (Codex round 5) -----------------
print("rotation writes are fsynced in order (a power cut must not split the journal):")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    w, r = wal_archive_with_reader(db, 200)
    events = []
    real = {"fsync": os.fsync, "replace": os.replace, "rename": pathlib.Path.rename,
            "unlink": pathlib.Path.unlink, "open_db": archiver.open_db,
            "fsync_dir": archiver._fsync_dir}

    def rec_fsync(fd):
        events.append(("fsync", None))
        return real["fsync"](fd)

    def rec_replace(a, b):
        events.append(("replace", Path(b).name))
        return real["replace"](a, b)

    def rec_rename(self, target):
        events.append(("rename", self.name))
        return real["rename"](self, target)

    def rec_unlink(self, *a, **k):
        events.append(("unlink", self.name))
        return real["unlink"](self, *a, **k)

    def rec_fsync_dir(dd):
        events.append(("dirsync", None))
        return real["fsync_dir"](dd)

    def rec_open_db(path):
        events.append(("open", None))
        return real["open_db"](path)

    os.fsync, os.replace = rec_fsync, rec_replace
    pathlib.Path.rename, pathlib.Path.unlink = rec_rename, rec_unlink
    archiver._fsync_dir, archiver.open_db = rec_fsync_dir, rec_open_db
    try:
        new, rotated = archiver.reopen_fresh(w, db)
    finally:
        os.fsync, os.replace = real["fsync"], real["replace"]
        pathlib.Path.rename, pathlib.Path.unlink = real["rename"], real["unlink"]
        archiver._fsync_dir, archiver.open_db = real["fsync_dir"], real["open_db"]
    r.close()
    kinds = [e for e in events if e[0] in ("fsync", "replace", "rename", "unlink", "dirsync",
                                           "open")]

    def first(kind, name=None):
        return next(i for i, e in enumerate(kinds) if e[0] == kind
                    and (name is None or e[1] == name))

    i_marker = first("replace", "archive.db.rotating")
    renames = [i for i, e in enumerate(kinds) if e[0] == "rename"]
    i_unlink = first("unlink", "archive.db.rotating")
    i_open = first("open")
    check("the marker's contents are fsynced before it is renamed into place",
          any(e[0] == "fsync" for e in kinds[:i_marker]))
    check("the directory is fsynced after the marker lands and BEFORE anything moves",
          any(e[0] == "dirsync" for e in kinds[i_marker:renames[0]]))
    check("db, -wal and -shm all moved", [kinds[i][1] for i in renames]
          == ["archive.db", "archive.db-wal", "archive.db-shm"])
    check("the directory is fsynced after the moves, before the marker is removed",
          any(e[0] == "dirsync" for e in kinds[renames[-1]:i_unlink]))
    check("...and again after the removal, before the fresh archive is opened",
          any(e[0] == "dirsync" for e in kinds[i_unlink:i_open]))
    check("the result is a working fresh archive and an intact rotated one",
          new.execute("SELECT count(*) FROM readings").fetchone()[0] == 0
          and sqlite3.connect(rotated).execute("SELECT count(*) FROM readings").fetchone()[0]
          == 200)
    new.close()

print("_fsync_dir tolerates a filesystem that cannot fsync directories, and nothing else:")
real_fsync = os.fsync


def einval(fd):
    raise OSError(errno.EINVAL, "Invalid argument")


def eio(fd):
    raise OSError(errno.EIO, "I/O error")


with tempfile.TemporaryDirectory() as d:
    os.fsync = einval
    try:
        archiver._fsync_dir(d)
        tolerated = True
    except OSError:
        tolerated = False
    finally:
        os.fsync = real_fsync
    check("EINVAL is logged, not fatal", tolerated)
    os.fsync = eio
    try:
        archiver._fsync_dir(d)
        raised = False
    except OSError:
        raised = True
    finally:
        os.fsync = real_fsync
    check("a real I/O error still raises (the rotation retries)", raised)


# --- 6. startup: a failing probe rotates --------------------------------------------------
class CountFails:
    """Wraps a real connection; a startup probe says malformed (the boat, 2026-09-02 on, where
    it was the count(*) — now replaced by the bounded `max(id)` probe)."""

    def __init__(self, real):
        self.real = real

    def execute(self, sql, *a):
        if "count(*)" in sql or "max(id)" in sql:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return self.real.execute(sql, *a)

    def close(self):
        self.real.close()


print("startup: a file that opens but fails a probe is rotated, not crash-looped:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    c = archiver.open_db(db)
    archiver.write_rows(c, [ROW] * 3)
    c.close()
    real_resilient = archiver.open_db_resilient
    archiver.open_db_resilient = lambda p: (CountFails(archiver.open_db(p)), None)
    try:
        conn, n, rotated = archiver.open_for_run(db)
    finally:
        archiver.open_db_resilient = real_resilient
    check("it returns instead of raising", conn is not None)
    check("the old file is kept under a corrupt- name",
          rotated is not None and rotated.exists() and "corrupt-" in rotated.name)
    check("the fresh archive is empty and usable", n == 0)
    conn.close()

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
