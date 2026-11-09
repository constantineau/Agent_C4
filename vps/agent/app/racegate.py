"""Frozen at the gun: the one boat-side race-time gate for shore homework (RRS 41).

Everything the shore computes for the boat (course, fleet, playbook, checklist, watch plan, the
forecast plume, the re-route chart and boat profile) is pre-start homework. Once the boat is
racing, none of it may land. The boat is the authority: its own race log says whether it is
racing. Starting a race session on the iPad (the gun tap) starts racing; only "Race over" on the
iPad ends it, or 24 h of unbroken stopped readings (racelog.race_status). Racing is its own
persisted state (#241): ending or auto-closing the recording session does not end it, and an
engine restart keeps it.

Rules (shared by every load endpoint that takes shore content):
  * racing means the race log's racing state is on (a practice session never turns it on);
  * a racing state that can't be read counts as racing (fail closed);
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
    """The boat's racing state: {kind: "race", start_ts, name, race_id} while racing, else None.
    Racing, not recording: a race session that was closed (LOG end, auto-close) still counts
    until the crew taps "Race over". If the state can't be read, fail closed (treated as racing).
    Never takes the session lock (hold() already holds it)."""
    try:
        from . import racelog
        src = racelog._src()
        if src is None:
            return None                # no onboard race log (cloud): nothing to consult
        st = racelog.race_state(src)
        if not st.get("racing"):
            return None
        return {"kind": "race", "start_ts": st.get("since"), "name": st.get("name"),
                "race_id": st.get("race_id")}
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


# ---- is shore homework aboard? (the iPad's LOG pre-selection: UI only, never a gate) ---------
def homework_aboard_detail() -> dict:
    """Which Deploy homework the stores hold right now: {course, fleet, playbook} booleans. Read
    from the persisted stores (so an upgraded boat is right too). Never raises."""
    out = {"course": False, "fleet": False, "playbook": False}
    try:
        from . import datasource, navigator
        ds = datasource.active()
        out["playbook"] = bool(ds.get_playbook())
        out["fleet"] = bool((ds.get_fleet() or {}).get("fleet"))
        route = navigator.active_route()
        out["course"] = route not in ("practice", "default", None) and bool(ds.marks(route))
    except Exception:  # noqa: BLE001
        pass
    return out


def homework_aboard() -> bool:
    """Any course, fleet or playbook from Deploy aboard? Only the iPad's LOG pre-selection reads
    this (Race pre-selected); the race session itself always comes from an explicit kind, or race."""
    return any(homework_aboard_detail().values())
