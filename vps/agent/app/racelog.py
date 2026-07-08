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

import time
from datetime import datetime, timezone

from . import datasource
from . import deviation


def _src():
    s = datasource.active()
    if not hasattr(s, "session_start"):
        return None                    # cloud datasource — sessions are onboard-only
    return s


def start(name=None, race_id=None, kind=None):
    """Start a session. Everything is optional — one tap at the gun works: race_id defaults
    to the loaded playbook's (if any), kind to 'race' when there's a race_id else 'practice',
    name to '<Race|Sail> <UTC date HH:MM>'."""
    s = _src()
    if s is None:
        return {"ok": False, "note": "race log is onboard-only"}
    if race_id is None:
        try:
            race_id = (deviation._load_playbook() or {}).get("race_id")
        except Exception:
            race_id = None
    kind = (kind or ("race" if race_id else "practice")).lower()
    if not name:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        name = f"{'Race' if kind == 'race' else 'Sail'} {stamp}Z"
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
    return {"available": True, "active": s.session_current(),
            "recent": s.sessions_list(limit)}
