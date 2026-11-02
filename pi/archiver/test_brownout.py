"""Archiver brownout tolerance — a bad SD card must cost seconds, not six weeks.

What happened, and what this test exists to prevent happening again. On 2026-07-18 at
20:40:30Z, at the bottom of a six-hour discharge of the house bank to 11.08 V, the full-res
archive went malformed mid-race. `open_db()` raised at startup, the process exited, Docker
restarted it, and that cycle repeated 48 times: the archiver recorded **nothing** from Jul 19
until a human moved the file aside by hand on Aug 30. The recipe that fixed it was three `mv`
commands — `CREATE TABLE IF NOT EXISTS` self-initialises a fresh DB — so the code already knew
how to survive this and simply never tried.

Three properties, in the order they matter:

  1. **Never exit.** Corruption at startup or mid-write rotates the file aside and keeps
     recording. There is no path where the recorder stops because a card went bad.
  2. **Never delete.** The corrupt file is kept; `tools/salvage.py` recovered 99.9994% of one
     of them, which is the only reason the Jul 15–17 archive exists.
  3. **Never go quiet.** A fresh archive is indistinguishable from a boat that has not sailed,
     so every rotation is counted in `sync_state` and logged.

DETERMINISTIC. Each kind of damage is its own case on a named page (corrupt_fixtures.py), with
seeded bytes, and the flusher is run until a condition holds (with a timeout), not for a fixed
75 ms. The old version scribbled `os.urandom` over a byte range and cancelled the flusher on a
timer: about 7% of runs failed, and two of its failure shapes were a real bug (a damaged schema
page raised `string or blob too big` or a UnicodeDecodeError, neither treated as corruption, so
the archiver crash-looped at startup).

Run:  python3 pi/archiver/test_brownout.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# archiver imports websockets at module scope; stub it so this test needs no dependencies.
sys.modules.setdefault("websockets", type(sys)("websockets"))
import archiver  # noqa: E402
import corrupt_fixtures as cf  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def good_db(path, rows=2000):
    """A real archive with enough rows to span many pages."""
    conn = archiver.open_db(Path(path))
    archiver.write_rows(conn, cf.rows(rows))
    return conn


def count(conn):
    return conn.execute("SELECT count(*) FROM readings").fetchone()[0]


def raw_error(path):
    """What a plain open + probes raises on this file, as 'Type: message' (or 'none')."""
    try:
        c = archiver.open_db(Path(path))
        try:
            archiver.fast_check(c)
        finally:
            c.close()
        return "none"
    except BaseException as exc:            # noqa: BLE001 - we want to see anything
        return f"{type(exc).__name__}: {exc}"


# --- classification ---------------------------------------------------------
print("_is_corruption (mid-run, narrow):")
check("'database disk image is malformed' is corruption",
      archiver._is_corruption(sqlite3.DatabaseError("database disk image is malformed")))
check("'file is not a database' is corruption",
      archiver._is_corruption(sqlite3.DatabaseError("file is not a database")))
check("'string or blob too big' (a garbled record header) is corruption",
      archiver._is_corruption(sqlite3.DataError("string or blob too big")))
check("a UnicodeDecodeError (garbled text) is corruption",
      archiver._is_corruption(UnicodeDecodeError("utf-8", b"\xea", 0, 1, "bad")))
check("'Could not decode to UTF-8 column' is corruption",
      archiver._is_corruption(sqlite3.OperationalError(
          "Could not decode to UTF-8 column 'path' with text '\\xff'")))
check("a missing table is NOT corruption (don't rotate a schema bug)",
      not archiver._is_corruption(sqlite3.OperationalError("no such table: readings")))
check("'database is locked' is NOT corruption (transient — requeue instead)",
      not archiver._is_corruption(sqlite3.OperationalError("database is locked")))
check("a non-sqlite exception is NOT corruption",
      not archiver._is_corruption(ValueError("nope")))

print("_is_bad_file (at open: every database error but the transient ones):")
for exc in (sqlite3.DatabaseError("malformed database schema (?)"),
            sqlite3.DataError("string or blob too big"),
            sqlite3.OperationalError("disk I/O error"),
            sqlite3.OperationalError("no such index: readings_time_idx"),
            UnicodeDecodeError("utf-8", b"\xea", 0, 1, "bad")):
    check(f"{type(exc).__name__}({exc}) -> rotate", archiver._is_bad_file(exc))
for exc in (sqlite3.OperationalError("database is locked"),
            sqlite3.OperationalError("database table is locked"),
            sqlite3.OperationalError("database or disk is full"),
            sqlite3.OperationalError("unable to open database file"),
            sqlite3.OperationalError("attempt to write a readonly database"),
            ValueError("not sqlite")):
    check(f"{type(exc).__name__}({exc}) -> NOT rotated (a fresh file would fail the same)",
          not archiver._is_bad_file(exc))


# --- every kind of startup damage rotates and records -----------------------
print("startup: each kind of damage is caught by the bounded probes, rotated, and recording "
      "continues:")
for case, (nrows, expect) in cf.STARTUP_CASES.items():
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "archive.db"
        good_db(db, nrows).close()
        desc = cf.damage(case, db)
        got = raw_error(db)
        check(f"{case} — {desc}: (precondition) raises {expect!r} [got {got[:70]!r}]",
              got.startswith(expect))
        # what the damaged file looks like before the archiver touches it
        before = {sfx: Path(str(db) + sfx).read_bytes()
                  for sfx in archiver._SIDECARS if Path(str(db) + sfx).exists()}
        try:
            conn, top, rotated = archiver.open_for_run(db)
        except BaseException as exc:        # noqa: BLE001
            check(f"{case}: open_for_run returned instead of raising ({exc!r})", False)
            continue
        check(f"{case}: rotated aside under a corrupt- name",
              rotated is not None and "corrupt-" in rotated.name and rotated.exists())
        check(f"{case}: the damaged file is kept byte for byte (never deleted)",
              rotated is not None and rotated.read_bytes() == before.get(""))
        check(f"{case}: the fresh archive starts empty", top == 0 and count(conn) == 0)
        archiver.write_rows(conn, cf.rows(3, start=9000))
        check(f"{case}: ...and records at once", count(conn) == 3)
        row = conn.execute("SELECT v FROM sync_state WHERE k='corrupt_rotations'").fetchone()
        check(f"{case}: the rotation is counted in sync_state", row is not None and row[0] == "1")
        conn.close()

with tempfile.TemporaryDirectory() as d:
    print("a healthy archive is left alone:")
    db = Path(d) / "archive.db"
    good_db(db, 2000).close()
    conn, top, rotated = archiver.open_for_run(db)
    check("no rotation on a good file", rotated is None)
    check("the startup reports the highest row id, not a count", top == 2000)
    check("existing rows survive", count(conn) == 2000)
    conn.close()

    # --- a second failure, later in the season ------------------------------
    print("repeat corruption (this card has done it four times):")
    cf.damage("table_root", db)
    conn, _top, rot1 = archiver.open_for_run(db)
    archiver.write_rows(conn, cf.rows(2000))
    conn.close()
    cf.damage("schema_page", db)
    conn, _top, rot2 = archiver.open_for_run(db)
    check("rotates again", rot2 is not None and rot2.exists() and rot2 != rot1)
    check("both corrupt files are kept",
          len(list(Path(d).glob("archive.corrupt-*.db"))) == 2)
    n = conn.execute("SELECT v FROM sync_state WHERE k='corrupt_rotations'").fetchone()
    # the counter lives in the archive that was replaced, so a fresh DB restarts it at 1 —
    # the LOG is the durable record across rotations. Assert the honest behaviour, not a wish.
    check("the new archive counts its own rotation", n is not None and n[0] == "1")
    conn.close()

    # --- the -wal / -shm siblings -------------------------------------------
    print("rotate_corrupt moves the WAL siblings too:")
    db2 = Path(d) / "sidecars.db"
    c = good_db(db2)
    c.close()
    for suffix in ("-wal", "-shm"):
        Path(str(db2) + suffix).write_bytes(b"x")
    dest = archiver.rotate_corrupt(db2, stamp="20260718T204030")
    check("the db moved", dest.exists() and not db2.exists())
    check("the -wal moved", Path(str(dest) + "-wal").exists())
    check("the -shm moved", Path(str(dest) + "-shm").exists())
    check("the name carries the timestamp", dest.name == "sidecars.corrupt-20260718T204030.db")

print("transient errors at open are NOT rotated (a good archive must never be moved aside):")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    good_db(db, 10).close()
    real = archiver.fast_check
    archiver.fast_check = lambda c: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked"))
    try:
        raised = None
        try:
            archiver.open_for_run(db)
        except sqlite3.OperationalError as exc:
            raised = exc
    finally:
        archiver.fast_check = real
    check("a lock at open raises (the caller retries) instead of rotating", raised is not None)
    check("...and nothing was moved", not list(Path(d).glob("archive.corrupt-*")))


print("an uncertain error at open ('disk I/O error') is retried before rotating; clear "
      "damage rotates at once:")


def flaky_open(errors):
    """Patch fast_check to raise each of `errors` in turn, then pass. Returns the call log."""
    calls = []
    real = archiver.fast_check

    def fake(conn):
        calls.append(1)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return real(conn)
    archiver.fast_check = fake
    return calls, real


archiver.OPEN_RETRY_S = 0.01
for label, errs, want_rot, want_calls in (
        ("I/O error twice, then fine: no rotation", [sqlite3.OperationalError("disk I/O error")] * 2,
         False, 3),
        ("I/O error on all 4 tries: rotated", [sqlite3.OperationalError("disk I/O error")] * 4,
         True, 4),
        ("'malformed' once: rotated at once, no retry",
         [sqlite3.DatabaseError("database disk image is malformed")], True, 1),
        ("'string or blob too big' once: rotated at once",
         [sqlite3.DataError("string or blob too big")], True, 1),
        ("a decode error once: rotated at once",
         [UnicodeDecodeError("utf-8", b"\xea", 0, 1, "bad")], True, 1)):
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "archive.db"
        good_db(db, 10).close()
        calls, real = flaky_open(errs)
        try:
            conn, rotated = archiver.open_db_resilient(db)
        finally:
            archiver.fast_check = real
        n = len(calls)
        check(f"{label} ({n} open(s))", (rotated is not None) == want_rot and n == want_calls)
        if not want_rot:
            check("...and the archive kept its rows", count(conn) == 10)
        conn.close()


# --- mid-race corruption, through the real flusher --------------------------
async def flush_until(state, buf, db, cond, timeout=10.0):
    """Run the real flusher until `cond()` holds, then stop it. True if it held in time.
    A condition, not a timer: the old fixed 75 ms lost the race to a slow disk's fsyncs."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(archiver.flusher(state["conn"], buf, loop, state, db=db))
    deadline = time.monotonic() + timeout
    held = False
    try:
        while time.monotonic() < deadline:
            if cond():
                held = True
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return held


archiver.FLUSH_SECONDS = 0.02
print("corruption DURING a write (mid-race, what actually happened):")
for case in ("table_tail_leaf", "path_index_root", "table_root"):
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "archive.db"
        conn = good_db(db)
        state = {"conn": conn}
        cf.damage(case, db)             # the card goes bad while the boat is racing
        buf = cf.rows(4, start=9000)
        held = asyncio.run(flush_until(
            state, buf, db, lambda: state.get("rotations", 0) >= 1 and state.get("total") == 4))
        check(f"{case}: the flusher rotated and wrote the batch within 10 s", held)
        check(f"{case}: it rotated the bad file aside (kept)",
              len(list(Path(d).glob("archive.corrupt-*.db"))) == 1)
        check(f"{case}: the buffered rows were NOT lost",
              state["conn"] is not None and count(state["conn"]) == 4)
        check(f"{case}: the buffer was drained", buf == [])
        state["conn"].close()

print("a transient write error requeues instead of rotating:")


class Flaky:
    """A connection stand-in that always raises 'database is locked'."""

    def __init__(self):
        self.calls = 0

    def executemany(self, *a):
        self.calls += 1
        raise sqlite3.OperationalError("database is locked")

    def commit(self):
        pass

    def rollback(self):
        pass


with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    flaky = Flaky()
    state = {"conn": flaky}
    buf = [("2026-07-18T20:40:31.000Z", "sr33", "s", "p", 1.0, None)]
    held = asyncio.run(flush_until(state, buf, db, lambda: flaky.calls >= 2))
    check("the flusher retried (two attempts seen)", held)
    check("no rotation for a lock (the file is fine)",
          list(Path(d).glob("archive.corrupt-*.db")) == [] and not state.get("rotate_pending"))
    check("the rows are back in the buffer for the next tick", len(buf) == 1)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
