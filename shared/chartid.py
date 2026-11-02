"""What a carried LAND chart was built for — one implementation for the Lab, the Pi and the Orin.

The Lab builds a course's land mask and stamps it with `course_fingerprint` (vps/lab/app/
orinassets.py); the Orin refuses a mask whose fingerprint doesn't match the course it routes
(pi/orin/reroute/job.py); the Pi's GPS export checks it the same way before it trusts the chart
(vps/agent/app/landchart.py). All three resolve the no-go depth with `safety_depth_m`, so they can
never disagree about which chart belongs to which course.
"""
import hashlib
import json

from shared import boat_profile

DEFAULT_SAFETY_DEPTH_M = 2.63      # SR33: 7 ft draft + 0.5 m under-keel margin


def safety_depth_m(boat, default=DEFAULT_SAFETY_DEPTH_M):
    """No-go depth (draft + margin) for boat profile dict `boat`; `default` with no boat/draft."""
    if boat and boat.get("draft_m") is not None:
        return boat_profile.safety_depth_m(boat)
    return default


def course_fingerprint(definition, course_id, safety_depth):
    """What a carried land mask was built from: the course (marks, islands, rounding sides), the
    race's zones and the no-go depth. A re-deployed course (a new exclusion zone) never matches an
    old mask."""
    courses = (definition or {}).get("courses") or []
    course = next((c for c in courses if c.get("id") == course_id), None) or \
        (courses[0] if courses else None)
    payload = json.dumps({"course": course, "zones": (definition or {}).get("zones") or [],
                          "depth": None if safety_depth is None else round(float(safety_depth), 3)},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode()).hexdigest()[:16]
