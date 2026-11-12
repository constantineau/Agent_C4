"""The boat the copilot speaks for, in words (#306): from the playbook's boat model, never fixed;
the boat being sailed when the Pi says it is another boat than the playbook's (#335).

The words come from shared/boat_words.py. The copilot unit runs from pi/orin without the repo
root on its path, so it is added here when missing (the checkout is the deploy).
"""
import os
import sys

try:
    from shared import boat_words, sail_vocab
except ImportError:                       # systemd runs `python -m copilot.serve` from pi/orin
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
    from shared import boat_words, sail_vocab  # noqa: F401  (sail_vocab: briefs reads it here)

from . import playbook as playbook_mod


def _playbook_model(pb):
    if pb is None:
        try:
            pb = playbook_mod.load(confirm=False)
        except Exception:
            return {}
    try:
        return pb.boat_model or {}
    except Exception:
        return {}


def boat_model(pb=None) -> dict:
    """The boat the copilot speaks for: the boat model of `pb`, else of the playbook file aboard
    (approved or not: it only names the boat). {} when there is none.

    #335: when the Pi says which boat is being sailed and the playbook is another boat's (or there
    is none), it is the session boat's profile instead ({"name": ...} for an "Other boat" with no
    profile), never the playbook's boat. A Pi that doesn't say (older engine) keeps the playbook's."""
    bm = _playbook_model(pb)
    try:
        sb = playbook_mod.session_boat()
    except Exception:
        sb = None
    if not isinstance(sb, dict):
        return bm
    pid = bm.get("boat_id")
    if (pid and pid == sb.get("boat_id")) or (bm and not pid):
        return bm                     # the playbook's boat is the one sailed (or it doesn't say)
    prof = sb.get("profile")
    if isinstance(prof, dict) and prof:
        return prof
    return {"name": sb["name"]} if sb.get("name") else {}


def describe(pb=None) -> str:
    """'the SR33 racing yacht 'C4'' — or 'the boat' with no playbook."""
    return boat_words.describe(boat_model(pb))


def possessive(pb=None) -> str:
    """'the SR33's' — or 'the boat's' with no playbook."""
    return boat_words.possessive(boat_model(pb))
