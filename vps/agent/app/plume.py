"""Forecast plume cache — the Lab's multi-model, run-by-run forecast, kept ON THE BOAT.

The Lab computes the plume (every router weather model sampled along the planned route, one
snapshot per model cycle — `vps/lab/app/plume.py`) because that needs eccodes/cfgrib, which the
Pi doesn't have. The Lab pushes the compact JSON here (`POST /plume/load`) over the same Tailscale
line its Deploy push uses, whenever the boat is reachable. This module writes it to disk next to
the engine store so it survives a restart and the boat going offline, and serves it back with its
age so the console can say how old the forecast is.

Legal in-race: it's common public forecast data (the same GRIB/open-data models anyone can pull)
loaded before or between connections — the boat never asks anyone for advice.
"""
import json
import os
import time

SCHEMA = "c4.plume/v1"
MAX_BYTES = 512 * 1024          # a plume is ~10-60 KB; refuse anything absurd


def _path():
    base = os.path.dirname(os.environ.get("ENGINE_DB", "/var/lib/sr33/engine/engine.db"))
    return os.environ.get("PLUME_CACHE", os.path.join(base, "plume.json"))


def validate(plume):
    """(ok, reason). Shape check only — enough that the console can't be fed garbage."""
    if not isinstance(plume, dict) or plume.get("schema") != SCHEMA:
        return False, "not a c4.plume/v1 payload"
    models = plume.get("models")
    if not isinstance(models, list) or not models:
        return False, "no models"
    for m in models:
        if not isinstance(m, dict) or not m.get("model") or not isinstance(m.get("runs"), list):
            return False, "bad model entry"
        for r in m["runs"]:
            pts = r.get("pts") if isinstance(r, dict) else None
            if not isinstance(pts, list) or not all(
                    isinstance(p, list) and len(p) == 3 and all(isinstance(x, (int, float)) for x in p)
                    for p in pts):
                return False, f"bad run points for {m.get('model')}"
    return True, ""


def load(plume, now=None):
    """Store a pushed plume (atomic replace). Returns the /plume/load response."""
    ok, why = validate(plume)
    if not ok:
        return {"loaded": False, "detail": why}
    body = json.dumps({"received_at": int(now if now is not None else time.time()),
                       "plume": plume}, separators=(",", ":"))
    if len(body) > MAX_BYTES:
        return {"loaded": False, "detail": f"plume too large ({len(body)} B)"}
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write(body)
    os.replace(tmp, p)
    return {"loaded": True, "key": plume.get("key"), "built_at": plume.get("built_at"),
            "models": len(plume["models"])}


def get(now=None):
    """The cached plume + how old it is. `age_s` = since the newest model run was recorded at the
    Lab (what "data age" means to the crew); `received_age_s` = since the boat last got a push."""
    now = time.time() if now is None else now
    try:
        with open(_path()) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return {"available": False, "note": "no forecast plume aboard yet — it arrives from the "
                "Lab whenever the boat has internet"}
    plume = rec.get("plume") or {}
    built = plume.get("built_at")
    return dict(plume, available=True, received_at=rec.get("received_at"),
                received_age_s=int(now - rec.get("received_at", now)),
                age_s=int(now - built) if built else None)
