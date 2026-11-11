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
from shared import boat_polar, boat_profile, race_def  # noqa: E402
from shared import boat_id as boat_id_setting  # noqa: E402

SCHEMA = "c4.reroute/v1"
LABEL = "Off the playbook"
STATE_DIR = os.environ["REROUTE_DIR"]
CHART_DIR = os.environ.get("REROUTE_CHART_DIR", os.path.join(STATE_DIR, "chart"))
MODELS = tuple(m.strip() for m in os.environ.get("REROUTE_MODELS", "gfs,hrrr").split(",")
               if m.strip())
BOAT_ID = boat_id_setting.require("REROUTE_BOAT")   # no default: never re-route as another boat (#307)
ROUTE_BUDGET_S = float(os.environ.get("REROUTE_ROUTE_BUDGET_S", "300"))   # optimizer time cap
RESOLUTION = os.environ.get("REROUTE_RESOLUTION", "auto")
# current / sea state: the Lab's sources, picked by the course area (#308); network only
USE_CURRENT = os.environ.get("REROUTE_CURRENT", "0") == "1"
USE_WAVES = os.environ.get("REROUTE_WAVES", "0") == "1"
# the carried chart format: land only, no waypoint pockets (vps/lab/app/orinassets.CHART_KIND). Older formats
# ("land" had pockets at every original mark, clearing shore for good) count as out of date.
CHART_KIND = "land-v2"
NO_CHART = "No chart aboard. Route doesn't check the shore."
OLD_CHART = "Chart is out of date. Route doesn't check the shore."
NO_COVER = "Chart doesn't cover the boat. Route doesn't check the shore."
NO_LAND = "Chart has no land in it. Route doesn't check the shore."
SHORT_FORECAST = "Forecast ends before the finish. Later legs use its last hour."
NO_BOAT = "Boat profile missing."
NOT_DEPLOYED_BOAT = "Boat profile not from Deploy."
BAD_BOAT = "Boat profile from Deploy is bad. Not used."
OTHER_BOAT = "Boat profile from Deploy is another boat's. Not used."
NO_POLAR = "No polar for this boat. Re-route not run."
NOT_DEPLOYED_POLAR = "Boat polar not from Deploy."
BOAT_IN_ZONE = "Boat is inside an exclusion zone. Re-route not run."
BOAT_IN_ISLAND = "Boat is inside an island's keep-out. Re-route not run."
INCOMPLETE = "Route incomplete."
NO_ZONES = "No zones or rounding sides aboard. Redeploy the course."
PLAN_NEAR_NM = 20.0          # compare with the plan only while the boat is this close to it


class RunError(Exception):
    """A run that can't start (bad request, no course). The message is crew-facing: short."""


# ---- the remaining course ---------------------------------------------------------------------
def remaining_definition(definition, course_id, next_index, lat, lon):
    """The deployed course, cut to what's left: it starts at the boat and runs from the next mark
    (navigator index `next_index`) to the finish. Every island stays an obstacle; the ones on legs
    already sailed lose their rounding side (no barrier); the ones ahead keep it. Zones all stay. Returns (definition, course_id, mark names)."""
    d = copy.deepcopy(definition)
    courses = d.get("courses") or []
    course = next((c for c in courses if c.get("id") == course_id), None) or \
        (courses[0] if courses else None)
    if not course:
        raise RunError("No course loaded.")
    idx, finish_idx = geo.nav_index_map(course)
    # each rounding island's leg bearing AS DEPLOYED (previous mark → next mark). Trimming puts the
    # boat in as the start, which would rotate the current leg's barrier and could open water the
    # deployed rounding rule closes, so the bearing is pinned on the kept island.
    # keyed by the island's place in the course (a course can pass the same island twice, once
    # each way: two occurrences, two bearings)
    deployed_brg = {r["mark_idx"]: r["transit_brg"]
                    for r in geo._island_rounding_marks(d, course.get("id"))}
    start = course.get("start") or {}
    has_start = start.get("lat") is not None
    # navigator index of the next mark; unknown → the first mark after the start. An explicit 0 is
    # kept: with no positioned start, index 0 is the first real mark.
    nxt = (1 if has_start else 0) if next_index is None else max(0, int(next_index))
    keep, prev_nav = [], (0 if has_start else -1)   # nav index of the mark before this island
    for mi, (m, k) in enumerate(zip(course.get("marks") or [], idx)):
        if k is None:
            if m.get("type") == "island" and m.get("lat") is not None:
                if nxt == 0 or prev_nav >= nxt - 1:
                    # an island on the current leg or later: as deployed, its leg bearing pinned
                    brg = deployed_brg.get(mi)
                    keep.append(dict(m, transit_brg=brg) if brg is not None else m)
                else:
                    # already rounded: still rock (its disk stays), but no rounding side, so its
                    # wrong-side barrier can't block a later leg
                    keep.append(dict(m, rounding="none"))
            continue
        prev_nav = k
        if k >= nxt:
            keep.append(m)
    if nxt == 0 and has_start:          # not started yet: go to the start line first
        keep.insert(0, {"name": "Start", "type": "buoy", "lat": start["lat"], "lon": start["lon"],
                        "rounding": "none"})
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


def leg_count(definition, course_id):
    """Legs left to sail (nav points after the start)."""
    return max(0, len(race_def.course_to_marks(definition, course_id)[0]) - 1)


def _race_obstacles(definition, course_id, bbox, res_deg, depth, leg=None):
    """The race's own obstacles for what's LEFT of the course: zones, island disks and the
    rounding-side barriers of islands still to round (an island already rounded leaves no barrier
    to block a later leg). Pure Python, no shoreline data. Built as "natural_earth" whatever the
    configured coastline source: under "enc" the builder skips the island disks (it expects ENC
    land polygons, which this build doesn't load), and an island must never read as open water."""
    # carve_waypoints=False: no zone or island disk is ever opened, for the boat or any mark (the
    # pockets go in the land mask only, see load_obstacles; never from the course's own
    # chart_overrides, only the playbook's signed ones the request carries). `leg`: only that
    # leg's barriers.
    if leg is not None:
        definition = geo.only_leg_barriers(definition, course_id, leg)
    return geo.build_for_course(definition, course_id, bbox, res_deg=res_deg, coastline_on=False,
                                use_cache=False, source="natural_earth", safety_depth=depth,
                                carve_waypoints=False)


def _inside(lat, lon, rings):
    """Even-odd point-in-polygon over (lon, lat) rings (holes included)."""
    hit = False
    for ring in rings:
        n = len(ring)
        for i in range(n):
            (x1, y1), (x2, y2) = ring[i], ring[(i + 1) % n]
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                hit = not hit
    return hit


def _no_go_at(definition, course_id, lat, lon):
    """"zone" / "island" when (lat, lon) is inside a race zone or an island's keep-out disk (as
    the obstacle builder fills them), else None."""
    for z in definition.get("zones", []) or []:
        if z.get("type") in ("exclusion", "hazard", "tss") and \
                _inside(lat, lon, geo._zone_rings(z.get("geometry"))):
            return "zone"
    for isl in geo._course_islands(definition, course_id):
        if optimizer._hav_nm(lat, lon, isl["lat"], isl["lon"]) <= isl["radius_nm"]:
            return "island"
    return None


def boat_in_no_go(definition, course_id, lat, lon):
    """The crew-facing reason not to route: the boat, or a mark still to sail to, is inside a race
    zone or an island's keep-out. Routing there would mean erasing it, which the Orin never does."""
    hit = _no_go_at(definition, course_id, lat, lon)
    if hit:
        return BOAT_IN_ZONE if hit == "zone" else BOAT_IN_ISLAND
    marks, _skip, _cid = race_def.course_to_marks(definition, course_id)
    for _s, name, mlat, mlon in marks[1:]:
        if mlat is not None and _no_go_at(definition, course_id, mlat, mlon):
            return f"Mark {name} is inside a no-go zone. Re-route not run."
    return None


_ON_DISK = object()      # load_obstacles: read the chart file (a Pi without the Deploy assets)


def _disk_chart(full_definition, full_course_id):
    try:
        with open(chart_path(full_definition.get("race_id"), full_course_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def override_names(req):
    """The marks the Lab signed a chart override for (`safety.overrides` of the frozen playbook,
    sent by the Pi as the request's `overrides`, #242): the only marks that get a pocket."""
    ov = (req or {}).get("overrides")
    return frozenset(str(o["mark"]) for o in (ov if isinstance(ov, list) else [])
                     if isinstance(o, dict) and o.get("mark"))


def load_obstacles(definition, course_id, full_definition, full_course_id, boat, leg=None,
                   chart=_ON_DISK, overrides=frozenset()):
    """The obstacle mask for the remaining course `definition` → (ObstacleField, chart note or
    None). Land: the Lab's land-only mask for the course, when it was built from this course and
    covers what's left of it, boat included. `chart` is the one the Lab's Deploy push carried (the
    Pi sends it with the request; None = the push had none); left out, the file in CHART_DIR is
    read (a Pi engine from before the Deploy carried it). On top, always, the race's
    zones/islands/barriers rebuilt from the remaining course. No usable land mask: those alone,
    and the result warns that land isn't checked.

    Pockets (#242): the boat always gets one (it is where it is). A mark gets one only when it is
    in `overrides`, the Lab's signed chart overrides: the Orin never opens land at a mark itself."""
    depth = boats.safety_depth_m(boat)
    need = optimizer.course_bbox(definition, course_id)      # the boat + the marks left, padded
    note = NO_CHART
    if full_definition:
        if chart is _ON_DISK:
            chart = _disk_chart(full_definition, full_course_id)
        if isinstance(chart, dict):
            # a land-only mask built from this exact course, zones and depth? A re-deployed course
            # (a new zone, a moved mark) or an older full-race mask must never be used.
            if chart.get("kind") != CHART_KIND or chart.get("fingerprint") != \
                    geo.course_fingerprint(full_definition, full_course_id, depth):
                note = OLD_CHART
            else:
                try:
                    land = geo.field_from_dict(chart)
                except (ValueError, KeyError, TypeError):
                    land = None
                if land is not None and not chart.get("open_water") and not any(land.mask):
                    # an empty mask = the shoreline never loaded when it was built: not "checked"
                    note = NO_LAND
                elif land is not None and need and W._contains(land.bbox, need):
                    # open the pockets in the land mask only, BEFORE the race layer goes on, so
                    # zones, island disks and barriers are never cleared: the boat's own (the
                    # first nav point), and a mark's only with the Lab's signed override (#242)
                    for i, (_s, nm, mla, mlo) in enumerate(
                            race_def.course_to_marks(definition, course_id)[0]):
                        if mla is not None and mlo is not None and (i == 0 or nm in overrides):
                            land._carve_disk(mla, mlo, geo.MARK_CARVE_NM)
                    race = _race_obstacles(definition, course_id, land.bbox, land.res, depth,
                                           leg)
                    n = len(land.mask)
                    if len(race.mask) != n:               # can't happen: same box, same cell size
                        return _race_obstacles(definition, course_id, need, geo.RES_DEG,
                                               depth, leg), NO_CHART
                    land.mask = bytearray((int.from_bytes(land.mask, "little")
                                           | int.from_bytes(race.mask, "little")).to_bytes(n, "little"))
                    for k, v in race.layers.items():
                        land.layers[k] = land.layers.get(k, 0) + v
                    land.active = land.active or race.active
                    return land, None
                else:
                    # outside the mask, land would read as open water: don't claim it was checked
                    note = NO_COVER if land is not None else OLD_CHART
    # no usable land mask: the race's own obstacles for the remaining course, boat included
    return _race_obstacles(definition, course_id, need, geo.RES_DEG, depth, leg), note


# ---- the comparison with the plan -------------------------------------------------------------
def _sea_none(online):
    """The `data` block when no route came back: what the run would have had (#308)."""
    from app import current, seasource, wave
    def why(use):
        return seasource.OFF if not use else (seasource.NO_DATA if online else seasource.OFFLINE)
    return seasource.data_block(current.ZeroCurrent(why(USE_CURRENT)),
                                wave.ZeroWave(why(USE_WAVES)))


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
            # from the two shown numbers, so the crew's sum always adds up (rounded separately,
            # 549.6 - 740.5 showed as 550 - 741 = -190)
            "gain_min": round(plan_left / 60.0) - round(new_left / 60.0)}


# ---- one run ----------------------------------------------------------------------------------
def _peak_rss_mb():
    me = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    kids = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024.0
    return {"process": round(me), "parser": round(kids)}


def _get_boat():
    b = boats.get_boat(BOAT_ID)
    return b


def _session_boat(carried):
    """The Pi named the boat being sailed (#320, `session_boat`): route on exactly the profile it
    sent (from the Deploy push or the boat library aboard), or not at all. Never the Orin's own
    files: they are some other boat's when the crew sails another one."""
    sb = carried.get("session_boat") if isinstance(carried.get("session_boat"), dict) else {}
    b = carried.get("boat")
    if not isinstance(b, dict):
        return None, None, NO_BOAT
    try:
        errs = boat_profile.shape_errors(b) or boat_profile.validate(b)[0]
    except (ValueError, TypeError, OverflowError):
        errs = ["bad values"]
    if b.get("boat_id") != sb.get("boat_id"):
        return None, None, OTHER_BOAT
    if errs or carried.get("boat_version") != boat_profile.version(b):
        return None, None, BAD_BOAT
    src = "deploy" if sb.get("from") == "deploy" else "library"
    return b, _boat_info(b, src, carried.get("received_at") if src == "deploy" else sb.get("at")), None


def pick_boat(carried):
    """The boat profile to route on → (profile or None, info for the result, warning or None).

    A Pi that names the boat being sailed (`session_boat`, #320) decides it: see _session_boat.

    `carried` is the request's `assets` (from the Lab's Deploy push, through the Pi), or None. The
    Deploy's reviewed profile is used when it is this boat's (REROUTE_BOAT), valid, and matches
    the version it was sent with. Otherwise the Orin's own copy (the repo's raw sr33.json unless
    one was copied aboard), and the result says so."""
    if isinstance(carried, dict) and "session_boat" in carried:
        return _session_boat(carried)
    warn = NOT_DEPLOYED_BOAT
    b = (carried or {}).get("boat")
    if isinstance(b, dict):
        try:          # types first (validate converts values), and never a crash on a bad one
            errs = boat_profile.shape_errors(b) or boat_profile.validate(b)[0]
        except (ValueError, TypeError, OverflowError):
            errs = ["bad values"]
        if b.get("boat_id") != BOAT_ID:
            warn = OTHER_BOAT
        elif errs or carried.get("boat_version") != boat_profile.version(b):
            warn = BAD_BOAT
        else:
            return b, _boat_info(b, "deploy", carried.get("received_at")), None
    own = _get_boat()
    if own is None:
        return None, None, NO_BOAT
    return own, _boat_info(own, "orin", None), warn


def _boat_info(b, source, received_at):
    return {"boat_id": b.get("boat_id"), "version": boat_profile.version(b), "source": source,
            "received_at": received_at, "helm_factor": boats.helm_factor(b),
            "polar_cells": len(boats.polar_adjustments(b)), "draft_m": b.get("draft_m")}


def pick_polar(carried, boat, boat_info):
    """The polar to route on → (block, "deploy" | "orin") or (None, None) (#304).

    The deployed boat's own polar, carried with the profile by the Lab's Deploy push, when the
    profile is the deployed one and the block is whole and is that boat's. Otherwise the profile's
    own: its inline ORC cert, or the files it names in this checkout — unless the push said
    "no polar" (an explicit null). Never another boat's."""
    if boat is None:
        return None, None
    if isinstance(carried, dict) and "session_boat" in carried:
        # the Pi sent the boat being sailed and its polar (#320): that polar or none at all
        pol = carried.get("polar")
        if isinstance(pol, dict) and not boat_polar.check(pol, boat.get("boat_id")):
            return pol, (boat_info or {}).get("source") or "library"
        return None, None
    if (boat_info or {}).get("source") == "deploy":
        pol = (carried or {}).get("polar")
        if isinstance(pol, dict) and not boat_polar.check(pol, boat.get("boat_id")):
            return pol, "deploy"
        if "polar" in (carried or {}) and pol is None:
            # Deploy sent no polar on purpose (none for this boat, or it changed since the
            # playbook): its word stands, no fallback (Codex on #319)
            return None, None
    pol = boats.polar_for(boat)
    return (pol, "orin") if pol else (None, None)


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
    # the chart + boat profile from the Lab's Deploy push, carried by the Pi with this request.
    # No `assets` at all: a Pi from before this build; the Orin's own files are used, as before.
    carried = req.get("assets") if isinstance(req.get("assets"), dict) else None
    boat, boat_info, boat_note = pick_boat(carried)
    polar, polar_from = pick_polar(carried, boat, boat_info)
    if polar is None:
        raise RunError(NO_POLAR)
    chart_kw = {"chart": carried.get("chart")} if carried is not None else {}
    chart_kw["overrides"] = override_names(req)      # the Lab's signed chart overrides (#242)
    why_not = boat_in_no_go(d, cid, lat, lon)
    if why_not:
        raise RunError(why_not)
    phase("chart")
    # one mask per leg: island disks, zones and land on every leg; an island's rounding barrier
    # only on the leg that rounds it
    leg_fields = [load_obstacles(d, cid, full, full_cid, boat, leg=i, **chart_kw)
                  for i in range(leg_count(d, cid))]
    field, chart_note = leg_fields[0] if leg_fields else \
        load_obstacles(d, cid, full, full_cid, boat, **chart_kw)
    leg_fields = [f for f, _n in leg_fields]

    # weather: the box the cache already holds, if one covers what's left of the course
    online = (is_online or plume_svc.online)()
    need = optimizer.course_bbox(d, cid)
    known = []
    plan_pts = lab_plume.clean_path(((req.get("plan") or {}).get("points")))
    if len(plan_pts) >= 2:
        known.append(lab_plume._bbox_for({"path": plan_pts}))      # the plume's own box
    known += W.load_boxes(STATE_DIR)
    t_end = now + optimizer.estimate_hours(d, cid) * 3600
    names_req = [m for m in (req.get("models") or MODELS)]

    say("online — new forecasts may download" if online else "offline — using saved forecasts")
    budget = W.Budget(online)
    bbox, why = W.choose_bbox(need, known, online)
    sources = W.sources_for(names_req, budget, bbox, now, t_end, known)
    # one load, one route: a model whose live download comes out short falls back to its best
    # cached cycle inside the loader (GribSource.prev_cycle); everything that loaded fine is kept
    res = routerun.run_course(
        d, cid, now, sources, avoid=True, per_model=False, resolution=RESOLUTION,
        use_waves=USE_WAVES, use_current=USE_CURRENT, offline=not online,
        on_progress=say, on_phase=phase, boat=boat, model_skill=False, obstacles=field,
        leg_obstacles=leg_fields,
        time_budget_s=ROUTE_BUDGET_S, fallback_tries=None if online else 0,
        bbox=bbox, t_end=t_end, briefing=False, grids=False, boat_polar=polar)
    if budget.downloads:
        W.remember_box(STATE_DIR, bbox)
    weather = W.summarize(res.get("windfield"), now, budget, why, sources, names_req)
    extras = {}
    if USE_CURRENT:
        extras["current"] = bool((res.get("current") or {}).get("loaded")) if online else "offline"
    if USE_WAVES:
        extras["waves"] = bool((res.get("waves") or {}).get("loaded")) if online else "offline"
    if extras:
        weather["extras"] = extras
    phase("saving")
    out = {"schema": SCHEMA, "label": LABEL, "off_playbook": True, "source": "orin",
           "job_id": req.get("job_id"), "asked_by": str(req.get("device") or "")[:40] or None,
           "asked_at": req.get("asked_at"), "computed_from": int(now),
           "from": {"lat": round(lat, 5), "lon": round(lon, 5)},
           "marks": names, "weather": weather, "boat": boat_info,
           # #308: the current and wave sources this route used, or "none: why", in words
           "data": res.get("data") or _sea_none(online),
           # which polar this route sailed on: the deployed boat's, or the profile's own here
           "polar": dict(boat_polar.summary(polar), **{"from": polar_from})}
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
        # sea data asked for but missing, plainly, after the rest (the tidal warning is already
        # first); a layer turned off in the settings is only in `data`
        warnings += [n for n in (out["data"].get("notes") or [])
                     if n not in warnings and not n.endswith(": turned off.")]
        if chart_note:
            warnings.insert(0, chart_note)
        if not full:
            warnings.insert(0, NO_ZONES)
        if boat_note == NO_BOAT:
            warnings.insert(0, boat_note)
        elif boat_note:                 # a profile, just not the Lab's reviewed one: after the rest
            warnings.append(boat_note)
        if polar_from not in ("deploy", "library") and boat_note != NO_BOAT:
            warnings.append(NOT_DEPLOYED_POLAR)
        until = weather.get("forecast_until")
        if until and res.get("finish_epoch") and res["finish_epoch"] > until:
            warnings.insert(0, SHORT_FORECAST)
        if weather.get("missing"):
            warnings.append("No forecast from " + ", ".join(weather["missing"]) + ".")
        complete = bool(res.get("complete"))
        # #242: the route's own verdict. A mark left on the wrong side (the corner round it was
        # blocked) or a route off the chart is never a finished route, whatever the legs say
        # (Codex on #287). Land and depth "not checked" are the chart notes above, not these.
        bad = [p.get("text") for p in (res.get("safety") or {}).get("problems") or []
               if p.get("code") in ("side", "box") and p.get("text")]
        if bad:
            complete = False
            for t in reversed(bad):
                warnings.insert(0, t)
        if not complete:
            warnings.insert(0, INCOMPLETE)
        out.update({
            "available": True, "complete": complete, "degraded": res.get("degraded"),
            # a stalled route's end isn't the finish: no finish time, no comparison with the plan
            "finish_eta": res.get("finish_epoch") if complete else None,
            "total_hours": res.get("total_hours") if complete else None,
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
            "vs_plan": (vs_plan(req.get("plan"), lat, lon, now, res.get("finish_epoch"))
                        if complete and res.get("finish_epoch") else
                        {"available": False, "note": INCOMPLETE}),
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
