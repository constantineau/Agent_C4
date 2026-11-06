"""Which course positions are missing: one check for the playbook build and Deploy (#151).

`shared.race_def.course_to_marks` (shared with the boat) drops a mark with no position and an
incomplete finish, and reports them as skipped. Two more gaps pass it silently: a gate with only
one end set becomes a waypoint at that end (the route goes TO it, not through the gate), and a
course with no finish at all ends at its last mark. Both are missing positions here.
"""
from __future__ import annotations

from shared import race_def


def _course(definition, course_id):
    courses = (definition or {}).get("courses") or []
    return next((c for c in courses if c.get("id") == course_id), None) or \
        (courses[0] if courses else None)


def _has(m, a, b):
    return m.get(a) is not None and m.get(b) is not None


def missing(definition, course_id=None):
    """Names of the course's missing positions, in plain words ([] when complete or no course)."""
    course = _course(definition, course_id)
    if not course:
        return []
    _marks, out, _cid = race_def.course_to_marks(definition, course_id)
    out = list(out)
    for m in course.get("marks") or []:
        if m.get("type") == "gate" and _has(m, "lat", "lon") != _has(m, "lat2", "lon2") \
                and m.get("lat") is not None:
            out.append(f"{m.get('name', 'gate')} (one end)")
    if course.get("finish") is None:
        out.append("Finish")
    return out
