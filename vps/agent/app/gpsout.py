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
position onto the path within the current leg; with no fix it falls back to the second point.

Onboard-only (the cloud has no bus): the endpoints answer 'unavailable' when the
broadcaster isn't reachable. Crew-initiated, crew-cleared: nothing broadcasts on its own.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import deviation, reoptimize, routing
from . import navigator as NAV

N2KOUT_URL = os.environ.get("N2KOUT_URL", "http://127.0.0.1:8210").rstrip("/")
MAX_WPTS = int(os.environ.get("GPSOUT_MAX_WPTS", "24"))


def _post(path, payload=None):
    req = urllib.request.Request(N2KOUT_URL + path, method="POST",
                                 data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
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


def _downsample_idx(path, max_n=MAX_WPTS, keep=(), blocked=None):
    """Indices of the kept vertices. Endpoints + `keep` (mark vertices) always; a uniform stride
    fills the rest of the budget; then every shortcut between consecutive kept vertices that
    `blocked(a, b)` says cuts an obstacle is split at its midpoint until it is clear (consecutive
    router vertices are the router's own checked steps). May exceed `max_n` only to stay clear."""
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
    if blocked is None:
        return idx
    out, stack = [idx[0]], list(reversed(idx[1:]))
    while stack:
        j = stack.pop()
        i = out[-1]
        if j - i > 1 and blocked(path[i], path[j]):
            stack += [j, (i + j) // 2]          # split: route through the midpoint first
            continue
        out.append(j)
    return out


def _downsample(path, max_n=MAX_WPTS, keep=(), blocked=None):
    return [path[i] for i in _downsample_idx(path, max_n, keep, blocked)]


def _blocked_fn(bundle):
    """Segment test against the bundle's frozen obstacle homework (islands + zones) — the same
    geometry the onboard router prunes against. None when there's nothing to hit."""
    disks = reoptimize._avoid_disks(bundle)
    zdisks, polys = reoptimize._parse_zones(bundle)
    disks = disks + zdisks
    if not disks and not polys:
        return None
    return lambda a, b: (routing._seg_blocked(a["lat"], a["lon"], b["lat"], b["lon"], disks)
                         or routing._seg_crosses_poly(a["lat"], a["lon"], b["lat"], b["lon"], polys))


ARRIVE_NM = 0.05     # boat this close to a waypoint has attained it — steer to the one after


def _dest_index(pts, boat, mark_pos=None, next_k=None):
    """The plotter's steer-to: project the boat onto the downsampled path and return the index of
    the first waypoint AHEAD of it. `mark_pos` is {course mark number: index in pts}; with the
    navigator's next-mark number `next_k` the search is confined to the CURRENT leg (previous
    mark's vertex → next mark's), so an out-and-back course can't snap the boat onto the other
    leg. Ties (the boat exactly on a waypoint, or off a corner where both segments are nearest
    at it) go to the LATER segment, and a waypoint the boat has reached (within ARRIVE_NM) is
    skipped. Next mark = the path's first vertex (pre-start) → 0. No fix → 1."""
    if not boat or boat[0] is None or len(pts) < 2:
        return 1
    last = len(pts) - 1
    lo, hi = 0, last
    mark_pos = mark_pos or {}
    if next_k is not None and next_k in mark_pos:
        hi = mark_pos[next_k]
        if hi == 0:
            return 0
        prev = [v for k, v in mark_pos.items() if k < next_k and v < hi]
        lo = max(prev) if prev else 0
    best = min(range(lo, max(lo + 1, hi)),
               key=lambda i: (round(routing._pt_to_seg_nm(boat[0], boat[1], pts[i]["lat"], pts[i]["lon"],
                                                          pts[i + 1]["lat"], pts[i + 1]["lon"]), 6),
                              -i))
    dest = min(last, best + 1)
    while dest < last and NAV._hav_nm(boat[0], boat[1], pts[dest]["lat"], pts[dest]["lon"]) <= ARRIVE_NM:
        dest += 1
    return dest


def _course_context(route=None):
    """(marks, boat (lat, lon), next-mark number) from the navigator — best effort; any miss
    degrades to 'no marks / no fix' (the downsample still keeps endpoints, dest falls back to 1).
    The next-mark number is its position in the course sequence (names are unique even when two
    marks share a spot, e.g. Finish == Start)."""
    try:
        nav = NAV.get_navigator(route)
        if not nav.get("available"):
            return [], None, None
        marks = NAV._marks(nav["route"]) or []
        s = NAV._latest() or {}
        boat = (s.get("lat"), s.get("lon")) if s.get("lat") is not None else None
        nxt = (nav.get("next_mark") or {}).get("name")
        k = next((i for i, m in enumerate(marks) if m.get("name") == nxt), None)
        return marks, boat, k
    except Exception:
        return [], None, None


def _variant(variant=None):
    """(label, route dict, error) — the route carries `leg_end_idx` when the Lab recorded it."""
    bundle = deviation._load_playbook()
    if not bundle:
        return None, None, "no playbook aboard"
    want = variant or bundle.get("recommended")
    for v in bundle.get("variants") or []:
        vid = str(v.get("id") or v.get("name") or "")
        if vid == str(want) or want is None:
            route = v.get("route") or {}
            if len(route.get("path") or []) >= 2:
                return vid, route, None
            return vid, None, f"variant '{vid}' carries no route track"
    return None, None, f"variant '{want}' not in the bundle"


def show(source: str = "playbook", variant=None, route=None):
    """Assemble + broadcast. source='playbook' (frozen variant, default recommended) or
    'reoptimize' (the onboard re-router's cached fresh route, computing it if needed)."""
    if source == "reoptimize":
        ro = reoptimize.get_reoptimize(route)
        if not ro.get("available"):
            return {"shown": False, "note": ro.get("note") or "no re-route available"}
        wire_name, label, path = "C4 REROUTE", "re-route (off-book)", ro.get("path") or []
        ends = ro.get("leg_end_idx")
        if len(path) < 2:
            return {"shown": False, "note": "re-route carries no path"}
    else:
        vid, vroute, err = _variant(variant)
        if err:
            return {"shown": False, "note": err}
        path, ends = vroute["path"], vroute.get("leg_end_idx")
        # the wire name is what the plotter shows — short and clean (LAU caps at 16 chars)
        wire_name, label = f"C4 {vid}"[:16], f"{vid} (gameplan)"
    marks, boat, next_k = _course_context(route)
    if source == "reoptimize" and next_k is not None:
        # the re-route starts at the boat: marks astern aren't on it (blank them so they can't
        # consume a later vertex — Start would otherwise grab a Finish that shares its spot)
        marks = [None if k < next_k else m for k, m in enumerate(marks)]
    if source == "reoptimize":
        # re-route leg j ends at course mark next_k + j; without the navigator's next mark the
        # numbering is unknown, so don't guess it
        verts = (_mark_vertices(path, marks, leg_end_idx=ends, first_leg_to=next_k)
                 if next_k is not None else {})
    else:
        verts = _mark_vertices(path, marks, leg_end_idx=ends)
    idx = _downsample_idx(path, MAX_WPTS, set(verts.values()), _blocked_fn(deviation._load_playbook()))
    pts = [path[i] for i in idx]
    where = {i: j for j, i in enumerate(idx)}
    dest = _dest_index(pts, boat, {k: where[i] for k, i in verts.items() if i in where}, next_k)
    wpts = [{"name": f"C4-{i + 1:02d}", "lat": p.get("lat"), "lon": p.get("lon"),
             "t": p.get("t")} for i, p in enumerate(pts)]
    st = _post("/route", {"name": wire_name, "waypoints": wpts, "dest_index": dest})
    if st.get("error"):
        return {"shown": False, "note": st["error"]}
    return {"shown": bool(st.get("broadcasting")), "route": st.get("route"), "label": label,
            "n_waypoints": st.get("n_waypoints"), "downsampled_from": len(path),
            "dest_index": dest,
            "broadcaster": st}


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
