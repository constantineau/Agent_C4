"""Which boat a recorded session was sailed on (#320).

The boat's rows are tagged by the KIT that recorded them (the Pi's BOAT_ID), and stay that way:
no archive rewrite. The SESSION names the boat sailed: the crew picks it on the iPad at LOG
(`boat_id` from the boat library, or only `boat_name` for an "Other boat"), and the session marker
carries it to the cloud. A wrong pick, or an "Other boat" that now has a profile (an ORC cert
imported later), is fixed here: the Lab keeps its own edit of a session's boat, which wins over
the marker. Older markers (before #320) name no boat: they were sailed on the kit's boat.

The debrief, the recordings list and learning file a session under this boat, never the kit id.
"""
from __future__ import annotations

import threading
import time

from . import boats, labstate

# Held across "change a session's boat + withdraw its old debriefs" and across a debrief job's
# "is this still the session's boat? + archive". Without it a debrief running while the boat is
# changed would file fresh learning under the old boat after the withdrawal (Codex on #321).
ARCHIVE_LOCK = threading.RLock()
BOAT_CHANGED = "This session's boat changed while it ran. Not filed. Debrief it again."

KEY = "session_boats"
REV_KEY = "session_boats_rev"
NAME_MAX = 40
OTHER_NO_PROFILE = "This session's boat has no profile. Set its boat first."


def key_of(kit, start_ts) -> str:
    """One recorded session: the kit that recorded it + the LOG button's start (the marker)."""
    return "%s@%.3f" % (kit or "", float(start_ts))


def clean_name(raw):
    s = "".join(ch for ch in str(raw or "") if ch.isprintable()).strip()[:NAME_MAX].strip()
    return s or None


def _name(bid):
    b = boats.get_boat(bid) if bid else None
    return (b or {}).get("name") or bid


def effective(ses) -> dict:
    """{boat_id, name, source: lab|boat|kit|active, has_profile} for a session (/racelog/sessions).
    boat_id None = an "Other boat" (a name only)."""
    ses = ses or {}
    edits = labstate.get(KEY) or {}
    e = edits.get(key_of(ses.get("kit"), ses["start_ts"])) if ses.get("start_ts") is not None \
        else None
    if e:
        bid, name, src = e.get("boat_id"), e.get("name"), "lab"
    elif ses.get("boat_id") or ses.get("boat_name"):
        bid, name, src = ses.get("boat_id"), ses.get("boat_name"), "boat"
    elif ses.get("kit"):
        bid, name, src = ses.get("kit"), None, "kit"
    else:
        # an agent from before #320 says neither: the Lab's active boat, as before
        bid, name, src = (boats.active_boat() or {}).get("boat_id"), None, "active"
    has = bool(bid and boats.get_boat(bid))
    return {"boat_id": bid, "name": name or _name(bid) or "Unknown boat", "source": src,
            "has_profile": has}


def set_boat(kit, start_ts, boat_id=None, boat_name=None, now=None):
    """The Lab's edit of a session's boat → (ok, result-or-reason). `boat_id` must be a Lab
    profile; a name alone makes it an "Other boat" (it can't be debriefed until it has one)."""
    try:
        k = key_of(kit, start_ts)
    except (TypeError, ValueError):
        return False, "Which session?"
    bid = str(boat_id).strip() if isinstance(boat_id, str) and boat_id.strip() else None
    name = clean_name(boat_name)
    if bid:
        if not boats.get_boat(bid):
            return False, "Unknown boat."
        name = _name(bid)
    elif not name:
        return False, "Pick a boat or type a name."
    rec = {"boat_id": bid, "name": name, "at": int(time.time() if now is None else now)}
    labstate.set_in(KEY, k, rec)
    labstate.set(REV_KEY, revision() + 1)
    return True, rec


def revision() -> int:
    """Bumped on every session-boat edit. A debrief notes it before it fetches its track and files
    nothing if it moved: an edit of ANOTHER session stitched into this one's window changes what
    the window holds, though this session's own boat is unchanged (Codex on #321)."""
    try:
        return int(labstate.get(REV_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def sessions_path() -> str:
    """The agent's /racelog/sessions, carrying the Lab's boat edits so the boat log derives each
    window per boat as edited (two sessions tagged one boat on the iPad and split here must not
    stay stitched into one window; Codex on #321). Boat key: the profile id, or "name:<name>"."""
    import json
    import urllib.parse
    edits = labstate.get(KEY) or {}
    m = {k: (e.get("boat_id") or "name:" + str(e.get("name") or "")) for k, e in edits.items()
         if isinstance(e, dict)}
    if not m:
        return "/racelog/sessions"
    return "/racelog/sessions?boats=" + urllib.parse.quote(json.dumps(m, separators=(",", ":")))

