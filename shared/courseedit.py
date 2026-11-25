"""Course revisions and the crew's course edits aboard (#406) — one implementation for the Pi,
the Orin and (later) the Lab.

A race committee can shorten the course, skip marks or change a rounding side while the boat is
offshore with no internet. The crew makes that change on the iPad; the Pi keeps it. Every change
makes a new **course revision**: a number that only ever goes up, a time, and where it came from
(`lab` = the Lab's Deploy push, `boat` = an edit made aboard). The revision rides in the
RaceDefinition as `course_revision` and every re-route result names the one it used.

A boat revision is the Lab's deployed course (its *base*) plus an ordered list of edits. The
course aboard is always rebuilt from the base and the whole list, never edited in place, so an
undo is just a shorter list.

Edits (each names its mark by `pos`, its place in `course["marks"]`, AND its `name`, so a
repeated mark of a lapped course is the right occurrence and a stale edit never lands on another
mark):
  * ``skip_to``   — the crew goes to this mark next. The course itself is unchanged; the Pi moves
                    the navigator's next mark.
  * ``finish_at`` — a shortened course: this mark becomes the finish (a gate becomes a finish
                    line). The marks after it go; the islands after it stay as rock with no side.
  * ``rounding``  — leave this mark (or island) to ``port``, ``starboard`` or ``none``.

None of these moves a point or adds a zone, so the land chart built for the base course still
covers the same water at the same depth (`same_geography`). Moving or adding marks and zones are
follow-ups; when they come, `same_geography` is what says the old land chart no longer fits.
"""
from __future__ import annotations

import copy
import json

ORIGINS = ("lab", "boat")
OPS = ("skip_to", "finish_at", "rounding")
SIDES = ("port", "starboard", "none")


class EditError(ValueError):
    """An edit that can't apply. The message is crew-facing: short and plain."""


def summary(rev):
    """{number, at, origin} of a revision dict, or None when it has no number."""
    if not isinstance(rev, dict):
        return None
    n = rev.get("number")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        return None
    origin = rev.get("origin") if rev.get("origin") in ORIGINS else None
    at = rev.get("at") if isinstance(rev.get("at"), (int, float)) and not isinstance(
        rev.get("at"), bool) else None
    return {"number": n, "at": at, "origin": origin}


def revision_errors(rev) -> list:
    """Shape problems in a `course_revision` block ({} = none stated, fine)."""
    if rev in (None, {}):
        return []
    if not isinstance(rev, dict):
        return ["course_revision is malformed (want {number, at, origin})"]
    errs = []
    if summary(rev) is None:
        errs.append("course_revision.number must be a whole number from 1 up")
    if rev.get("origin") not in ORIGINS:
        errs.append(f"course_revision.origin must be one of {', '.join(ORIGINS)}")
    edits = rev.get("edits", [])
    if not isinstance(edits, list):
        errs.append("course_revision.edits must be a list")
    else:
        for k, e in enumerate(edits):
            if not isinstance(e, dict) or e.get("op") not in OPS:
                errs.append(f"course_revision.edits[{k}]: unknown edit")
    return errs


def _course(definition, course_id):
    courses = (definition or {}).get("courses") or []
    return next((c for c in courses if c.get("id") == course_id), None) or \
        (courses[0] if courses else None)


def nav_indices(course):
    """For each course mark, its navigator index (`race_def.course_to_marks` order: Start is 0
    when it has a position; islands and unplaced marks get None), and the finish's index."""
    k = 1 if (course.get("start") or {}).get("lat") is not None else 0
    out = []
    for m in course.get("marks") or []:
        if m.get("type") == "island" or m.get("lat") is None:
            out.append(None)
        else:
            out.append(k)
            k += 1
    return out, k


def _target(course, edit):
    """The mark an edit names → (pos, mark). Checked by place AND name."""
    marks = course.get("marks") or []
    pos = edit.get("pos")
    if not isinstance(pos, int) or isinstance(pos, bool) or not 0 <= pos < len(marks):
        raise EditError("No such mark.")
    m = marks[pos]
    if str(m.get("name") or "") != str(edit.get("name") or ""):
        raise EditError("Course changed. Pick the mark again.")
    return pos, m


def _finish_at(course, pos, m):
    if m.get("type") == "island" or m.get("lat") is None:
        raise EditError("Can't finish at an island.")
    if m.get("type") == "gate" and m.get("lat2") is not None and m.get("lon2") is not None:
        pts = [{"name": m.get("name"), "lat": m["lat"], "lon": m["lon"]},
               {"name": m.get("name"), "lat": m["lat2"], "lon": m["lon2"]}]
    else:
        pts = [{"name": m.get("name"), "lat": m["lat"], "lon": m["lon"]}]
    rest = course.get("marks")[pos + 1:]
    # the islands after the new finish are still rock (their disks stay obstacles), but no side
    course["marks"] = course["marks"][:pos] + [dict(x, rounding="none") for x in rest
                                               if x.get("type") == "island"]
    course["finish"] = {"type": "shortened", "points": pts, "crossing": "",
                        "note": f"Shortened at {m.get('name')}.", "coords_source": "boat"}


def apply_edits(definition, course_id, edits):
    """The base definition with `edits` applied in order → a new definition (the base is never
    changed). Raises EditError (crew-facing) for an edit that can't apply."""
    d = copy.deepcopy(definition or {})
    course = _course(d, course_id)
    if not course:
        raise EditError("No course aboard to change.")
    for e in edits or []:
        if not isinstance(e, dict) or e.get("op") not in OPS:
            raise EditError("Unknown course change.")
        pos, m = _target(course, e)
        if e["op"] == "rounding":
            side = e.get("side")
            if side not in SIDES:
                raise EditError("Pick port, starboard or none.")
            if m.get("type") == "gate":
                raise EditError("A gate is sailed through. No side to change.")
            course["marks"][pos] = dict(m, rounding=side)
        elif e["op"] == "finish_at":
            _finish_at(course, pos, m)
        elif e["op"] == "skip_to":
            if m.get("type") == "island" or m.get("lat") is None:
                raise EditError("Can't sail to an island. Pick a mark.")
    return d


def nav_index_of(definition, course_id, pos):
    """Navigator index of course mark `pos` (None for an island or an unplaced mark)."""
    course = _course(definition, course_id)
    idx, _fin = nav_indices(course or {})
    return idx[pos] if course and 0 <= pos < len(idx) else None


def _points(course):
    """Every positioned point of a course: start, marks (both ends of a gate), finish points."""
    pts = set()
    st = course.get("start") or {}
    if st.get("lat") is not None:
        pts.add((round(float(st["lat"]), 6), round(float(st["lon"]), 6)))
    for m in course.get("marks") or []:
        for a, b in (("lat", "lon"), ("lat2", "lon2")):
            if m.get(a) is not None and m.get(b) is not None:
                pts.add((round(float(m[a]), 6), round(float(m[b]), 6)))
    fin = course.get("finish") if isinstance(course.get("finish"), dict) else {}
    for p in fin.get("points") or []:
        if isinstance(p, dict) and p.get("lat") is not None and p.get("lon") is not None:
            pts.add((round(float(p["lat"]), 6), round(float(p["lon"]), 6)))
    return pts


def _islands(course):
    return sorted(json.dumps({k: m.get(k) for k in ("name", "lat", "lon", "radius_nm")},
                             sort_keys=True)
                  for m in course.get("marks") or [] if m.get("type") == "island")


def same_geography(base, edited, course_id) -> bool:
    """True when `edited` covers no water the base course doesn't: the same zones, the same
    islands, the same start, and every point of it is a point of the base course. A land chart
    built for the base (land only, see pi/orin/reroute) then still fits the edited course: the
    edits only chose which of the base's marks are sailed, and on which side."""
    b, e = _course(base, course_id), _course(edited, course_id)
    if not b or not e or b.get("id") != e.get("id"):
        return False
    dump = lambda v: json.dumps(v or [], sort_keys=True)   # noqa: E731
    if dump((base or {}).get("zones")) != dump((edited or {}).get("zones")):
        return False
    if dump(b.get("start")) != dump(e.get("start")):
        return False
    if _islands(b) != _islands(e):
        return False
    return _points(e) <= _points(b)
