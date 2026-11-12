"""A boat's sail vocabulary — its own sail names, each with a family (#305).

The boat profile's `sail_inventory` lists the boat's sail names (["J1", "A3", ...]) and the
optional `sail_families` gives a family to a name ({"Heavy": "jib"}). Logic keys on the FAMILY;
screens and text use the boat's own NAMES.

Families: jib, genoa (the upwind headsails), code0, staysail, asym, sym (the kites). A name with
no family set gets one from the usual sailmaker shorthand (J1 → jib, G2 → genoa, A3 → asym,
S2 → sym, C0 → code0, SS → staysail). A name that fits none is "other": named, never guessed
into a slot.

Stdlib only (every service imports shared/).
"""
from __future__ import annotations

import re

FAMILIES = ("jib", "genoa", "code0", "staysail", "asym", "sym")
HEADSAILS = ("jib", "genoa")          # share the upwind slot: one is rated, the rest are change-downs
KITES = ("asym", "sym")
# which sail "drives" when several fly together (the kite over the jib over the staysail)
DRIVE_ORDER = ("asym", "sym", "code0", "jib", "genoa", "staysail")
# the family in a few plain words (prompts, sail lists)
WORDS = {"jib": "jib", "genoa": "genoa", "code0": "Code 0", "staysail": "staysail",
         "asym": "asymmetric", "sym": "symmetric kite"}

_SHORTHAND = (
    (re.compile(r"^(C0|CODE ?0|CODE ?ZERO)\b"), "code0"),
    (re.compile(r"^(SS|STAY)"), "staysail"),
    (re.compile(r"^J\d"), "jib"),
    (re.compile(r"^G\d"), "genoa"),
    (re.compile(r"^A\d"), "asym"),
    (re.compile(r"^S\d"), "sym"),
)


def token(text):
    """The sail name in free text: 'a3' → 'A3', 'A3-A' → 'A3', 'J2 (heavy)' → 'J2'. None if blank."""
    if not isinstance(text, str):
        return None
    t = re.split(r"[\s\-(/+,]", text.strip().upper(), maxsplit=1)[0]
    return t or None


def infer_family(name):
    """The family a sail name's shorthand implies, or None."""
    t = (name or "").strip().upper() if isinstance(name, str) else ""
    for rx, fam in _SHORTHAND:
        if rx.match(t):
            return fam
    return None


def inventory(src):
    """[{name, family}] from a profile / boat model dict (`sail_inventory` + `sail_families`) or a
    list of names. Names are upper-cased and unique, in the boat's order."""
    raw = src.get("sail_inventory") if isinstance(src, dict) else src
    fams = src.get("sail_families") if isinstance(src, dict) else None
    fams = {str(k).strip().upper(): v for k, v in fams.items()} if isinstance(fams, dict) else {}
    out, seen = [], set()
    for e in raw or ():
        if not isinstance(e, str):
            continue
        name = e.strip().upper()
        if not name or name in seen:
            continue
        seen.add(name)
        fam = fams.get(name)
        fam = fam if fam in FAMILIES else infer_family(name)
        out.append({"name": name, "family": fam or "other"})
    return out


def merge(*srcs):
    """ONE family resolver over several sources (frozen boat model, profile, sail config…): the
    first source that lists an inventory gives the names, and `sail_families` merge with the
    earlier source winning a clash. Pass the result as `src` to family()/match()/drive_rank()."""
    inv, fams = None, {}
    for src in reversed([s for s in srcs if isinstance(s, dict)]):
        f = src.get("sail_families")
        if isinstance(f, dict):
            fams.update({str(k).strip().upper(): v for k, v in f.items()})
    for src in srcs:
        if isinstance(src, dict) and src.get("sail_inventory"):
            inv = list(src["sail_inventory"])
            break
    return {"sail_inventory": inv or [], "sail_families": fams}


def names(src):
    return [s["name"] for s in inventory(src)]


def family(name, src=None):
    """A sail's family: the inventory's when it lists the sail, else its shorthand, else None."""
    n = (name or "").strip().upper() if isinstance(name, str) else ""
    if not n:
        return None
    fams = src.get("sail_families") if isinstance(src, dict) else None
    if isinstance(fams, dict):
        fam = {str(k).strip().upper(): v for k, v in fams.items()}.get(n)
        if fam in FAMILIES:
            return fam
    for s in inventory(src):
        if s["name"] == n:
            return s["family"] if s["family"] != "other" else None
    return infer_family(n)


def match(text, src=None, extra=()):
    """The boat's own sail name that `text` means ('a3' / 'A3 asym' / 'A3-A' → 'A3'), checked
    against the inventory (then `extra`, e.g. the speed guide's sails) by longest name first.
    A boat that lists no inventory: the text's sail token when its shorthand has a family.
    None when the boat has no such sail."""
    if not isinstance(text, str) or not text.strip():
        return None
    up = text.strip().upper()
    own = names(src)
    for n in sorted(set(own + [str(x).upper() for x in extra or () if x]), key=len, reverse=True):
        if up.startswith(n):
            return n
    if own:
        return None
    t = token(up)
    return t if infer_family(t) else None


def of_family(fams, src=None, extra=()):
    """The sails (inventory, then `extra`) in family/families `fams`, in order, unique."""
    fams = (fams,) if isinstance(fams, str) else tuple(fams)
    out = []
    for n in names(src) + [str(x).upper() for x in extra or () if x]:
        if n not in out and family(n, src) in fams:
            out.append(n)
    return out


def drive_rank(name, src=None):
    """Sort key: which of several flying sails is the driving one (kite first)."""
    fam = family(name, src)
    return (DRIVE_ORDER.index(fam) if fam in DRIVE_ORDER else len(DRIVE_ORDER), str(name))


def describe_list(sails, src=None):
    """'J1 jib, A3 asymmetric, S1 symmetric kite' — each sail with its family in plain words."""
    parts = []
    for n in sails or ():
        w = WORDS.get(family(n, src))
        parts.append(f"{n} {w}" if w and w != n else str(n))
    return ", ".join(parts)
