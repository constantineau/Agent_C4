"""Course edits aboard (#406) — the Pi engine's side.

The race committee shortens the course, skips a mark or changes a rounding side, and the boat
has no internet. The crew makes the change on the iPad (`POST /course/edit`); it is kept here,
on the Pi, next to the Lab's deployed course (vps/agent/app/orinroute.py's course record), and
the next re-route uses it. Every change is a new course revision (shared/courseedit.py): a
number, a time, "boat". `GET /course/revision` says which revision is aboard and lists the marks
the iPad can pick.

The course aboard is always rebuilt from the Lab's base and the whole list of edits. A shortened
course also rewrites the navigator's marks (the finish moves) and keeps the run where it is.

Not shore homework: a crew decision aboard, allowed while racing (the race gate guards what
comes from the shore, `app.racegate`). Sync back to the Lab, and moving or adding marks and
zones, are follow-ups.
"""
import time

from shared import courseedit
from shared.race_def import course_to_marks

from . import orinroute
from . import navigator as NAV

NO_COURSE = "No course aboard to change."
NOTHING = "No course change to undo."
BEHIND = "That mark is behind you."
MAX_EDITS = 50
MOVED_ON = "Boat has moved on. Next mark not changed."
NEXT_KEPT = "Next mark not changed."


def _device(raw):
    s = "".join(ch for ch in str(raw or "") if ch.isprintable()).strip()[:40]
    return s or None


def _current():
    """(route, navigator marks, course record) for the course aboard, or (route, marks, None)."""
    route = NAV.active_route()
    marks = NAV._marks(route) or []
    rec = orinroute.record_for(route, marks) if marks else None
    return route, marks, rec


def _marks_list(definition, course_id):
    """The course marks the iPad can pick: [{pos, name, type, rounding, index}]. `index` is the
    navigator's (None for an island)."""
    course = next((c for c in definition.get("courses") or [] if c.get("id") == course_id), None) \
        or ((definition.get("courses") or [None])[0])
    if not course:
        return []
    idx, _fin = courseedit.nav_indices(course)
    return [{"pos": p, "name": m.get("name"), "type": m.get("type"),
             "rounding": m.get("rounding", "none"), "index": idx[p]}
            for p, m in enumerate(course.get("marks") or [])]


def _view(route, rec):
    d = rec["definition"]
    cid = rec.get("course_id")
    fin = next((c for c in d.get("courses") or [] if c.get("id") == cid), {}).get("finish") or {}
    return {"available": True, "route": route, "course_id": cid,
            "revision": courseedit.summary(rec.get("revision")),
            "base": (rec.get("revision") or {}).get("base"),
            "edits": [{k: e.get(k) for k in ("op", "pos", "name", "side", "at", "by", "number")}
                      for e in rec.get("edits") or []],
            "shortened": fin.get("type") == "shortened",
            "marks": _marks_list(d, cid)}


def get():
    """GET /course/revision: the revision aboard, the edits made aboard, the marks to pick."""
    with orinroute.COURSE_LOCK:
        route, _marks, rec = _current()
        if rec is None:
            return {"available": False, "note": NO_COURSE}
        return _view(route, rec)


def _flat(definition, course_id):
    return [tuple(m) for m in course_to_marks(definition, course_id)[0]]


def _nav_next():
    """The navigator's next mark index, or None."""
    try:
        nm = (NAV.get_navigator() or {}).get("next_mark") or {}
    except Exception:  # noqa: BLE001
        return None
    i = nm.get("index")
    return i if isinstance(i, int) and not isinstance(i, bool) else None


def edit(body, after_change=None):
    """POST /course/edit → {ok, note, revision, ...}.

    body: {op: "skip_to"|"finish_at"|"rounding"|"undo", pos, name, side?, device?}.
    `after_change()` runs (still under the course lock) when the course itself changed, so the
    engine can stop a plotter route made for the old one."""
    b = body if isinstance(body, dict) else {}
    op = b.get("op")
    if op not in courseedit.OPS + ("undo",):
        return {"ok": False, "note": "Unknown course change."}
    with orinroute.COURSE_LOCK:
        route, marks, rec = _current()
        if rec is None:
            return {"ok": False, "note": NO_COURSE}
        cid = rec.get("course_id")
        base = orinroute.base_definition(rec) or rec["definition"]
        edits = list(rec.get("edits") or [])
        undone = None
        if op == "undo":
            if not edits:
                return {"ok": False, "note": NOTHING}
            undone = edits.pop()
            new_edit = None
        else:
            if len(edits) >= MAX_EDITS:
                return {"ok": False, "note": "Too many changes. Redeploy the course."}
            new_edit = {"op": op, "pos": b.get("pos"), "name": b.get("name")}
            if op == "rounding":
                new_edit["side"] = b.get("side")
            edits.append(new_edit)
        try:
            new_def = courseedit.apply_edits(base, cid, edits)
        except courseedit.EditError as exc:
            return {"ok": False, "note": str(exc)}
        nxt = _nav_next()
        target = None
        if new_edit and op in ("skip_to", "finish_at"):
            # the place names a mark of the course aboard NOW (edits apply in turn), so its
            # navigator index comes from that course, before this edit
            target = courseedit.nav_index_of(rec["definition"], cid, new_edit["pos"])
            if op == "finish_at" and nxt is not None and target is not None and target < nxt:
                return {"ok": False, "note": BEHIND}
        if op == "skip_to" and new_edit:
            # where the navigator was, so an undo of the skip can put it back
            new_edit.update(prev_index=nxt, to_index=target)
        now = int(time.time())
        old_flat = [(m.get("seq"), m.get("name"), m.get("lat"), m.get("lon")) for m in marks]
        new_flat = _flat(new_def, cid)
        if len(new_flat) < 2:
            return {"ok": False, "note": "Course too short."}
        # every revision is a new course for the routes, a skip and its undo too: a route made
        # through the skipped marks (or from before an undo) must never look current
        course_changed = True
        # the navigator change that goes with this edit (a skip, or the undo of one), decided now
        # so it rides in the journal and a power cut can't separate it from the revision
        nav_to, nav_note = None, None
        if op == "skip_to" and target is not None:
            nav_to = target
        elif undone and undone.get("op") == "skip_to":
            nav_to, nav_note = _unskip_target(undone)
        kept_run = True
        journal = False
        snap = NAV.progress_snapshot(route)
        orinroute._RECOVERING.append(1)      # this edit's own journal: not for recovery now
        try:
            number = orinroute.next_revision()
            if new_edit:
                new_edit.update(at=now, by=_device(b.get("device")), number=number)
            revision = {"number": number, "at": now, "origin": "boat",
                        "base": (rec.get("revision") or {}).get("base")}
            new_def["course_revision"] = dict(revision, edits=edits)
            out = dict(rec, definition=new_def, edits=edits, revision=revision,
                       base_definition=base if edits else None, saved_at=now,
                       marks_hash=orinroute._marks_hash([(n, a, o) for _s, n, a, o in new_flat]))
            if course_changed:
                # a new course: every route made before counts as an old one (orinroute)
                out["load_id"] = orinroute.new_load_id()
            # both records on disk first: a power cut in the steps below is put right on the next
            # read (orinroute.recover)
            orinroute.begin_edit(route, rec, out, nav_index=nav_to, progress=snap)
            journal = True
            if new_flat != old_flat:
                kept_run = NAV.replace_marks(route, new_flat)
            if course_changed:
                orinroute.set_load_id(route, out["load_id"])
            orinroute.write_record(out)
            picked = NAV.pick_mark(nav_to, route) if nav_to is not None else None
            orinroute.end_edit()
        except Exception as exc:  # noqa: BLE001
            print(f"[course/edit] failed: {type(exc).__name__}: {exc}", flush=True)
            # the marks may be the new ones now: put the old ones back, so the old record still
            # pairs with them (by its marks hash and load id)
            if journal:
                try:
                    if new_flat != old_flat:
                        NAV.replace_marks(route, old_flat)
                    # the run exactly as it was (new marks clamp it; a pick may have moved it)
                    NAV.restore_progress(route, snap, force=True)
                    orinroute.set_load_id(route, rec.get("load_id"))
                    orinroute.write_record(rec)
                    orinroute.end_edit()
                except Exception:  # noqa: BLE001
                    # can't put it back now: the journal stays, the next read puts it right
                    pass
            return {"ok": False, "note": "Couldn't save the change. Try again."}
        finally:
            orinroute._RECOVERING.clear()
        if course_changed and after_change is not None:
            try:
                after_change()
            except Exception as exc:  # noqa: BLE001
                print(f"[course/edit] after change: {type(exc).__name__}: {exc}", flush=True)
        res = {"ok": True, "revision": courseedit.summary(revision),
               "note": f"Course rev {number} saved."}
        if picked is not None:
            res["note"] = f"Course rev {number} saved. Next mark: {picked.get('next_mark')}." \
                if picked.get("ok") else f"Course rev {number} saved. {picked.get('note') or ''}".strip()
        elif nav_note:
            res["note"] = f"Course rev {number} saved. {nav_note}"
        elif not kept_run:
            res["note"] = f"Course rev {number} saved. Pick the next mark again."
        return res


def _unskip_target(skip):
    """Undo of a skip → (navigator index to go back to, None) or (None, why not): back to the mark
    it was on before the skip, unless the boat has moved on from the skipped-to mark since."""
    prev, to = skip.get("prev_index"), skip.get("to_index")
    if not isinstance(prev, int) or isinstance(prev, bool):
        return None, NEXT_KEPT
    if _nav_next() != to:
        return None, MOVED_ON
    return prev, None
