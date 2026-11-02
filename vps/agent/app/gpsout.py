"""GPS-OUT — put a route on the boat's chartplotter, from the iPad's button.

The engine assembles the waypoints (the frozen bundle's variant path, or the onboard
re-optimizer's fresh route) and hands them to the Pi's `n2kout` broadcaster (:8210, host
network), which transmits 129285/129284/129283 onto the N2K bus for the Garmin 943. This
module never touches the bus itself — assembly and a localhost HTTP call, nothing else.

Paths are DOWNSAMPLED to `GPSOUT_MAX_WPTS` (default 24): an isochrone path's ~40+ points
draw noisy on a plotter and chunk into more 129285 messages than the display needs. The
endpoints and every course-mark vertex are always kept, and a shortcut between two kept
points that would cut an island / exclusion zone from the frozen obstacle homework is split
back toward the router's own vertices (safety beats the point budget). The destination index
— the plotter's "steer to" — is the first waypoint AHEAD of the boat, found by projecting its
position onto the path within the current leg. With no fresh own-ship fix nothing is sent.

After the send, n2kout keeps the leg live by itself: once a second it reads `fix_feed()` (the
engine's GET /gps/fix — the freshest own-ship position and the course token) and advances the
waypoint and recomputes XTE / distance / bearing / ETA. The token is the course load id + the
active route: any course change (a /course/load, a practice course, a switch) changes it and
n2kout ends the route with "navigation terminated", even if the explicit clear never arrived.

Onboard-only (the cloud has no bus): the endpoints answer 'unavailable' when the
broadcaster isn't reachable. Crew-initiated, crew-cleared: nothing broadcasts on its own.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request

from . import datasource, deviation, landchart, orinroute, reoptimize, routing
from . import navigator as NAV

N2KOUT_URL = os.environ.get("N2KOUT_URL", "http://127.0.0.1:8210").rstrip("/")
MAX_WPTS = int(os.environ.get("GPSOUT_MAX_WPTS", "24"))
POST_TIMEOUT_S = float(os.environ.get("GPSOUT_POST_TIMEOUT_S", "3"))
# GEOMETRY-FREE GUARD on the thinning. The obstacle test only knows the obstacles aboard (the
# bundle's + the deployed course's); the Pi holds no land, and a route can bend round anything
# its router knew. So a thinned segment may stand in for the router's vertices only while every
# vertex it drops lies within DEV_TOL_NM of it: the exported line stays on the computed one.
# 0.05 nm (~90 m) is the plotter's own arrival radius (ARRIVE_NM), well inside what a crew
# steers to at race zoom, and loose enough that the router's straight runs (dozens of
# collinear steps) still collapse: a real route keeps its tacks, gybes and bends, not its steps.
# Garmin chartplotters take up to 250 points per route (to be confirmed on the 943 dockside);
# MAX_WPTS (24) is the drawing budget, not the limit. When even that tolerance needs more than
# HARD_MAX_WPTS points the route is refused, never thinned further: a shortcut is worse than no
# route on the plotter.
DEV_TOL_NM = float(os.environ.get("GPSOUT_DEV_TOL_NM", "0.05"))
HARD_MAX_WPTS = int(os.environ.get("GPSOUT_HARD_MAX_WPTS", "250"))
TOO_DETAILED = "Route too detailed for the GPS. Not sent."
NO_SAFE_WPT = "No safe waypoint from here. Press Re-route now."
NO_SHORE = "GPS route doesn't check the shore."
PI_CROSSES_LAND = "Pi route crosses land. Not sent to GPS."
CROSSES_LAND = "Route crosses land. Not sent to GPS."
NO_FIX = "No GPS fix. Not sent to GPS."
NOT_ACTIVE = "Route isn't the active course. Switch to it first."
# a fix older than this is no fix (n2kout applies the same limit to its live nav data)
FIX_MAX_AGE_S = float(os.environ.get("GPSOUT_FIX_MAX_AGE_S", "10"))


def _fix_with_source(now=None):
    """(lat, lon, epoch, source) of the freshest own-ship position, lat and lon from the SAME
    source (the source policy's pick among sources no older than FIX_MAX_AGE_S: a fresh backup
    GPS takes over from a preferred one that went quiet), else None. Only the live Signal K
    cache counts: the archive fallback can be minutes old."""
    src = datasource.active()
    getter = getattr(src, "latest_position", None)
    if getter is None:
        return None
    try:
        fix = getter(max_age_s=FIX_MAX_AGE_S)
    except Exception:  # noqa: BLE001 — no fix read is no fix
        return None
    if not fix:
        return None
    now = time.time() if now is None else now
    lat, lon, t = fix[:3]
    if now - t > FIX_MAX_AGE_S:
        return None
    return lat, lon, t, (fix[3] if len(fix) > 3 else None)


def own_fix(now=None):
    """(lat, lon, epoch) — see _fix_with_source."""
    fix = _fix_with_source(now)
    return None if fix is None else fix[:3]


def course_token():
    """Which course is aboard: the course load id (new on every /course/load, practice course
    or switch) + the active route. Cheap: one small file read."""
    try:
        load = (orinroute._read(orinroute._load_id_path()) or {}).get("load_id") or "noload"
        return f"{load}:{NAV.active_route()}"
    except Exception:  # noqa: BLE001
        return None


def leg_check(body):
    """POST /gps/leg-check — n2kout asks before every advance (and every 30 s for the current
    leg): is boat → waypoint clear of every obstacle aboard? The same test the send makes: the
    bundle's and the deployed course's islands + zones and the land chart (_obstacles +
    _blocked_fn). → {"clear": bool, "reason"}. Not clear when the course changed, when the
    course lock is busy (a load in progress) or on any error: n2kout then pauses the nav data."""
    try:
        a, b = body["from"], body["to"]
        frm = {"lat": float(a["lat"]), "lon": float(a["lon"])}
        to = {"lat": float(b["lat"]), "lon": float(b["lon"])}
        if not all(math.isfinite(v) for v in (*frm.values(), *to.values())):
            raise ValueError("not finite")
    except (KeyError, TypeError, ValueError):
        return {"clear": False, "reason": "bad request"}
    if not orinroute.COURSE_LOCK.acquire(timeout=1.5):
        return {"clear": False, "reason": "busy"}
    try:
        course = body.get("course")
        if course is not None and course != course_token():
            return {"clear": False, "reason": "course changed"}
        bundle = deviation._load_playbook()
        try:
            obst = _obstacles(bundle, None, (frm["lat"], frm["lon"]), strict=True)
        except landchart.ChartUnreadable:
            return {"clear": False, "reason": "chart unreadable"}
        blocked = _blocked_fn(bundle, obst)
        if blocked is not None and blocked(frm, to):
            return {"clear": False, "reason": "obstacle"}
        # no obstacle homework aboard → nothing known in the way (as at send time)
        return {"clear": True, "reason": None if blocked is not None else "no obstacles aboard",
                "next_mark_index": next_mark_index()}
    except Exception as exc:  # noqa: BLE001 — a failed check is never a clear one
        print(f"[gpsout] leg check failed: {type(exc).__name__}: {exc}", flush=True)
        return {"clear": False, "reason": "error"}
    finally:
        orinroute.COURSE_LOCK.release()


NEXT_MARK_TTL_S = 5.0
_NEXT_MARK = {"at": None, "val": None, "course": None}


def course_progress(lock_timeout=0.3):
    """(course token, the navigator's next mark as a position in THAT course) read together
    under COURSE_LOCK, so the index can never be paired with another course's token. The index
    is cached NEXT_MARK_TTL_S per token (n2kout polls every second, the navigator does real
    work); a new token is never served an old course's index. Lock busy (a load or a send in
    progress) → (token read without the lock, None): no verdict this second."""
    if not orinroute.COURSE_LOCK.acquire(timeout=lock_timeout):
        return course_token(), None
    try:
        tok = course_token()
        now = time.monotonic()
        if (_NEXT_MARK["course"] == tok and _NEXT_MARK["at"] is not None
                and now - _NEXT_MARK["at"] < NEXT_MARK_TTL_S):
            return tok, _NEXT_MARK["val"]
        try:
            nav = NAV.get_navigator()
            val = (reoptimize._next_course_index(nav, NAV._marks(nav["route"]) or [])
                   if nav.get("available") else None)
        except Exception:  # noqa: BLE001 — no navigator read is no verdict
            val = None
        _NEXT_MARK.update({"at": now, "val": val, "course": tok})
        return tok, val
    finally:
        orinroute.COURSE_LOCK.release()


def next_mark_index():
    return course_progress()[1]


def fix_feed():
    """GET /gps/fix — what n2kout polls every second to keep the plotter's leg live: the fix,
    the course token, and the navigator's next-mark index (a course-mark waypoint advances
    when the navigator says it's rounded, however wide)."""
    fix = _fix_with_source()
    tok, nmi = course_progress()
    return {"fix": None if fix is None else {"lat": fix[0], "lon": fix[1], "t": fix[2],
                                             "source": fix[3]},
            "course": tok, "next_mark_index": nmi}


def _post(path, payload=None, timeout=None):
    req = urllib.request.Request(N2KOUT_URL + path, method="POST",
                                 data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        # short: show() holds the course lock across this call, so a slow broadcaster can hold up
        # a /course/load for at most this long
        with urllib.request.urlopen(req, timeout=timeout or POST_TIMEOUT_S) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return {"available": False, "error": f"n2kout unreachable: {e}"}


def _get(path):
    try:
        with urllib.request.urlopen(N2KOUT_URL + path, timeout=4) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return {"available": False, "error": f"n2kout unreachable: {e}"}


def _valid_ends(ends, n):
    """A usable `leg_end_idx`: ints, strictly increasing, inside a path of n points, none at 0."""
    if not ends or not isinstance(ends, (list, tuple)):
        return False
    if not all(isinstance(i, int) and not isinstance(i, bool) for i in ends):
        return False
    return ends[0] > 0 and all(a < b for a, b in zip(ends, ends[1:])) and ends[-1] < n


def _mark_vertices(path, marks, tol_nm=0.5, leg_end_idx=None, first_leg_to=1):
    """{course mark number: path index} for the marks this path sails through.

    Preferred: `leg_end_idx` — the path index where each leg ended, recorded by the router as it
    built the route leg by leg (leg j ends at course mark `first_leg_to + j`; a whole-course route
    starts AT mark 0, a re-route starts at the boat with `first_leg_to` = the next mark). That is the
    real rounding, never a guess.

    Fallback (routes frozen before the router carried it): an ORDERED best fit — each mark gets one
    vertex, strictly after the previous mark's, choosing the assignment with the least total
    distance over the whole path. So a tack that passes 0.4 nm from Top on the way up loses to the
    actual rounding, and a Finish on the Start's spot takes the last vertex, not the first. A mark
    with no position consumes nothing; a best fit farther than `tol_nm` is dropped (that mark isn't
    on this path)."""
    n = len(path)
    if _valid_ends(leg_end_idx, n):
        out = {0: 0} if first_leg_to == 1 else {}
        for j, i in enumerate(leg_end_idx):
            k = first_leg_to + j
            if not marks or k < len(marks):
                out[k] = i
        return out
    ks = [k for k, m in enumerate(marks or [])
          if m and m.get("lat") is not None and m.get("lon") is not None]
    if not ks or not n:
        return {}
    strict = len(ks) <= n
    dist = {k: [NAV._hav_nm(p["lat"], p["lon"], marks[k]["lat"], marks[k]["lon"]) for p in path]
            for k in ks}
    cost, back = list(dist[ks[0]]), []
    for k in ks[1:]:
        cur, ptr = [float("inf")] * n, [None] * n
        best, arg = float("inf"), None
        for i in range(n):
            if not strict and cost[i] < best:      # non-strict: the same vertex may serve twice
                best, arg = cost[i], i
            if best < float("inf"):
                cur[i], ptr[i] = dist[k][i] + best, arg
            if strict and cost[i] < best:          # strict: only vertices BEFORE i feed i
                best, arg = cost[i], i
        cost = cur
        back.append(ptr)
    i = min(range(n), key=lambda x: cost[x])
    if cost[i] == float("inf"):
        return {}
    chosen = [i]
    for ptr in reversed(back):
        i = ptr[i]
        chosen.append(i)
    chosen.reverse()
    return {k: i for k, i in zip(ks, chosen) if dist[k][i] <= tol_nm}


def _worst_dropped(path, i, j):
    """(index, nm) of the vertex strictly between i and j farthest from the segment i→j."""
    a, b = path[i], path[j]
    worst, at = 0.0, None
    for k in range(i + 1, j):
        d = routing._pt_to_seg_nm(path[k]["lat"], path[k]["lon"], a["lat"], a["lon"],
                                  b["lat"], b["lon"])
        if d > worst:
            worst, at = d, k
    return at, worst


def _downsample_idx(path, max_n=MAX_WPTS, keep=(), blocked=None, dev_tol_nm=DEV_TOL_NM):
    """Indices of the kept vertices. Endpoints + `keep` (mark vertices) always; a uniform stride
    fills the rest of the budget; then every shortcut between consecutive kept vertices is split
    until it is safe on BOTH counts: `blocked(a, b)` says it cuts no known obstacle (split at the
    midpoint), and every vertex it drops lies within `dev_tol_nm` of it (split at the farthest
    one; None turns this off). Consecutive router vertices are the router's own checked steps.
    May exceed `max_n` only to stay safe."""
    n = len(path)
    if n == 0:
        return []
    idx = {0, n - 1} | {k for k in keep if 0 <= k < n}
    if n <= max_n:
        idx = set(range(n))
    else:
        room = max(2, max_n - len(idx) + 2)
        stride = (n - 1) / (room - 1)
        idx |= {round(i * stride) for i in range(room)}
    idx = sorted(idx)
    if blocked is None and dev_tol_nm is None:
        return idx
    out, stack = [idx[0]], list(reversed(idx[1:]))
    while stack:
        j = stack.pop()
        i = out[-1]
        if j - i > 1 and blocked is not None and blocked(path[i], path[j]):
            stack += [j, (i + j) // 2]          # split: route through the midpoint first
            continue
        if j - i > 1 and dev_tol_nm is not None:
            k, d = _worst_dropped(path, i, j)
            if k is not None and d > dev_tol_nm:
                stack += [j, k]                 # split at the vertex it strays farthest from
                continue
        out.append(j)
    return out


def _downsample(path, max_n=MAX_WPTS, keep=(), blocked=None):
    return [path[i] for i in _downsample_idx(path, max_n, keep, blocked)]


def _blocked_fn(bundle, obstacles=None):
    """Segment test against the obstacles aboard: `obstacles` = (disks, polygons[, land]) from
    _obstacles (the bundle's AND the deployed course's islands + zones, what the re-route was
    computed around, and the deployed land chart); without it, the bundle's alone. Used for the
    thinning AND the boat→steer-to line. None when there's nothing."""
    land = None
    if obstacles is not None:
        disks, polys = obstacles[0], obstacles[1]
        land = obstacles[2] if len(obstacles) > 2 else None
    else:
        disks = reoptimize._avoid_disks(bundle)
        zdisks, polys = reoptimize._parse_zones(bundle)
        disks = disks + zdisks
    if not disks and not polys and land is None:
        return None
    return lambda a, b: ((disks and routing._seg_blocked(a["lat"], a["lon"], b["lat"], b["lon"], disks))
                         or (polys and routing._seg_crosses_poly(a["lat"], a["lon"], b["lat"], b["lon"],
                                                                 polys))
                         or (land is not None and land.crosses(a["lat"], a["lon"], b["lat"], b["lon"])))


ARRIVE_NM = 0.05     # boat this close to a waypoint has attained it — steer to the one after


def _dest_index(pts, boat, mark_pos=None, next_k=None, blocked=None):
    """The plotter's steer-to: project the boat onto the downsampled path and return the index of
    the first waypoint AHEAD of it. `mark_pos` is {course mark number: index in pts}; with the
    navigator's next-mark number `next_k` the search is confined to the CURRENT leg (previous
    mark's vertex → next mark's), so an out-and-back course can't snap the boat onto the other
    leg. Next mark = the path's first vertex (pre-start) → 0. No fix → None: the caller refuses
    (never a default waypoint).

    Moving PAST the projected waypoint — the later segment winning a tie (boat on a waypoint, or
    off a corner both segments are nearest at), or a waypoint the boat is within ARRIVE_NM of — is
    a shortcut: the boat would steer straight at the waypoint after it. That is allowed only when
    `blocked` (the same obstacle test the downsample used) says the boat→that-waypoint line is
    clear. A waypoint may be an obstacle-avoidance vertex, so with no obstacle test available the
    attained waypoint is kept (conservative).

    ONE invariant at the single exit (with an obstacle test): the boat→destination line is clear.
    Whatever picked the index, a blocked one walks back to the nearest preceding waypoint the boat
    can reach in a straight line, never before the current leg's start; if none can, None — the
    caller refuses the push rather than steer through a hazard."""
    if not boat or boat[0] is None or len(pts) < 2:
        return None
    dest, lo = _dest_candidate(pts, boat, mark_pos, next_k, blocked)
    if blocked is None:
        return dest
    here = {"lat": boat[0], "lon": boat[1]}
    while dest >= lo and blocked(here, pts[dest]):
        dest -= 1
    return dest if dest >= lo else None


def _dest_candidate(pts, boat, mark_pos, next_k, blocked):
    """(destination index, current leg's first index) before the clear-line guard. The index never
    exceeds the next mark's vertex when the navigator's next mark is known."""
    last = len(pts) - 1
    lo, hi = 0, last
    mark_pos = mark_pos or {}
    if next_k is not None and next_k in mark_pos:
        hi = mark_pos[next_k]
        if hi == 0:
            return 0, 0
        prev = [v for k, v in mark_pos.items() if k < next_k and v < hi]
        lo = max(prev) if prev else 0
    here = {"lat": boat[0], "lon": boat[1]}
    dist = {i: round(routing._pt_to_seg_nm(boat[0], boat[1], pts[i]["lat"], pts[i]["lon"],
                                           pts[i + 1]["lat"], pts[i + 1]["lon"]), 6)
            for i in range(lo, max(lo + 1, hi))}
    best = min(dist, key=lambda i: (dist[i], i))          # earliest nearest segment
    dest = min(last, best + 1)

    def _clear_to(j):
        return blocked is not None and not blocked(here, pts[j])

    # never past `hi`: the plotter doesn't steer beyond an unrounded mark — only the navigator's
    # mark advance moves the leg on
    if dist.get(best + 1) == dist[best] and dest < hi and _clear_to(dest + 1):
        dest += 1                                         # tie → the later segment, if clear
    while (dest < hi and NAV._hav_nm(boat[0], boat[1], pts[dest]["lat"], pts[dest]["lon"]) <= ARRIVE_NM
           and _clear_to(dest + 1)):
        dest += 1                                         # attained → the next one, if clear
    return dest, lo


CHART_UNREADABLE = "Chart unreadable. Not sent to GPS."


def _obstacles(bundle, route=None, boat=None, strict=False):
    """(disks, polygons, land) of every obstacle aboard for the navigator's course: the bundle's
    and the deployed definition's islands + zones (reoptimize.course_avoid), and the deployed LAND
    chart when it is this course's (landchart.for_course: the same chart, checked the same way,
    the Orin routes on), else None. Read under the course lock show() holds, so it is the course
    the route was just checked against.

    "No chart aboard" (land None) is a known state: the send goes out with the shore warning. A
    chart aboard that can't be read raises landchart.ChartUnreadable — always: never treated as
    "no chart", so nothing is sent or approved across land nobody could check. `strict` (the
    live leg check) also raises when the course itself can't be read, and reads the active
    course directly when the navigator has no answer (no position yet)."""
    try:
        nav = NAV.get_navigator(route)
        r = nav.get("route") if nav.get("available") else None
        if r is None and strict:
            r = route or NAV.active_route()
        marks = (NAV._marks(r) or []) if r else []
    except Exception:  # noqa: BLE001
        if strict:
            raise
        r, marks = None, []
    disks, polys, _d, _c, _n = reoptimize.course_avoid(r, marks, bundle)
    try:
        land = landchart.for_course(r, marks, boat)
    except landchart.ChartUnreadable as exc:
        print(f"[gpsout] land chart unreadable: {exc}", flush=True)
        raise
    except Exception as exc:  # noqa: BLE001 — any failure reading a chart is "unreadable"
        print(f"[gpsout] land chart unreadable: {exc}", flush=True)
        raise landchart.ChartUnreadable(str(exc)) from exc
    return disks, polys, land


def _course_context(route=None):
    """ONE navigator snapshot: (course marks, boat (lat, lon), next-mark NAME, next-mark course
    INDEX — which occurrence on a lapped course; None when it can't be told). The boat fix is the
    position the navigator itself used to pick the next mark (its `position`), so the fix and the
    next mark can't come from two different moments; a separate latest-fix read is only the
    fallback when the navigator doesn't report one. Any miss → ([], None, None): the downsample
    still keeps endpoints and the dest falls back to 1."""
    try:
        nav = NAV.get_navigator(route)
        if not nav.get("available"):
            return [], None, None, None
        marks = NAV._marks(nav["route"]) or []
        pos = nav.get("position") or {}
        if pos.get("lat") is None:
            pos = NAV._latest() or {}
        boat = (pos.get("lat"), pos.get("lon")) if pos.get("lat") is not None else None
        return (marks, boat, (nav.get("next_mark") or {}).get("name"),
                reoptimize._next_course_index(nav, marks))
    except Exception:
        return [], None, None, None


def _variant(variant=None, bundle=None):
    """(label, route dict, error) from ONE bundle snapshot — the route carries `leg_end_idx` (and
    its legs' `to` names) when the Lab recorded them."""
    if not bundle:
        return None, None, "No gameplan loaded."
    want = variant or bundle.get("recommended")
    for v in bundle.get("variants") or []:
        vid = str(v.get("id") or v.get("name") or "")
        if vid == str(want) or want is None:
            route = v.get("route") or {}
            if len(route.get("path") or []) >= 2:
                return vid, route, None
            return vid, None, f"'{vid}' has no track."
    return None, None, f"'{want}' isn't in the gameplan."


def _route_frame(path, ends, names, nav_marks, starts_at_mark):
    """The route's OWN mark frame: {local mark: path index}. Local mark j is `names[j]` — the mark
    leg j of THIS route ends at, as the route itself recorded it — and -1 is the course start when
    the route begins at it (a whole-course variant). Leg ends from the router when they line up
    with the names; else the ordered best fit against those marks' positions (looked up by name in
    the navigator's course — positions are static, the numbering stays the route's)."""
    verts = {}
    if names and _valid_ends(ends, len(path)) and len(ends) == len(names):
        verts = _mark_vertices(path, None, leg_end_idx=ends, first_leg_to=0)
    elif names:
        by_name = {m.get("name"): m for m in nav_marks or []}
        verts = _mark_vertices(path, [by_name.get(n) for n in names])
    if starts_at_mark:
        verts[-1] = 0
    return verts


NO_NEW_ROUTE = "No new route yet. Press Re-route now."
OLD_COURSE = "Route is for an old course. Press Re-route now."
INCOMPLETE = "Route incomplete. Not sent to GPS."


def _course_ok(ro):
    """Was this route made for the course aboard now (same route, same marks AND positions)? A
    route with no hash is from before the check, so it counts as another course."""
    h = ro.get("course_hash")
    return bool(h) and h == orinroute.current_course_hash()


def _new_route(source, route):
    """The stored re-route to send → (route dict, None) or (None, crew-facing note). Never computes
    one: routes are made only on the crew's Re-route now button (#125). 'orin' is the Orin's last
    route, 'reoptimize' the Pi's, 'reroute' whichever of the two is newest."""
    if source == "reroute":
        _src, ro = orinroute.latest()
    elif source == "orin":
        ro = orinroute.stored()
    else:
        ro = reoptimize.get_reoptimize(route)
    if not ro or not ro.get("available"):
        note = (ro or {}).get("note") if source == "reoptimize" else None
        return None, note or NO_NEW_ROUTE
    if ro.get("complete") is False:
        return None, INCOMPLETE              # it stalled short of the finish: never a GPS route
    if not _course_ok(ro):
        return None, OLD_COURSE              # moved marks / another course since: old coordinates
    ro = dict(ro)
    if "path" not in ro and ro.get("source") == "orin":
        ro["path"] = ro.get("points") or []          # the Orin's track is `points`
    return ro, None


def show(source: str = "playbook", variant=None, route=None):
    """See _show. Runs under the course lock (orinroute.COURSE_LOCK, the one /course/load holds
    for its whole load): the course check, the route assembly against the navigator's marks and
    the broadcast all see ONE course, so a reload can't land between the check and the send.
    This runs in the engine process, the same one that serves /course/load; the n2kout POST is
    bounded by POST_TIMEOUT_S, so the lock is held a few seconds at most."""
    with orinroute.COURSE_LOCK:
        # the live leg after the send (course token, mark progress, leg checks) follows the
        # ACTIVE course, so the route must be that course. The iPad never names another one
        # (it sends no `route`); a direct caller that does is refused, never mixed.
        if route and route != NAV.active_route():
            return {"shown": False, "dest_index": None, "note": NOT_ACTIVE}
        return _show(source, variant, route)


def _show(source: str = "playbook", variant=None, route=None):
    """Assemble + broadcast. source='playbook' (frozen variant, default recommended), or a stored
    re-route: 'reroute' (the newest), 'orin' or 'reoptimize' (the Pi's). Only on the crew's tap.

    Inputs come from different moments — the route (frozen at the gun, or computed seconds to
    minutes ago), the navigator (now) and the bundle. They are reconciled explicitly: one bundle
    read, one navigator snapshot (fix + next mark together), and the route's marks numbered in the
    ROUTE's own frame (its legs' names / the re-route's `marks`); the navigator's next mark is then
    LOCATED in that frame by name. A re-route whose passed marks the boat has since rounded is
    trimmed to the leg the boat is on; one that doesn't contain the navigator's next mark at all
    is stale and refused by name."""
    bundle = deviation._load_playbook()
    rerouted = source in ("reroute", "orin", "reoptimize")
    ro = None
    if rerouted:
        ro, why = _new_route(source, route)
        if ro is None:
            return {"shown": False, "note": why or NO_NEW_ROUTE}
        wire_name, label, path = "C4 REROUTE", "re-route (off-book)", ro.get("path") or []
        ends, names = ro.get("leg_end_idx"), list(ro.get("marks") or [])
        ro_idx = ro.get("mark_idx")
        if len(path) < 2:
            return {"shown": False, "note": "New route has no track."}
    else:
        vid, vroute, err = _variant(variant, bundle)
        if err:
            return {"shown": False, "note": err}
        path, ends = vroute["path"], vroute.get("leg_end_idx")
        names = [l.get("to") for l in (vroute.get("legs") or []) if l.get("to")]
        # the wire name is what the plotter shows — short and clean (LAU caps at 16 chars)
        wire_name, label = f"C4 {vid}"[:16], f"{vid} (gameplan)"
    nav_marks, boat, next_name, next_idx = _course_context(route)
    fix = own_fix()
    if fix is None:
        return {"shown": False, "dest_index": None, "note": NO_FIX}
    # the fresh same-source fix, never the navigator's read (which can fall back to the archive),
    # for the steer-to choice and every obstacle / land check below; the navigator gives only
    # the next mark
    boat = (fix[0], fix[1])
    nav_names = [m.get("name") for m in nav_marks]
    if source == "playbook" and not names and len(nav_marks) > 1:
        # a variant frozen before legs carried names: assume it sails the navigator's course
        names = nav_names[1:]
    verts = _route_frame(path, ends, names, nav_marks, starts_at_mark=(source == "playbook"))

    # Which COURSE position each of the route's marks is — the occurrence, not just the name (a
    # lapped course repeats Top). A whole-course variant whose legs name the navigator's course
    # mark for mark is positions 1..N; a re-route records its own `mark_idx`. Either is trusted
    # only when it names the same marks as the navigator's course at those positions.
    route_idx = None
    if rerouted:
        if (isinstance(ro_idx, list) and len(ro_idx) == len(names) and all(
                isinstance(i, int) and 0 <= i < len(nav_names) and nav_names[i] == n
                for i, n in zip(ro_idx, names))):
            route_idx = ro_idx
    elif names and nav_names[1:] == names:
        route_idx = list(range(1, len(names) + 1))

    # where is the boat in the ROUTE's frame?
    nxt = None
    if next_name is None:
        if rerouted and names:
            nxt = 0                                    # no navigator: the route's own next mark
    elif route_idx is not None and next_idx is not None:
        if next_idx in route_idx:
            nxt = route_idx.index(next_idx)
        elif source == "playbook" and next_idx == 0:
            nxt = -1                                   # pre-start: steering to the start
        elif rerouted:
            return {"shown": False, "note": (
                f"New route is stale: starts at {names[0]} (mark {route_idx[0] + 1}), next mark is "
                f"{next_name} (mark {next_idx + 1}). Re-route now, then send.")}
    else:
        # positions can't be aligned: a NAME is only trusted when it names one occurrence
        hits = names.count(next_name)
        if hits == 1:
            nxt = names.index(next_name)
        elif (source == "playbook" and hits == 0 and nav_names
              and next_name == nav_names[0] and nav_names.count(next_name) == 1):
            nxt = -1
        elif rerouted and names:
            why = ("isn't on it" if hits == 0 else
                   f"is on it {hits}×, can't tell which lap")
            return {"shown": False, "note": (
                f"New route is stale: next mark {next_name} {why}. Re-route now, then send.")}
    # local mark → its POSITION in the navigator's course (what n2kout compares with the
    # navigator's next-mark index to tell a wide rounding): by position when the route's marks
    # line up with the course, else by name when that name occurs once; the start is 0
    def _course_pos(j):
        if j == -1:
            return 0
        if route_idx is not None and 0 <= j < len(route_idx):
            return route_idx[j]
        if 0 <= j < len(names) and nav_names.count(names[j]) == 1:
            return nav_names.index(names[j])
        return None

    course_of = {j: _course_pos(j) for j in verts}
    if rerouted and nxt:
        # the boat rounded marks while the re-route was computing: drop the passed legs so the
        # route starts at the last rounded mark and the plotter can't steer back to it
        cut = verts.get(nxt - 1)
        if cut is None:
            return {"shown": False, "note": (
                f"New route is stale: you've passed {names[nxt - 1]} since. Re-route now, then send.")}
        path = path[cut:]
        verts = {j - nxt: i - cut for j, i in verts.items() if j >= nxt}
        course_of = {j - nxt: c for j, c in course_of.items() if j >= nxt}
        nxt = 0

    try:
        obstacles = _obstacles(bundle, route, boat)
    except landchart.ChartUnreadable:
        return {"shown": False, "dest_index": None, "note": CHART_UNREADABLE}
    blocked = _blocked_fn(bundle, obstacles)
    idx = _downsample_idx(path, MAX_WPTS, set(verts.values()), blocked)
    if len(idx) > HARD_MAX_WPTS:
        return {"shown": False, "note": TOO_DETAILED}
    pts = [path[i] for i in idx]
    where = {i: j for j, i in enumerate(idx)}
    dest = _dest_index(pts, boat, {k: where[i] for k, i in verts.items() if i in where}, nxt,
                       blocked)
    # course-mark waypoints: n2kout never advances past them on the turn bisector (crossing that
    # isn't rounding the mark) — only on the arrival radius or the navigator's rounding
    mark_wps = {where[i]: course_of.get(j) for j, i in verts.items() if i in where}
    wpts = [{"name": f"C4-{i + 1:02d}", "lat": p.get("lat"), "lon": p.get("lon"),
             "t": p.get("t"), "mark": i in mark_wps, "mark_idx": mark_wps.get(i)}
            for i, p in enumerate(pts)]
    if dest is None:
        return {"shown": False, "dest_index": None,
                "note": NO_SAFE_WPT if rerouted else
                ("Not sent: every waypoint ahead is behind a hazard. Steer clear "
                 "by eye, then send again.")}
    land = obstacles[2]
    if land is not None:
        # The thinning trusts consecutive router vertices (the router's own steps) — but the Pi's
        # fallback router never sees land, and a plan may come from a coarser chart. So EVERY
        # exported segment, and the boat's line to the steer-to waypoint, is checked against the
        # land chart once more; one crossing refuses the send.
        legs = list(zip(pts, pts[1:]))
        legs.append(({"lat": boat[0], "lon": boat[1]}, pts[dest]))
        if any(land.crosses(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in legs):
            pi_route = rerouted and (ro or {}).get("source") == "pi"
            return {"shown": False, "dest_index": None,
                    "note": PI_CROSSES_LAND if pi_route else CROSSES_LAND}
    # shore-checked only when the chart covers the boat AND every waypoint: the boat→steer-to
    # line was then checked too
    shore = bool(land is not None and land.covers(boat[0], boat[1])
                 and all(land.covers(p["lat"], p["lon"]) for p in pts))
    st = _post("/route", {"name": wire_name, "waypoints": wpts, "dest_index": dest,
                          "course": course_token()})
    if st.get("error"):
        return {"shown": False, "note": st["error"]}
    return {"shown": bool(st.get("broadcasting")), "route": st.get("route"), "label": label,
            "n_waypoints": st.get("n_waypoints"), "downsampled_from": len(path),
            "dest_index": dest, "shore_checked": shore,
            # no land chart for this course (or the route leaves it): said, never implied
            "warning": None if shore else NO_SHORE,
            "broadcaster": st}


CLEAR_TIMEOUT_S = 1.5


def clear_other_courses():
    """A course change: stop the plotter route unless it was sent for the course aboard NOW
    (`keep_course` = the current token). Called under COURSE_LOCK by /course/load, so it must be
    short: n2kout's stop retires the worker at once and doesn't wait long for it, and this call
    gives up after CLEAR_TIMEOUT_S. Even if it fails, n2kout ends the old route itself within a
    second, from the token on the fix feed. Because it names the course to keep, a late stop
    can never end a route sent for the new course."""
    st = _post("/stop", {"keep_course": course_token()}, timeout=CLEAR_TIMEOUT_S)
    if st.get("error"):
        return {"cleared": False, "note": st["error"]}
    return {"cleared": not st.get("broadcasting", False), "broadcaster": st}


def clear():
    st = _post("/stop")
    if st.get("error"):
        return {"cleared": False, "note": st["error"]}
    return {"cleared": not st.get("broadcasting", False), "broadcaster": st}


def status(route=None):
    st = _get("/status")
    if st.get("error"):
        return {"available": False, "note": st["error"]}
    bundle = deviation._load_playbook() or {}
    return {"available": True, **st,
            "variants": [str(v.get("id") or v.get("name") or "") for v in
                         (bundle.get("variants") or [])],
            "recommended": bundle.get("recommended")}
