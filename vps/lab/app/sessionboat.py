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
    return True, rec


def for_debrief(body):
    """The boat a debrief of `body`'s session is for → (boat_id or None, reason or None).
    The Lab's edit wins, then what the recordings row sent (the marker's boat). Nothing at all
    (an uploaded GPX/YB track, an older client) → (None, None): the active boat, as before."""
    b = body or {}
    if b.get("start_ts") is not None and "kit" in b:
        eff = effective({"kit": b.get("kit"), "start_ts": b["start_ts"],
                         "boat_id": b.get("boat_id"), "boat_name": b.get("boat_name")})
        if not eff["has_profile"]:
            return None, OTHER_NO_PROFILE
        return eff["boat_id"], None
    if b.get("boat_id"):
        return (b["boat_id"], None) if boats.get_boat(b["boat_id"]) else (None, OTHER_NO_PROFILE)
    if b.get("boat_name"):
        return None, OTHER_NO_PROFILE
    return None, None
