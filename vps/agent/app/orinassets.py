"""The Orin re-route's pre-race assets, kept on the Pi (#139, #140).

The Lab's Deploy push builds them (vps/lab/app/orinassets.py) and posts them here
(`POST /reroute/assets`) right after the course:

  * the course's LAND chart (land-only mask, "land-v2", with a fingerprint of the course, zones and
    no-go depth) — the Orin can't build it, it has no shoreline data;
  * the Lab's reviewed BOAT PROFILE (helm factor, approved polar cells, draft) and its version.

The Pi stores them in one file (atomic replace) and puts them in every re-route request it sends
the Orin (orinroute.build_request). The Orin keeps nothing: it routes on what the request carries,
so it never holds a chart or profile older than the Pi's, and it needn't be on at deploy time.

Pre-race homework only: refused while the boat's race log has a race session running (the same
check, under the same lock, as the shore forecast plume). Loaded only when the owner pushes from
the Lab; nothing here asks for it.
"""
import base64
import json
import math
import os
import tempfile
import threading
import time
import zlib

from shared import boat_profile

from . import plume

SCHEMA = "c4.orin-assets/v1"
FIELD_SCHEMA = "c4.obstacles/v1"
CHART_KIND = "land-v2"               # pi/orin/reroute/job.CHART_KIND
MAX_BYTES = 1536 * 1024              # well under the Orin's 2 MB request cap, with the course
MAX_CELLS = 20_000_000              # = the Lab's obstacles.MAX_FIELD_CELLS
RACING = "Racing now. Not loaded."
_LOCK = threading.Lock()


def _path():
    base = os.path.dirname(os.environ.get("ENGINE_DB", "/var/lib/sr33/engine/engine.db"))
    return os.path.join(base, "orin-assets.json")


def _str(x, n=120):
    return isinstance(x, str) and 0 < len(x) <= n


def grid_dims(chart):
    """(nx, ny) of a carried chart, derived from its box and cell size the way the Lab's
    ObstacleField does, when they match the stated nx/ny and stay under MAX_CELLS; else
    (None, None). Never trusts the stated size alone: a tiny cell size with nx = ny = 1 would make
    the Orin allocate terabytes."""
    try:
        n, s, w, e = (float(x) for x in chart["bbox"])
        res = float(chart["res_deg"])
        stated = (int(chart["nx"]), int(chart["ny"]))
    except (KeyError, TypeError, ValueError, OverflowError):    # e.g. nx = 1e309, a 400-digit int
        return None, None
    if not all(math.isfinite(x) for x in (n, s, w, e, res)) or res <= 0 or n <= s or e <= w:
        return None, None
    qx, qy = (e - w) / res, (n - s) / res
    # a vanishing cell size divides to inf (math.ceil would raise OverflowError → a 500)
    if not (math.isfinite(qx) and math.isfinite(qy)) or qx > MAX_CELLS or qy > MAX_CELLS:
        return None, None
    nx, ny = max(1, int(math.ceil(qx))), max(1, int(math.ceil(qy)))
    if nx * ny > MAX_CELLS or (nx, ny) != stated:
        return None, None
    return nx, ny


def _check_chart(chart, race_id, course_id):
    """(ok, reason) for a carried land chart. The Orin checks it again before routing."""
    if not isinstance(chart, dict):
        return False, "bad chart"
    if chart.get("schema") != FIELD_SCHEMA or chart.get("kind") != CHART_KIND:
        return False, "chart is not land-v2"
    if not _str(chart.get("fingerprint"), 64):
        return False, "chart has no fingerprint"
    if chart.get("race_id") != race_id or chart.get("course_id") != course_id:
        return False, "chart is for another course"
    nx, ny = grid_dims(chart)
    if nx is None:
        return False, "bad chart grid"
    try:
        d = zlib.decompressobj()
        mask = d.decompress(base64.b64decode(chart["mask_zb64"], validate=True), nx * ny + 1)
    except (KeyError, TypeError, ValueError, zlib.error):
        return False, "bad chart mask"
    if len(mask) != nx * ny or d.unconsumed_tail:
        return False, "chart mask doesn't match its grid"
    if mask.translate(None, b"\x00\x01"):
        return False, "chart mask has values other than 0 and 1"
    if chart.get("open_water") not in (None, False, True):
        return False, "bad open-water flag"
    # stored as derived from the mask, never as sent: an inactive field ignores every cell
    chart["active"] = any(mask)
    if not any(mask) and chart.get("open_water") is not True:
        # the shoreline didn't load when it was built: an empty mask is not "land checked"
        return False, "chart has no land in it"
    return True, ""


def validate(body):
    """(ok, reason). Everything or nothing: a bad part refuses the whole load."""
    if not isinstance(body, dict) or body.get("schema") != SCHEMA:
        return False, "not a c4.orin-assets/v1 payload"
    if not _str(body.get("race_id")) or not _str(body.get("course_id")):
        return False, "no race or course"
    boat = body.get("boat")
    if boat is not None:
        if not isinstance(boat, dict):
            return False, "bad boat profile"
        bad = boat_profile.shape_errors(boat)       # types first: validate() converts values
        if bad:
            return False, f"boat profile: {bad[0]}"
        try:
            errs, _w = boat_profile.validate(boat)
        except (ValueError, TypeError, OverflowError):
            return False, "boat profile: bad values"
        if errs:
            return False, f"boat profile: {errs[0]}"
        if body.get("boat_version") != boat_profile.version(boat):
            return False, "boat profile version doesn't match"
        if body.get("boat_id") is not None and body["boat_id"] != boat.get("boat_id"):
            return False, "boat profile is for another boat"
    chart = body.get("chart")
    if chart is not None:
        ok, why = _check_chart(chart, body["race_id"], body["course_id"])
        if not ok:
            return False, why
    return True, ""


def _write(text):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".orin-assets-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def load(body, now=None):
    """Store a pushed chart + boat profile (one file, atomic replace) → the /reroute/assets answer.
    A null chart or profile replaces what was held: the boat never keeps an older one as this
    push's. Refused while a race session is running."""
    # everything that reads the candidate runs BEFORE the atomic replace: a payload that would
    # trip any of it is refused and never replaces good assets
    try:
        ok, why = validate(body)
        if ok:
            t = int(time.time() if now is None else now)
            answer = dict(summary(body, t), loaded=True)
            text = json.dumps({"received_at": t, "assets": body}, separators=(",", ":"),
                              allow_nan=False)
    except (ValueError, TypeError, OverflowError, AttributeError) as exc:
        ok, why = False, f"bad payload ({type(exc).__name__})"
    if not ok:
        return {"loaded": False, "detail": why}
    if len(text) > MAX_BYTES:
        return {"loaded": False, "detail": "Chart and boat profile too large."}
    with _LOCK, plume._session_lock():
        if plume.race_session():
            return {"loaded": False, "detail": RACING}
        _write(text)
    return answer


def _read():
    try:
        with open(_path()) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("assets"), dict):
        return None
    return rec


def summary(a, received_at=None):
    boat = a.get("boat") or {}
    chart = a.get("chart") or {}
    return {"race_id": a.get("race_id"), "course_id": a.get("course_id"),
            "received_at": received_at, "chart": bool(a.get("chart")),
            "chart_source": chart.get("source"), "open_water": bool(chart.get("open_water")),
            "boat_id": boat.get("boat_id"), "boat_version": a.get("boat_version") if boat else None,
            "helm_factor": boat.get("helm_factor"),
            "polar_cells": len(boat.get("polar_adjustments") or []) if boat else None}


def get():
    """What's held for the Orin (no geometry): race, course, chart yes/no, boat id + version."""
    rec = _read()
    try:
        if rec:
            return dict(summary(rec["assets"], rec.get("received_at")), available=True)
    except (ValueError, TypeError, AttributeError):
        pass                       # a file from an older build that summary can't read
    return {"available": False, "note": "No chart or boat profile aboard."}


def for_run(definition, course_id):
    """What goes into a re-route request: {chart, boat, boat_version, received_at}, or None when
    nothing was ever pushed (the Orin then falls back to its own files, as before this build).
    The chart only for the course aboard (same race and course id); the Orin also checks its
    fingerprint against the definition it gets."""
    rec = _read()
    if not rec:
        return None
    a = rec["assets"]
    same = bool(definition) and a.get("race_id") == (definition or {}).get("race_id") \
        and a.get("course_id") == course_id
    return {"chart": a.get("chart") if same else None, "boat": a.get("boat"),
            "boat_version": a.get("boat_version") if a.get("boat") else None,
            "race_id": a.get("race_id"), "course_id": a.get("course_id"),
            "received_at": rec.get("received_at")}
