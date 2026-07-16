"""Shareable read-only route-player links — "send the gameplan to the crew".

POST /api/share (team-gated) freezes the client's current gameplan — the same body the PDF
report endpoint receives — into a compact JSON bundle stored under an unguessable token.
GET /share/<token> (open, no team login: the 128-bit token IS the access) serves the player
page; GET /share/<token>/data serves the bundle. The bundle is a frozen snapshot — a later
re-optimize never mutates an already-shared link. The PDF report auto-creates a share and
prints the link + QR, so the emailed document always carries the live view.
"""
import json
import os
import re
import secrets
import time

SHARE_DIR = os.environ.get("SHARE_DIR", "/srv/shares")
PUBLIC_URL = os.environ.get("LAB_PUBLIC_URL", "https://lab.racertracer.net").rstrip("/")

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")

# Exactly what MapView.render() reads (vps/lab/web/mapview.js) plus the scalars the player
# header shows. Everything else in the optimize result stays out of the public bundle.
_MAP_FIELDS = ("path", "marks", "legs", "wind_grid", "current_grid", "wave_grid",
               "isochrones", "laylines", "candidate_paths", "obstacles", "start_epoch",
               "sail_plan")     # the map's peel chips + live current-sail chip read this
_SCALAR_FIELDS = ("race_id", "course_id", "route_confidence", "eta_utc", "total_nm",
                  "distance_nm", "roundings")


def build_bundle(body: dict) -> dict:
    r = (body or {}).get("result") or {}
    result = {k: r[k] for k in _MAP_FIELDS + _SCALAR_FIELDS if r.get(k) is not None}
    return {"race_name": (body or {}).get("race_name") or r.get("race_id") or "C4 gameplan",
            "boat": (body or {}).get("boat") or "",
            "created_epoch": time.time(),
            "result": result}


def create(body: dict) -> dict:
    bundle = build_bundle(body)
    if not (bundle["result"].get("path") and bundle["result"].get("legs")):
        raise ValueError("no optimized route to share — run the optimizer first")
    os.makedirs(SHARE_DIR, exist_ok=True)
    token = secrets.token_urlsafe(16)
    with open(os.path.join(SHARE_DIR, token + ".json"), "w") as f:
        json.dump(bundle, f, separators=(",", ":"))
    return {"token": token, "url": f"{PUBLIC_URL}/share/{token}"}


def load(token: str):
    """Bundle for a token, or None. The token regex also keeps path traversal out."""
    if not _TOKEN_RE.match(token or ""):
        return None
    p = os.path.join(SHARE_DIR, token + ".json")
    if not os.path.isfile(p):
        return None
    with open(p) as f:
        return json.load(f)
