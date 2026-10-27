"""GPS-OUT — put a route on the boat's chartplotter, from the iPad's button.

The engine assembles the waypoints (the frozen bundle's variant path, or the onboard
re-optimizer's fresh route) and hands them to the Pi's `n2kout` broadcaster (:8210, host
network), which transmits 129285/129284/129283 onto the N2K bus for the Garmin 943. This
module never touches the bus itself — assembly and a localhost HTTP call, nothing else.

Paths are DOWNSAMPLED to `GPSOUT_MAX_WPTS` (default 24): an isochrone path's ~40+ points
draw noisy on a plotter and chunk into more 129285 messages than the display needs; the
endpoints are always kept. The destination index is the SECOND point (the first leg's end)
— the plotter's "steer to" — until a live-leg refinement is warranted.

Onboard-only (the cloud has no bus): the endpoints answer 'unavailable' when the
broadcaster isn't reachable. Crew-initiated, crew-cleared: nothing broadcasts on its own.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import deviation, reoptimize

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


def _downsample(path, max_n=MAX_WPTS):
    if len(path) <= max_n:
        return list(path)
    stride = (len(path) - 1) / (max_n - 1)
    idx = sorted({round(i * stride) for i in range(max_n)} | {0, len(path) - 1})
    return [path[i] for i in idx if i < len(path)]


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
    pts = _downsample(path)
    wpts = [{"name": f"C4-{i + 1:02d}", "lat": p.get("lat"), "lon": p.get("lon"),
             "t": p.get("t")} for i, p in enumerate(pts)]
    st = _post("/route", {"name": wire_name, "waypoints": wpts, "dest_index": 1})
    if st.get("error"):
        return {"shown": False, "note": st["error"]}
    return {"shown": bool(st.get("broadcasting")), "route": st.get("route"), "label": label,
            "n_waypoints": st.get("n_waypoints"), "downsampled_from": len(path),
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
