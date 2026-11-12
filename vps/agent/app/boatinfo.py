"""The boat this agent serves: its profile and its speed guide (#305, #306). Never another boat's.

Aboard (`DATA_SOURCE=onboard`) it is the current boat (boatlib.current_boat: the session's boat,
else the last used, from the Deploy push or the boat library aboard). On the shore it is the
profile named by `BOAT_ID` (`knowledge/boats/<boat>.json` in the image, the Lab's bundled seed in
a checkout) and `knowledge/<boat>_speed_guide.md`. Nothing found → None / "": the text says
"the boat" and gives no speed guide.
"""
import json
import os

from shared import boat_polar as BP

_HERE = os.path.dirname(os.path.abspath(__file__))
_KNOWLEDGE = os.path.join(_HERE, "..", "knowledge")
_PROFILE_DIRS = (os.path.join(_KNOWLEDGE, "boats"), os.path.join(_HERE, "..", "..", "lab", "boats"))


def _onboard():
    return os.environ.get("DATA_SOURCE", "").strip().lower() == "onboard"


def _boat_id():
    bid = (os.environ.get("BOAT_ID") or "").strip()
    return bid if bid and BP.name_ok(f"{bid}.json") else None


def profile():
    """The boat's profile dict, or None."""
    if _onboard():
        try:
            from . import boatlib
            p = (boatlib.current_boat() or {}).get("profile")
        except Exception:
            p = None
        return p if isinstance(p, dict) else None
    return bundled(_boat_id())


def bundled(bid):
    """Boat `bid`'s bundled profile (`knowledge/boats/<bid>.json` in the agent image, the Lab's
    seed in a checkout), or None. Never another boat's: the file must name `bid`."""
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


def _current_boat():
    """(known, boat_id) of the boat being sailed. Aboard: the session's boat (an "Other boat" is
    known, with id None), else the last used or the deployed one; unknown only when nothing says.
    Shore: BOAT_ID."""
    if _onboard():
        try:
            from . import boatlib
            c = boatlib.current() or {}
        except Exception:
            return False, None
        if c.get("boat_id") is None and c.get("from") == "kit":
            return False, None                  # nothing aboard says which boat
        return True, c.get("boat_id")
    return True, _boat_id()


def frozen_for_current(boat_model):
    """The playbook's frozen boat model only when it is the boat being sailed (Codex on #334): a
    LOG session switched to Bravo with sr33's playbook still loaded must not get sr33's sails or
    thresholds. A model with no boat_id, or no known current boat, stands."""
    if not isinstance(boat_model, dict) or not boat_model.get("boat_id"):
        return boat_model
    known, bid = _current_boat()
    return None if known and bid != boat_model["boat_id"] else boat_model


def loa_m():
    """The boat's length overall in metres, or None."""
    try:
        v = float((profile() or {}).get("loa_m"))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _guide_table(guide):
    """A polar block's speed guide {"<tws>": [rows]} as compact markdown tables."""
    out = []
    for k in sorted(guide or {}, key=lambda x: float(x)):
        out.append(f"## TWS {k} kn\n\n| TWA | BTV | VMG | AWA | Heel | Sail |\n|--:|--:|--:|--:|--:|:--|")
        for r in guide[k] or ():
            cells = [r.get(c) for c in ("twa", "btv", "vmg", "awa", "heel")]
            out.append("| " + " | ".join("" if c is None else f"{c:g}" for c in cells)
                       + f" | {r.get('sail') or ''} |")
        out.append("")
    return "\n".join(out).strip()


def speed_guide():
    """The boat's speed guide as markdown, or "" when it has none."""
    if _onboard():
        try:
            from . import boatlib
            block = boatlib.current_polar()
        except Exception:
            block = None
        return _guide_table((block or {}).get("speed_guide")) if block else ""
    bid = _boat_id()
    if not bid:
        return ""
    path = os.path.join(_KNOWLEDGE, f"{bid}_speed_guide.md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""
