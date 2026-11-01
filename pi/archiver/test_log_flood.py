"""No log floods, and the hourly prune never holds the write lock for long.

Before: once the 300k-row buffer was full, every incoming Signal K delta trimmed a few rows and
printed a line — ~200 lines/s, ~2 GB/day of Docker logs on the archive's own SD card. And the
hourly prune held the write lock for one DELETE of everything old, minutes on the card, which
is exactly what fills that buffer (pi review 2026-11-02 #5).

Run:  python3 pi/archiver/test_log_flood.py
"""
import contextlib
import io
import os
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

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


ROW = ("2026-11-01T12:00:00.000Z", "sr33", "n2k.15", "navigation.speedOverGround", 3.0, None)

# --- 1. the buffer-full warning is rate-limited -------------------------------------------
print("buffer full: one line per DROP_LOG_EVERY_S, with the count, not one per delta:")
clock = Clock()
state, buf = {}, [ROW] * 100
out = io.StringIO()
with contextlib.redirect_stdout(out):
    # 1,250 deltas of 4 rows each over 25 simulated seconds — the review's repro
    for i in range(1250):
        buf.extend([ROW] * 4)
        archiver.cap_buffer(buf, state, limit=100, clock=clock)
        clock.t += 0.02
lines = [ln for ln in out.getvalue().splitlines() if "DROPPED" in ln]
check(f"1,250 trims in 25 s printed {len(lines)} line(s), not 1,250", len(lines) == 1)
check("every dropped row is still counted", state["buffer_dropped"] == 5000)
check("the buffer holds the cap", len(buf) == 100)
out = io.StringIO()
with contextlib.redirect_stdout(out):
    clock.t += archiver.DROP_LOG_EVERY_S          # the next window
    buf.extend([ROW] * 4)
    archiver.cap_buffer(buf, state, limit=100, clock=clock)
lines = [ln for ln in out.getvalue().splitlines() if "DROPPED" in ln]
check("after the window, one more line", len(lines) == 1)
check("...carrying the rows dropped since the last line (4996 + 4)",
      lines and "DROPPED the oldest 5000 since the last report" in lines[0]
      and "5004 dropped since start" in lines[0])

print("a day of a stuck flusher at ~200 deltas/s stays small:")
clock, state, buf = Clock(), {}, []
out = io.StringIO()
with contextlib.redirect_stdout(out):
    for _ in range(24 * 3600 // 10):              # one call per 10 s stands in for 2,000
        buf.extend([ROW] * 3)
        archiver.cap_buffer(buf, state, limit=1, clock=clock)
        clock.t += 10
size = len(out.getvalue())
check(f"{size / 1024:.0f} KB of log for a day (was ~2 GB)", size < 1024 * 1024)

print("a failing write requeues quietly too (one line per window):")
state, buf = {}, []
out = io.StringIO()
with contextlib.redirect_stdout(out):
    for _ in range(50):
        archiver._requeue(buf, [ROW], state, "write failed (database is locked)")
lines = [ln for ln in out.getvalue().splitlines() if "requeued" in ln]
check(f"50 failed flushes in a burst printed {len(lines)} line", len(lines) == 1)
check("...and kept every row", len(buf) == 50)


# --- 2. the prune deletes in small batches, releasing the lock between them ---------------
class SpyLock:
    """Stands in for archiver._WRITE_LOCK: records who held it, and for how long."""

    def __init__(self):
        self.real = threading.Lock()
        self.log = []                   # (thread name, held seconds)

    def __enter__(self):
        self.real.acquire()
        self._t = time.monotonic()
        return self

    def __exit__(self, *a):
        self.log.append((threading.current_thread().name, time.monotonic() - self._t))
        self.real.release()
        return False


def engine_db(path, sessions):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, race_id TEXT, name TEXT, "
              "kind TEXT, start_ts REAL, end_ts REAL)")
    c.executemany("INSERT INTO sessions (id, race_id, start_ts, end_ts) VALUES (?,?,?,?)",
                  sessions)
    c.commit()
    c.close()


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


print("prune: batches under the lock, the flusher gets in between, the same rows go:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    eng = Path(d) / "engine.db"
    now = datetime.now(timezone.utc)
    old0 = now - timedelta(days=20)
    conn = archiver.open_db(db)
    # 3,000 old rows, one per minute; a session window over 200 of them; 50 recent rows
    rows = [(iso(old0 + timedelta(minutes=i)),) + ROW[1:] for i in range(3000)]
    rows += [(iso(now - timedelta(minutes=i)),) + ROW[1:] for i in range(50)]
    archiver.write_rows(conn, rows)
    s0 = (old0 + timedelta(minutes=1000)).timestamp()
    engine_db(eng, [(1, None, s0, s0 + 199 * 60 + 1)])
    real_lock, archiver._WRITE_LOCK = archiver._WRITE_LOCK, SpyLock()
    real_win = archiver.session_windows
    # the marker window only: the derived-window maths has its own test (test_prune_window)
    archiver.session_windows = lambda e, conn=None: real_win(e, conn=None)
    spy = archiver._WRITE_LOCK
    writes = []
    done = threading.Event()

    def flusher_thread():
        i = 0
        while not done.is_set():
            archiver.write_rows(conn, [(iso(now),) + ROW[1:]])
            writes.append(i)
            i += 1
            time.sleep(0.001)
    try:
        t = threading.Thread(target=flusher_thread, name="flusher")
        t.start()
        threading.current_thread().name = "MainThread"
        deleted = archiver.prune(conn, engine_db=str(eng), retain_days=7, chunk_rows=100,
                                 pause_s=0.01)
        done.set()
        t.join()
    finally:
        archiver._WRITE_LOCK = real_lock
        archiver.session_windows = real_win
    prune_holds = [h for name, h in spy.log if name == "MainThread"]
    order = [name for name, _ in spy.log]
    first = order.index("MainThread")
    last = len(order) - 1 - order[::-1].index("MainThread")
    check(f"deleted the 2,800 out-of-session old rows ({deleted})", deleted == 2800)
    check(f"in batches: {len(prune_holds)} lock holds for chunk_rows=100 (>= 28)",
          len(prune_holds) >= 28)
    check("the flusher wrote between the prune's batches",
          "flusher" in order[first:last])
    check(f"longest single prune hold {max(prune_holds) * 1000:.0f} ms (< 1 s)",
          max(prune_holds) < 1.0)
    n_old = conn.execute("SELECT count(*) FROM readings WHERE time < ?",
                         (iso(now - timedelta(days=7)),)).fetchone()[0]
    check("exactly the session window's 200 old rows are kept", n_old == 200)
    check("recent rows and the flusher's rows are untouched",
          conn.execute("SELECT count(*) FROM readings WHERE time >= ?",
                       (iso(now - timedelta(days=7)),)).fetchone()[0] == 50 + len(writes))
    again = archiver.prune(conn, engine_db=str(eng), retain_days=7, chunk_rows=100, pause_s=0)
    check("a second prune finds nothing", again == 0)
    conn.close()

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
