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

RECORDING and RACING are two states (#241, decided 2026-11-09). A session is recording: "keep this
window". Racing is "RRS 41 applies, no shore loading" (app/racegate.py). Starting a RACE session
(the gun tap) starts racing; only the crew's "Race over" tap ends it. Ending or auto-closing the
recording session never does. The boat only SUGGESTS "Race over?" once it has been stopped for a
while, and ends racing by itself only after RACE_OVER_FALLBACK_H (24 h) of unbroken stopped
readings. Racing is persisted in the engine store, so an engine restart mid-race keeps it.
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
#     samples, which must never be read as "parked". Only an unbroken run of stopped readings
#     (no hole longer than STILL_MAX_GAP_S) up to now counts; a gap restarts the clock (#241).
#   * if no under-way moment can be found in the look-back at all, it does NOT close. Picking an
#     end for a session that has been open for weeks would put those weeks on the deletion path,
#     which is the exact failure this is meant to prevent. A human closes that one.
AUTOCLOSE_IDLE_H = float(os.environ.get("RACELOG_AUTOCLOSE_IDLE_H", "12"))
AUTOCLOSE_LOOKBACK_H = float(os.environ.get("RACELOG_AUTOCLOSE_LOOKBACK_H", "48"))
# The longest hole between two speed readings that still counts as one unbroken stopped stretch.
# The archive keeps up to one reading a second while the bus is up, so a longer hole is an outage.
STILL_MAX_GAP_S = float(os.environ.get("RACELOG_STILL_MAX_GAP_S", "300"))
AUTOCLOSE_CHECK_EVERY_S = float(os.environ.get("RACELOG_AUTOCLOSE_CHECK_EVERY_S", "600"))
_MS_TO_KN = 1.943844
_last_check = [0.0]


def still_since(rows, now, after=None, max_gap_s=None):
    """Start of the unbroken stretch of stopped readings that runs up to `now`, or None.

    rows: [(epoch_s, kn)] in any order. None when the newest reading is under way or older than
    the max gap (no evidence about now). The stretch ends going back at the first under-way
    reading, at the first hole longer than the max gap, or at `after` (readings before it don't
    count). Holes never add to it."""
    gap = STILL_MAX_GAP_S if max_gap_s is None else float(max_gap_s)
    pts = sorted((float(t), float(v)) for t, v in rows if float(t) <= now)
    if not pts or now - pts[-1][0] > gap:
        return None
    start = prev = None
    for t, kn in reversed(pts):
        if (after is not None and t < after) or kn >= race_window.UNDERWAY_KN:
            break
        if prev is not None and prev - t > gap:
            break
        start = prev = t
    return start


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
        # Only an UNBROKEN run of stopped readings counts: a hole in the record is not
        # stillness (a dead bus, a crashed archiver, a powered-down boat), so it restarts the clock.
        still = still_since(rows, now)
        if still is None or now - still < AUTOCLOSE_IDLE_H * 3600.0:
            return {"closed": False,
                    "note": f"not stopped for {AUTOCLOSE_IDLE_H:.0f} h without a gap, not closing"}
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
        # yesterday's last under-way moment, a negative or wrong window. So:
        # under the session lock, re-check that the same session is still the open one and that
        # the end is after its start, and close only that id.
        with SESSION_LOCK:
            _settle_race_state(s)
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


def _settle_race_state(s):
    """Write down the racing state BEFORE any session is closed or replaced. A boat upgraded with
    a race session open has no racing state yet; it is read from that session (race_state), so
    the session must still be open when that happens. Call under SESSION_LOCK. Raises if the
    state can't be read or written: the caller must then NOT close the session (closing it would
    lose the only record that the boat is racing)."""
    if hasattr(s, "race_state"):
        race_state(s)


def start(name=None, race_id=None, kind=None):
    """Start a session. Everything is optional: race_id defaults to the loaded playbook's (if
    any), name to '<Race|Sail> <UTC date HH:MM>'. kind is race|practice; with no kind it is a
    RACE (fails closed: a race session is what makes the boat refuse shore homework). The iPad
    always sends the kind the crew picked. Kind and race id are resolved under SESSION_LOCK, with
    the start itself."""
    s = _src()
    if s is None:
        return {"ok": False, "note": "race log is onboard-only"}
    kind = str(kind or "race").lower()
    if kind not in ("race", "practice"):
        return {"ok": False, "note": "kind must be race or practice"}
    with SESSION_LOCK:
        if race_id is None:
            try:
                race_id = (deviation._load_playbook() or {}).get("race_id")
            except Exception:
                race_id = None
        if not name:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            name = f"{'Race' if kind == 'race' else 'Sail'} {stamp}Z"
        _settle_race_state(s)          # session_start closes the open one
        ts = time.time()
        if kind == "race" and hasattr(s, "save_race_state"):
            # racing first: if the session write then fails, the boat is racing with no session
            # (fails closed), never the other way round
            s.save_race_state({"racing": True, "since": ts, "name": str(name),
                               "race_id": race_id})
        cur = s.session_start(str(name), race_id, kind, ts)
    return {"ok": True, "active": cur}


ALREADY_ENDED = "Already ended"


def end(session_id=None):
    """End the running session. `session_id` is the one the caller means (the iPad sends the id it
    shows); when another session is running now, nothing is ended: "Already ended". Read and
    close happen under SESSION_LOCK and the close is scoped to that one id, so an end can never
    close a race that started in between."""
    s = _src()
    if s is None:
        return {"ok": False, "note": "race log is onboard-only"}
    with SESSION_LOCK:
        _settle_race_state(s)
        cur = s.session_current()
        if not cur:
            return {"ok": False, "note": "no session running"}
        if session_id is not None and str(session_id) != str(cur.get("id")):
            return {"ok": False, "note": ALREADY_ENDED}
        s.session_end(time.time(), session_id=cur["id"])
    return {"ok": True, "ended": cur}


# ---- RACING (RRS 41): its own state, ended by the crew --------------------------------------
# A long calm, a sensor outage or a forgotten tap must never reopen shore loading mid-race. So:
#   * racing starts with a RACE session (the gun tap) and is persisted (engine restart keeps it);
#   * the crew ends it: "Race over" on the iPad (also for an abandoned race);
#   * after RACE_OVER_SUGGEST_H of unbroken stopped readings the iPad asks "Race over?";
#   * after RACE_OVER_FALLBACK_H (24 h) of unbroken stopped readings the boat ends it itself, so
#     a forgotten tap can't block next week's homework forever. Holes in the record never count.
RACE_OVER_SUGGEST_H = float(os.environ.get("RACE_OVER_SUGGEST_H", "1"))
RACE_OVER_FALLBACK_H = float(os.environ.get("RACE_OVER_FALLBACK_H", "24"))
RACE_OVER_CHECK_EVERY_S = float(os.environ.get("RACE_OVER_CHECK_EVERY_S", "300"))
NOT_RACING = "Not racing"
_race_check = {"t": 0.0, "since": None, "still_h": 0.0}


def race_state(s=None):
    """The racing state {racing, since, name, race_id, ...}. Raises if it can't be read: callers
    fail closed. A store without a racing state (test stubs) keeps the old rule: an open race
    session is racing. A boat upgraded from the one-switch log reads its open race session once,
    and that is written down (only if nothing is there yet), so a later recording close can't
    drop it."""
    s = _src() if s is None else s
    if s is None:
        return {"racing": False, "since": None}
    if not hasattr(s, "race_state"):
        cur = s.session_current()
        if cur and str(cur.get("kind") or "").lower() == "race":
            return {"racing": True, "since": cur.get("start_ts"), "name": cur.get("name"),
                    "race_id": cur.get("race_id")}
        return {"racing": False, "since": None}
    st = s.race_state()
    if st is None:
        cur = s.session_current()
        racing = bool(cur and str(cur.get("kind") or "").lower() == "race")
        st = ({"racing": True, "since": cur.get("start_ts"), "name": cur.get("name"),
               "race_id": cur.get("race_id")} if racing else {"racing": False, "since": None})
        s.save_race_state(st, only_if_absent=True)
        st = s.race_state() or st
    return st


def _same_race(a, b):
    try:
        return abs(float(a) - float(b)) < 1e-3
    except (TypeError, ValueError):
        return False


def race_over(since=None, by="crew", now=None):
    """End racing (shore loading may open again). `since` is the race start the iPad shows: if a
    newer race is running now, nothing is ended ("Already ended"). The recording session is left
    alone: it is a separate switch (LOG)."""
    s = _src()
    if s is None:
        return {"ok": False, "note": "race log is onboard-only"}
    if not hasattr(s, "save_race_state"):
        return {"ok": False, "note": "racing state not available"}
    now = time.time() if now is None else float(now)
    with SESSION_LOCK:
        st = race_state(s)
        if not st.get("racing"):
            return {"ok": False, "note": NOT_RACING}
        if since is not None and not _same_race(since, st.get("since")):
            return {"ok": False, "note": ALREADY_ENDED}
        s.save_race_state(dict(st, racing=False, ended_ts=now, ended_by=by))
    if by != "crew":
        print(f"[racelog] racing ended by the boat ({by}): '{st.get('name')}'", flush=True)
    return {"ok": True, "ended": st}


def race_status(now=None, force=False):
    """The iPad's view of racing: {racing, since, name, suggest_over, still_h, ...}. Runs the
    throttled stopped-check, and the 24 h fallback. Never raises: an unreadable state reads as
    racing (fail closed)."""
    now = time.time() if now is None else float(now)
    s = _src()
    try:
        st = race_state(s)
    except Exception as exc:  # noqa: BLE001
        return {"racing": True, "since": None, "suggest_over": False,
                "note": f"racing state unreadable ({type(exc).__name__})"}
    out = {"racing": bool(st.get("racing")), "since": st.get("since"), "name": st.get("name"),
           "suggest_over": False}
    if not out["racing"]:
        if st.get("ended_ts"):
            out["ended_ts"], out["ended_by"] = st.get("ended_ts"), st.get("ended_by")
        return out
    since = st.get("since")
    fresh = _race_check["since"] is not None and _same_race(_race_check["since"], since)
    if force or not fresh or now - _race_check["t"] >= RACE_OVER_CHECK_EVERY_S:
        still_h = 0.0
        try:
            look_h = RACE_OVER_FALLBACK_H + 1.0
            if since is not None:
                look_h = min(look_h, max(0.0, (now - float(since)) / 3600.0) + 0.1)
            rows = [(t, v * _MS_TO_KN) for t, v in
                    (s.series("navigation.speedOverGround", look_h * 60.0) or ())]
            st_ts = still_since(rows, now, after=float(since) if since is not None else None)
            still_h = (now - st_ts) / 3600.0 if st_ts is not None else 0.0
        except Exception:  # noqa: BLE001
            still_h = 0.0          # no evidence: never a reason to end a race
        _race_check.update(t=now, since=since, still_h=still_h)
    still_h = _race_check["still_h"]
    out["still_h"] = round(still_h, 2)
    if RACE_OVER_FALLBACK_H > 0 and still_h >= RACE_OVER_FALLBACK_H:
        r = race_over(since=since, by="auto", now=now)
        if r.get("ok"):
            return {"racing": False, "since": since, "name": st.get("name"),
                    "suggest_over": False, "ended_ts": now, "ended_by": "auto"}
    out["suggest_over"] = RACE_OVER_SUGGEST_H > 0 and still_h >= RACE_OVER_SUGGEST_H
    return out


def status(limit=10):
    """{active: {...}|None, recent: [...]} — the dashboard REC control's read."""
    s = _src()
    if s is None:
        return {"available": False, "note": "race log is onboard-only"}
    auto = autoclose()      # throttled; the REC poll is what drives it
    from . import racegate
    out = {"available": True, "active": s.session_current(), "recent": s.sessions_list(limit),
           # UI only: the iPad pre-selects Race on LOG when Deploy homework is in the stores
           "homework_aboard": racegate.homework_aboard(),
           # racing (RRS 41) is its own state: the iPad's "Race over" button reads this
           "race": race_status()}
    if auto.get("closed"):
        # The crew must see that the boat ended their session, and why.
        out["auto_closed"] = {"name": auto["session"]["name"], "end_ts": auto["end_ts"],
                              "note": auto["note"]}
    return out
