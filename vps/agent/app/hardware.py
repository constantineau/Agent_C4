"""The hardware of the boat this engine serves (#313): from its profile, never another boat's kit.

The facts and their generic defaults are shared/boat_hardware.py. This module only finds the profile:

  * Aboard (`DATA_SOURCE=onboard`): the current boat (boatlib.current_boat: the session's boat,
    else the last used, else the Deploy push / library aboard). An "Other boat" (a name only) has
    no profile: generic defaults. A profile carried from a Lab older than #313 has no `hardware`
    block; the same boat's copy in the library aboard (or the push) that has one is used, then the
    Lab's bundled seed when a checkout carries it (the replay rig, tests). Never another boat's.
  * On the shore: the profile named by `BOAT_ID` (`knowledge/boats/<boat>.json` in the image, the
    Lab's bundled seed in a checkout).

Sensor priority: importing this module installs the current boat's as `source_policy`'s provider.
On the shore the cloud's priority is the `source_priority` table (datasource.load_priority), as
`/conditions` reads it; the profile's only when the database can't answer.
"""
import json
import os
import time

from shared import boat_hardware as BH
from shared import boat_polar as BP
from shared import source_policy

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROFILE_DIRS = (os.path.join(_HERE, "..", "knowledge", "boats"),
                 os.path.join(_HERE, "..", "..", "lab", "boats"))
TTL_S = 5.0                       # the priority is read on every instrument pick: keep it cheap
_MEMO = {"key": None, "at": 0.0, "val": None}


def _onboard():
    return os.environ.get("DATA_SOURCE", "").strip().lower() == "onboard"


def clear_cache():
    _MEMO.update(key=None, at=0.0, val=None)


def _bundled(bid):
    """Boat `bid`'s bundled profile file, or None (the Pi image carries none)."""
    if not bid or not BP.name_ok(f"{bid}.json"):
        return None
    for d in _PROFILE_DIRS:
        path = os.path.join(d, f"{bid}.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    p = json.load(f)
            except (OSError, ValueError):
                return None
            return p if isinstance(p, dict) and p.get("boat_id") == bid else None
    return None


def _with_hardware(bid, prof):
    """`prof`, or, when it carries no `hardware` block, the same boat's copy that does."""
    if isinstance(prof, dict) and "hardware" in prof:
        return prof
    cands = []
    if _onboard():
        try:
            from . import boatlib
            cands.append((boatlib.entries().get(bid) or {}).get("profile"))
            pushed = boatlib._pushed() or {}
            if pushed.get("boat_id") == bid:
                cands.append(pushed.get("profile"))
        except Exception:  # noqa: BLE001 — an unreadable store: the bundled seed, else none
            pass
    cands.append(_bundled(bid))
    for c in cands:
        if isinstance(c, dict) and c.get("boat_id") == bid and isinstance(c.get("hardware"), dict):
            return dict(prof or {}, hardware=c["hardware"]) if prof else c
    return prof if isinstance(prof, dict) else None


def profile_for(bid):
    """Boat `bid`'s profile with its hardware block, or None."""
    if not bid:
        return None
    prof = None
    if _onboard():
        try:
            from . import boatlib
            prof = boatlib.boat_for(bid).get("profile")
        except Exception:  # noqa: BLE001
            prof = None
    return _with_hardware(bid, prof)


def _current():
    """(boat_id, profile) of the boat being sailed. boat_id None = an Other boat / none known."""
    if _onboard():
        try:
            from . import boatlib
            cur = boatlib.current_boat() or {}
        except Exception:  # noqa: BLE001
            return None, None
        bid = cur.get("boat_id")
        return bid, (_with_hardware(bid, cur.get("profile")) if bid else None)
    bid = (os.environ.get("BOAT_ID") or "").strip() or None
    return bid, _with_hardware(bid, None) if bid else None


def _memo_key():
    if not _onboard():
        return ("shore", os.environ.get("BOAT_ID"))
    try:
        from . import boatlib
        return ("onboard", boatlib._key())
    except Exception:  # noqa: BLE001
        return ("onboard", None)


def profile():
    """The current boat's profile (with its hardware block when any copy has one), or None.
    Memoised for TTL_S and until the boat or the boat files change."""
    key, now = _memo_key(), time.monotonic()
    if _MEMO["key"] == key and now - _MEMO["at"] < TTL_S:
        return _MEMO["val"]
    _bid, prof = _current()
    _MEMO.update(key=key, at=now, val=prof)
    return prof


def source_priority():
    """The current boat's sensor priority {channel: [matchers]}, or None (none set).
    On the shore: the cloud's `source_priority` table when it answers, else the profile's."""
    if not _onboard():
        try:
            from . import datasource
            prio = datasource.load_priority()
            if prio:
                return prio
        except Exception:  # noqa: BLE001 — no database (a test, a bench): the profile's
            pass
    return BH.source_priority(profile())


def battery_lines():
    """The bank watch's lines for the current boat's bank (shared/boat_hardware.battery_lines)."""
    return BH.battery_lines(profile())


def plotter():
    """The current boat's chartplotter {present, model, max_route_waypoints}."""
    return BH.plotter(profile())


def has_orin(bid=None):
    """False only when the boat's profile (boat `bid`, else the current boat) says no Orin."""
    return BH.has_orin(profile_for(bid) if bid else profile())


source_policy.set_provider(source_priority)
