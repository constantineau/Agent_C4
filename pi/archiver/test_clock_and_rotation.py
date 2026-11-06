"""Archiver: untrusted clock (#184-9), prune vs rotation (#184-12), moved-file check (#184-15).

#184-9 (rule settled in #240): the Pi has no RTC, so until NTP or GNSS sets the clock rows are
stamped with a stale date. Recording goes on; the prune waits until the clock is trusted, and
the rows written before that are kept by a window recorded by row id, because their stale date
would otherwise read as "old" to the first prune after trust.

#184-12: a rotation sets _ROTATING; the prune must stop between window derivations, not run all
of them first.

#184-15: a check of the file moved aside that keeps deferring must not block the live file's
checks forever.

Run: docker run --rm -v $PWD:/repo:ro -w /tmp -e PYTHONPATH=/repo/pi/archiver:/repo \
       sr33-ci-archiver python3 /repo/pi/archiver/test_clock_and_rotation.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import archiver                                                          # noqa: E402
from shared import clock_trust                                           # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakeClock:
    def __init__(self, trusted):
        self.source = "test" if trusted else None

    @property
    def trusted(self):
        return bool(self.source)


def archive(d):
    conn = sqlite3.connect(os.path.join(d, "a.db"), check_same_thread=False)
    conn.executescript(archiver.SCHEMA)
    return conn


def engine(d, sessions=()):
    eng = os.path.join(d, "engine.db")
    ec = sqlite3.connect(eng)
    ec.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, race_id TEXT, start_ts REAL, "
               "end_ts REAL)")
    ec.executemany("INSERT INTO sessions (start_ts, end_ts) VALUES (?, ?)", sessions)
    ec.commit()
    ec.close()
    return eng


def add(conn, t0, n, step=60):
    conn.executemany("INSERT INTO readings (time, boat_id, source, path, value, str_value) "
                     "VALUES (?, 'sr33', 'gps', 'navigation.speedOverGround', 1.0, NULL)",
                     [(iso(t0 + k * step),) for k in range(n)])
    conn.commit()


def count(conn, t0, t1):
    return conn.execute("SELECT count(*) FROM readings WHERE time >= ? AND time <= ?",
                        (iso(t0), iso(t1))).fetchone()[0]


NOW = time.time()
print("#184-9 untrusted clock:")
with tempfile.TemporaryDirectory() as d:
    conn, eng = archive(d), engine(d)
    real_old = NOW - 10 * 86400            # a real day sail 10 days ago, out of session: prunable
    stale = NOW - 30 * 86400               # what a clock with no RTC said after power-up
    add(conn, real_old, 20)
    state = {"conn": conn, "clock": FakeClock(False)}
    archiver.open_clock_window(conn)       # what run() does before the first row
    state["clock_open_gen"], state["clock_saved"] = 0, True
    add(conn, stale, 20)                   # recording goes on with the stale date
    check("while untrusted, housekeeping says do not prune",
          archiver._clock_housekeeping(state) is False)

    pruned = []
    real_prune = archiver.prune
    archiver.prune = lambda *a, **k: pruned.append(1)
    archiver.PRUNE_EVERY_S = 0.05
    archiver.CLOCK_RECHECK_S = 0.05

    async def run_pruner():
        task = asyncio.create_task(archiver.pruner(state, asyncio.get_running_loop()))
        await asyncio.sleep(0.3)
        task.cancel()
    asyncio.run(run_pruner())
    archiver.prune = real_prune
    check("the pruner never prunes while the clock is untrusted", pruned == [])

    state["clock"].source = "ntp"          # NTP synced (or a GNSS fix agreed)
    check("trusted, but rows collected before may still be in the RAM buffer: wait",
          archiver._clock_housekeeping(state) is False and archiver.clock_window_open(conn))
    check("a prune with the stretch still open deletes nothing",
          archiver.prune(conn, engine_db=eng, retain_days=7) is None
          and count(conn, real_old, real_old + 19 * 60) == 20)
    add(conn, stale + 20 * 60, 1)          # the last stale row the flusher still held
    state["drained_at"] = time.monotonic()  # the flusher has drained the buffer since
    check("once trusted and drained, housekeeping allows the prune",
          archiver._clock_housekeeping(state) is True)
    check("the untrusted stretch is closed as an id range (ids 21..41)",
          archiver.clock_keep_ids(conn) == [[21, 41]])
    archiver.prune(conn, engine_db=eng, retain_days=7)
    check("rows written before trust (stale date), including the late one, are KEPT",
          count(conn, stale, stale + 20 * 60) == 21)
    check("real old out-of-session rows are still pruned", count(conn, real_old,
                                                                 real_old + 19 * 60) == 0)
    doc = archiver.write_status(state, path=os.path.join(d, "status.json"))
    check("status file says clock_trusted", doc.get("clock_trusted") is True)

print("#184-9 a stretch holding stale AND corrected times does not shield other history:")
with tempfile.TemporaryDirectory() as d:
    conn, eng = archive(d), engine(d)
    unrelated = NOW - 400 * 86400          # a real, old, out-of-session row: prunable
    add(conn, unrelated, 3)
    state = {"conn": conn, "clock": FakeClock(False)}
    archiver.open_clock_window(conn)
    state["clock_open_gen"], state["clock_saved"] = 0, True
    add(conn, NOW - 2000 * 86400, 2)       # stale clock: years ago
    add(conn, NOW - 60, 2)                 # NTP stepped it before the stretch was closed
    state["clock"].source = "ntp"
    state["drained_at"] = time.monotonic() + 1
    archiver._clock_housekeeping(state)
    archiver.prune(conn, engine_db=eng, retain_days=7)
    check("the unrelated old rows are pruned", count(conn, unrelated, unrelated + 180) == 0)
    check("the stale rows of the stretch are kept",
          count(conn, NOW - 2001 * 86400, NOW - 1999 * 86400) == 2)

print("#184-9 kept stretch rows are stepped over in bounded chunks:")
with tempfile.TemporaryDirectory() as d:
    conn, eng = archive(d), engine(d)
    base = NOW - 300 * 86400
    add(conn, base, 50, step=1)            # ids 1..50: a kept stretch (same era, interleaved)
    add(conn, base + 10, 10, step=1)       # ids 51..60: prunable, timestamps inside the stretch
    archiver._save_clock_windows(conn, [{"boot": "b", "from_id": 1, "to_id": 50}])
    deleted = archiver.prune(conn, engine_db=eng, retain_days=7, chunk_rows=5, pause_s=0)
    left = {r[0] for r in conn.execute("SELECT id FROM readings")}
    check(f"only the 10 unprotected rows went (deleted {deleted})",
          deleted == 10 and left == set(range(1, 51)))
    plan = " ".join(str(r) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT id, time FROM readings WHERE time >= ? AND time >= ? "
        "AND (? IS NULL OR time > ? OR id > ?) AND time < ? ORDER BY time, id LIMIT ?",
        ("", "", None, "", None, "z", 5)))
    check(f"the chunk query walks the time index with no sort ({plan})",
          "readings_time_idx" in plan and "TEMP B-TREE" not in plan)

print("#184-9 a rotation while untrusted saves the fresh file's stretch before any row:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    old = archiver.open_db(db)
    state = {"conn": old, "clock": FakeClock(False), "clock_open_gen": 0, "clock_saved": True,
             "generation": 0, "rotate_pending": True}

    async def recover():
        return await archiver._recover([], asyncio.get_running_loop(), state, db)
    got = asyncio.run(recover())
    wins = archiver._clock_windows(state["conn"])
    check("fresh file has an open stretch from row 1, saved",
          got is True and wins and wins[-1]["from_id"] == 1 and wins[-1]["to_id"] is None
          and state["clock_saved"] and state["clock_open_gen"] == 1)
    state["conn"].close()

print("#184-9 a rotation while untrusted: the fresh file's stretch starts at row 1:")
with tempfile.TemporaryDirectory() as d:
    conn = archive(d)
    state = {"conn": conn, "clock": FakeClock(False), "clock_open_gen": 0, "generation": 1}
    add(conn, NOW - 40 * 86400, 5)          # written to the fresh file before the pruner looked
    archiver._clock_housekeeping(state)
    state["clock"].source = "gnss"
    archiver._clock_housekeeping(state)
    state["drained_at"] = time.monotonic()
    archiver._clock_housekeeping(state)
    wins = archiver._clock_windows(conn)
    check("one closed window covering ids 1..5", len(wins) == 1 and wins[0]["from_id"] == 1
          and wins[0]["to_id"] == 5)

print("#184-9 a stretch left open by a run that died is closed on the next start:")
with tempfile.TemporaryDirectory() as d:
    conn = archive(d)
    archiver.open_clock_window(conn)
    add(conn, NOW - 50 * 86400, 3)
    archiver.open_clock_window(conn)        # the next run, still untrusted
    wins = archiver._clock_windows(conn)
    check("first closed at the top id, second open after it",
          [(w["from_id"], w["to_id"]) for w in wins] == [(1, 3), (4, None)])

print("#184-9 a stretch that cannot be saved stops the prune, not the record:")


class FailingConn:
    """Reads fine, every write fails (a full or read-only card)."""
    def __init__(self, real):
        self.real = real

    def execute(self, sql, *a):
        if sql.lstrip().upper().startswith(("INSERT", "DELETE", "UPDATE")):
            raise sqlite3.OperationalError("disk I/O error")
        return self.real.execute(sql, *a)

    def commit(self):
        return self.real.commit()


with tempfile.TemporaryDirectory() as d:
    real = archive(d)
    state = {"conn": FailingConn(real), "clock": FakeClock(False), "clock_open_gen": 0,
             "clock_from_id": 1, "clock_saved": False}
    raised = False
    try:
        archiver._clock_housekeeping(state)
    except sqlite3.OperationalError:
        raised = True
    check("housekeeping raises (the pruner skips this pass) instead of carrying on",
          raised and state["clock_saved"] is False)
    state["conn"] = real                    # the card recovers
    archiver._clock_housekeeping(state)
    check("the next pass saves the stretch", state["clock_saved"] is True
          and archiver.clock_window_open(real))

print("#184-12 the prune stops between window derivations when a rotation starts:")
with tempfile.TemporaryDirectory() as d:
    conn = archive(d)
    eng = engine(d, [(NOW - 40 * 86400 + k * 3600, NOW - 40 * 86400 + k * 3600 + 600)
                     for k in range(6)])
    add(conn, NOW - 60 * 86400, 10)
    calls = []
    real_derive = archiver._derived_window

    def derive(*a, **k):
        calls.append(1)
        archiver._ROTATING.set()            # a rotation begins during the first derivation
        return None
    archiver._derived_window = derive
    try:
        got = archiver._prune(conn, engine_db=eng, retain_days=7)
    finally:
        archiver._derived_window = real_derive
        archiver._ROTATING.clear()
    check(f"one derivation, not six (got {len(calls)})", len(calls) == 1)
    check("nothing deleted", got is None and count(conn, NOW - 61 * 86400, NOW) == 10)

print("#184-15 a moved-file check that keeps deferring does not block the live checks:")


class FakeCheck:
    def __init__(self, live, status):
        self.live = live
        self.result = {"status": status, "detail": "a race/practice session is open"
                       if status == "deferred" else None}


with tempfile.TemporaryDirectory() as d:
    conn = archive(d)
    starts = []

    def fake_start(state, loop, db, rotated, engine_db=None):
        starts.append("moved" if rotated is not None else "live")
        chk = FakeCheck(rotated is None, "deferred" if rotated is not None else "ok")
        state["check"] = chk
        return chk
    real_start = archiver.start_background_check
    archiver.start_background_check = fake_start
    state = {"conn": conn, "generation": 0}

    async def go():
        task = asyncio.create_task(archiver.check_scheduler(
            state, asyncio.get_running_loop(), os.path.join(d, "a.db"),
            os.path.join(d, "moved.db"), later_s=0.02))
        await asyncio.sleep(0.4)
        task.cancel()
    try:
        asyncio.run(go())
    finally:
        archiver.start_background_check = real_start
    check(f"the live file got checked (starts: {starts[:6]})", "live" in starts)
    check("the moved file is still retried", starts.count("moved") >= 2)

print("shared.clock_trust is what the archiver uses:", archiver.clock_trust is clock_trust)
print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
