"""Full re-route on the Orin — the Pi engine's side (issue #124).

The boat's Orin runs the Lab's full multi-model optimizer (pi/orin/reroute). This module is how
the Pi asks for one and keeps the answer:

  * `run(device)` — POST /reroute/run. Builds the run request from what the Pi knows (position,
    the deployed course and which mark is next, the plan's track) and hands it to the Orin over
    the direct cable. Refuses, in a short sentence, when there's no position or course, when the
    Orin can't be reached, or when a run is already going (with its progress).
  * `get()` — GET /reroute. The Orin's run status + progress, and the last route. A finished route
    is fetched from the Orin once and stored on disk here, so it survives a restart and the Orin
    being off.

Runs happen ONLY when asked (a crew button, #125). Nothing here starts one on its own.

Legal in-race (RRS 41): the boat's own computers, own position + polars, public forecasts. The
route is labelled "Off the playbook", like the Pi's own small re-route (reoptimize.py), which stays
as the fallback when the Orin is down.
"""
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import deviation, orinassets
from . import navigator as NAV
from . import reroutelog

SCHEMA = "c4.reroute/v1"
LABEL = "Off the playbook"
MAX_RESULT_BYTES = 1024 * 1024
_LOCK = threading.Lock()
# One course change at a time: /course/load holds it for its whole prepare → save marks → activate
# → commit, and /reroute/run holds it while it reads the course it sends to the Orin, so a run never
# sees one load's marks with another load's definition.
COURSE_LOCK = threading.RLock()
_SYNC_LOCK = threading.Lock()   # status → fetch → store as one step, so an older route never overwrites a newer

# crew-facing (short and plain: they reach the iPad as-is)
NO_ORIN = "Can't reach the Orin. Re-route not started."
NO_ORIN_STATUS = "Can't reach the Orin."
NO_POSITION = "No boat position yet."
NO_COURSE = "No course loaded."
NO_ZONES = "No zones or rounding sides aboard. Redeploy the course."
PENDING_KEEP = 20          # requests remembered until their route comes back (per job id)


def orin_url():
    return os.environ.get("ORIN_REROUTE_URL", "http://10.10.10.2:8210").rstrip("/")


def _timeout():
    return float(os.environ.get("ORIN_REROUTE_TIMEOUT_S", "5"))


def _dir():
    return os.path.dirname(os.environ.get("ENGINE_DB", "/var/lib/sr33/engine/engine.db"))


def _result_path():
    return os.path.join(_dir(), "reroute-orin.json")


def _pending_path():
    return os.path.join(_dir(), "reroute-orin-pending.json")


def _definition_path():
    return os.path.join(_dir(), "course-definition.json")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".part")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---- the deployed course ------------------------------------------------------------------------
def _marks_hash(marks):
    """A short hash of the flattened marks (name + position, in order): binds a stored
    definition to the marks the navigator holds, so a moved mark never pairs with old geometry."""
    import hashlib
    sig = [[str(m[0]), round(float(m[1]), 5), round(float(m[2]), 5)] for m in marks]
    return hashlib.sha1(json.dumps(sig, separators=(",", ":")).encode()).hexdigest()[:16]


def course_hash(route, marks):
    """The course a route was made for: the course LOAD (every /course/load, practice course or
    switch of the active course gets a new id, even with identical marks: a new zone or rounding
    side changes the course without moving a mark), the route name, and every mark's name and
    position, in order. A route stored with one hash is never sent to the GPS (or shown as
    current) once the course aboard has another. Read it under COURSE_LOCK."""
    try:
        mh = _marks_hash([(m.get("name"), m.get("lat"), m.get("lon")) for m in marks])
    except (TypeError, ValueError, AttributeError):
        return None
    load = (_read(_load_id_path()) or {}).get("load_id") or "noload"
    return f"{load}:{route}:{mh}"


def bump_load_id(route):
    """A course change that isn't a /course/load (a practice course, a switch of the active
    course): give it a new load id, so every route made before it counts as an old course. The
    caller holds COURSE_LOCK across its whole change. Never raises (a store without the engine's
    disk, e.g. the cloud, has no re-route to protect)."""
    try:
        with COURSE_LOCK:
            load_id = new_load_id()
            set_load_id(route, load_id)
            _rebind_definition(route, load_id)
    except OSError as exc:
        print(f"[orinroute] could not bump the course load id: {exc}", flush=True)


def _rebind_definition(route, load_id):
    """A switch back to the course the definition was deployed for: the stored definition (its
    zones, islands and rounding sides) is re-bound to the new load id, so the Orin keeps routing
    around them. Only when it is that course: same route, and the marks aboard hash to the marks
    it was deployed with (a moved mark or other marks → not re-bound, never a stale pair). Caller
    holds COURSE_LOCK."""
    rec = _read(_definition_path())
    if not rec or rec.get("route") != route or not rec.get("marks_hash"):
        return False
    try:
        have = _marks_hash([(m.get("name"), m.get("lat"), m.get("lon")) for m in NAV._marks(route)])
    except Exception:  # noqa: BLE001
        return False
    if have != rec["marks_hash"]:
        return False
    rec["load_id"] = load_id
    _write(_definition_path(), json.dumps(rec, separators=(",", ":")))
    return True


def current_course_hash():
    """The hash of the course the navigator holds now, or None (no course aboard)."""
    try:
        with COURSE_LOCK:
            nav = NAV.get_navigator()
            if not nav.get("route"):
                return None
            marks = NAV._marks(nav["route"])
            return course_hash(nav["route"], marks) if marks else None
    except Exception:  # noqa: BLE001 — no course read → treated as "not this course"
        return None


def _load_id_path():
    return os.path.join(_dir(), "course-load.json")


def new_load_id():
    import uuid
    return uuid.uuid4().hex


def set_load_id(route, load_id):
    """Record which course load the marks store now holds (written right after the marks)."""
    with _LOCK:
        _write(_load_id_path(), json.dumps({"route": route, "load_id": load_id}))


def prepare_definition(definition, course_id, route, load_id=None):
    """Write the RaceDefinition the Lab deployed (POST /course/load) to a temp file, trimmed to
    what routing needs: the course with its islands and rounding sides, and the race's zones. The
    marks store keeps only flattened marks, and the Orin's optimizer needs the rest. Returns the
    temp path for `commit_definition`, or None when the definition has no such course. Raises
    OSError when it can't be written."""
    from shared.race_def import course_to_marks
    d = definition or {}
    courses = d.get("courses") or []
    course = next((c for c in courses if c.get("id") == course_id), None) or \
        (courses[0] if courses else None)
    if not course:
        return None
    keep = {"race_id": d.get("race_id"), "name": d.get("name"), "timezone": d.get("timezone"),
            "courses": [course], "zones": d.get("zones") or []}
    flat, _skip, _cid = course_to_marks(keep, course.get("id"))
    rec = {"route": route, "course_id": course.get("id"), "saved_at": int(time.time()),
           "load_id": load_id,
           "marks_hash": _marks_hash([(n, a, o) for _s, n, a, o in flat]), "definition": keep}
    path = _definition_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".part")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(rec, separators=(",", ":")))
    except OSError:
        discard(tmp)
        raise
    return tmp


def commit_definition(tmp):
    with _LOCK:
        os.replace(tmp, _definition_path())


def discard(tmp):
    if tmp and os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def drop_definition():
    """Forget the stored definition (a course load failed half way): the re-route then uses the
    navigator's marks only, never old geometry."""
    try:
        with _LOCK:
            os.remove(_definition_path())
    except FileNotFoundError:
        pass
    except OSError:
        pass                  # can't delete: the marks hash still keeps it from being used


def save_definition(definition, course_id, route):
    with COURSE_LOCK:
        load_id = new_load_id()
        tmp = prepare_definition(definition, course_id, route, load_id)
        if tmp is None:
            drop_definition()
            return False
        set_load_id(route, load_id)
        commit_definition(tmp)
        return True


def _definition_for(route, marks):
    """The stored definition, if it's the course the navigator holds: same route and the same marks
    (names AND positions, by the hash stored with it)."""
    rec = _read(_definition_path())
    if not rec or rec.get("route") != route or not rec.get("marks_hash"):
        return None, None
    # the same LOAD as the marks aboard (a definition from another load of the same marks may carry
    # other zones)
    cur = _read(_load_id_path()) or {}
    if not rec.get("load_id") or cur.get("load_id") != rec["load_id"] or cur.get("route") != route:
        return None, None
    try:
        have = _marks_hash([(m.get("name"), m.get("lat"), m.get("lon")) for m in marks])
    except (TypeError, ValueError):
        return None, None
    if have != rec["marks_hash"]:
        return None, None
    return rec["definition"], rec.get("course_id")


def _plan_track():
    """The active playbook variant's track ({points: [{lat, lon, t}]}) or None."""
    bundle = deviation._load_playbook()
    if not bundle:
        return None
    v = deviation._pick_variant(bundle, None)
    path = ((v or {}).get("route") or {}).get("path") or []
    pts = [{"lat": p.get("lat"), "lon": p.get("lon"), "t": p.get("t")} for p in path
           if p.get("lat") is not None and p.get("lon") is not None and p.get("t") is not None]
    if len(pts) < 2:
        return None
    return {"variant": str(v.get("id") or v.get("name") or ""),
            "variant_label": v.get("name") or str(v.get("id") or "the plan"), "points": pts}


def _device(raw):
    s = "".join(ch for ch in str(raw or "") if ch.isprintable()).strip()[:40]
    return s or None


def build_request(device=None):
    """→ (request dict, None) or (None, crew-facing reason)."""
    with COURSE_LOCK:                    # a course load can't land half way through this read
        return _build_request(device)


def _build_request(device):
    from .reoptimize import _next_course_index
    nav = NAV.get_navigator()
    if not nav.get("available"):
        note = str(nav.get("note") or "")
        return None, NO_POSITION if "position" in note.lower() else NO_COURSE
    marks = NAV._marks(nav["route"])
    if not marks:
        return None, NO_COURSE
    s = NAV._latest()
    if s.get("lat") is None or s.get("lon") is None:
        return None, NO_POSITION
    nxt = _next_course_index(nav, marks)
    definition, cid = _definition_for(nav["route"], marks)
    req = {"device": _device(device), "asked_at": int(time.time()),
           "position": {"lat": s["lat"], "lon": s["lon"], "t": int(time.time())},
           "route": nav["route"], "next_index": nxt,
           "marks": [{"seq": m.get("seq"), "name": m.get("name"), "lat": m.get("lat"),
                      "lon": m.get("lon")} for m in marks],
           "definition": definition, "course_id": cid, "plan": _plan_track(),
           "course_hash": course_hash(nav["route"], marks)}
    assets = orinassets.for_run(definition, cid)     # the chart + boat profile from Deploy
    if assets is not None:
        req["assets"] = assets
    return req, None


# ---- talking to the Orin ------------------------------------------------------------------------
def _call(method, path, body=None):
    """→ (http status, json body), or (None, None) when the Orin can't be reached at all."""
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    req = urllib.request.Request(orin_url() + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as r:
            raw = r.read(MAX_RESULT_BYTES + 1)
            return r.status, (json.loads(raw.decode()) if len(raw) <= MAX_RESULT_BYTES else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(65536).decode())
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception:  # noqa: BLE001 — refused, timed out, no route to host
        return None, None


def run(device=None, call=None):
    """Ask the Orin for one full re-route → (http status, body). Never starts one any other way.
    Every press is logged (reroutelog), whatever came of it."""
    call = call or _call
    dev = _device(device)
    req, why = build_request(device)
    if req is None:
        reroutelog.record("pressed", "orin", device=dev, outcome="refused", detail=why)
        return 409, {"ok": False, "detail": why}
    # The POST and the pending write are ONE step under _SYNC_LOCK, the lock get() holds from the
    # Orin's status through storing a result: a fast job polled by another iPad can't be stored
    # before its request (course, mark positions) is written. Lock order: build_request took and
    # released COURSE_LOCK above; nothing takes COURSE_LOCK while holding _SYNC_LOCK, and _LOCK is
    # only ever taken inside it (here and in _store), so no cycle. The POST is bounded by the
    # Orin timeout (ORIN_REROUTE_TIMEOUT_S), which bounds how long a poll waits.
    with _SYNC_LOCK:
        return _start(call, req, dev)


def _start(call, req, dev):
    code, body = call("POST", "/run", req)
    if code is None:
        reroutelog.record("pressed", "orin", device=dev, outcome="unreachable", detail=NO_ORIN)
        return 503, {"ok": False, "orin": "unreachable", "detail": NO_ORIN}
    body = body if isinstance(body, dict) else {}
    if code == 202:
        job = body.get("job_id")
        # what the Pi asked for, kept until the route comes back: who asked, and which course
        # position the route starts at (so the GPS output can tell the laps of a lapped course apart)
        # Kept PER JOB: a later run never overwrites an earlier one's, so a route that finished
        # unpolled and is fetched after a newer run is still matched to its own course.
        try:
            with _LOCK:
                pend = _pending_all()
                pend[str(job)] = {"device": dev, "asked_at": req["asked_at"],
                                  "course_hash": req.get("course_hash"),
                                  "route": req["route"], "next_index": req["next_index"],
                                  "definition": req.get("definition") is not None,
                                  "marks": [m.get("name") for m in req["marks"]]}
                keep = sorted(pend.items(), key=lambda kv: kv[1].get("asked_at") or 0)[-PENDING_KEEP:]
                _write(_pending_path(), json.dumps(dict(keep), separators=(",", ":")))
        except OSError:
            pass
        reroutelog.record("pressed", "orin", device=dev, outcome="started", job_id=job)
        return 202, {"ok": True, "job_id": job, "state": "running"}
    if code == 409:
        reroutelog.record("pressed", "orin", device=dev, outcome="busy", job_id=body.get("job_id"))
        return 409, {"ok": False, "busy": True, "job_id": body.get("job_id"),
                     "progress": body.get("progress"),
                     "detail": body.get("detail") or "A re-route is already running."}
    detail = str(body.get("detail") or "Re-route failed. Try again.")[:120]
    reroutelog.record("pressed", "orin", device=dev, outcome="failed", detail=detail)
    return 502, {"ok": False, "detail": detail}


def _valid_result(r):
    return (isinstance(r, dict) and r.get("schema") == SCHEMA and isinstance(r.get("job_id"), str)
            and len(json.dumps(r, separators=(",", ":"))) <= MAX_RESULT_BYTES)


def stored():
    """The last route the Orin made, as stored here, with its age."""
    rec = _read(_result_path())
    if not rec or not isinstance(rec.get("result"), dict):
        return None
    r = dict(rec["result"])
    fin = r.get("finished_at") or rec.get("received_at")
    r["age_s"] = max(0, int(time.time() - fin)) if fin else None
    return r


def _pending_all():
    """{job_id: what the Pi asked for}. An older single-record file reads as one entry."""
    raw = _read(_pending_path())
    if not isinstance(raw, dict):
        return {}
    if "job_id" in raw and "next_index" in raw:          # the one-record shape before per-job
        return {str(raw["job_id"]): raw}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def _pending_for(job_id):
    return _pending_all().get(str(job_id)) if job_id else None


def _mark_idx(result, pend):
    """The course position of each of the route's marks, from the request it answers: the Orin
    routes from the request's next mark on, in course order. Only kept when the names line up
    with the course the request was made on; the GPS output checks them again."""
    pend = pend or {}
    names = result.get("marks") or []
    nxt, course = pend.get("next_index"), pend.get("marks") or []
    if not isinstance(nxt, int) or not names:
        return None
    idx = list(range(nxt, nxt + len(names)))
    if idx[-1] >= len(course) or any(course[i] != n for i, n in zip(idx, names)):
        return None
    return idx


def _store(result, received_at=None):
    result = dict(result, label=LABEL, off_playbook=True, source="orin")
    result.pop("age_s", None)
    pend = _pending_for(result.get("job_id"))           # THIS job's request, never another's
    idx = _mark_idx(result, pend)
    if idx is not None:
        result["mark_idx"] = idx
    if pend and pend.get("course_hash"):
        # the course this route answers; unknown (None) counts as an old course
        result["course_hash"] = pend["course_hash"]
    if pend and pend.get("definition") is False:
        # routed on the marks alone: say so, whatever the Orin's own wording
        warns = [w for w in (result.get("warnings") or []) if w != "Course without islands or rounding sides."]
        if NO_ZONES not in warns:
            warns.insert(0, NO_ZONES)
        result["warnings"] = warns
        result["no_zones"] = True
    with _LOCK:
        _write(_result_path(), json.dumps({"received_at": received_at or int(time.time()),
                                           "result": result}, separators=(",", ":")))
    vs = result.get("vs_plan") or {}
    reroutelog.record("done", "orin", once_key=result.get("job_id"), job_id=result.get("job_id"),
                      asked_by=result.get("asked_by"), asked_at=result.get("asked_at"),
                      finished_at=result.get("finished_at"), run_s=result.get("run_s"),
                      weather=reroutelog.weather_summary(result.get("weather")),
                      gain_min=vs.get("gain_min") if vs.get("available") else None,
                      finish_eta=result.get("finish_eta"), complete=result.get("complete"),
                      boat=result.get("boat"), marks=result.get("marks"),
                      warnings=(result.get("warnings") or [])[:4], no_zones=result.get("no_zones"))


def _heal():
    """Self-repair: a stored route without its course stamp (stored before its request was
    written, by any path) is re-stamped from its own pending entry once that exists. Without it
    the GPS would refuse a valid route forever. Caller holds _SYNC_LOCK."""
    rec = _read(_result_path())
    r = (rec or {}).get("result")
    if not isinstance(r, dict) or r.get("course_hash"):
        return False
    pend = _pending_for(r.get("job_id"))
    if not pend or not pend.get("course_hash"):
        return False
    _store(r, received_at=rec.get("received_at"))
    reroutelog.record("restamped", "orin", once_key=r.get("job_id"), job_id=r.get("job_id"),
                      warnings=(stored() or {}).get("warnings"),
                      no_zones=(stored() or {}).get("no_zones"))
    return True


def get(call=None):
    """The Orin's status + progress, and the last stored route. A newly finished route is fetched
    and stored first. Works with the Orin off (then `orin.reachable` is false)."""
    with _SYNC_LOCK:
        return _get(call or _call)


def _get(call):
    _heal()                                   # Orin reachable or not
    code, st = call("GET", "/status")
    if code != 200 or not isinstance(st, dict):
        return {"orin": {"reachable": False}, "state": None, "progress": None,
                "note": NO_ORIN_STATUS, "result": stored()}
    last = st.get("last") or {}
    have = stored()
    # sync on the job behind /result, not on the last run: a run that finished unpolled and was
    # followed by a failed one is still fetched
    want = st.get("result_job_id")
    if want and want != (have or {}).get("job_id"):
        rc, r = call("GET", "/result")
        if rc == 200 and _valid_result(r) and r.get("job_id") == want:
            _store(r)
            have = stored()
    out = {"orin": {"reachable": True}, "state": st.get("state"), "job_id": st.get("job_id"),
           "asked_by": st.get("asked_by"), "progress": st.get("progress"), "result": have}
    if want and want != (have or {}).get("job_id"):
        # the Orin has a newer route we couldn't fetch: never pass the old one off as it
        out["note"] = "New route not fetched yet. Showing the last one."
        out["result_stale"] = True
    if st.get("state") == "error":
        out["note"] = str(last.get("detail") or "Re-route failed. Try again.")[:120]
        if last.get("job_id"):
            reroutelog.record("failed", "orin", once_key=last["job_id"], job_id=last["job_id"],
                              asked_by=last.get("asked_by"), detail=out["note"])
    return out


# ---- what the iPad's Route tile reads ---------------------------------------------------------
def _finished(r):
    return (r or {}).get("finished_at") or (r or {}).get("computed_at") or 0


def latest():
    """(source, route) of the newest route made by either re-router: 'orin' or 'pi' (the Pi's
    own fallback), or (None, None). Full routes, track included."""
    from . import reoptimize
    o = stored()
    o = o if (o or {}).get("available") else None
    p = reoptimize.get_reoptimize()
    p = p if p.get("available") else None
    if o and (not p or _finished(o) >= _finished(p)):
        return "orin", o
    if p:
        return "pi", p
    return None, None


def tile(call=None, compact=False):
    """GET /reroute: the Orin's status + its last route, the Pi fallback's status + its last
    route, and which of the two is newest (`latest`). `compact` drops the routes' tracks (the
    iPad polls this; the GPS output reads the full route itself)."""
    from . import reoptimize
    out = get(call)
    out["pi"] = reoptimize.status()
    src, _r = latest()
    out["latest"] = src
    if compact and isinstance(out.get("result"), dict):
        out["result"] = {k: v for k, v in out["result"].items() if k not in ("points", "leg_end_idx")}
    # a route made for another course (moved marks, a new course loaded) must not look current:
    # it stays stored (the history keeps it), flagged for the tile
    now_hash = current_course_hash()
    for r in (out.get("result"), out["pi"].get("result")):
        if isinstance(r, dict):
            r["old_course"] = not r.get("course_hash") or r.get("course_hash") != now_hash
    return out
