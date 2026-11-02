"""Frozen at the gun: the one boat-side race-time gate for shore homework (RRS 41).

Everything the shore computes for the boat (course, fleet, playbook, checklist, watch plan, the
forecast plume, the re-route chart and boat profile) is pre-start homework. Once the boat is
racing, none of it may land. The boat is the authority: its own race log says whether it is
racing. The crew taps a race session on at the gun and off at the finish on the iPad.

Rules (shared by every load endpoint that takes shore content):
  * a running session of kind "race" means racing (a practice session doesn't);
  * a race log that can't be read counts as racing (fail closed);
  * the check and the write happen under racelog.SESSION_LOCK, so a session can't start in
    between.

The cloud has no race log (`racelog._src()` is None there), so this gate never refuses on the
cloud; the cloud's own race mode (race_mode.py) covers it.

Usage:
    with racegate.hold() as racing:
        if racing:
            return racegate.refused()
        ...write...
"""
import threading
from contextlib import contextmanager

RACING = "Racing now. Not loaded."


def race_session():
    """The boat's own race log: the running session if it is a RACE, else None. If the log can't
    be read, fail closed (treated as racing)."""
    try:
        from . import racelog
        src = racelog._src()
        if src is None:
            return None                # no onboard race log (cloud): nothing to consult
        cur = src.session_current()
        return cur if cur and str(cur.get("kind") or "").lower() == "race" else None
    except Exception:  # noqa: BLE001
        return {"kind": "race", "start_ts": None, "note": "race log unreadable"}


def session_lock():
    """The race log's session lock (taken by racelog.start). Held across the check and the write.
    If the race log can't be imported, race_session() already fails closed, so a private lock is
    enough."""
    try:
        from . import racelog
        return racelog.SESSION_LOCK
    except Exception:  # noqa: BLE001
        return threading.Lock()


def racing() -> bool:
    return bool(race_session())


@contextmanager
def hold():
    """Hold the session lock; yield True when the boat is racing (the caller must refuse)."""
    with session_lock():
        yield racing()


def refused():
    return {"loaded": False, "detail": RACING}


# ---- is shore homework aboard? (the race log's default kind) -----------------------------------
HOMEWORK_KINDS = ("course", "fleet", "playbook")


def _homework_path():
    import os
    base = os.path.dirname(os.environ.get("ENGINE_DB", "/var/lib/sr33/engine/engine.db"))
    return os.path.join(base, "homework-aboard.json")


def mark_homework(kind):
    """Record that a Deploy load of `kind` (course/fleet/playbook) landed. Never raises."""
    import json
    import os
    import tempfile
    import time
    try:
        path = _homework_path()
        cur = homework_aboard_detail()
        cur[kind] = int(time.time())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".hw-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(cur, f)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def homework_aboard_detail() -> dict:
    """{kind: when} for each Deploy load aboard; {} when none (or unreadable)."""
    import json
    try:
        with open(_homework_path()) as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if k in HOMEWORK_KINDS} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def homework_aboard() -> bool:
    """Is any shore homework (a course, fleet or playbook from Deploy) aboard? Then a session
    started with no kind is a RACE (old clients); the iPad pre-selects Race."""
    return bool(homework_aboard_detail())
