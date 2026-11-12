"""The hardware of the boat this engine serves (#313): from its profile, never another boat's kit.

The facts and their generic defaults are shared/boat_hardware.py. This module only finds the profile:

  * Aboard (`DATA_SOURCE=onboard`): the current boat (boatlib.current: the session's boat, else
    the last used, else the Deploy push / library aboard). Of that boat's stored copies (the
    Deploy push, the library entry) the NEWEST is its kit, whether or not it carries a polar:
    boatlib.boat_for prefers a copy with a polar, which is right for the polar and wrong for the
    kit (a newer push saying "no Orin" must not lose to an older library entry). An "Other boat"
    (a name only) has no profile: generic defaults.
  * A stored copy from before #313 has no `hardware` block. Then the same boat's other stored
    copy that has one, else the hardware seed baked into the image for that boat id
    (`knowledge/boat_hardware/<boat>.json`, see shared/boat_hardware.seed_files), else the
    bundled profile (boatinfo.bundled: the agent image, a checkout). Never another boat's.
  * On the shore: the bundled profile named by `BOAT_ID` (boatinfo).

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

from . import boatinfo

_HERE = os.path.dirname(os.path.abspath(__file__))
SEED_DIRS = [os.path.join(_HERE, "..", "knowledge", "boat_hardware")]
TTL_S = 5.0                       # the priority is read on every instrument pick: keep it cheap
_MEMO = {"key": None, "at": 0.0, "val": None}


def _onboard():
    return os.environ.get("DATA_SOURCE", "").strip().lower() == "onboard"


def clear_cache():
    _MEMO.update(key=None, at=0.0, val=None)


def _seed(bid):
    """Boat `bid`'s hardware block from the image's seed, else its bundled profile, or None."""
    if not bid or not BP.name_ok(f"{bid}.json"):
        return None
    for d in SEED_DIRS:
        path = os.path.join(d, f"{bid}.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    s = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(s, dict) and s.get("boat_id") == bid and isinstance(s.get("hardware"), dict):
                return s["hardware"]
    b = boatinfo.bundled(bid)
    return b["hardware"] if b and isinstance(b.get("hardware"), dict) else None


def _stored(bid):
    """Boat `bid`'s stored copies aboard, newest first: [(at, profile)]."""
    out = []
    try:
        from . import boatlib
        pushed = boatlib._pushed() or {}
        if pushed.get("boat_id") == bid and isinstance(pushed.get("profile"), dict):
            out.append((float(pushed.get("at") or 0), 1, pushed["profile"]))
        lib = boatlib.entries().get(bid) or {}
        if isinstance(lib.get("profile"), dict):
            out.append((float(lib.get("got_at") or 0), 0, lib["profile"]))
    except Exception:  # noqa: BLE001 — an unreadable store: the seed, else none
        return []
    out.sort(key=lambda c: (c[0], c[1]), reverse=True)       # a tie goes to the Deploy push
    return [(at, p) for at, _k, p in out if p.get("boat_id") in (bid, None)]


def profile_for(bid):
    """Boat `bid`'s profile for its kit (see the module doc), or None."""
    if not bid:
        return None
    stored = _stored(bid) if _onboard() else []
    prof = stored[0][1] if stored else None
    if isinstance(prof, dict) and "hardware" in prof:
        return prof
    hw = next((p["hardware"] for _at, p in stored[1:] if isinstance(p.get("hardware"), dict)),
              None) or _seed(bid)
    if hw is None:
        return prof
    return dict(prof or {"boat_id": bid}, hardware=hw)


def _current_id():
    if _onboard():
        try:
            from . import boatlib
            return (boatlib.current() or {}).get("boat_id")
        except Exception:  # noqa: BLE001
            return None
    return boatinfo._boat_id()


def _memo_key():
    if not _onboard():
        return ("shore", os.environ.get("BOAT_ID"))
    try:
        from . import boatlib
        return ("onboard", boatlib._key())
    except Exception:  # noqa: BLE001
        return ("onboard", None)


def profile():
    """The current boat's profile for its kit, or None (an Other boat, none known). Memoised for
    TTL_S and until the boat or the boat files change."""
    key, now = _memo_key(), time.monotonic()
    if _MEMO["key"] == key and now - _MEMO["at"] < TTL_S:
        return _MEMO["val"]
    prof = profile_for(_current_id())
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
