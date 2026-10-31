"""When does a Gameplan run route from? (issue #92)

A blank Start used to mean "now" on the server, said only in a hover tooltip — so homework run days
early routed on the wrong weather window. Now a blank Start means THE RACE START: our division's gun
(`own.division` in `division_starts`), else the earliest gun in the race. Only when there is no gun
time, or the start has already passed, does a blank Start route from now — and the reason travels
back with the result so the UI can say so in plain words.

One function decides it (`resolve`), used by the optimizer, the playbook synth/freeze routes and the
`/api/races/{rid}/start` endpoint the Gameplan screen reads to label the field.
"""
import time

from shared.race_def import _gun_epoch


def race_start(definition):
    """The race's start gun as {epoch, division, basis} — basis 'own_division' (our class's gun) or
    'earliest_gun' (no gun for our class, so the first gun of the day) — or None when the definition
    carries no usable gun time."""
    d = definition or {}
    tz = d.get("timezone") or ""
    guns = {}
    for k, v in (d.get("division_starts") or {}).items():
        e = _gun_epoch(v, tz)
        if e is not None:
            guns[str(k).strip()] = e
    if not guns:
        return None
    own = str((d.get("own") or {}).get("division") or "").strip()
    if own and own in guns:
        return {"epoch": guns[own], "division": own, "basis": "own_division"}
    div = min(guns, key=lambda k: guns[k])
    return {"epoch": guns[div], "division": div, "basis": "earliest_gun"}


def resolve(definition, start_epoch=None, now=None):
    """The start a run routes from, and why. Returns {epoch, source, race_start, note}:
    source 'picked' (the user typed a Start), 'race_start' (blank → the race's gun),
    'now_no_gun' (blank, no gun time in the definition) or 'now_passed' (blank, the gun has gone)."""
    now = time.time() if now is None else float(now)
    rs = race_start(definition)
    if start_epoch not in (None, "", 0):
        return {"epoch": float(start_epoch), "source": "picked", "race_start": rs,
                "note": "Routed from the start you picked."}
    if rs is None:
        return {"epoch": now, "source": "now_no_gun", "race_start": None,
                "note": "No gun time is set for this race, so it routed from now. "
                        "Set the gun times on the Fleet tab, or pick a Start."}
    if rs["epoch"] <= now:
        return {"epoch": now, "source": "now_passed", "race_start": rs,
                "note": f"The {rs['division']} start has passed, so it routed from now."}
    return {"epoch": rs["epoch"], "source": "race_start", "race_start": rs,
            "note": f"Routed from the {rs['division']} gun (the race start)."}
