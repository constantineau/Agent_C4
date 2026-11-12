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


PLAYS_OFF = "Playbook is for {boat}. Plays off."
# what the Orin copilot needs of the session boat to name it and read its crew thresholds (#335)
_COPILOT_KEYS = ("boat_id", "name", "boat_class", "sail_number", "sail_inventory", "sail_families",
                 "jib_crossovers", "code0", "main_reefs", "draft_m", "loa_m")


def playbook_for_other_boat(boat_model):
    """Aboard: the playbook's boat id when another boat is being sailed (#335), else None.

    The same rule as frozen_for_current: a LOG session sailing Bravo with sr33's playbook loaded,
    or an "Other boat" (known, no id), is another boat. A model with no boat_id, an unknown current
    boat, or the shore (one boat per agent) is not."""
    if not _onboard() or not isinstance(boat_model, dict) or not boat_model.get("boat_id"):
        return None
    return None if frozen_for_current(boat_model) is not None else boat_model["boat_id"]


def boat_label(bid, boat_model=None):
    """A boat's name in a few words for the crew: the yacht's name, else its class, else its id."""
    from shared import boat_words
    p = None
    if _onboard():
        try:
            from . import boatlib
            p = boatlib.boat_for(bid).get("profile")
        except Exception:
            p = None
    for src in (p, boat_model):
        if isinstance(src, dict) and src.get("boat_id") in (None, bid):
            cls, name = boat_words.parts(src)
            if name or cls:
                return name or cls
    return str(bid)


def plays_off_note(boat_model):
    """"Playbook is for <boat>. Plays off." when the playbook is another boat's, else None."""
    bid = playbook_for_other_boat(boat_model)
    return PLAYS_OFF.format(boat=boat_label(bid, boat_model)) if bid else None


def copilot_boat():
    """The boat being sailed, for the Orin copilot (#335): {boat_id, name, profile} with only the
    profile bits it names the boat and reads crew thresholds from (profile None when the boat has
    none aboard, e.g. an "Other boat"). None when nothing aboard says which boat, or on the shore:
    the copilot then keeps the playbook's boat, as before."""
    if not _onboard():
        return None
    known, bid = _current_boat()
    if not known:
        return None
    try:
        from . import boatlib
        cur = boatlib.current_boat() or {}
    except Exception:
        cur = {}
    same = cur.get("boat_id") == bid
    prof = cur.get("profile") if same else None
    if isinstance(prof, dict) and bid and prof.get("boat_id") == bid:
        prof = {k: prof[k] for k in _COPILOT_KEYS if k in prof}
    else:
        prof = None
    return {"boat_id": bid, "name": cur.get("name") if same else None, "profile": prof}


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
