"""RACE LOG sessions — the owner's record switch, started/ended from the iPad.

The archiver records everything all the time (collect-everything: the engine's live features
need recent history even on a casual sail, and forgetting to press record must never cost a
race). A SESSION marks the windows the boat wants KEPT: inside a session the full-res archive
is permanent and backfills to the cloud for debrief + learnings; outside, the archiver's
retention prune erases it after `ARCHIVE_RETAIN_DAYS` and it never leaves the boat — so day
sails and deliveries don't accumulate anywhere.

Deliberately independent of the Lab: no RaceDefinition, no cloud, no playbook required. A
session is just a name (defaults to the date) + kind (race | practice). If a playbook IS
aboard, starting a session picks up its race_id automatically so the debrief can link them.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from shared import race_window

from . import datasource
from . import deviation

# ---- AUTO-CLOSE ------------------------------------------------------------------------------
# A session that is never stopped never expires, and one open session suspends retention for the
# WHOLE archive (`archiver.prune` keeps every window, and an open one runs to now + 1 day). The
# boat has been in that state since 2026-07-18: the tap at 18:52:20 during the kite hoist STARTED
# `Race 2026-07-18 18:52Z`, which auto-closed the previous session and was itself never closed.
# Seven weeks of no retention from one mis-tap.
#
# So a session closes itself once the record shows the boat has stopped — but only ever at the
# LAST MOMENT IT WAS UNDER WAY, and only on positive evidence:
#   * silence is not stillness. A dead bus, a crashed archiver or a powered-down boat produce no
#     samples, which must never be read as "parked" — hence the minimum sample count.
#   * if no under-way moment can be found in the look-back at all, it does NOT close. Picking an
#     end for a session that has been open for weeks would put those weeks on the deletion path,
#     which is the exact failure this is meant to prevent. A human closes that one.
AUTOCLOSE_IDLE_H = float(os.environ.get("RACELOG_AUTOCLOSE_IDLE_H", "12"))
AUTOCLOSE_LOOKBACK_H = float(os.environ.get("RACELOG_AUTOCLOSE_LOOKBACK_H", "48"))
AUTOCLOSE_MIN_SAMPLES = int(os.environ.get("RACELOG_AUTOCLOSE_MIN_SAMPLES", "30"))
AUTOCLOSE_CHECK_EVERY_S = float(os.environ.get("RACELOG_AUTOCLOSE_CHECK_EVERY_S", "600"))
_MS_TO_KN = 1.943844
_last_check = [0.0]


def autoclose(now=None, force=False):
    """Close a session the crew never closed, at the last moment the boat was under way.

    Returns {closed, note, ...}; called from `status()` (the iPad's REC poll), so there is no new
    background loop and the check runs whenever anything is watching. Never raises — a failure
    here must not take the race log down."""
    s = _src()
    if s is None or AUTOCLOSE_IDLE_H <= 0:
        return {"closed": False, "note": "auto-close disabled"}
    now = time.time() if now is None else float(now)
    if not force and now - _last_check[0] < AUTOCLOSE_CHECK_EVERY_S:
        return {"closed": False, "note": "throttled"}
    _last_check[0] = now
    try:
        cur = s.session_current()
        if not cur:
            return {"closed": False, "note": "no session running"}
        look_h = min(AUTOCLOSE_LOOKBACK_H,
                     max(AUTOCLOSE_IDLE_H, (now - float(cur["start_ts"])) / 3600.0))
        rows = [(t, v * _MS_TO_KN) for t, v in
                (s.series("navigation.speedOverGround", look_h * 60.0) or ())]
        recent = [(t, v) for t, v in rows if t >= now - AUTOCLOSE_IDLE_H * 3600.0]
        if len(recent) < AUTOCLOSE_MIN_SAMPLES:
            # a quiet bus is not a parked boat
            return {"closed": False, "note": f"only {len(recent)} speed reading(s) in the last "
                                             f"{AUTOCLOSE_IDLE_H:.0f} h, not closing"}
        if max(v for _, v in recent) >= race_window.UNDERWAY_KN:
            return {"closed": False, "note": "under way within the idle window"}
        # Only moments INSIDE the session can end it. The crew taps REC at the dock often enough
        # that the last under-way moment in the look-back is frequently the sail before this one,
        # and closing there would write end_ts < start_ts — a negative window, and the prune reads
        # these.
        underway = [t for t, v in rows
                    if v >= race_window.UNDERWAY_KN and t > float(cur["start_ts"])]
        if not underway:
            return {"closed": False,
                    # not closing on a guess: the end decides what gets deleted
                    "note": f"Not moving in this session ({look_h:.0f} h). "
                            f"Close '{cur['name']}' from the iPad."}
        end_ts = max(underway)
        # Close THAT session, never a newer one. The series read above is slow; the crew may tap
        # REC (a new race session) meanwhile, and an unscoped close would end today's race at
        # yesterday's last under-way moment, reopening the homework gate (racegate) mid-race. So:
        # under the session lock, re-check that the same session is still the open one and that
        # the end is after its start, and close only that id.
        with SESSION_LOCK:
            now_cur = s.session_current()
            if not now_cur or now_cur.get("id") != cur.get("id") \
                    or end_ts <= float(now_cur["start_ts"]):
                return {"closed": False, "note": "a new session started; not closing"}
            s.session_end(end_ts, session_id=cur["id"])
        print(f"[racelog] auto-closed '{cur['name']}' (id {cur['id']}) at "
              f"{datetime.fromtimestamp(end_ts, tz=timezone.utc):%Y-%m-%dT%H:%M:%SZ} — the last "
              f"moment the boat was under way; stopped for "
              f"{(now - end_ts) / 3600:.1f} h since", flush=True)
        return {"closed": True, "session": cur, "end_ts": end_ts,
                "note": "closed at the last under-way moment in the record"}
    except Exception as exc:            # never let housekeeping break the race log
        return {"closed": False, "note": f"auto-close check failed: {type(exc).__name__}: {exc}"}


# Held while a session starts, and by the forecast-plume load around its "is a race session
# running?" check + write (app/plume.py), so a plume can't land between the crew tapping race
# start and the session existing.
SESSION_LOCK = threading.Lock()


def _src():
    s = datasource.active()
    if not hasattr(s, "session_start"):
        return None                    # cloud datasource — sessions are onboard-only
    return s


def start(name=None, race_id=None, kind=None):
    """Start a session. Everything is optional: race_id defaults to the loaded playbook's (if
    any); kind to 'race' when there's a race_id OR any shore homework from Deploy is aboard (a
    course, fleet or playbook: racegate.homework_aboard), else 'practice'. The iPad always sends
    the kind the crew picked; the default only covers old clients, and errs to race (the gate
    closes). Name defaults to '<Race|Sail> <UTC date HH:MM>'."""
    s = _src()
    if s is None:
        return {"ok": False, "note": "race log is onboard-only"}
    if race_id is None:
        try:
            race_id = (deviation._load_playbook() or {}).get("race_id")
        except Exception:
            race_id = None
    if not kind:
        from . import racegate
        kind = "race" if (race_id or racegate.homework_aboard()) else "practice"
    kind = str(kind).lower()
    if kind not in ("race", "practice"):
        return {"ok": False, "note": "kind must be race or practice"}
    if not name:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        name = f"{'Race' if kind == 'race' else 'Sail'} {stamp}Z"
    with SESSION_LOCK:
        cur = s.session_start(str(name), race_id, kind, time.time())
    return {"ok": True, "active": cur}


def end():
    s = _src()
    if s is None:
        return {"ok": False, "note": "race log is onboard-only"}
    cur = s.session_current()
    if not cur:
        return {"ok": False, "note": "no session running"}
    s.session_end(time.time())
    return {"ok": True, "ended": cur}


def status(limit=10):
    """{active: {...}|None, recent: [...]} — the dashboard REC control's read."""
    s = _src()
    if s is None:
        return {"available": False, "note": "race log is onboard-only"}
    auto = autoclose()      # throttled; the REC poll is what drives it
    from . import racegate
    out = {"available": True, "active": s.session_current(), "recent": s.sessions_list(limit),
           # the iPad pre-selects Race on LOG when shore homework is aboard
           "homework_aboard": racegate.homework_aboard()}
    if auto.get("closed"):
        # The crew must see that the boat ended their session, and why.
        out["auto_closed"] = {"name": auto["session"]["name"], "end_ts": auto["end_ts"],
                              "note": auto["note"]}
    return out
