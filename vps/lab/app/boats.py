"""Boat library — load/save BoatProfile instances + track the active boat.

Mirrors `store.py` (the race library): bundled profiles ship in the image (`/srv/boats`); edited /
new ones land on the writable `lab_ingested` volume (`/srv/ingested/boats`) and take PRECEDENCE over a
bundled seed of the same `boat_id`, so draft edits persist. The **active boat** id is held in
`labstate` (Lab-wide). The active boat's draft sets the optimizer's ENC depth no-go.
"""
import glob
import json
import os

from shared import boat_profile
from . import labstate

BUNDLED_DIR = os.environ.get("BOATS_DIR", "/srv/boats")
EDITED_DIR = os.path.join(os.environ.get("INGESTED_DIR", "/srv/ingested"), "boats")
ACTIVE_KEY = "active_boat"


def _files():
    out = []
    for d in (EDITED_DIR, BUNDLED_DIR):          # edited first → overrides the bundled seed
        if os.path.isdir(d):
            out += sorted(glob.glob(os.path.join(d, "*.json")))
    return out


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_boats():
    """Selector/list summaries (one per boat_id, edited copy winning), with validation counts."""
    out, seen = [], set()
    for f in _files():
        d = _load(f)
        if not d:
            continue
        bid = d.get("boat_id") or os.path.splitext(os.path.basename(f))[0]
        if bid in seen:
            continue
        seen.add(bid)
        errs, warns = boat_profile.validate(d)
        s = boat_profile.summary(d)
        s["errors"] = len(errs)
        s["warnings"] = len(warns)
        out.append(s)
    return out


def get_boat(bid):
    for f in _files():
        d = _load(f)
        if d and (d.get("boat_id") or os.path.splitext(os.path.basename(f))[0]) == bid:
            return d
    return None


def save_boat(d: dict) -> dict:
    """Persist a (reviewed/edited) profile to the writable volume. Returns the stored dict."""
    bid = d.get("boat_id")
    if not bid:
        raise ValueError("boat_id required")
    d.setdefault("schema_version", boat_profile.SCHEMA_VERSION)
    os.makedirs(EDITED_DIR, exist_ok=True)
    path = os.path.join(EDITED_DIR, f"{bid}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)
    return d


def active_id():
    """The selected boat id — the stored one if it still exists, else the first available, else None."""
    bid = labstate.get(ACTIVE_KEY)
    if bid and get_boat(bid):
        return bid
    boats = list_boats()
    return boats[0]["boat_id"] if boats else None


def set_active(bid):
    if not get_boat(bid):
        raise ValueError(f"unknown boat_id {bid}")
    labstate.set(ACTIVE_KEY, bid)
    return bid


def active_boat():
    """The active BoatProfile dict (or None)."""
    bid = active_id()
    return get_boat(bid) if bid else None


def active_safety_depth_m(default=2.63):
    """No-go depth (draft + margin) for the active boat; `default` if no boat/draft is set."""
    b = active_boat()
    if b and b.get("draft_m") is not None:
        return boat_profile.safety_depth_m(b)
    return default
