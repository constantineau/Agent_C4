"""Onboard engine store: one SQLite connection shared by the engine's threadpool (#162).

Two failures from sharing it with no lock and no rollback:
1. A course load that fails part-way (a mark with no name) left the DELETE in an open
   transaction. Readers saw the half-written course at once, and the next commit from any other
   write (sail state, nav progress) made it permanent.
2. Reads racing writes on the one connection threw (`InterfaceError: bad parameter or other API
   misuse`, `IndexError`) or saw a course mid-replace, which 500'd /fleet and /strategy.

Run:  python3 vps/agent/test_engine_store.py
"""
import os
import sys
import tempfile
import threading
import time

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


def names(src, route):
    return [m["name"] for m in src.marks(route)]


GOOD = [(1, "Start", 45.0, -83.0), (2, "Mark A", 45.2, -83.1), (3, "Finish", 45.4, -83.0)]
BAD = [(1, "Start", 45.0, -83.0), (2, None, 45.2, -83.1), (3, "Finish", 45.4, -83.0)]

# RAM-backed when there is one: a container's overlay fsyncs each commit in ~0.1 s, and this is a
# test of the locking, not of the disk
_tmp = "/dev/shm" if os.path.isdir("/dev/shm") else None
with tempfile.TemporaryDirectory(dir=_tmp) as d:
    ds.ENGINE_DB = os.path.join(d, "engine.db")
    src = ds.OnboardSource()

    print("a course load that fails part-way leaves the old course, now and on disk:")
    src.save_course("race", GOOD)
    try:
        src.save_course("race", BAD)
        check("the bad load raises", False)
    except Exception as exc:   # noqa: BLE001
        check(f"the bad load raises ({type(exc).__name__})", True)
    check("readers still see the old course right after", names(src, "race") ==
          ["Start", "Mark A", "Finish"])
    src.save_nav_progress("race", {"fp": "x", "i": 1})       # an unrelated write commits
    other = ds.OnboardSource()                               # a fresh connection reads the disk
    check("the old course is still on disk after an unrelated write",
          names(other, "race") == ["Start", "Mark A", "Finish"])
    check("the unrelated write itself landed", other.get_nav_progress("race") == {"fp": "x", "i": 1})

    print("reads racing writes on the shared connection never throw or see a half-written course:")
    LONG = [(i, f"M{i}", 45.0 + i / 100, -83.0) for i in range(1, 41)]
    src.save_course("race", LONG)
    errors, partial = [], []
    stop = threading.Event()

    def writer():
        flip = False
        while not stop.is_set():
            try:
                src.save_course("race", GOOD if flip else LONG)
                src.save_sail_state({"ts": 1.0, "flying": ["J1"], "reef": None})
            except Exception as exc:   # noqa: BLE001
                errors.append(f"write {type(exc).__name__}: {exc}")
            flip = not flip
            time.sleep(0.002)          # let readers in (a plain lock is not fair)

    def reader():
        for _ in range(200):
            try:
                n = len(src.marks("race"))
                src.get_fleet()
                src.get_sail_state()
                src.session_current()
                if n not in (len(GOOD), len(LONG)):
                    partial.append(n)
            except Exception as exc:   # noqa: BLE001
                errors.append(f"read {type(exc).__name__}: {exc}")

    # daemon threads + bounded joins: on the old code the shared connection can wedge, and that
    # must fail the check, not hang the run
    writers = [threading.Thread(target=writer, daemon=True) for _ in range(2)]
    readers = [threading.Thread(target=reader, daemon=True) for _ in range(8)]
    for t in writers + readers:
        t.start()
    deadline = time.monotonic() + 60
    for t in readers:
        t.join(timeout=max(0.0, deadline - time.monotonic()))
    stop.set()
    for t in writers:
        t.join(timeout=max(0.0, deadline + 10 - time.monotonic()))
    check("every thread finished (none wedged on the connection)",
          not any(t.is_alive() for t in writers + readers))
    check(f"no read or write threw ({len(errors)} errors{': ' + errors[0] if errors else ''})",
          not errors)
    check(f"no reader saw a half-written course ({len(partial)} partial reads)", not partial)

print("\nPASS" if ok else "\nFAIL")
sys.stdout.flush()
os._exit(0 if ok else 1)   # a wedged daemon thread must not hold the exit
