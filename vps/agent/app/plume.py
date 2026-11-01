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


def _past_end(plume, t):
    re_ = (plume or {}).get("race_end")
    return _num(re_) and re_ <= t


def started(plume, t):
    """The payload's own race has started by `t` (and carries no race_end already past)."""
    rs = (plume or {}).get("race_start")
    return _num(rs) and t >= rs and not _past_end(plume, t)


def race_session():
    """The boat's own race log: the running session if it is a RACE, else None. The crew taps it on
    at the gun and off at the finish on the iPad — the authority on "racing" aboard. If the log
    can't be read, fail closed (treated as racing)."""
    try:
        from . import racelog
        src = racelog._src()
        if src is None:
            return None                # no onboard race log (cloud) — nothing to consult
        cur = src.session_current()
        return cur if cur and str(cur.get("kind") or "").lower() == "race" else None
    except Exception:  # noqa: BLE001
        return {"kind": "race", "start_ts": None, "note": "race log unreadable"}


_LOCK = threading.Lock()        # one load at a time: check → write


def _read():
    try:
        with open(_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load(plume, now=None):
    """Store a pushed plume (atomic replace). Returns the /plume/load response.

    Stateless and boat-authoritative (RRS 41: a shore-computed, route-specific forecast is
    pre-start homework). Inside the lock, on the boat's own clock, a plume is refused when:
      a) the boat's race log has a race session running;
      b) it has no finite race_start (checked in validate);
      c) its race has started (now >= race_start) and it carries no race_end already past;
      d) it was computed after its race started (generated_at >= race_start, no past race_end).
    `now` is for tests only; in service the clock is read inside the lock."""
    ok, why = validate(plume)
    if not ok:
        return {"loaded": False, "detail": why}
    if len(json.dumps(plume, separators=(",", ":"))) + 40 > MAX_BYTES:
        return {"loaded": False, "detail": "plume too large"}
    with _LOCK:
        t = time.time() if now is None else now
        if race_session():
            return {"loaded": False, "detail": "a race session is running — not taken while racing"}
        if started(plume, t):
            return {"loaded": False, "detail": "its race has started — not taken"}
        gen = plume.get("generated_at")
        if _num(gen) and started(plume, gen):
            return {"loaded": False, "detail": "computed after its race started — not taken"}
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
    sess = race_session()
    frozen = bool(sess) or started(plume, now)
    frozen_at = None
    if frozen:   # the moment it froze: the race start if passed, else the session's start
        frozen_at = plume.get("race_start") if started(plume, now) else (sess or {}).get("start_ts")
    return dict(plume, available=True, received_at=rec.get("received_at"),
                received_age_s=int(now - rec.get("received_at", now)),
                age_s=int(now - built) if built else None, frozen=frozen, frozen_at=frozen_at)
