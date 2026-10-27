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


def _mark_indices(path, marks, tol_nm=0.5):
    """Path vertices that ARE course marks (or the router's rounding standoff beside one): for each
    mark, the nearest vertex within `tol_nm`. Those must survive downsampling — the plotter draws
    the course through them."""
    out = set()
    for m in marks or []:
        mlat, mlon = m.get("lat"), m.get("lon")
        if mlat is None or mlon is None or not path:
            continue
        j = min(range(len(path)), key=lambda i: NAV._hav_nm(path[i]["lat"], path[i]["lon"], mlat, mlon))
        if NAV._hav_nm(path[j]["lat"], path[j]["lon"], mlat, mlon) <= tol_nm:
            out.add(j)
    return out


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


def _dest_index(pts, boat, mark_pos=(), next_mark_pos=None):
    """The plotter's steer-to: project the boat onto the downsampled path and return the index of
    the first waypoint AHEAD of it. The search is confined to the CURRENT leg (between the kept
    vertices of the previous and next marks) so an out-and-back course can't snap the boat onto
    the other leg. No fix → 1 (the first leg's end)."""
    if not boat or boat[0] is None or len(pts) < 2:
        return 1
    lo, hi = 0, len(pts) - 1
    if next_mark_pos is not None:
        nk = [k for k in mark_pos if k > 0]
        hits = [k for k in nk if NAV._hav_nm(pts[k]["lat"], pts[k]["lon"], *next_mark_pos) <= 0.5]
        if hits:
            hi = hits[0]
            prev = [k for k in mark_pos if k < hi]
            lo = max(prev) if prev else 0
    best = min(range(lo, max(lo + 1, hi)),
               key=lambda i: routing._pt_to_seg_nm(boat[0], boat[1], pts[i]["lat"], pts[i]["lon"],
                                                   pts[i + 1]["lat"], pts[i + 1]["lon"]))
    return min(len(pts) - 1, best + 1)


def _course_context(route=None):
    """(marks, boat (lat, lon), next-mark (lat, lon)) from the navigator — best effort; any miss
    degrades to 'no marks / no fix' (the downsample still keeps endpoints, dest falls back to 1)."""
    try:
        nav = NAV.get_navigator(route)
        if not nav.get("available"):
            return [], None, None
        marks = NAV._marks(nav["route"]) or []
        s = NAV._latest() or {}
        boat = (s.get("lat"), s.get("lon")) if s.get("lat") is not None else None
        nxt = (nav.get("next_mark") or {}).get("name")
        nm = next(((m["lat"], m["lon"]) for m in marks if m.get("name") == nxt), None)
        return marks, boat, nm
    except Exception:
        return [], None, None


def _variant_route(variant=None):
    """(label, path) from the frozen bundle — the recommended variant unless one is named."""
    bundle = deviation._load_playbook()
    if not bundle:
        return None, None, "no playbook aboard"
    want = variant or bundle.get("recommended")
    for v in bundle.get("variants") or []:
        vid = str(v.get("id") or v.get("name") or "")
        if vid == str(want) or want is None:
            path = ((v.get("route") or {}).get("path")) or []
            if len(path) >= 2:
                return vid, path, None
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
        if len(path) < 2:
            return {"shown": False, "note": "re-route carries no path"}
    else:
        vid, path, err = _variant_route(variant)
        if err:
            return {"shown": False, "note": err}
        # the wire name is what the plotter shows — short and clean (LAU caps at 16 chars)
        wire_name, label = f"C4 {vid}"[:16], f"{vid} (gameplan)"
    marks, boat, next_pos = _course_context(route)
    keep = _mark_indices(path, marks)
    idx = _downsample_idx(path, MAX_WPTS, keep, _blocked_fn(deviation._load_playbook()))
    pts = [path[i] for i in idx]
    mark_pos = [k for k, i in enumerate(idx) if i in keep]
    dest = _dest_index(pts, boat, mark_pos, next_pos)
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
