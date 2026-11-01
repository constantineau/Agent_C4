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

from . import deviation
from . import navigator as NAV

SCHEMA = "c4.reroute/v1"
LABEL = "Off the playbook"
MAX_RESULT_BYTES = 1024 * 1024
_LOCK = threading.Lock()

# crew-facing (short and plain: they reach the iPad as-is)
NO_ORIN = "Can't reach the Orin. Re-route not started."
NO_ORIN_STATUS = "Can't reach the Orin."
NO_POSITION = "No boat position yet."
NO_COURSE = "No course loaded."


def orin_url():
    return os.environ.get("ORIN_REROUTE_URL", "http://10.10.10.2:8210").rstrip("/")


def _timeout():
    return float(os.environ.get("ORIN_REROUTE_TIMEOUT_S", "5"))


def _dir():
    return os.path.dirname(os.environ.get("ENGINE_DB", "/var/lib/sr33/engine/engine.db"))


def _result_path():
    return os.path.join(_dir(), "reroute-orin.json")


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
def save_definition(definition, course_id, route):
    """Keep the RaceDefinition the Lab deployed (POST /course/load), trimmed to what routing needs:
    the course with its islands and rounding sides, and the race's zones. The marks store keeps
    only flattened marks, and the Orin's optimizer needs the rest."""
    d = definition or {}
    courses = d.get("courses") or []
    course = next((c for c in courses if c.get("id") == course_id), None) or \
        (courses[0] if courses else None)
    if not course:
        return False
    keep = {"race_id": d.get("race_id"), "name": d.get("name"), "timezone": d.get("timezone"),
            "courses": [course], "zones": d.get("zones") or []}
    rec = {"route": route, "course_id": course.get("id"), "saved_at": int(time.time()),
           "definition": keep}
    with _LOCK:
        _write(_definition_path(), json.dumps(rec, separators=(",", ":")))
    return True


def _definition_for(route, marks):
    """The stored definition, if it's the course the navigator holds (same route, same marks)."""
    rec = _read(_definition_path())
    if not rec or rec.get("route") != route:
        return None, None
    try:
        from shared.race_def import course_to_marks
        flat, _skip, cid = course_to_marks(rec["definition"], rec.get("course_id"))
    except Exception:  # noqa: BLE001
        return None, None
    if [n for _s, n, _a, _o in flat] != [m.get("name") for m in marks]:
        return None, None
    return rec["definition"], cid


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
           "route": nav["route"], "next_index": nxt if nxt is not None else 1,
           "marks": [{"seq": m.get("seq"), "name": m.get("name"), "lat": m.get("lat"),
                      "lon": m.get("lon")} for m in marks],
           "definition": definition, "course_id": cid, "plan": _plan_track()}
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
    """Ask the Orin for one full re-route → (http status, body). Never starts one any other way."""
    call = call or _call
    req, why = build_request(device)
    if req is None:
        return 409, {"ok": False, "detail": why}
    code, body = call("POST", "/run", req)
    if code is None:
        return 503, {"ok": False, "orin": "unreachable", "detail": NO_ORIN}
    body = body if isinstance(body, dict) else {}
    if code == 202:
        return 202, {"ok": True, "job_id": body.get("job_id"), "state": "running"}
    if code == 409:
        return 409, {"ok": False, "busy": True, "job_id": body.get("job_id"),
                     "progress": body.get("progress"),
                     "detail": body.get("detail") or "A re-route is already running."}
    return 502, {"ok": False, "detail": str(body.get("detail") or "Re-route failed. Try again.")[:120]}


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


def _store(result):
    result = dict(result, label=LABEL, off_playbook=True, source="orin")
    with _LOCK:
        _write(_result_path(), json.dumps({"received_at": int(time.time()), "result": result},
                                          separators=(",", ":")))


def get(call=None):
    """The Orin's status + progress, and the last stored route. A newly finished route is fetched
    and stored first. Works with the Orin off (then `orin.reachable` is false)."""
    call = call or _call
    code, st = call("GET", "/status")
    if code != 200 or not isinstance(st, dict):
        return {"orin": {"reachable": False}, "state": None, "progress": None,
                "note": NO_ORIN_STATUS, "result": stored()}
    last = st.get("last") or {}
    have = stored()
    if last.get("state") == "done" and last.get("job_id") and \
            last.get("job_id") != (have or {}).get("job_id"):
        rc, r = call("GET", "/result")
        if rc == 200 and _valid_result(r) and r.get("job_id") == last.get("job_id"):
            _store(r)
            have = stored()
    out = {"orin": {"reachable": True}, "state": st.get("state"), "job_id": st.get("job_id"),
           "asked_by": st.get("asked_by"), "progress": st.get("progress"), "result": have}
    if st.get("state") == "error":
        out["note"] = str(last.get("detail") or "Re-route failed. Try again.")[:120]
    return out
