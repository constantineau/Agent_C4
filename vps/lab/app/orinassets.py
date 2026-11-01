"""The Orin re-route's pre-race assets, built here and carried by the Deploy push (#139, #140).

The boat's Orin runs the Lab's optimizer for a re-route (pi/orin/reroute). Two things it can't make
itself:

  * the course's LAND chart: the Orin has no GDAL and no shoreline data, so the Lab builds the
    land-only mask (format "land-v2") with a fingerprint of the course, zones and no-go depth;
  * the reviewed BOAT PROFILE (helm factor, approved polar cells, draft) on the Lab's volume.

`build` makes both into one payload (`c4.orin-assets/v1`). The Deploy push posts it to the Pi
engine (`POST /reroute/assets`) right after the course; the Pi keeps it and hands it to the Orin
with each re-route request. Built only when the owner presses Push: nothing here runs on its own.

The chart and the profile go together, so the chart's no-go depth is always the carried boat's.
The boat is the one the Deploy is for (`DEPLOY_BOAT_ID`, default sr33 = C4), not whichever boat
is active in the Lab at the time.
"""
import os
import time

from shared import boat_profile

from . import boats, labstate, optimizer
from .geo import obstacles as geo

SCHEMA = "c4.orin-assets/v1"
# the chart format the Orin takes: land only, no waypoint pockets (pi/orin/reroute/job.CHART_KIND)
CHART_KIND = "land-v2"
LAND_LAYERS = ("coastline", "shoal", "obstruction")

# Lab-facing reasons (shown on the Deploy step)
NO_MARKS = "Course has no mark positions. No chart built."
NO_LAND = "No land in the chart. The shoreline didn't load."
NO_BOAT = "No boat profile for the boat."
BUILD_FAILED = "Chart build failed. Try again."


def boat_id():
    return os.environ.get("DEPLOY_BOAT_ID", "sr33")


def build_chart(definition, course_id, boat, open_water=False):
    """The course's land-only mask for the Orin → (chart dict, None) or (None, reason).

    The Lab's own choices (its chart source), the carried boat's draft. LAND ONLY: the race's
    zones, island disks and rounding barriers are rebuilt on the Orin from what's left of the
    course, so the barrier of an island already rounded can't block a later leg. No waypoint
    pockets either: a pocket at a near-shore mark would clear that shore for every later leg; the
    Orin opens pockets for the boat and the marks still to sail, each run."""
    bbox = optimizer.course_bbox(definition, course_id)
    if not bbox:
        return None, NO_MARKS
    src = (labstate.get("coastline_source") or geo.COASTLINE_SOURCE or "natural_earth").lower()
    depth = boats.safety_depth_m(boat)
    field = geo.build_for_course(definition, course_id, bbox, source=src, safety_depth=depth,
                                 zones_on=False, islands_on=False, carve_waypoints=False)
    # The builder never fails: if the shoreline couldn't load it returns an EMPTY mask, which the
    # boat would take as "land checked". Refuse that unless the box really is open water.
    land_cells = sum(field.layers.get(k, 0) for k in LAND_LAYERS)
    if not land_cells and not open_water:
        return None, NO_LAND
    out = geo.field_to_dict(field)
    out["open_water"] = bool(open_water and not land_cells)
    # what it was built from: the Orin checks this against the course aboard before using it
    out.update(race_id=(definition or {}).get("race_id"), course_id=course_id, kind=CHART_KIND,
               fingerprint=geo.course_fingerprint(definition, course_id, depth))
    return out, None


def build(definition, course_id, now=None):
    """The Deploy payload → (payload, problems). `problems` lists what's missing (empty when the
    chart and the profile are both in it). A missing part is sent as null, never left out: the
    boat then drops what it held, so an older chart or profile is never taken for this push's."""
    problems = []
    boat = boats.get_boat(boat_id())
    if boat is None:
        problems.append(NO_BOAT)
    else:
        errs, _warns = boat_profile.validate(boat)
        if errs:
            problems.append(f"Boat profile has errors: {errs[0]}.")
            boat = None
    chart = None
    if boat is not None:              # the chart's depth is the carried boat's draft
        try:
            chart, why = build_chart(definition, course_id, boat)
        except Exception as exc:  # noqa: BLE001 — reported on the Deploy step, never half-sent
            print(f"[deploy] chart build failed: {type(exc).__name__}: {exc}", flush=True)
            chart, why = None, BUILD_FAILED
        if why:
            problems.append(why)
    payload = {"schema": SCHEMA, "race_id": (definition or {}).get("race_id"),
               "course_id": course_id, "built_at": int(time.time() if now is None else now),
               "chart": chart, "boat": boat,
               "boat_version": boat_profile.version(boat) if boat else None}
    return payload, problems
