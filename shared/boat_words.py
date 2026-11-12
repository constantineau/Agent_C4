"""How text names the boat (#306): from its profile or the playbook's boat model, never a fixed one.

A profile gives `name` and `boat_class` (and `sail_number`). A name written `Class "Yacht"` (the
seed's style) splits into the two when no class is given. No boat → neutral words ("the boat").

Stdlib only (every service imports shared/).
"""
from __future__ import annotations

import re

_QUOTED = re.compile(r"""^(.*?)\s*["“'‘](.+?)["”'’]\s*$""")


def parts(boat):
    """(class, yacht name) of a profile / boat model dict; either may be ''."""
    b = boat if isinstance(boat, dict) else {}
    cls = str(b.get("boat_class") or "").strip()
    name = str(b.get("name") or "").strip()
    m = _QUOTED.match(name)
    if m:
        pre, yacht = m.group(1).strip(), m.group(2).strip()
        if not cls:
            cls = pre
        elif pre and pre != cls:
            yacht = name.replace('"', "").replace("'", "")
        name = yacht
    if cls and name == cls:
        name = ""
    return cls, name


def describe(boat):
    """'the SR33 racing yacht 'C4'' / 'the racing yacht 'Bravo'' / 'the J/105' / 'the boat'."""
    cls, name = parts(boat)
    if name:
        return f"the {cls + ' ' if cls else ''}racing yacht '{name}'"
    return f"the {cls}" if cls else "the boat"


def possessive(boat):
    """'the SR33's' / 'Bravo's' / 'the boat's'."""
    cls, name = parts(boat)
    if cls:
        return f"the {cls}'s"
    return f"{name}'s" if name else "the boat's"


def short(boat):
    """'SR33' / 'Bravo' / '' — a few words to put before 'polars', 'Speed Guide', 'AI Navigator'."""
    cls, name = parts(boat)
    return cls or name
