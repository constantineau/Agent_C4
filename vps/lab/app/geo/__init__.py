"""geo — race-agnostic obstacle avoidance for the optimizer (the "obstacles track").

`obstacles.build_for_course(definition, course_id, bbox)` returns an `ObstacleField` the isochrone
router consults so a route can't cut across land, islands, or a race's exclusion zones. It layers:

  - a GLOBAL coastline (GSHHG full-res by default, Natural Earth fallback; auto-clipped to the course
    bbox) — works for ANY race, ocean or lake, nothing region-hardcoded (`coastline.py`, pluggable);
  - the race's own `zones[]` (exclusion / hazard / tss polygons) — per race;
  - the race's geocoded `island` marks as buffered obstacles — per race.

All three rasterize into one boolean mask the hot routing loop queries in O(1). See README.
"""
from .obstacles import ObstacleField, build_for_course  # noqa: F401
