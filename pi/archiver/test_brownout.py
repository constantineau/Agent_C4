"""Archiver brownout tolerance — a bad SD card must cost minutes, not six weeks.

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

Run:  python3 pi/archiver/test_brownout.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# archiver imports websockets at module scope; stub it so this test needs no dependencies.
sys.modules.setdefault("websockets", type(sys)("websockets"))
import archiver  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def good_db(path, rows=2000):
    """A real archive with enough rows to span several pages — a 5-row DB is mostly empty space
    and scribbling on it corrupts nothing, which is how the first version of this test passed
    while asserting the opposite."""
    conn = archiver.open_db(Path(path))
    archiver.write_rows(conn, [("2026-07-18T18:00:00.000Z", "sr33", "n2k-socketcan.15",
                                "navigation.speedThroughWater", 3.2, None)] * rows)
    return conn


def wreck(path, lo=1024, hi=32768):
    """Corrupt the archive the way a failing SD card does: checkpoint the WAL first so the
    content really is in the main file, then scribble across the b-tree pages.

    The checkpoint is the load-bearing step — in WAL mode the recent rows live in `-wal`, so
    garbling the main file alone leaves SQLite reading perfectly good data from the log."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        f.seek(lo)
        f.write(os.urandom(max(0, min(hi, size) - lo)))
        f.flush()
        os.fsync(f.fileno())


# --- classification ---------------------------------------------------------
print("_is_corruption:")
check("'database disk image is malformed' is corruption",
      archiver._is_corruption(sqlite3.DatabaseError("database disk image is malformed")))
check("'file is not a database' is corruption",
      archiver._is_corruption(sqlite3.DatabaseError("file is not a database")))
check("a missing table is NOT corruption (don't rotate a schema bug)",
      not archiver._is_corruption(sqlite3.OperationalError("no such table: readings")))
check("'database is locked' is NOT corruption (transient — requeue instead)",
      not archiver._is_corruption(sqlite3.OperationalError("database is locked")))
check("a non-sqlite exception is NOT corruption",
      not archiver._is_corruption(ValueError("nope")))

with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"

    # --- integrity check ----------------------------------------------------
    print("integrity_ok:")
    conn = good_db(db)
    check("a healthy archive passes", archiver.integrity_ok(conn) is True)
    conn.close()
    wreck(db)
    conn2 = sqlite3.connect(db)
    check("a wrecked archive fails", archiver.integrity_ok(conn2) is False)
    conn2.close()

    # --- startup recovery ---------------------------------------------------
    print("open_db_resilient at startup (the 48-restart crash loop):")
    conn, rotated = archiver.open_db_resilient(db)
    check("it returns a usable connection instead of raising", conn is not None)
    check("the corrupt file was moved aside", rotated is not None and rotated.exists())
    check("...under a salvageable name", rotated and "corrupt-" in rotated.name)
    check("...and was NOT deleted", rotated and rotated.stat().st_size > 0)
    check("the fresh archive works",
          conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 0)
    archiver.write_rows(conn, [("2026-07-18T20:41:00.000Z", "sr33", "n2k-socketcan.15",
                                "navigation.speedThroughWater", 3.1, None)])
    check("...and accepts writes immediately",
          conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 1)
    row = conn.execute("SELECT v FROM sync_state WHERE k='corrupt_rotations'").fetchone()
    check("the rotation is counted in sync_state (a fresh archive must not look normal)",
          row is not None and row[0] == "1")
    row = conn.execute("SELECT v FROM sync_state WHERE k='last_corrupt_rotation'").fetchone()
    check("...with when and which file", row is not None and "corrupt-" in row[0])
    conn.close()

    print("a healthy archive is left alone:")
    conn, rotated = archiver.open_db_resilient(db)
    check("no rotation on a good file", rotated is None)
    check("existing rows survive",
          conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 1)
    conn.close()

    # --- a second failure, later in the season ------------------------------
    print("repeat corruption (this card has done it three times):")
    wreck(db)
    conn, rotated2 = archiver.open_db_resilient(db, )
    check("rotates again", rotated2 is not None and rotated2.exists())
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


# --- mid-race corruption, through the real flusher --------------------------
print("corruption DURING a write (mid-race, what actually happened):")


async def one_flush(state, buf, db, wake=None):
    """Run exactly one flusher iteration."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(archiver.flusher(state["conn"], buf, loop, state, db=db,
                                                wake=wake))
    await asyncio.sleep(archiver.FLUSH_SECONDS * 1.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    conn = good_db(db)
    state = {"conn": conn}
    wreck(db)                       # the card goes bad while the boat is racing
    buf = [("2026-07-18T20:40:31.000Z", "sr33", "n2k-socketcan.15",
            "navigation.speedThroughWater", 6.4, None)] * 4
    archiver.FLUSH_SECONDS = 0.05
    asyncio.run(one_flush(state, buf, db))
    check("the flusher survived the corruption", True)   # reaching here at all is the point
    check("it rotated the bad file aside",
          len(list(Path(d).glob("archive.corrupt-*.db"))) == 1)
    check("the buffered rows were NOT lost",
          state["conn"].execute("SELECT count(*) FROM readings").fetchone()[0] == 4)
    check("the buffer was drained", buf == [])
    state["conn"].close()

print("a transient write error requeues instead of rotating:")


class Flaky:
    """A connection stand-in that raises 'database is locked' once, then works."""

    def __init__(self):
        self.calls = 0

    def executemany(self, *a):
        self.calls += 1
        raise sqlite3.OperationalError("database is locked")

    def commit(self):
        pass


with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    state = {"conn": Flaky()}
    buf = [("2026-07-18T20:40:31.000Z", "sr33", "s", "p", 1.0, None)]
    archiver.FLUSH_SECONDS = 0.05
    asyncio.run(one_flush(state, buf, db))
    check("no rotation for a lock (the file is fine)",
          list(Path(d).glob("archive.corrupt-*.db")) == [])
    check("the rows are back in the buffer for the next tick", len(buf) >= 1)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
