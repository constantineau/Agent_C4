"""The archiver records within seconds of a restart, whatever the size or state of the archive.

Before this, startup read the whole archive twice — `PRAGMA quick_check`, then `count(*)` —
before opening the Signal K stream. On the boat's SD card that was 39 min for 24 GB (2026-10-28),
~80 min at the 7-day size: no full-res record after every restart, including a mid-race brownout
(pi review 2026-11-02 #1). Now startup is bounded probes only, recording starts, and the full
quick_check runs afterwards in a low-priority thread.

How this proves it without an 80-minute file:
  - a synthetic archive big enough that a whole-file read is measurable (~300k rows);
  - every SQL statement the startup path runs is traced: no quick_check, integrity_check or
    count(*) may appear;
  - the background check is made to hang mid-scan (its progress handler blocks), standing in
    for an 80-minute scan, and rows must reach the disk while it hangs.

Run:  python3 pi/archiver/test_startup.py
"""
import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.modules.setdefault("websockets", type(sys)("websockets"))
import archiver  # noqa: E402
import corrupt_fixtures as cf  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


archiver.FLUSH_SECONDS = 0.05
archiver.PRUNE_EVERY_S = 3600
# A readable sessions table with no open session: the background check may run (an UNREADABLE
# one counts as a session and holds it back). Retention off, so the prune keeps every test row.
_ENGINE_DIR = tempfile.TemporaryDirectory()
archiver.ENGINE_DB = os.path.join(_ENGINE_DIR.name, "engine.db")
_c = sqlite3.connect(archiver.ENGINE_DB)
_c.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, race_id TEXT, start_ts REAL, "
           "end_ts REAL)")
_c.execute("INSERT INTO sessions (start_ts, end_ts) VALUES (0, 1)")
_c.commit()
_c.close()
archiver.RETAIN_DAYS = 0
archiver.STATUS_EVERY_S = 0.1
# The disk guard measures the REAL filesystem, and a nearly full CI disk would stop recording
# for reasons this test is not about (test_disk_guard covers the guard).
archiver.DISK_HIGH_PCT, archiver.DISK_LOW_PCT = 101.0, 100.0
archiver.DISK_HARD_PCT, archiver.DISK_MIN_FREE_MB = 101.0, 0.0
FULL_SCAN = re.compile(r"quick_check|integrity_check|count\(\*\)", re.I)
BIG_ROWS = 300_000


def big_db(path, n=BIG_ROWS):
    conn = archiver.open_db(path)
    for start in range(0, n, 50_000):
        archiver.write_rows(conn, cf.rows(min(50_000, n - start), start=start))
    conn.close()
    cf.checkpoint(path)


def count(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT count(*) FROM readings").fetchone()[0]
    finally:
        c.close()


class Traced:
    """Patch sqlite3.connect so every connection's statements land in `self.sql`."""

    def __enter__(self):
        self.sql, self.real = [], sqlite3.connect

        def connect(*a, **k):
            c = self.real(*a, **k)
            c.set_trace_callback(self.sql.append)
            return c
        sqlite3.connect = connect
        return self

    def __exit__(self, *exc):
        sqlite3.connect = self.real


HELLO = json.dumps({"name": "signalk-server", "self": "vessels.urn:mrn:signalk:uuid:c4"})


def delta(i):
    return json.dumps({"updates": [{"$source": "n2k.15", "timestamp":
                                    f"2026-11-01T12:00:{i % 60:02d}.000Z",
                                    "values": [{"path": "navigation.speedOverGround",
                                                "value": 3.0}]}]})


class FakeWS:
    """A Signal K stream: hello, then a delta every 10 ms, forever."""

    def __init__(self):
        self.sent = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        yield HELLO
        while True:
            self.sent += 1
            yield delta(self.sent)
            await asyncio.sleep(0.01)


async def run_until(db, cond, timeout=20.0, check=True, ws=None):
    """Start the real `archiver.run` on `db` against a fake Signal K; wait for `cond(state)`.
    Returns (held, seconds_to_hold, state)."""
    state = {}
    t0 = time.monotonic()
    task = asyncio.create_task(archiver.run(db=db, ws_connect=ws or (lambda *a, **k: FakeWS()),
                                            state=state, check=check))
    held = False
    try:
        while time.monotonic() - t0 < timeout:
            if task.done():
                break
            if cond(state):
                held = True
                break
            await asyncio.sleep(0.02)
    finally:
        took = time.monotonic() - t0
        task.cancel()
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()
        await asyncio.sleep(0.1)
    if state.get("check") is not None:
        state["check"].stop()
    if state.get("conn") is not None:
        try:
            state["conn"].close()
        except Exception:
            pass
    return held, took, state


with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    t = time.monotonic()
    big_db(db)
    size_mb = db.stat().st_size / 2**20
    print(f"(synthetic archive: {BIG_ROWS} rows, {size_mb:.0f} MB, built in "
          f"{time.monotonic() - t:.1f} s)")

    # --- 1. the startup path never scans the file ----------------------------
    print("startup reads a bounded number of pages, never the whole file:")
    with Traced() as tr:
        t = time.monotonic()
        conn, top, rotated = archiver.open_for_run(db)
        took = time.monotonic() - t
    conn.close()
    scans = [s for s in tr.sql if FULL_SCAN.search(s)]
    check(f"no quick_check / integrity_check / count(*) on the startup path {scans or ''}",
          not scans and len(tr.sql) > 0)
    check(f"the startup open took {took:.2f} s (< 2 s)", took < 2.0)
    check("it reports the highest row id", top == BIG_ROWS and rotated is None)
    c = sqlite3.connect(db)
    t = time.monotonic()
    c.execute("PRAGMA quick_check").fetchall()
    qc = time.monotonic() - t
    c.close()
    print(f"    (for scale: a quick_check of this file took {qc:.2f} s here; on the boat's card "
          f"a 24 GB archive took 39 min)")

    # --- 2. recording starts while a whole-file check is still running -------
    print("recording starts at once, while the background check is still mid-scan:")
    gate = threading.Event()
    entered = threading.Event()
    give_up = [0.0]
    real_throttle = archiver.BackgroundCheck._throttle

    def hang(self):
        """The check's progress handler: block mid-scan until released (an 80-min scan).
        Gives up after 30 s, so an archiver that waits for the check FAILS this test instead
        of hanging CI."""
        if not entered.is_set():
            give_up[0] = time.monotonic() + 30
            entered.set()
        while not gate.is_set() and time.monotonic() < give_up[0]:
            if self.stop_event.wait(0.01):
                return 1
        return real_throttle(self)
    archiver.BackgroundCheck._throttle = hang
    try:
        seen = {}

        def recorded_while_checking(s):
            if s.get("total", 0) >= 20 and entered.is_set():
                seen["check_running"] = s["check"].result is None
                return True
            return False
        held, took, state = asyncio.run(run_until(db, recorded_while_checking))
        chk = state.get("check")
        check(f"rows reached the disk {took:.2f} s after start (< 5 s)", held and took < 5)
        check("...while the background check was still mid-scan (it never blocks recording)",
              seen.get("check_running") is True)
        check("it was checking the live file", chk is not None and chk.live and chk.path == db)
        status = json.loads((Path(d) / "archiver-status.json").read_text())
        check("the status file says recording, check running",
              status["recording"] is True
              and (status["background_check"] or {}).get("status") == "running")
    finally:
        gate.set()
        archiver.BackgroundCheck._throttle = real_throttle
    check("the new rows are in the archive", count(db) >= BIG_ROWS + 20)

    print("the background check finishes and reports ok on a healthy file:")
    chk = archiver.BackgroundCheck(db, live=True, duty=1.0).start()
    chk.thread.join(60)
    check("verdict ok", chk.result == {"status": "ok", "detail": None})

    print("the background check can be stopped mid-scan (before a rotation moves the file):")
    gate2 = threading.Event()

    def park(self):
        """Park the scan once (up to 30 s) so it is mid-scan when stopped."""
        if not gate2.is_set():
            gate2.set()
            return int(self.stop_event.wait(30))
        return int(self.stop_event.is_set())
    archiver.BackgroundCheck._throttle = park
    try:
        chk = archiver.BackgroundCheck(db, live=True, duty=1.0).start()
        gate2.wait(5)
        t = time.monotonic()
        archiver.stop_checks(db)
        stopped_in = time.monotonic() - t
    finally:
        archiver.BackgroundCheck._throttle = real_throttle
    check(f"stopped in {stopped_in:.2f} s, verdict aborted",
          not chk.thread.is_alive() and (chk.result or {}).get("status") == "aborted")

    print("the background check gives up its share of the disk (duty cycle):")
    real_slice = archiver.CHECK_SLICE_S
    archiver.CHECK_SLICE_S = 0.005        # many slices even on a fast CI disk
    try:
        chk = archiver.BackgroundCheck(db, live=True, duty=0.25)
        t = time.monotonic()
        chk.start().thread.join(120)
        took = time.monotonic() - t
    finally:
        archiver.CHECK_SLICE_S = real_slice
    # at 25% duty it should sleep 3 s for every 1 s of reading: ~75% of the time
    check(f"at 25% duty it slept {chk.slept:.2f} s of {took:.2f} s (>= 50%)",
          chk.result and chk.result["status"] == "ok" and chk.slept >= 0.5 * took)
    chk = archiver.BackgroundCheck(db, live=True, duty=1.0)
    chk.start().thread.join(120)
    check("flat out (duty 1.0) it never sleeps", chk.slept == 0.0)

# --- 3. a suspect (damaged) large file: fresh segment at once, old one checked aside ----
print("a damaged large archive: record into a fresh file at once, check the old one aside:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    big_db(db)
    cf.damage("table_tail_leaf", db)
    size = db.stat().st_size
    archiver.CHECK_DUTY = 1.0
    held, took, state = asyncio.run(run_until(
        db, lambda s: s.get("total", 0) >= 20
        and s.get("moved_file_check") is not None
        and s["moved_file_check"].result is not None))
    corrupt = list(Path(d).glob("archive.corrupt-*.db"))
    check(f"rows reached a fresh archive {took:.2f} s after start", held and took < 10)
    check("the damaged file was moved aside whole", len(corrupt) == 1
          and corrupt[0].stat().st_size == size)
    chk = state.get("moved_file_check")
    check("the background check read the moved file, not the live one",
          chk is not None and not chk.live and chk.path == corrupt[0])
    check("...and found it damaged (a verdict for the human)",
          chk is not None and (chk.result or {}).get("status") == "corrupt")
    check("the fresh archive holds the new rows only", 20 <= count(db) < 1000)

# --- 4. damage the probes cannot see: the background check finds it, the flusher rotates ----
print("damage in the MIDDLE of the file (missed by the bounded probes) is found by the "
      "background check, and the archive rotates while recording continues:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    c = archiver.open_db(db)
    archiver.write_rows(c, cf.rows(2000))
    c.close()
    cf.damage("table_middle_leaf", db)
    conn, _top, rotated = archiver.open_for_run(db)
    conn.close()
    check("(precondition) the bounded probes pass this file", rotated is None)
    held, took, state = asyncio.run(run_until(
        db, lambda s: s.get("rotations", 0) >= 1 and s.get("total", 0) >= 40
        and not s.get("rotate_pending")))
    check(f"the check flagged it and the flusher rotated it ({took:.2f} s)", held)
    check("the damaged file is kept", len(list(Path(d).glob("archive.corrupt-*.db"))) == 1)
    check("recording continued into the fresh archive", count(db) > 0)

# --- 5. a check that will not let go: the file is NOT moved, recording continues -----------
print("a rotation never moves the file under a background check that still holds it:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    conn = archiver.open_db(db)
    archiver.write_rows(conn, cf.rows(2000))
    release = threading.Event()
    parked = threading.Event()

    def stalled(self):
        """A read stalled on a dying card: ignores the stop request until released."""
        parked.set()
        release.wait(30)
        return 1
    real_throttle = archiver.BackgroundCheck._throttle
    archiver.BackgroundCheck._throttle = stalled
    real_timeout = archiver.CHECK_STOP_TIMEOUT_S
    archiver.CHECK_STOP_TIMEOUT_S = 0.2
    try:
        chk = archiver.BackgroundCheck(db, live=True, duty=1.0).start()
        parked.wait(10)
        state = {"conn": conn, "rotate_pending": True}
        buf = cf.rows(5, start=9000)

        async def flush():
            await archiver._flush_once(buf, asyncio.get_running_loop(), state, db, None)
        asyncio.run(flush())
        check("(setup) the check is stalled holding the file", chk.conn_open)
        check("nothing was moved while it held the file",
              not list(Path(d).glob("archive.corrupt-*")))
        check("the batch was written into the current file",
              buf == [] and state["conn"] is conn and count(db) == 2005)
        check("the rotation is still owed", state.get("rotate_pending") is True)
    finally:
        release.set()
        archiver.BackgroundCheck._throttle = real_throttle
    chk.thread.join(10)
    check("(setup) the check let go", not chk.conn_open)
    asyncio.run(flush())
    check("once it let go, the next flush rotated the file",
          len(list(Path(d).glob("archive.corrupt-*.db"))) == 1
          and not state.get("rotate_pending") and state.get("generation") == 1)
    archiver.CHECK_STOP_TIMEOUT_S = real_timeout
    state["conn"].close()

# --- 6. a late verdict about a file already replaced is ignored -----------------------------
print("a background verdict about a file that was already rotated does not rotate the new one:")


async def verdicts():
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "archive.db"
        archiver.open_db(db).close()
        out = {}
        for label, bump in (("same file", False), ("file replaced since", True)):
            state = {}
            chk = archiver.start_background_check(state, loop, db, None)
            chk.stop()
            if bump:                       # a write found damage first and rotated
                state["generation"] = state.get("generation", 0) + 1
            chk.on_corrupt()               # the verdict arrives late
            await asyncio.sleep(0.05)
            out[label] = bool(state.get("rotate_pending"))
        return out
got = asyncio.run(verdicts())
check("a verdict about the current file schedules a rotation", got["same file"] is True)
check("a verdict about a replaced file is ignored", got["file replaced since"] is False)

# --- 7. the background check retries uncertain errors before a verdict ----------------------
print("background check: a one-off 'disk I/O error' is retried, not a corruption verdict:")


class FailingConn:
    def __init__(self, exc):
        self.exc = exc

    def set_progress_handler(self, *a):
        pass

    def execute(self, *a):
        raise self.exc

    def close(self):
        pass


def check_with(errors):
    """Run a live BackgroundCheck whose first len(errors) attempts raise those errors."""
    real_connect = sqlite3.connect
    calls = []

    def connect(*a, **k):
        calls.append(1)
        if len(calls) <= len(errors):
            return FailingConn(errors[len(calls) - 1])
        return real_connect(*a, **k)
    flagged = []
    sqlite3.connect = connect
    try:
        chk = archiver.BackgroundCheck(db, live=True, duty=1.0,
                                       on_corrupt=lambda: flagged.append(1)).start()
        chk.thread.join(30)
    finally:
        sqlite3.connect = real_connect
    return chk.result["status"], len(calls), bool(flagged)


archiver.CHECK_RETRY_S = 0.01
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    c = archiver.open_db(db)
    archiver.write_rows(c, cf.rows(100))
    c.close()
    io_err = sqlite3.OperationalError("disk I/O error")
    status, n, flagged = check_with([io_err])
    check(f"I/O error once, then ok: verdict {status}, {n} attempts, no rotation",
          status == "ok" and n == 2 and not flagged)
    status, n, flagged = check_with([io_err] * 4)
    check(f"I/O error on every try: verdict {status} after {n} attempts, rotation",
          status == "corrupt" and n == 4 and flagged)
    status, n, flagged = check_with([sqlite3.OperationalError("database is locked")] * 4)
    check(f"locked on every try: verdict {status}, never a rotation",
          status == "error" and n == 4 and not flagged)
    status, n, flagged = check_with([sqlite3.DatabaseError("database disk image is malformed")])
    check(f"'malformed' once: verdict {status} at once ({n} attempt)",
          status == "corrupt" and n == 1 and flagged)

# --- 8. the live check never lets the WAL run away, and never runs in a session ---------------
print("the live check steps aside when the WAL passes its cap (it holds one snapshot):")


def engine_with_session(path, open_):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, race_id TEXT, "
              "start_ts REAL, end_ts REAL)")
    c.execute("DELETE FROM sessions")
    c.execute("INSERT INTO sessions (start_ts, end_ts) VALUES (?, ?)",
              (time.time() - 600, None if open_ else time.time() - 60))
    c.commit()
    c.close()


real_cap, real_watch_s = archiver.CHECK_WAL_CAP_MB, archiver.CHECK_WATCH_S
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    eng = Path(d) / "engine.db"
    engine_with_session(eng, open_=False)
    big_db(db, 100_000)
    writer = archiver.open_db(db)
    archiver.CHECK_WATCH_S = 0.0
    archiver.CHECK_WAL_CAP_MB = 2.0
    grow = threading.Event()
    real_throttle = archiver.BackgroundCheck._throttle

    def throttle_and_note(self):
        grow.set()                         # the scan is running: its snapshot is held
        return real_throttle(self)
    archiver.BackgroundCheck._throttle = throttle_and_note
    try:
        chk = archiver.BackgroundCheck(db, live=True, duty=0.05,
                                       watch=archiver.check_watch(db, str(eng))).start()
        grow.wait(10)
        # recording goes on during the scan: the WAL grows past the cap behind its snapshot
        for i in range(10):
            archiver.write_rows(writer, cf.rows(5000, start=200_000 + 5000 * i))
        chk.thread.join(60)
    finally:
        archiver.BackgroundCheck._throttle = real_throttle
        archiver.CHECK_WAL_CAP_MB, archiver.CHECK_WATCH_S = real_cap, real_watch_s
    wal_mb = Path(str(db) + "-wal").stat().st_size / 2**20
    check(f"(setup) the WAL grew past the 2 MB cap ({wal_mb:.0f} MB)", wal_mb > 2)
    check(f"the check stepped aside: {chk.result}",
          (chk.result or {}).get("status") == "deferred" and "WAL" in chk.result["detail"])
    check("...and let go of the file (its snapshot is released)", not chk.conn_open)
    busy, log, done = archiver._checkpoint(writer)
    check(f"once it let go, a checkpoint recycles the whole WAL ({done}/{log} frames)",
          busy == 0 and log > 0 and done == log)
    archiver.write_rows(writer, cf.rows(5000, start=900_000))
    after = Path(str(db) + "-wal").stat().st_size / 2**20
    check(f"...so new writes reuse it instead of growing it ({wal_mb:.0f} -> {after:.0f} MB)",
          after <= wal_mb)
    writer.close()

print("no live full check runs while a race/practice session is open:")


async def scheduled(eng_open):
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "archive.db"
        eng = Path(d) / "engine.db"
        engine_with_session(eng, open_=eng_open)
        conn = archiver.open_db(db)
        archiver.write_rows(conn, cf.rows(1000))
        state = {"conn": conn}
        started = []
        real_attempt = archiver.BackgroundCheck._attempt

        def spy(self):
            started.append(self.path)
            return real_attempt(self)
        archiver.BackgroundCheck._attempt = spy
        try:
            task = asyncio.create_task(archiver.check_scheduler(state, loop, db, None,
                                                                engine_db=str(eng),
                                                                later_s=0.2))
            await asyncio.sleep(1.0)
            during = (list(started), state.get("check_note"), state.get("checkpoint_due"))
            engine_with_session(eng, open_=False)          # the session ends
            for _ in range(500):
                if state.get("check_passes"):
                    break
                await asyncio.sleep(0.02)
            result = state["check"].result
        finally:
            archiver.BackgroundCheck._attempt = real_attempt
            task.cancel()
        last = conn.execute("SELECT v FROM sync_state WHERE k='last_full_check'").fetchone()
        conn.close()
        return during, result, last


(started, note, ckpt), result, last = asyncio.run(scheduled(True))
check("during the session: no full check was started", started == [])
check(f"...a quiet note says it is waiting ({note!r})",
      note and note.startswith("Archive check waiting") and "session" in note)
check("...and a checkpoint was asked for", ckpt is True)
check(f"after the session closed it ran and passed ({result})", result["status"] == "ok")
check("...and the pass is remembered in the archive", last is not None)

print("a session starting mid-scan makes the check step aside:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    eng = Path(d) / "engine.db"
    engine_with_session(eng, open_=False)
    big_db(db, 100_000)
    archiver.CHECK_WATCH_S = 0.0
    entered = threading.Event()
    real_throttle = archiver.BackgroundCheck._throttle

    def first_then_session(self):
        if not entered.is_set():
            entered.set()
            engine_with_session(eng, open_=True)            # the crew taps LOG
        return real_throttle(self)
    archiver.BackgroundCheck._throttle = first_then_session
    try:
        chk = archiver.BackgroundCheck(db, live=True, duty=1.0,
                                       watch=archiver.check_watch(db, str(eng))).start()
        chk.thread.join(30)
    finally:
        archiver.BackgroundCheck._throttle = real_throttle
        archiver.CHECK_WATCH_S = real_watch_s
    check(f"stepped aside: {chk.result}", (chk.result or {}).get("status") == "deferred"
          and "session" in chk.result["detail"] and not chk.conn_open)

print("a check deferred for more than 7 days says 'Archive check overdue':")
with tempfile.TemporaryDirectory() as d:
    conn = archiver.open_db(Path(d) / "archive.db")
    now = datetime.now(timezone.utc)
    check("first deferral starts the clock, no note", archiver._overdue_note(conn, now) is None)
    check("6 days later: still quiet",
          archiver._overdue_note(conn, now + timedelta(days=6)) is None)
    check("8 days later: overdue",
          archiver._overdue_note(conn, now + timedelta(days=8)) == "Archive check overdue")
    archiver._meta(conn, "last_full_check", (now + timedelta(days=8)).isoformat())
    check("a passing check resets it",
          archiver._overdue_note(conn, now + timedelta(days=9)) is None)
    conn.close()

# --- 9. unreadable sessions = wait; the scheduler keeps going after a pass -----------------
print("an unreadable sessions table counts as a session (the check waits):")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    archiver.open_db(db).close()
    chk = archiver.BackgroundCheck(db, live=True, duty=1.0,
                                   watch=archiver.check_watch(db, str(Path(d) / "nope.db")))
    chk.start().thread.join(10)
    check(f"deferred: {chk.result}", (chk.result or {}).get("status") == "deferred"
          and "unreadable" in chk.result["detail"])

print("after a pass the scheduler keeps watching: overdue later, then checks again:")


async def long_run():
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "archive.db"
        eng = Path(d) / "engine.db"
        engine_with_session(eng, open_=False)
        conn = archiver.open_db(db)
        archiver.write_rows(conn, cf.rows(500))
        state = {"conn": conn}
        real = (archiver.CHECK_EVERY_DAYS, archiver.CHECK_OVERDUE_DAYS)
        archiver.CHECK_EVERY_DAYS = 0.6 / 86400          # re-check every 0.6 s
        archiver.CHECK_OVERDUE_DAYS = 1.0 / 86400        # overdue after 1 s
        out = {}
        task = asyncio.create_task(archiver.check_scheduler(state, loop, db, None,
                                                            engine_db=str(eng),
                                                            later_s=0.05))
        try:
            async def until(cond, t=10):
                for _ in range(int(t / 0.02)):
                    if cond():
                        return True
                    await asyncio.sleep(0.02)
                return False
            out["first pass"] = await until(lambda: state.get("check_passes", 0) >= 1)
            out["scheduler alive after the pass"] = not task.done()
            engine_with_session(eng, open_=True)          # a long race weekend begins
            out["overdue while held back"] = await until(
                lambda: state.get("check_note") == "Archive check overdue")
            out["no pass during the session"] = state.get("check_passes") == 1
            engine_with_session(eng, open_=False)
            out["checked again after"] = await until(lambda: state.get("check_passes", 0) >= 2)
            out["note cleared"] = state.get("check_note") is None
        finally:
            task.cancel()
            archiver.CHECK_EVERY_DAYS, archiver.CHECK_OVERDUE_DAYS = real
            if state.get("check") is not None:
                state["check"].stop()
            conn.close()
        return out
for label, good in asyncio.run(long_run()).items():
    check(label, good)

# --- 10. a Signal K that accepts and closes is still an outage ------------------------------
print("Signal K accepting then closing, with no telemetry, reads as 'no instrument data':")


class EmptyWS(FakeWS):
    async def _gen(self):
        yield HELLO                                     # a hello is not telemetry
        return


real_down, real_retry = archiver.WS_DOWN_S, archiver.WS_RETRY_S
archiver.WS_DOWN_S, archiver.WS_RETRY_S = 0.5, 0.02
try:
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "archive.db"
        held, took, state = asyncio.run(run_until(
            db, lambda s: archiver.recording_state(s)[1] == "no_signalk",
            timeout=5, ws=lambda *a, **k: EmptyWS(), check=False))
        check(f"reported as no_signalk after {took:.2f} s despite constant reconnects", held)
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "archive.db"
        held, took, state = asyncio.run(run_until(
            db, lambda s: s.get("total", 0) >= 5 and archiver.recording_state(s)[0],
            timeout=5, check=False))
        check("real telemetry ends the outage", held)
finally:
    archiver.WS_DOWN_S, archiver.WS_RETRY_S = real_down, real_retry

# --- 11. stop() means the thread is gone; a stalled watcher never opens afterwards ----------
print("a check stalled in its watcher: stop() does not claim success, and it never opens "
      "the file after being stopped:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    archiver.open_db(db).close()
    in_watch, release = threading.Event(), threading.Event()

    def stalled_watch():
        in_watch.set()
        release.wait(30)                   # e.g. the engine DB read hangs on a dying card
        return None
    opened = []
    real_connect = sqlite3.connect

    def spy_connect(target, *a, **k):
        if "archive.db" in str(target):
            opened.append(str(target))
        return real_connect(target, *a, **k)
    sqlite3.connect = spy_connect
    try:
        chk = archiver.BackgroundCheck(db, live=True, duty=1.0, watch=stalled_watch).start()
        in_watch.wait(10)
        stopped = chk.stop(timeout=0.2)
        check("stop() returns False while the thread is still alive (no connection open is "
              "not enough)", stopped is False and chk.thread.is_alive())
        check("(setup) it holds no connection at that moment", not chk.conn_open)
        release.set()                      # the watcher returns: 'nothing to step aside for'
        chk.thread.join(10)
    finally:
        sqlite3.connect = real_connect
    check("after the stall it did NOT open the archive", opened == [])
    check(f"...and finished as aborted ({chk.result})",
          (chk.result or {}).get("status") == "aborted")
    check("stop() on the finished thread returns True", chk.stop(timeout=0) is True)

# --- 12. connected but silent is an outage too ------------------------------------------------
print("Signal K connected, one delta, then silence: 'no instrument data' after WS_DOWN_S:")


class OneThenSilent(FakeWS):
    async def _gen(self):
        yield HELLO
        yield delta(1)
        await asyncio.sleep(3600)          # stays connected, sends nothing
        yield delta(2)


real_down = archiver.WS_DOWN_S
archiver.WS_DOWN_S = 0.5
try:
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "archive.db"
        seen = {}

        def silent_outage(s):
            if s.get("last_telemetry") and "first" not in seen and s.get("total", 0) >= 1:
                seen["first"] = archiver.recording_state(s)[0]
            return archiver.recording_state(s)[1] == "no_signalk"
        held, took, state = asyncio.run(run_until(
            db, silent_outage, timeout=5, ws=lambda *a, **k: OneThenSilent(), check=False))
        check("recording while the delta was fresh", seen.get("first") is True)
        check(f"no_signalk {took:.2f} s in, with the socket still open", held)
finally:
    archiver.WS_DOWN_S = real_down

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
