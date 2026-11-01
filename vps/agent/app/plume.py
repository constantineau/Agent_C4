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
import math
import os
import tempfile
import threading
import time

SCHEMA = "c4.plume/v1"
MAX_BYTES = 512 * 1024          # a plume is ~10-60 KB; refuse anything absurd
# the model ids the Lab may send (mirrors vps/lab/app/plume.SUPPORTED)
SUPPORTED = ("gfs", "nam", "hrrr", "ecmwf", "icon", "gem")
_TEXT_FIELDS = ("key", "race_id", "label", "basis")
_NUM_FIELDS = ("generated_at", "built_at", "race_start", "race_end")


def _path():
    base = os.path.dirname(os.environ.get("ENGINE_DB", "/var/lib/sr33/engine/engine.db"))
    return os.environ.get("PLUME_CACHE", os.path.join(base, "plume.json"))


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate(plume):
    """(ok, reason). Known model ids only, plain short strings, numbers where numbers belong."""
    if not isinstance(plume, dict) or plume.get("schema") != SCHEMA:
        return False, "not a c4.plume/v1 payload"
    for k in _TEXT_FIELDS:
        v = plume.get(k)
        if v is not None and (not isinstance(v, str) or len(v) > 200):
            return False, f"bad {k}"
    for k in _NUM_FIELDS:
        if plume.get(k) is not None and not _num(plume[k]):
            return False, f"bad {k}"
    if not _num(plume.get("race_start")):
        # fail closed: without a known start the boat can't tell whether its race is on
        return False, "no race start"
    models = plume.get("models")
    if not isinstance(models, list) or not models:
        return False, "no models"
    for m in models:
        if not isinstance(m, dict) or m.get("model") not in SUPPORTED or not isinstance(m.get("runs"), list):
            return False, "unknown or bad model entry"
        for r in m["runs"]:
            if not isinstance(r, dict) or not _num(r.get("cycle")) or not _num(r.get("at")):
                return False, f"bad run for {m['model']}"
            pts = r.get("pts")
            if not isinstance(pts, list) or not all(
                    isinstance(p, list) and len(p) == 3 and all(_num(x) for x in p) for p in pts):
                return False, f"bad run points for {m['model']}"
    return True, ""


def frozen(plume, now):
    """True from the plume's race start until its RECORDED finish (`race_end`, set only when the
    Lab marks the race over) — with no finish, from the start on. The boat keeps the pre-start
    plume (RRS 41: shore-computed, route-specific homework is frozen at the gun)."""
    rs = (plume or {}).get("race_start")
    if rs is None:
        return False
    re_ = plume.get("race_end")
    return rs <= now and (re_ is None or now < re_)


_LOCK = threading.Lock()        # one load at a time: read cache → check → write


def _read():
    try:
        with open(_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load(plume, now=None):
    """Store a pushed plume (atomic replace). Returns the /plume/load response.

    One rule, checked inside the lock against a fresh clock (fail closed, RRS 41):
      * if the cached plume's race is on (started, no recorded finish yet), refuse every incoming
        plume — any race — except that same race carrying its recorded finish (already past);
      * refuse any plume whose own race is on at receipt, or that was computed after its start.
    `now` is for tests only; in service the boat's own clock is read inside the lock."""
    ok, why = validate(plume)
    if not ok:
        return {"loaded": False, "detail": why}
    body = json.dumps({"received_at": 0, "plume": plume}, separators=(",", ":"))
    if len(body) > MAX_BYTES:
        return {"loaded": False, "detail": f"plume too large ({len(body)} B)"}
    with _LOCK:
        t = time.time() if now is None else now
        cur = (_read() or {}).get("plume")
        if cur and frozen(cur, t):
            fin = plume.get("race_end")
            if not (plume.get("race_id") == cur.get("race_id") and fin is not None and fin <= t):
                return {"loaded": False, "detail": "racing — the pre-start plume stays aboard"}
        if frozen(plume, t):
            return {"loaded": False, "detail": "its race is on — frozen at the gun"}
        if frozen(plume, plume.get("generated_at", t)):
            return {"loaded": False, "detail": "computed after the start — frozen at the gun"}
        body = json.dumps({"received_at": int(t), "plume": plume}, separators=(",", ":"))
        p = _path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".plume-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(body)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return {"loaded": True, "key": plume.get("key"), "built_at": plume.get("built_at"),
            "models": len(plume["models"])}


def get(now=None):
    """The cached plume + how old it is. `age_s` = since the newest model run was recorded at the
    Lab (what "data age" means to the crew); `received_age_s` = since the boat last got a push."""
    now = time.time() if now is None else now
    rec = _read()
    if not rec:
        return {"available": False, "note": "no forecast plume aboard yet — it arrives from the "
                "Lab whenever the boat has internet"}
    plume = rec.get("plume") or {}
    built = plume.get("built_at")
    return dict(plume, available=True, received_at=rec.get("received_at"),
                received_age_s=int(now - rec.get("received_at", now)),
                age_s=int(now - built) if built else None, frozen=frozen(plume, now))
