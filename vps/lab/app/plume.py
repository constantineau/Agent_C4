"""Forecast plume — every router model's wind along the planned route, run by run.

The router blends several weather models into one wind field. The crew also wants to see the
models SEPARATELY, and how each one has changed from run to run: a "spaghetti plume" of wind
speed / direction vs time for the next ~24 h, latest run bold, older runs fading.

What this module does:

  * **Where/when.** The plume is sampled ALONG THE PLANNED ROUTE at the time the boat would reach
    each point (the active race's recommended playbook variant `route.path` = [{lat, lon, t}]),
    the same idea as `forecast_ref.build_fingerprint`. Before the start the boat sits at the first
    route point, after the finish at the last one. No route → a fixed point (the race start).
  * **Which weather.** The SAME sources the Lab router uses (`wind.models.MODELS`): the NOMADS GRIB
    models + ECMWF open data + the Open-Meteo-regridded ICON/GEM, each sampled on its own (no
    blend), so the lines are the models the router actually routes through. Ensembles are left out
    (dozens of members per run).
  * **History.** A snapshot per model per new cycle — ~30 hourly points of [t, tws, twd], a few
    hundred bytes — kept `PLUME_KEEP_H` (48 h) per model in one JSON file per route target on the
    `PLUME_DIR` volume (like pbstore/retrostore persist on theirs).
  * **Boat.** `build_plume` makes the compact payload the onboard engine caches and the console
    draws; the recorder pushes it to the Pi engine (`POST /plume/load`) over the SAME Tailscale
    line and address the Lab's Deploy push uses (`DEPLOY_PI_ENGINE_URL`), retrying each cycle
    until the boat answers. The Pi has no eccodes/cfgrib, so it never computes any of this.

The recorder (`start_recorder`) only runs when `PLUME_RECORDER=1`, so tests and ad-hoc Lab
processes never fetch weather or write to the boat on their own.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time

PLUME_DIR = os.environ.get("PLUME_DIR", "/srv/plume")
SCHEMA = "c4.plume/v1"
KEEP_S = float(os.environ.get("PLUME_KEEP_H", "48")) * 3600       # run history per model
AHEAD_H = int(os.environ.get("PLUME_AHEAD_H", "24"))               # forecast window recorded
BACK_H = int(os.environ.get("PLUME_BACK_H", "3"))                  # ...plus this much before "now"
VIEW_BACK_H = 6                                                     # payload window: now-6 h ..
EVERY_S = float(os.environ.get("PLUME_EVERY_S", "900"))            # recorder check cadence
# deterministic, key-free models the router knows (ensembles deliberately left out)
MODELS = tuple(m for m in os.environ.get("PLUME_MODELS", "gfs,nam,hrrr,ecmwf,icon,gem").split(",")
               if m.strip())
# the model ids a plume may carry (the router's deterministic sources; the boat checks the same list)
SUPPORTED = ("gfs", "nam", "hrrr", "ecmwf", "icon", "gem")
MODELS = tuple(m for m in MODELS if m in SUPPORTED)
CYCLE_FALLBACK_TRIES = 2
MIN_FRAME_FRAC = 0.5
_PAD = 3 * 3600

_KEY_TARGET = "plume:target"        # labstate: an explicit target (else: what the boat holds)
_KEY_PUSH = "plume:push"            # labstate: the last push to the boat


# ---- the planned track ---------------------------------------------------------------------
def clean_path(path):
    """Route points with a position and an ETA, in time order."""
    pts = [{"lat": float(p["lat"]), "lon": float(p["lon"]), "t": float(p["t"])}
           for p in (path or [])
           if p.get("lat") is not None and p.get("lon") is not None and p.get("t") is not None]
    pts.sort(key=lambda p: p["t"])
    return pts


def position_at(path, t):
    """(lat, lon) the boat would be at, at epoch `t`, along `path` ([{lat, lon, t}] sorted by t):
    the first point before the start, the last after the finish, linear in between."""
    if not path:
        return None
    if t <= path[0]["t"]:
        return (path[0]["lat"], path[0]["lon"])
    if t >= path[-1]["t"]:
        return (path[-1]["lat"], path[-1]["lon"])
    lo, hi = 0, len(path) - 1
    while hi - lo > 1:                       # bisect: routes run to a few hundred points
        mid = (lo + hi) // 2
        if path[mid]["t"] <= t:
            lo = mid
        else:
            hi = mid
    a, b = path[lo], path[hi]
    f = (t - a["t"]) / max(1.0, b["t"] - a["t"])
    return (a["lat"] + (b["lat"] - a["lat"]) * f, a["lon"] + (b["lon"] - a["lon"]) * f)


def sample_times(now, back_h=BACK_H, ahead_h=AHEAD_H):
    """Hourly valid times from `back_h` before the current hour to `ahead_h` after it."""
    h0 = int(now // 3600) * 3600
    return [h0 + k * 3600 for k in range(-back_h, ahead_h + 1)]


# ---- one model run → one compact snapshot ----------------------------------------------------
def _uv_to_tws_twd(u, v):
    kn = math.hypot(u, v) * 1.9438445
    return kn, (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0


def sample_frames(frames, pos_at, times):
    """[[t, tws_kn, twd_deg]] from one model's frame series (objects with `.valid_time` and
    `.sample_uv(lat, lon)`) at the position the boat holds at each `t`. Times outside the frames'
    span are skipped — no extrapolating a run past its own horizon."""
    frames = sorted((f for f in frames or []), key=lambda f: f.valid_time)
    if not frames:
        return []
    out = []
    for t in times:
        if t < frames[0].valid_time - 1 or t > frames[-1].valid_time + 1:
            continue
        pos = pos_at(t)
        if pos is None:
            continue
        uv = None
        if len(frames) == 1 or t <= frames[0].valid_time:
            uv = frames[0].sample_uv(*pos)
        else:
            for a, b in zip(frames, frames[1:]):
                if a.valid_time <= t <= b.valid_time:
                    ua, ub = a.sample_uv(*pos), b.sample_uv(*pos)
                    if ua is None or ub is None:
                        uv = ua or ub
                    else:
                        f = (t - a.valid_time) / max(1.0, b.valid_time - a.valid_time)
                        uv = (ua[0] + (ub[0] - ua[0]) * f, ua[1] + (ub[1] - ua[1]) * f)
                    break
        if uv is None or any(x is None or x != x for x in uv):
            continue
        tws, twd = _uv_to_tws_twd(*uv)
        out.append([int(t), round(tws, 1), int(round(twd)) % 360])
    return out


def fetch_run(source, bbox, times, now, parser=None):
    """Load one model's freshest posted run over `times` → (cycle_epoch, frames, estimated) or None.

    GRIB sources: the freshest cycle `pick_cycle` expects to be posted, stepping back a cycle (up
    to CYCLE_FALLBACK_TRIES) while it is still too sparse — the router's own rule. Frames share the
    router's GRIB cache (same bbox → same files). API-grid sources (ICON/GEM via Open-Meteo) have no
    cycle in the response, so the cycle is the expected one and flagged `estimated`."""
    import datetime as dt
    from .wind import grib
    when = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    t0, t1 = times[0], times[-1]
    if hasattr(source, "load_series"):
        series, _meta = source.load_series(bbox, t0, t1)
        frames = series.get((source.name, "det")) or []
        return (source.pick_cycle(when).timestamp(), frames, True) if frames else None
    cycle = source.pick_cycle(when)
    for _attempt in range(CYCLE_FALLBACK_TRIES + 1):
        fhrs = [f for f in source.fhrs(source.horizon_for(cycle))
                if t0 - _PAD <= source.valid_time(cycle, f) <= t1 + _PAD]
        frames = []
        for fhr in fhrs:
            path = source.fetch(cycle, fhr, "det", bbox)
            if not path:
                continue
            try:
                frames.append(grib.GribFrame.from_file(path, source.name, "det",
                                                       source.valid_time(cycle, fhr),
                                                       parser=parser).crop(bbox))
            except Exception:  # noqa: BLE001 — an unreadable frame is skipped, like the router
                continue
        if fhrs and len(frames) >= MIN_FRAME_FRAC * len(fhrs):
            return cycle.timestamp(), frames, False
        cycle = source.prev_cycle(cycle)
    return None


# ---- the store (one JSON file per route target) ---------------------------------------------
def _safe(s) -> str:
    return re.sub(r"[^a-z0-9_.-]", "", str(s or "").lower())[:120]


def _path(key):
    return os.path.join(PLUME_DIR, _safe(key) + ".json")


def load_store(key):
    try:
        with open(_path(key)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_store(st):
    os.makedirs(PLUME_DIR, exist_ok=True)
    p = _path(st["key"])
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, p)                      # atomic: a reader never sees half a file


def prune(st, now, keep_s=KEEP_S):
    """Drop runs recorded more than `keep_s` ago (per model)."""
    for m, runs in list((st.get("runs") or {}).items()):
        st["runs"][m] = [r for r in runs if r.get("at", 0) >= now - keep_s]
    return st


def add_run(st, model, cycle, pts, at, estimated=False):
    """Append a run unless it adds nothing: the same cycle already recorded, or (for an
    estimated-cycle source) the same numbers as the latest run. Returns True when added."""
    runs = st.setdefault("runs", {}).setdefault(model, [])
    if not pts:
        return False
    if any(int(r["cycle"]) == int(cycle) for r in runs):
        return False
    if estimated and runs:
        last = max(runs, key=lambda r: r["at"])
        common = {p[0]: p for p in last["pts"]}
        same = [p for p in pts if p[0] in common]
        if same and all(common[p[0]][1:] == p[1:] for p in same):
            return False                     # the API served the run we already have
    run = {"cycle": int(cycle), "at": int(at), "pts": pts}
    if estimated:
        run["est"] = True
    runs.append(run)
    runs.sort(key=lambda r: r["cycle"])
    return True


def latest_cycle(st, model):
    runs = (st.get("runs") or {}).get(model) or []
    return max((int(r["cycle"]) for r in runs), default=None)


# ---- the payload the boat caches + the console draws ----------------------------------------
def build_plume(st, now, back_h=VIEW_BACK_H, ahead_h=AHEAD_H, models=None, window=None):
    """The compact plume: per model, its runs newest-first with points clipped to
    [now - back_h, now + ahead_h]. A run with nothing left in the window is dropped, and only the
    supported model ids are carried. `window` = the race's (start, end) epochs, sent along so the
    boat can show the plume as frozen at the start once racing."""
    if not st:
        return None
    w0, w1 = now - back_h * 3600, now + ahead_h * 3600
    order = [m for m in (models or MODELS) if m in SUPPORTED]
    out_models = []
    for m in order:
        runs = []
        for r in sorted((st.get("runs") or {}).get(m) or [], key=lambda r: -r["cycle"]):
            pts = [p for p in r["pts"] if w0 <= p[0] <= w1]
            if not pts:
                continue
            run = {"cycle": r["cycle"], "at": r["at"], "pts": pts}
            if r.get("est"):
                run["est"] = True
            runs.append(run)
        if runs:
            out_models.append({"model": m, "runs": runs})
    built = max((r["at"] for mm in out_models for r in mm["runs"]), default=None)
    out = {"schema": SCHEMA, "key": st.get("key"), "race_id": st.get("race_id"),
           "label": st.get("label"), "basis": st.get("basis"), "generated_at": int(now),
           "built_at": built, "window": [int(w0), int(w1)], "models": out_models}
    if window:
        out["race_start"] = int(window[0])
        out["race_end"] = int(window[1]) if window[1] is not None else None
    return out


# ---- frozen at the gun (RRS 41) ---------------------------------------------------------------
# The plume is shore-computed and route-specific, so it is homework: it may reach the boat only
# BEFORE the start. From the gun the Lab neither records nor pushes it for that race, and the boat
# keeps the last pre-start plume (labelled frozen on the console). In-race updates belong on board
# (the Orin), not here. Fail closed: the window never reopens on an estimate — it stays shut until
# someone marks the race finished in the Lab (`mark_finished`).
_KEY_FINISHED = "plume:finished:"     # labstate: + race_id → {"at": epoch}
_clock = time.time                    # the real clock (tests swap it)


def finished_at(race_id):
    rec = _labstate_get(_KEY_FINISHED + str(race_id)) or {}
    return rec.get("at")


def mark_finished(race_id, at=None, undo=False):
    """Record that a race is over (the Lab's "Race over" button) — reopens plume updates for it.
    `undo` clears it."""
    from . import labstate
    if not race_id:
        return None
    if undo:
        labstate.set(_KEY_FINISHED + str(race_id), None)
        return None
    rec = {"at": int(_clock() if at is None else at)}
    labstate.set(_KEY_FINISHED + str(race_id), rec)
    return rec


def race_window(spec):
    """(start, end) of the race the spec follows, or None when no start is known. Start = our gun
    (racestart), else the planned route's first ETA. End = the recorded finish for this race_id
    (only if after this start), else None — open-ended: frozen until marked finished."""
    from . import racestart
    d = spec.get("definition") or {}
    rs = racestart.race_start(d)
    path = spec.get("path") if spec.get("basis") == "route" else None
    start = spec.get("window_start")
    if start is None:
        start = rs["epoch"] if rs else (path[0]["t"] if path else None)
    if start is None:
        return None
    fin = finished_at(spec.get("race_id")) if spec.get("race_id") else None
    end = float(fin) if fin is not None and fin >= start else None
    return (float(start), end)


def is_racing(window, now):
    """Inside [start, end) — and with no recorded end, from the start on."""
    return bool(window) and window[0] <= now and (window[1] is None or now < window[1])



# ---- targets: which route the plume follows -------------------------------------------------
def _recommended_path(bundle):
    if not bundle:
        return None, None
    vs = bundle.get("variants") or []
    rec = bundle.get("recommended")
    v = next((x for x in vs if str(x.get("id")) == str(rec)), None) or (vs[0] if vs else None)
    path = clean_path(((v or {}).get("route") or {}).get("path"))
    return (path or None), (v or {}).get("name") or (v or {}).get("id")


_KEY_LOCK = "plume:locked"            # labstate: the race whose window has started (see below)


def _route_spec(race_id, d, course_id, path, tag, variant):
    return {"race_id": race_id, "course_id": course_id, "definition": d,
            "key": f"{race_id}__{tag}", "basis": "route", "path": path,
            "label": f"along the {variant or 'planned'} route"}


def _unlocked_target(now):
    """The target as configured (pinned, else the race last pushed to the boat) — before the lock."""
    from shared import race_def

    from . import deploy, pbstore, store
    tgt = _labstate_get(_KEY_TARGET)
    if not tgt:
        lp = (deploy.last_push() or {}).get("last_ok") or {}
        if lp.get("race_id"):
            tgt = {"race_id": lp["race_id"], "playbook_id": lp.get("playbook_id")}
    if not tgt or not tgt.get("race_id"):
        return None
    race_id = tgt["race_id"]
    d = store.get_race(race_id) or {}
    bundle = pbstore.get(tgt["playbook_id"]) if tgt.get("playbook_id") else None
    course_id = (bundle or {}).get("course_id") or tgt.get("course_id")
    path, variant = (clean_path(tgt["path"]), tgt.get("variant")) if tgt.get("path") \
        else _recommended_path(bundle)
    if path:
        tag = tgt.get("playbook_id") or ("path-" + hashlib.md5(
            json.dumps([path[0], path[-1], len(path)]).encode()).hexdigest()[:8])
        spec = _route_spec(race_id, d, course_id, path, tag, variant)
        # the route stays the target while it's current — AND, whatever its ETAs say, while its race
        # is in its window: the switch to the point fallback may only happen outside a race
        if path[-1]["t"] >= now - BACK_H * 3600 or is_racing(race_window(spec), now):
            return spec
    spec = {"race_id": race_id, "course_id": course_id, "definition": d}
    lat, lon = tgt.get("lat"), tgt.get("lon")
    if lat is None:
        marks, _skip, _cid = race_def.course_to_marks(d, course_id)
        first = next((m for m in marks if m[2] is not None), None)
        if not first:
            return None
        lat, lon = first[2], first[3]
    spec.update(key=f"{race_id}__pt-{round(lat, 2)}_{round(lon, 2)}", basis="point",
                path=[{"lat": float(lat), "lon": float(lon), "t": 0.0}],
                label="at the race start (no current route)")
    return spec


def locked_race():
    """The race whose window has started and that isn't marked finished yet: {race_id, key, start,
    ...} or None. While set, the target, its store key and its window start are pinned to it."""
    lock = _labstate_get(_KEY_LOCK)
    if not lock or not lock.get("race_id"):
        return None
    fin = finished_at(lock["race_id"])
    if fin is not None and fin >= lock.get("start", 0):
        return None
    return lock


def resolve_target(now=None):
    """The route spec the plume follows, or None when there's nothing to follow.

    An explicit target (labstate `plume:target`: {race_id, playbook_id?, path?, lat?, lon?}) wins;
    else the race + playbook last pushed to the boat. The route is the playbook's recommended
    variant; when there's no playbook, or its route is over (and its race is not on), the plume
    holds at the race start (first course mark).

    Once a race's window starts, the target is LOCKED to it (same store key, same window start)
    until the race is marked finished — so nothing that changes mid-race (ETAs running out, a
    cleared pin) can reopen recording or hide the frozen state. A different race configured as the
    target (a deliberate new push/pin) releases the lock and is judged on its own window."""
    from . import labstate, store
    now = time.time() if now is None else now
    spec = _unlocked_target(now)
    lock = locked_race()
    if lock and (spec is None or spec.get("race_id") == lock["race_id"]):
        return {"race_id": lock["race_id"], "course_id": lock.get("course_id"),
                "definition": store.get_race(lock["race_id"]) or {}, "key": lock["key"],
                "basis": lock["basis"], "path": lock["path"], "label": lock.get("label"),
                "window_start": lock["start"]}
    if lock or _labstate_get(_KEY_LOCK):
        labstate.set(_KEY_LOCK, None)        # finished, or a different race took over
    if spec:
        win = race_window(spec)
        if is_racing(win, now):
            labstate.set(_KEY_LOCK, {"race_id": spec["race_id"], "key": spec["key"],
                                     "basis": spec["basis"], "path": spec["path"],
                                     "label": spec.get("label"), "course_id": spec.get("course_id"),
                                     "start": win[0], "locked_at": int(now)})
            spec["window_start"] = win[0]
    return spec


def _labstate_get(k):
    from . import labstate
    return labstate.get(k)


def set_target(race_id=None, playbook_id=None, path=None, lat=None, lon=None, variant=None):
    """Pin the plume to a race (+ playbook / explicit route / point); race_id=None clears it."""
    from . import labstate
    if not race_id:
        labstate.set(_KEY_TARGET, None)
        return None
    tgt = {k: v for k, v in dict(race_id=race_id, playbook_id=playbook_id, path=path, lat=lat,
                                 lon=lon, variant=variant).items() if v is not None}
    labstate.set(_KEY_TARGET, tgt)
    return tgt


def _bbox_for(spec):
    from . import optimizer
    bb = None
    if spec.get("definition"):
        bb = optimizer.course_bbox(spec["definition"], spec.get("course_id"))
    lats = [p["lat"] for p in spec["path"]]
    lons = [p["lon"] for p in spec["path"]]
    n, s, w, e = max(lats) + 0.5, min(lats) - 0.5, min(lons) - 0.5, max(lons) + 0.5
    if bb:                                   # the router's bbox → the router's GRIB cache files
        if bb[0] >= n and bb[1] <= s and bb[2] <= w and bb[3] >= e:
            return bb
        return (max(bb[0], n), min(bb[1], s), min(bb[2], w), max(bb[3], e))
    return (n, s, w, e)


# ---- record ----------------------------------------------------------------------------------
def record(spec, now=None, models=None, on_progress=None, fetch=None):
    """Check every model for a cycle newer than the one on file; sample + store the new ones.
    Returns the updated store. `fetch` is injectable for tests (defaults to `fetch_run`)."""
    import datetime as dt

    from .wind import grib
    from .wind.models import MODELS as SOURCES
    now = time.time() if now is None else now
    say = on_progress or (lambda _m: None)
    st = load_store(spec["key"]) or {"key": spec["key"], "runs": {}}
    st.update(race_id=spec.get("race_id"), label=spec.get("label"), basis=spec.get("basis"))
    path = spec["path"]
    pos_at = (lambda _t: (path[0]["lat"], path[0]["lon"])) if spec.get("basis") == "point" \
        else (lambda t: position_at(path, t))
    times = sample_times(now)
    bbox = _bbox_for(spec)
    when = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    fetch = fetch or fetch_run
    parser = grib.IsolatedGribParser() if (fetch is fetch_run and grib.ISOLATE) else None
    try:
        for name in models or MODELS:
            src = SOURCES.get(name)
            if src is None or src.kind != "deterministic":
                continue
            have = latest_cycle(st, name)
            if have is not None and have >= src.pick_cycle(when).timestamp():
                continue                      # nothing newer is due yet — no download
            try:
                got = fetch(src, bbox, times, now, parser)
            except Exception as exc:  # noqa: BLE001 — one model down never stops the rest
                say(f"{name}: failed ({type(exc).__name__})")
                continue
            if not got:
                say(f"{name}: no run available")
                continue
            cycle, frames, est = got
            pts = sample_frames(frames, pos_at, times)
            added = add_run(st, name, cycle, pts, now, estimated=est)
            say(f"{name}: {dt.datetime.fromtimestamp(cycle, dt.timezone.utc):%d %HZ} "
                f"{'recorded' if added else 'unchanged'} ({len(pts)} pts)")
    finally:
        if parser is not None:
            parser.close()
    prune(st, now)
    st["checked_at"] = int(now)
    save_store(st)
    return st


# ---- push to the boat ------------------------------------------------------------------------
def push_url():
    return os.environ.get("PLUME_PUSH_URL") or (
        os.environ.get("DEPLOY_PI_ENGINE_URL", "http://100.79.180.102:8200").rstrip("/")
        + "/plume/load")


def push(plume, post=None, now=None):
    """POST the plume to the Pi engine (the Deploy push's line — Tailscale, no extra auth layer).
    Records the outcome in labstate. Never raises. Refuses once the plume's race has started —
    checked on the real clock at the moment of sending (`now` only for tests)."""
    from . import deploy, labstate
    now = _clock() if now is None else now
    if plume.get("race_start") is not None and \
            is_racing((plume["race_start"], plume.get("race_end")), now):
        return {"at": int(now), "ok": False, "key": plume.get("key"), "frozen": True,
                "built_at": plume.get("built_at"), "error": "racing: frozen at the start"}
    post = post or deploy._post_json
    ok, resp = post(push_url(), plume)
    ok = bool(ok and (resp or {}).get("loaded"))
    rec = {"at": int(now), "ok": ok, "key": plume.get("key"),
           "built_at": plume.get("built_at"), "race_start": plume.get("race_start"),
           "race_end": plume.get("race_end"),
           "error": None if ok else str((resp or {}).get("error") or resp)[:200]}
    labstate.set(_KEY_PUSH, rec)
    return rec


def last_push():
    return _labstate_get(_KEY_PUSH)


def needs_push(plume, last):
    """Push when the boat hasn't confirmed this plume yet: new runs, new target, a changed race
    window (e.g. the race was just marked over), or the last try failed."""
    if not plume or not plume.get("models"):
        return False
    if not last or not last.get("ok"):
        return True
    return any(last.get(k) != plume.get(k) for k in ("key", "built_at", "race_start", "race_end"))


def current(now=None):
    """The plume for the active target from what's on file (no fetching)."""
    now = time.time() if now is None else now
    spec = resolve_target(now)
    if not spec:
        return None
    return build_plume(load_store(spec["key"]), now, window=race_window(spec))


def run_once(on_progress=None, do_push=None, now=None):
    """One recorder pass: resolve the target, record new cycles, push to the boat if changed.
    The race window is checked on the real clock before recording AND again right before the push,
    so a pass that starts just before the gun can never deliver after it."""
    t_now = _clock() if now is None else now
    spec = resolve_target(t_now)
    if not spec:
        return {"ok": False, "note": "no target — lock in + push a race, or set a plume target"}
    win = race_window(spec)

    def frozen_out():
        if on_progress:
            on_progress("racing — plume frozen at the start; no updates until marked finished")
        return {"ok": True, "frozen": True, "key": spec["key"], "race_start": int(win[0]),
                "race_end": None if win[1] is None else int(win[1])}

    if is_racing(win, t_now):
        return frozen_out()
    with _REC_LOCK:                          # the recorder thread vs a manual refresh
        st = record(spec, now=t_now, on_progress=on_progress)
    done = _clock()                          # when the computation actually finished
    if is_racing(win, done):
        return frozen_out()                  # the gun went while we were recording
    plume = build_plume(st, t_now, window=win)
    if plume:
        plume["generated_at"] = int(done)
    out = {"ok": True, "key": spec["key"], "basis": spec["basis"],
           "models": {m["model"]: len(m["runs"]) for m in (plume or {}).get("models", [])}}
    do_push = (os.environ.get("PLUME_PUSH", "0").strip().lower() in ("1", "true", "yes", "on")
               if do_push is None else do_push)
    if do_push and needs_push(plume, last_push()):
        out["push"] = push(plume)
    return out


_THREAD = None
_REC_LOCK = threading.Lock()


def start_recorder():
    """Start the background recorder (once) when PLUME_RECORDER=1. Checks every EVERY_S; each
    pass only downloads models with a newer cycle due, so most passes are free."""
    global _THREAD
    if os.environ.get("PLUME_RECORDER", "0").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    if _THREAD and _THREAD.is_alive():
        return True

    def _loop():
        time.sleep(30)                       # let the app finish starting
        while True:
            try:
                run_once()
            except Exception as exc:  # noqa: BLE001 — the loop must survive anything
                print(f"[plume] pass failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(EVERY_S)

    _THREAD = threading.Thread(target=_loop, name="plume-recorder", daemon=True)
    _THREAD.start()
    return True
