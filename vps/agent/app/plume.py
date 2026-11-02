"""Forecast plume cache — the Lab's multi-model, run-by-run forecast, kept ON THE BOAT.

The Lab computes the plume (every router weather model sampled along the planned route, one
snapshot per model cycle — `vps/lab/app/plume.py`) because that needs eccodes/cfgrib, which the
Pi doesn't have. The Lab pushes the compact JSON here (`POST /plume/load`) over the same Tailscale
line its Deploy push uses, whenever the boat is reachable. This module writes it to disk next to
the engine store so it survives a restart and the boat going offline, and serves it back with its
age so the console can say how old the forecast is.

Legal in-race: it's common public forecast data (the same GRIB/open-data models anyone can pull)
loaded before or between connections — the boat never asks anyone for advice.

Two sources, two files:
  * SHORE (`POST /plume/load` → plume.json): the Lab's plume. Pre-start homework only — refused
    once a race is on (RRS 41), and shown frozen from the gun.
  * ONBOARD (`POST /plume/onboard` → plume-onboard.json): the same plume computed on the boat's
    own Orin from public model data it downloads itself (pi/orin/plume). Own computer + public
    data is legal in-race, so it is taken while racing. It is only accepted from the Orin's
    boat-local address (the direct Pi↔Orin cable, `PLUME_ONBOARD_FROM`, default 10.10.10.2) and
    never through a proxy, so nothing on shore can post as "onboard".
`get` serves whichever of the two holds the newer weather, judged by model cycle (`prefer_onboard`).
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


def _onboard_path():
    return os.path.join(os.path.dirname(_path()), "plume-onboard.json")


def onboard_from():
    """The addresses an onboard plume may come from: the Orin's end of the direct Pi↔Orin cable.
    Never loopback or a Tailscale address — those are how shore (or the console proxy) reaches
    the Pi."""
    raw = os.environ.get("PLUME_ONBOARD_FROM", "10.10.10.2")
    return {a.strip() for a in raw.split(",") if a.strip()}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate(plume, need_race_start=True):
    """(ok, reason). Known model ids only, plain short strings, numbers where numbers belong.
    A shore plume must carry its race_start (`need_race_start`); an onboard one needn't."""
    if not isinstance(plume, dict) or plume.get("schema") != SCHEMA:
        return False, "not a c4.plume/v1 payload"
    for k in _TEXT_FIELDS:
        v = plume.get(k)
        if v is not None and (not isinstance(v, str) or len(v) > 200):
            return False, f"bad {k}"
    for k in _NUM_FIELDS:
        if plume.get(k) is not None and not _num(plume[k]):
            return False, f"bad {k}"
    if need_race_start and not _num(plume.get("race_start")):
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
    """The boat's race log check, shared by every homework load (app/racegate.py): the running
    RACE session, else None; an unreadable log counts as racing. Imported lazily, so this file
    also loads on its own (the Lab's tests load it by path); then it fails closed."""
    try:
        from . import racegate
    except Exception:  # noqa: BLE001
        return {"kind": "race", "start_ts": None, "note": "race gate unavailable"}
    return racegate.race_session()


def _session_lock():
    """The race log's session lock (racegate.session_lock), held across the check and the write.
    Standalone, race_session() already fails closed, so a private lock is enough."""
    try:
        from . import racegate
    except Exception:  # noqa: BLE001
        return threading.Lock()
    return racegate.session_lock()


RACING = "Racing now. Not loaded."
_LOCK = threading.Lock()        # one load at a time: check → write


def _read(path=None):
    try:
        with open(path or _path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path, body):
    """Atomic replace: a reader never sees half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".plume-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


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
        return {"loaded": False, "detail": "Forecast spread too large."}
    with _LOCK, _session_lock():
        t = time.time() if now is None else now
        if race_session():
            return {"loaded": False, "detail": RACING}
        if started(plume, t):
            return {"loaded": False, "detail": "Race already started. Not loaded."}
        gen = plume.get("generated_at")
        if _num(gen) and started(plume, gen):
            return {"loaded": False, "detail": "Made after the start. Not loaded."}
        _write(_path(), json.dumps({"received_at": int(t), "plume": plume}, separators=(",", ":")))
    return {"loaded": True, "key": plume.get("key"), "built_at": plume.get("built_at"),
            "models": len(plume["models"])}


def is_waiting(plume):
    """The Orin's marker: "no onboard forecast for this spot yet" (the target changed while offline
    and nothing cached covers it). Replaces the old onboard plume so it isn't shown as current."""
    return isinstance(plume, dict) and plume.get("waiting") is True and plume.get("models") == []


def validate_waiting(plume):
    if plume.get("schema") != SCHEMA:
        return False, "not a c4.plume/v1 payload"
    for k in _TEXT_FIELDS:
        v = plume.get(k)
        if v is not None and (not isinstance(v, str) or len(v) > 200):
            return False, f"bad {k}"
    for k in _NUM_FIELDS:
        if plume.get(k) is not None and not _num(plume[k]):
            return False, f"bad {k}"
    return True, ""


WAITING_NOTE = "Onboard forecast: waiting for internet."


# Forwarding headers mean the request came through a proxy (the console's nginx on the Pi passes
# client headers through), so the peer address is not the sender's. An onboard plume never takes
# that path: the Orin posts straight to the engine over the cable.
_PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host")


def load_onboard(plume, client_ip, headers=None, now=None):
    """Store a plume the boat's own Orin computed (atomic replace). Taken while racing: it is the
    boat's own computer on public data. Refused unless it comes straight from the Orin's
    boat-local address (`onboard_from`), with no proxy in between, and says `source: onboard`.
    The shore rules in `load` are untouched."""
    hdrs = {str(k).lower() for k in (headers or {})}
    if any(h in hdrs for h in _PROXY_HEADERS):
        return {"loaded": False, "detail": "onboard plumes are not taken through a proxy"}
    if not client_ip or client_ip not in onboard_from():
        return {"loaded": False, "detail": "onboard plumes come only from the Orin's boat-local link"}
    ok, why = validate_waiting(plume) if is_waiting(plume) else validate(plume, need_race_start=False)
    if not ok:
        return {"loaded": False, "detail": why}
    if plume.get("source") != "onboard":
        return {"loaded": False, "detail": "not an onboard plume"}
    if len(json.dumps(plume, separators=(",", ":"))) + 40 > MAX_BYTES:
        return {"loaded": False, "detail": "plume too large"}
    with _LOCK:
        t = time.time() if now is None else now
        _write(_onboard_path(), json.dumps({"received_at": int(t), "plume": plume},
                                           separators=(",", ":")))
    return {"loaded": True, "key": plume.get("key"), "built_at": plume.get("built_at"),
            "models": len(plume["models"]), "waiting": is_waiting(plume)}


def _built(rec):
    b = ((rec or {}).get("plume") or {}).get("built_at")
    return b if _num(b) else 0


def _cycles(rec):
    """{model: newest model cycle (epoch)} in a stored plume."""
    out = {}
    for m in ((rec or {}).get("plume") or {}).get("models") or []:
        cs = [r.get("cycle") for r in m.get("runs") or [] if _num(r.get("cycle"))]
        if cs:
            out[m.get("model")] = max(cs)
    return out


def prefer_onboard(onboard, shore):
    """Which plume holds the newer WEATHER — judged by model cycle, not by when it was sampled
    (a shore GFS 12Z recorded at 16:00 beats an onboard GFS 06Z sampled at 16:15). Over the models
    both carry, the source with the newer cycle on more of them wins; a tie goes to the one whose
    freshest shared model is newer; still tied (or nothing shared), the more recently recorded
    (`built_at`), and onboard on a full tie."""
    if not onboard or not onboard.get("plume"):
        return False
    if is_waiting(onboard["plume"]):
        return False                          # a marker carries no weather; a shore plume wins
    if not shore:
        return True
    a, b = _cycles(onboard), _cycles(shore)
    common = set(a) & set(b)
    if common:
        votes = sum((a[m] > b[m]) - (a[m] < b[m]) for m in common)
        if votes:
            return votes > 0
        fa, fb = max(a[m] for m in common), max(b[m] for m in common)
        if fa != fb:
            return fa > fb
    return _built(onboard) >= _built(shore)


def get(now=None):
    """The plume to show + how old it is: whichever of onboard / shore holds the newer model runs
    (`prefer_onboard`). `age_s` = since the newest model run was recorded (what "data age" means
    to the crew); `received_age_s` = since the boat last got it."""
    now = time.time() if now is None else now
    shore, onboard = _read(), _read(_onboard_path())
    if prefer_onboard(onboard, shore):
        return _onboard_view(onboard, now)
    if not shore:
        if onboard and is_waiting(onboard.get("plume")):
            return {"available": False, "source": "onboard", "waiting": True, "note": WAITING_NOTE,
                    "key": onboard["plume"].get("key"), "received_at": onboard.get("received_at")}
        return {"available": False, "note": "No forecast spread yet. It arrives when the boat has internet."}
    return _shore_view(shore, now)


def _onboard_view(rec, now):
    """Computed aboard, so never frozen: it keeps updating through the race."""
    plume = {k: v for k, v in (rec.get("plume") or {}).items() if k not in ("race_start", "race_end")}
    built = plume.get("built_at")
    return dict(plume, available=True, source="onboard", received_at=rec.get("received_at"),
                received_age_s=int(now - rec.get("received_at", now)),
                age_s=int(now - built) if _num(built) else None, frozen=False, frozen_at=None)


def _shore_view(rec, now):
    plume = rec.get("plume") or {}
    built = plume.get("built_at")
    sess = race_session()
    frozen = bool(sess) or started(plume, now)
    frozen_at = None
    if frozen:   # the moment it froze: the race start if passed, else the session's start
        frozen_at = plume.get("race_start") if started(plume, now) else (sess or {}).get("start_ts")
    return dict(plume, available=True, source="shore", received_at=rec.get("received_at"),
                received_age_s=int(now - rec.get("received_at", now)),
                age_s=int(now - built) if built else None, frozen=frozen, frozen_at=frozen_at)
