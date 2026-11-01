"""One onboard re-route run: the Lab's full optimizer, from the boat to the remaining marks.

Run by the re-route service (service.py) as a short-lived child process, one at a time, so the
weather parser's memory goes back to the OS after every run:

    python job.py <job_dir>        # reads <job_dir>/request.json, writes progress.jsonl + result.json

The request comes from the Pi engine (POST /reroute/run → the service's POST /run). It carries
the boat's position, the course (the RaceDefinition the Lab deployed, and which mark is next), the
plan's track and who asked. This module turns that into the remaining course from the boat, runs
`app.routerun.run_course` (the Lab's own run, see vps/lab/app/routerun.py) on the plume's GRIB
cache (orin_weather.py), and returns a compact result for the Pi and the console.

Legal in-race (RRS 41): the boat's own computer, its own position and polars, public forecasts.
The result is labelled "Off the playbook", like the Pi's small re-route.
"""
from __future__ import annotations

import copy
import json
import os
import resource
import sys
import time

_HOME = os.path.expanduser("~")
# One download layer on the boat: the plume's GRIB cache. Set before the Lab's wind code loads.
os.environ.setdefault("GRIB_CACHE", os.path.join(_HOME, "c4-plume", "gribcache"))
os.environ.setdefault("REROUTE_DIR", os.path.join(_HOME, "c4-reroute"))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "plume"))

import onboard_plume as plume_svc  # noqa: E402  (the plume's internet probe + route geometry)
import orin_weather as W  # noqa: E402

from app import boats, optimizer, routerun  # noqa: E402  (vps/lab on PYTHONPATH)
from app import plume as lab_plume  # noqa: E402
from app.geo import obstacles as geo  # noqa: E402
from shared import race_def  # noqa: E402

SCHEMA = "c4.reroute/v1"
LABEL = "Off the playbook"
STATE_DIR = os.environ["REROUTE_DIR"]
CHART_DIR = os.environ.get("REROUTE_CHART_DIR", os.path.join(STATE_DIR, "chart"))
MODELS = tuple(m.strip() for m in os.environ.get("REROUTE_MODELS", "gfs,nam,hrrr").split(",")
               if m.strip())
BOAT_ID = os.environ.get("REROUTE_BOAT", "sr33")
ROUTE_BUDGET_S = float(os.environ.get("REROUTE_ROUTE_BUDGET_S", "300"))   # optimizer time cap
RESOLUTION = os.environ.get("REROUTE_RESOLUTION", "auto")
USE_CURRENT = os.environ.get("REROUTE_CURRENT", "0") == "1"   # LMHOFS: network only
USE_WAVES = os.environ.get("REROUTE_WAVES", "0") == "1"       # GLWU: network only
PLAN_NEAR_NM = 20.0          # compare with the plan only while the boat is this close to it


class RunError(Exception):
    """A run that can't start (bad request, no course). The message is crew-facing: short."""


# ---- the remaining course ---------------------------------------------------------------------
def _nav_index_map(course):
    """For each course mark, the navigator index it gets (course_to_marks order), or None for an
    island / a mark with no position. Start is index 0 when it has a position."""
    k = 1 if (course.get("start") or {}).get("lat") is not None else 0
    out = []
    for m in course.get("marks", []) or []:
        if m.get("type") == "island" or m.get("lat") is None:
            out.append(None)
        else:
            out.append(k)
            k += 1
    return out, k              # k = the finish's index (when the course has one)


def remaining_definition(definition, course_id, next_index, lat, lon):
    """The deployed course, cut to what's left: it starts at the boat and runs from the next mark
    (navigator index `next_index`) to the finish. Islands on the legs already sailed are dropped;
    the ones ahead stay (they carry rounding sides). Returns (definition, course_id, mark names)."""
    d = copy.deepcopy(definition)
    courses = d.get("courses") or []
    course = next((c for c in courses if c.get("id") == course_id), None) or \
        (courses[0] if courses else None)
    if not course:
        raise RunError("No course loaded.")
    idx, finish_idx = _nav_index_map(course)
    nxt = max(1, int(next_index or 1))
    keep, prev_nav = [], 0
    for m, k in zip(course.get("marks") or [], idx):
        if k is None:
            if m.get("type") == "island" and m.get("lat") is not None and prev_nav >= nxt - 1:
                keep.append(m)            # an island on the current leg or later
            continue
        prev_nav = k
        if k >= nxt:
            keep.append(m)
    course["marks"] = keep
    course["start"] = {"type": "point", "lat": float(lat), "lon": float(lon), "name": "Boat"}
    if nxt > finish_idx:
        raise RunError("Course finished. Nothing to re-route.")
    d["courses"] = [course]
    marks, _skip, cid = race_def.course_to_marks(d, course.get("id"))
    if len(marks) < 2:
        raise RunError("No marks left to route to.")
    return d, cid, [n for _s, n, _a, _o in marks[1:]]


def minimal_definition(marks, next_index, lat, lon):
    """No deployed definition aboard (a course loaded before this build): the navigator's marks
    from the next one on. No islands or rounding sides — the result says so."""
    rest = [m for m in (marks or [])[int(next_index or 0):]
            if m.get("lat") is not None and m.get("lon") is not None]
    if not rest:
        raise RunError("No marks left to route to.")
    *mid, last = rest
    course = {"id": "nav", "start": {"type": "point", "lat": float(lat), "lon": float(lon)},
              "marks": [{"name": m.get("name") or "Mark", "type": "buoy", "lat": m["lat"],
                         "lon": m["lon"], "rounding": "none"} for m in mid],
              "finish": {"points": [{"lat": last["lat"], "lon": last["lon"]}]}}
    d = {"race_id": "nav", "name": "", "courses": [course], "zones": []}
    return d, "nav", [m.get("name") for m in mid] + ["Finish"]


# ---- the chart (land) -------------------------------------------------------------------------
def chart_path(race_id, course_id):
    safe = lambda s: "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(s))[:60]
    return os.path.join(CHART_DIR, f"{safe(race_id)}__{safe(course_id)}.json")


def load_obstacles(definition, course_id, full_definition, full_course_id, boat):
    """The land/island/zone mask → (ObstacleField or None, chart note or None). The Lab's own land
    mask for the course, carried aboard (pi/orin/reroute/prep_chart.py), when there is one. Else
    the race's islands and zones only: the result warns that land isn't checked."""
    if full_definition:
        p = chart_path(full_definition.get("race_id"), full_course_id)
        try:
            with open(p) as f:
                return geo.field_from_dict(json.load(f)), None
        except (OSError, ValueError, KeyError):
            pass
    base, cid = (full_definition, full_course_id) if full_definition else (definition, course_id)
    bbox = optimizer.course_bbox(base, cid)
    field = geo.build_for_course(base, cid, bbox, coastline_on=False, use_cache=False,
                                 safety_depth=boats.safety_depth_m(boat))
    return field, "No chart aboard. Route doesn't check the shore."


# ---- the comparison with the plan -------------------------------------------------------------
def vs_plan(plan, lat, lon, now, finish_eta):
    """Minutes saved against the plan: the plan's own time left from the boat's nearest point on
    it, minus the new route's time left. Positive = the new route is faster."""
    pts = lab_plume.clean_path((plan or {}).get("points"))
    if len(pts) < 2:
        return {"available": False, "note": "No plan to compare."}
    near = plume_svc._nearest_on_path(pts, lat, lon)
    if near is None or near[0] > PLAN_NEAR_NM:
        return {"available": False, "note": "Too far off the plan to compare."}
    plan_left = pts[-1]["t"] - near[1]
    new_left = finish_eta - now
    return {"available": True, "basis": "plan", "variant": (plan or {}).get("variant"),
            "plan_label": (plan or {}).get("variant_label"),
            "off_plan_nm": round(near[0], 1),
            "plan_finish_eta": round(now + plan_left),
            "plan_left_min": round(plan_left / 60.0),
            "new_left_min": round(new_left / 60.0),
            "gain_min": round((plan_left - new_left) / 60.0)}


# ---- one run ----------------------------------------------------------------------------------
def _peak_rss_mb():
    me = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    kids = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024.0
    return {"process": round(me), "parser": round(kids)}


def _get_boat():
    b = boats.get_boat(BOAT_ID)
    return b


def _validate(req):
    pos = (req or {}).get("position") or {}
    try:
        lat, lon = float(pos["lat"]), float(pos["lon"])
    except (KeyError, TypeError, ValueError):
        raise RunError("No boat position yet.")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise RunError("No boat position yet.")
    return lat, lon


def run(req, now=None, on_progress=None, on_phase=None, is_online=None):
    """Request → result (see README.md for both shapes). Raises RunError for a run that can't
    start; anything else the caller reports as a failed run."""
    t_wall = time.time()
    now = float(req.get("now") or time.time()) if now is None else float(now)
    say = on_progress or (lambda _m: None)
    phase = on_phase or (lambda _p: None)
    lat, lon = _validate(req)
    full = req.get("definition")
    full_cid = req.get("course_id")
    if full and (full.get("courses") or []):
        d, cid, names = remaining_definition(full, full_cid, req.get("next_index"), lat, lon)
        full_cid = full_cid or (full["courses"][0].get("id"))
    else:
        full = None
        d, cid, names = minimal_definition(req.get("marks"), req.get("next_index"), lat, lon)
    boat = _get_boat()
    phase("chart")
    field, chart_note = load_obstacles(d, cid, full, full_cid, boat)

    # weather: the box the cache already holds, if one covers what's left of the course
    online = (is_online or plume_svc.online)()
    budget = W.Budget(online)
    need = optimizer.course_bbox(d, cid)
    known = []
    plan_pts = lab_plume.clean_path(((req.get("plan") or {}).get("points")))
    if len(plan_pts) >= 2:
        known.append(lab_plume._bbox_for({"path": plan_pts}))      # the plume's own box
    known += W.load_boxes(STATE_DIR)
    bbox, why = W.choose_bbox(need, known, online)
    t_end = now + optimizer.estimate_hours(d, cid) * 3600
    names_req = [m for m in (req.get("models") or MODELS)]
    sources = W.sources_for(names_req, budget, bbox, now, t_end)
    say("online — new forecasts may download" if online else "offline — using saved forecasts")
    res = routerun.run_course(
        d, cid, now, sources, avoid=True, per_model=False, resolution=RESOLUTION,
        use_waves=USE_WAVES and online, use_current=USE_CURRENT and online,
        on_progress=say, on_phase=phase, boat=boat, model_skill=False, obstacles=field,
        time_budget_s=ROUTE_BUDGET_S, fallback_tries=None if online else 0, bbox=bbox,
        t_end=t_end, briefing=False, grids=False)
    if budget.downloads:
        W.remember_box(STATE_DIR, bbox)
    weather = W.summarize(res.get("windfield"), now, budget, why)
    phase("saving")
    out = {"schema": SCHEMA, "label": LABEL, "off_playbook": True, "source": "orin",
           "job_id": req.get("job_id"), "asked_by": str(req.get("device") or "")[:40] or None,
           "asked_at": req.get("asked_at"), "computed_from": int(now),
           "from": {"lat": round(lat, 5), "lon": round(lon, 5)},
           "marks": names, "weather": weather}
    if not res.get("available"):
        no_wind = not (res.get("windfield") or {}).get("loaded")
        out.update(available=False, note="No forecast aboard. Connect to the internet." if no_wind
                   else "No route found. Try again.")
    else:
        path = res.get("path") or []
        legs = [{"to": lg["to"], "eta": lg["eta_epoch"], "leg_minutes": lg["leg_minutes"],
                 "sailed_nm": lg["sailed_nm"], "tacks": lg["tacks"], "sail": lg.get("sail"),
                 "point_of_sail": lg.get("point_of_sail"), "first_heading": lg.get("first_heading"),
                 "reached": lg.get("reached", True)} for lg in res.get("legs") or []]
        warnings = list(res.get("warnings") or [])
        if chart_note:
            warnings.insert(0, chart_note)
        if not full:
            warnings.insert(0, "Course without islands or rounding sides.")
        out.update({
            "available": True, "complete": res.get("complete"), "degraded": res.get("degraded"),
            "finish_eta": res.get("finish_epoch"), "total_hours": res.get("total_hours"),
            "sailed_nm": res.get("total_sailed_nm"), "tacks": res.get("total_tacks"),
            "first_heading": (legs[0]["first_heading"] if legs else None),
            "confidence": res.get("route_confidence"), "wind_coverage": res.get("wind_coverage"),
            "timed_out": res.get("timed_out"),
            "legs": legs,
            "sail_plan": [s.get("sail") for s in res.get("sail_plan") or [] if s.get("sail")],
            "points": [{"lat": round(p["lat"], 5), "lon": round(p["lon"], 5), "t": p["t"]}
                       for p in path],
            "leg_end_idx": res.get("leg_end_idx"),
            "warnings": warnings,
            "vs_plan": vs_plan(req.get("plan"), lat, lon, now, res.get("finish_epoch") or now),
            "chart": {"land": chart_note is None,
                      "source": (res.get("obstacles") or {}).get("source")},
        })
    out["run_s"] = round(time.time() - t_wall, 1)
    out["peak_rss_mb"] = _peak_rss_mb()
    out["finished_at"] = int(time.time())
    return out


# ---- process entry ----------------------------------------------------------------------------
def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def main(job_dir):
    with open(os.path.join(job_dir, "request.json")) as f:
        req = json.load(f)
    prog = open(os.path.join(job_dir, "progress.jsonl"), "a", buffering=1)

    def emit(kind, text):
        prog.write(json.dumps({"t": round(time.time(), 1), kind: str(text)[:200]}) + "\n")

    try:
        out = run(req, on_progress=lambda m: emit("msg", m), on_phase=lambda p: emit("phase", p))
        _write_json(os.path.join(job_dir, "result.json"), out)
        return 0
    except RunError as exc:
        _write_json(os.path.join(job_dir, "error.json"), {"detail": str(exc)})
        return 2
    except Exception as exc:  # noqa: BLE001 — the service reports it; the details go to the log
        print(f"[reroute] run failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        _write_json(os.path.join(job_dir, "error.json"), {"detail": "Re-route failed. Try again.",
                                                          "error": type(exc).__name__})
        return 1
    finally:
        prog.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
