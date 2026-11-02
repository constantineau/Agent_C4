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
archiver.ENGINE_DB = "/nonexistent/engine.db"     # prune skips (never deletes blind)
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


async def run_until(db, cond, timeout=20.0, check=True):
    """Start the real `archiver.run` on `db` against a fake Signal K; wait for `cond(state)`.
    Returns (held, seconds_to_hold, state)."""
    state = {}
    t0 = time.monotonic()
    task = asyncio.create_task(archiver.run(db=db, ws_connect=lambda *a, **k: FakeWS(),
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
        and s.get("check") is not None and s["check"].result is not None))
    corrupt = list(Path(d).glob("archive.corrupt-*.db"))
    check(f"rows reached a fresh archive {took:.2f} s after start", held and took < 10)
    check("the damaged file was moved aside whole", len(corrupt) == 1
          and corrupt[0].stat().st_size == size)
    chk = state.get("check")
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

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
