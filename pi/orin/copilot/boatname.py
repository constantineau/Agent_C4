"""The boat the copilot speaks for, in words (#306): from the playbook's boat model, never fixed.

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


def boat_model(pb=None) -> dict:
    """The boat model of `pb`, else of the playbook file aboard (approved or not: it only names
    the boat). {} when there is none."""
    if pb is None:
        try:
            pb = playbook_mod.load(confirm=False)
        except Exception:
            return {}
    try:
        return pb.boat_model or {}
    except Exception:
        return {}


def describe(pb=None) -> str:
    """'the SR33 racing yacht 'C4'' — or 'the boat' with no playbook."""
    return boat_words.describe(boat_model(pb))


def possessive(pb=None) -> str:
    """'the SR33's' — or 'the boat's' with no playbook."""
    return boat_words.possessive(boat_model(pb))
