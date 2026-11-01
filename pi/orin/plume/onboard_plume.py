"""Onboard forecast plume — computed ON THE BOAT, on the Orin, from public model data.

The Lab's plume (vps/lab/app/plume.py) freezes at the gun: it is shore-computed and
route-specific, so under RRS 41 it is pre-start homework only. This service computes the SAME
plume aboard — every router weather model sampled along the planned route at the boat's ETAs,
run by run — from public model data the Orin downloads itself over Starlink. Own computer +
public data is legal in-race, so this one keeps updating through the race.

One pass (`run_once`, run by a systemd timer every 15 min as a short-lived process):
  1. Ask the Pi engine (over the direct Pi↔Orin cable) which route the boat holds:
     GET /playbook/track (the active playbook variant, with ETAs). Once the race is on and the
     boat is near the route, the ETAs shift (whole hours) to the boat's real progress. No route,
     or its ETAs are over → the boat's own position from GET /conditions.
  2. If there's internet, download any NEW model cycle for that route's box (bbox-subset NOMADS
     GRIBs, ECMWF open data, Open-Meteo ICON/GEM — the Lab's own sources and cache layout) and
     record it. No internet → record from the cache into the current store (any newer cycle already
     cached for this box), with no network.
  3. Build the plume (the exact c4.plume/v1 shape the engine and console already use), mark it
     `source: onboard`, POST it to the engine's /plume/onboard. The engine only takes that from
     the Orin's cable address (10.10.10.2).
  4. Prune GRIB files older than GRIB_KEEP_H and run stores of old targets.
A cycle that is due but not posted yet is re-probed at most once an hour (BackoffFetch).

It never runs the route optimizer. The GRIB cache it keeps (GRIB_CACHE, the Lab's layout) is the
download layer #124's onboard optimizer can reuse later.

Run:  PYTHONPATH=<repo>/vps/lab python3 pi/orin/plume/onboard_plume.py   (see README.md)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import socket
import sys
import threading
import time
import urllib.request

# The Lab's wind code reads GRIB_CACHE at import time — set our defaults before importing it.
_HOME = os.path.expanduser("~")
os.environ.setdefault("GRIB_CACHE", os.path.join(_HOME, "c4-plume", "gribcache"))
os.environ.setdefault("PLUME_DIR", os.path.join(_HOME, "c4-plume", "store"))

from app import plume as lab  # noqa: E402  (vps/lab on PYTHONPATH)

ENGINE_URL = os.environ.get("ENGINE_URL", "http://10.10.10.1:8200").rstrip("/")
ENGINE_TIMEOUT = float(os.environ.get("ENGINE_TIMEOUT", "10"))
GRIB_KEEP_S = float(os.environ.get("GRIB_KEEP_H", "30")) * 3600
ONLINE_PROBE = os.environ.get("PLUME_ONLINE_PROBE", "nomads.ncep.noaa.gov:443")
SOURCE = "onboard"


def _log(msg):
    print(f"[onboard-plume] {msg}", flush=True)


# ---- the engine (Pi) -------------------------------------------------------------------------
def engine_get(path):
    """GET a JSON endpoint on the Pi engine, or None if it can't be reached / read."""
    try:
        with urllib.request.urlopen(ENGINE_URL + path, timeout=ENGINE_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001 — the engine down must never crash a pass
        return None


def engine_post(path, body):
    """POST JSON to the Pi engine → (ok, response). Straight to the engine, no proxy: the engine
    refuses onboard plumes that arrive through one."""
    data = json.dumps(body, separators=(",", ":")).encode()
    req = urllib.request.Request(ENGINE_URL + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=ENGINE_TIMEOUT) as r:
            return True, json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return False, {"error": f"{type(exc).__name__}: {exc}"[:200]}


# ---- which route --------------------------------------------------------------------------------
POINT_STEP = 0.25            # the boat's position is snapped to this grid (≈ a model cell)
SHIFT_STEP_S = 3600          # ETA shift to the boat's real progress, in whole hours
SHIFT_MAX_OFF_NM = 20.0      # ...only while the boat is this close to the planned route
SHIFT_MAX_S = 12 * 3600      # ...and no more than this off plan (beyond it the plan is history)


def _route_hash(pts):
    """Short hash of the route geometry + ETAs: a replacement playbook that reuses the race and
    variant ids still gets its own store, so its runs never mix with the old route's."""
    sig = [[round(p["lat"], 3), round(p["lon"], 3), int(p["t"])] for p in pts]
    return hashlib.md5(json.dumps(sig, separators=(",", ":")).encode()).hexdigest()[:8]


def _nearest_on_path(path, lat, lon):
    """(distance_nm, plan_t) of the point on `path` nearest (lat, lon) — flat-earth per segment,
    fine at a few miles."""
    k = math.cos(math.radians(lat))
    best = None
    for a, b in zip(path, path[1:]):
        ax, ay = (a["lon"] - lon) * k, a["lat"] - lat
        bx, by = (b["lon"] - lon) * k, b["lat"] - lat
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        f = 0.0 if L == 0 else min(1.0, max(0.0, -(ax * dx + ay * dy) / L))
        d = math.hypot(ax + f * dx, ay + f * dy) * 60.0
        if best is None or d < best[0]:
            best = (d, a["t"] + f * (b["t"] - a["t"]))
    return best


def progress_shift(path, boat, now, step=SHIFT_STEP_S, max_off_nm=SHIFT_MAX_OFF_NM):
    """Seconds to add to the plan's ETAs so they match the boat's real progress: plan time at the
    boat's nearest point on the route vs now, in whole `step`s (positive = behind plan). 0 before
    the planned start, with no position, when the boat is off the route (at the dock, delivery),
    or when it's more than SHIFT_MAX_S off plan (an old playbook, the boat moored weeks later).
    Assumes the route doesn't pass the same water twice (true for point-to-point races)."""
    if not boat or len(path) < 2 or now < path[0]["t"]:
        return 0
    near = _nearest_on_path(path, boat[0], boat[1])
    if near is None or near[0] > max_off_nm:
        return 0
    shift = int(round((now - near[1]) / step)) * step
    return shift if abs(shift) <= SHIFT_MAX_S else 0


def resolve_spec(track, conditions, now):
    """The spec `lab.record` follows: the playbook route the boat holds (ETAs shifted to the boat's
    real progress once the race is on), else the boat's position snapped to a POINT_STEP grid.
    None when there's neither. The store key carries everything the samples depend on — route
    geometry + ETAs + shift, or the snapped point — so a changed target never reuses old samples
    (or skips a cycle because the old target already had it)."""
    c = conditions or {}
    boat = (float(c["lat"]), float(c["lon"])) if c.get("available") and c.get("lat") is not None \
        and c.get("lon") is not None else None
    pts = lab.clean_path((track or {}).get("points")) if (track or {}).get("available") else []
    if len(pts) >= 2:
        shift = progress_shift(pts, boat, now)
        path = [dict(p, t=p["t"] + shift) for p in pts] if shift else pts
        if path[-1]["t"] >= now - lab.BACK_H * 3600:
            # ids capped so the hash and shift survive the store's 120-char filename cut
            race = (lab._safe((track or {}).get("race_id")) or "race")[:40]
            variant = (lab._safe((track or {}).get("variant")) or "plan")[:40]
            name = (track or {}).get("variant_label") or "planned"
            key = f"onboard__{race}__{variant}__{_route_hash(pts)}"
            if shift:
                key += f"__s{shift // 3600:+d}h"
            return {"key": key, "race_id": (track or {}).get("race_id"), "basis": "route",
                    "path": path, "shift_s": shift, "label": f"along the {name} route"}
    if boat:
        lat, lon = (round(x / POINT_STEP) * POINT_STEP for x in boat)
        return {"key": f"onboard__pt-{lat:.2f}_{lon:.2f}", "race_id": (track or {}).get("race_id"),
                "basis": "point", "path": [{"lat": lat, "lon": lon, "t": 0.0}], "label": "at the boat"}
    return None


# ---- re-check back-off --------------------------------------------------------------------------
MISS_BACKOFF_S = float(os.environ.get("PLUME_MISS_BACKOFF_MIN", "60")) * 60
_MISSES = "misses.state"     # not .json: never mistaken for a run store


# Starlink can pass the TCP probe and then stall: bound the whole online download phase, well under
# the unit's TimeoutStartSec=600, so a pass always saves its state and posts something.
DOWNLOAD_BUDGET_S = float(os.environ.get("PLUME_DOWNLOAD_BUDGET_S", "240"))
REQUEST_TIMEOUT_S = float(os.environ.get("PLUME_REQUEST_TIMEOUT_S", "20"))


class _StampsDownloads:
    """A run's `at` should be when its files were downloaded, not when this pass sampled them: a
    pass that resamples cached GRIBs (a new target, offline or online) would otherwise report old
    downloads as fresh. `note` keeps each model's newest file time; `stamp` applies it."""

    def note(self, model, t):
        d = self.downloaded_at
        d[model] = max(d.get(model, 0), t)

    def stamp(self, st, now):
        """Runs this pass added get `at` = their files' download time (never later than now)."""
        for m, t in self.downloaded_at.items():
            for r in (st.get("runs") or {}).get(m) or []:
                if r.get("at") == int(now):
                    r["at"] = min(int(t), int(now))
        return st


def _miss_key(name, bbox):
    """Back-off is per model AND box: a model with no run for one box (HRRR outside its coverage)
    may well have one for the next."""
    return name + "@" + ",".join(f"{float(x):.2f}" for x in bbox)


class BackoffFetch(_StampsDownloads):
    """`lab.record`'s fetch, with a back-off for a cycle that is due by the model's nominal lag but
    not posted yet (ECMWF posts hours later than its lag says). After a miss, that cycle isn't
    re-probed for MISS_BACKOFF_S; a newer due cycle is tried at once. While backing off, a target
    with no run on file yet still gets the last cycle that WAS there (from the cache, no download).
    Uses its own crash-isolated GRIB parser, like `lab.record` does for the stock fetch.

    Time budget: every request gets at most REQUEST_TIMEOUT_S, and the whole download phase at most
    DOWNLOAD_BUDGET_S. Each model's fetch runs in a daemon thread joined against what's left of
    the budget (ICON/GEM's API calls and ECMWF's client can't all be given a timeout). Once the
    budget is gone `exhausted` is set and every remaining model is skipped at once."""

    def __init__(self, key, now, backoff_s=MISS_BACKOFF_S, fetch_run=None, budget_s=None):
        self.path = os.path.join(os.environ["PLUME_DIR"], _MISSES)
        self.backoff_s, self.now = backoff_s, now
        self.downloaded_at = {}
        self.deadline = time.monotonic() + (DOWNLOAD_BUDGET_S if budget_s is None else budget_s)
        self.exhausted = False
        self.fetch_run = fetch_run or lab.fetch_run
        st = lab.load_store(key) or {}
        self.have = {m: lab.latest_cycle(st, m) for m in (st.get("runs") or {})}
        try:
            with open(self.path) as f:
                self.misses = json.load(f)
        except (OSError, ValueError):
            self.misses = {}
        self._parser = None

    def parser(self):
        from app.wind import grib
        if self._parser is None and grib.ISOLATE:
            self._parser = grib.IsolatedGribParser()
        return self._parser

    def left(self):
        return self.deadline - time.monotonic()

    def _run(self, source, bbox, times, now):
        """fetch_run in a daemon thread, joined against the budget; None once it's spent."""
        left = self.left()
        if left <= 0.5:
            self.exhausted = True
            return None
        box = {}
        parser = self.parser()

        def work():
            try:
                box["r"] = self.fetch_run(_Budgeted(source, self), bbox, times, now, parser)
            except Exception as exc:  # noqa: BLE001 — re-raised in the caller's thread
                box["e"] = exc

        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(left)
        if th.is_alive():
            # stalled past the budget: abandon it (a daemon thread, gone when the pass exits) and
            # drop the parser it may still be using
            self.exhausted = True
            _log(f"{source.name}: download budget spent — stopping downloads")
            if self._parser is not None:
                self._parser.close()
                self._parser = None
            return None
        if "e" in box:
            raise box["e"]
        return box.get("r")

    def __call__(self, source, bbox, times, now, _parser=None):
        if self.exhausted:
            return None
        when = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        due = int(source.pick_cycle(when).timestamp())
        mkey = _miss_key(source.name, bbox)
        miss = self.misses.get(mkey) or {}
        if miss.get("cycle") == due and now - miss.get("at", 0) < self.backoff_s:
            if self.have.get(source.name) is not None or not miss.get("got"):
                return None                       # backing off; the target already has a run
            got_dt = dt.datetime.fromtimestamp(miss["got"], dt.timezone.utc)
            return self._run(_Pinned(source, got_dt), bbox, times, now)
        got = self._run(source, bbox, times, now)
        if self.exhausted:
            return got                            # a stall says nothing about what's posted
        if hasattr(source, "load_series") or (got and int(got[0]) >= due):
            self.misses.pop(mkey, None)
        else:
            self.misses[mkey] = {"cycle": due, "at": int(now),
                                 "got": int(got[0]) if got else None}
        return got

    def close(self):
        if self._parser is not None:
            self._parser.close()
        # drop expired entries (and any keyed the old way, by model only), so boxes don't pile up
        self.misses = {k: v for k, v in self.misses.items()
                       if "@" in k and self.now - (v or {}).get("at", 0) < self.backoff_s}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.misses, f)
        except OSError:
            pass


class CacheOnlyFetch(_StampsDownloads):
    """Offline: sample model runs already in the GRIB cache into the current target's store — never
    touches the network. Covers a target that changed while offline (the boat fell another hour
    behind, so the store key changed) and a store that is empty or older than what's cached (a
    cycle downloaded under another target for the same box). Takes each model's newest cached
    cycle for this box, skipping it when the store already holds it (no parse); the API-grid models
    (ICON/GEM) need the network and are skipped. A run's `at` is set
    to when its files were downloaded, so the plume's age stays honest."""

    def __init__(self, key=None, fetch_run=None):
        self.fetch_run = fetch_run or lab.fetch_run
        self.downloaded_at = {}
        self._parser = None
        st = (lab.load_store(key) if key else None) or {}
        self.have = {m: lab.latest_cycle(st, m) for m in (st.get("runs") or {})}

    def parser(self):
        from app.wind import grib
        if self._parser is None and grib.ISOLATE:
            self._parser = grib.IsolatedGribParser()
        return self._parser

    def close(self):
        if self._parser is not None:
            self._parser.close()

    @staticmethod
    def newest_cached(source, bbox):
        """The newest cycle with a cached file for this model + box, or None."""
        probe = os.path.basename(source._cache_path(dt.datetime(2000, 1, 1), 0, "det", bbox))
        suffix = probe[probe.rindex("_"):]                  # _<bbox hash>.grib2
        try:
            names = os.listdir(os.path.join(os.environ["GRIB_CACHE"], source.name))
        except OSError:
            return None
        cycles = []
        for n in names:
            parts = n.split("_")
            if n.endswith(suffix) and n.startswith(source.name + "_") and "_det_" in n:
                try:
                    cycles.append(dt.datetime.strptime(parts[-4], "%Y%m%d%H").replace(tzinfo=dt.timezone.utc))
                except (ValueError, IndexError):
                    continue
        return max(cycles) if cycles else None

    def __call__(self, source, bbox, times, now, _parser=None):
        if hasattr(source, "load_series"):
            return None
        cyc = self.newest_cached(source, bbox)
        have = self.have.get(source.name)
        if cyc is None or (have is not None and cyc.timestamp() <= have):
            return None                           # nothing cached newer than the store — no parse
        return self.fetch_run(_CachedOnly(source, cyc, self), bbox, times, now, self.parser())


class _CachedOnly:
    """A model source that only reads the cache: its freshest cycle is the newest cached one, and
    `fetch` returns a cached file or None — never a download."""

    def __init__(self, src, cycle, owner):
        self._src, self._cycle, self._owner = src, cycle, owner

    def pick_cycle(self, now=None, min_horizon_h=0):
        return self._cycle

    def fetch(self, cycle, fhr, member, bbox, timeout=None):
        path = self._src._cache_path(cycle, fhr, member, bbox)
        if not (os.path.exists(path) and os.path.getsize(path) > 100):
            return None
        self._owner.note(self._src.name, os.path.getmtime(path))
        return path

    def __getattr__(self, k):
        return getattr(self._src, k)


class _Budgeted:
    """A model source whose downloads get at most REQUEST_TIMEOUT_S each, and none once the pass's
    download budget is spent. Notes each file's download time: a file that was already cached
    keeps its own (mtime), a fresh one is `now`."""

    def __init__(self, src, owner):
        self._src, self._owner = src, owner

    def fetch(self, cycle, fhr, member, bbox, timeout=None):
        left = self._owner.left()
        if left <= 0.5:
            return None
        cache_path = getattr(self._src, "_cache_path", None)
        path = cache_path(cycle, fhr, member, bbox) if cache_path else None
        cached = bool(path and os.path.exists(path) and os.path.getsize(path) > 100)
        got = self._src.fetch(cycle, fhr, member, bbox, timeout=min(REQUEST_TIMEOUT_S, left))
        if got:
            self._owner.note(self._src.name, os.path.getmtime(got) if cached else self._owner.now)
        return got

    def __getattr__(self, k):
        return getattr(self._src, k)


class _Pinned:
    """A model source whose freshest cycle is pinned (the last one that was actually posted)."""

    def __init__(self, src, cycle):
        self._src, self._cycle = src, cycle

    def pick_cycle(self, now=None, min_horizon_h=0):
        return self._cycle

    def __getattr__(self, k):
        return getattr(self._src, k)


# ---- internet + disk ----------------------------------------------------------------------------
def online(probe=ONLINE_PROBE, timeout=5.0):
    """Cheap check before any download: can we open a TCP connection to the model server? Offline,
    this saves every model's own timeouts (ECMWF's alone is 75 s a frame)."""
    host, _, port = probe.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def prune_gribs(cache=None, keep_s=GRIB_KEEP_S, now=None):
    """Delete cached weather files older than `keep_s` (by mtime). Returns how many went."""
    cache = cache or os.environ["GRIB_CACHE"]
    now = time.time() if now is None else now
    gone = 0
    for root, _dirs, files in os.walk(cache):
        for f in files:
            p = os.path.join(root, f)
            try:
                if os.path.getmtime(p) < now - keep_s:
                    os.remove(p)
                    gone += 1
            except OSError:
                continue
    return gone


def prune_stores(keep_key=None, keep_s=None, now=None):
    """Delete run stores (one per target key) untouched for `keep_s` (the run history length,
    48 h) — a moving boat or a shifted route leaves old keys behind. Never the current one."""
    d = os.environ["PLUME_DIR"]
    keep_s = lab.KEEP_S if keep_s is None else keep_s
    now = time.time() if now is None else now
    mine = lab._safe(keep_key) + ".json" if keep_key else None
    gone = 0
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for n in names:
        if not n.endswith(".json") or n == mine:
            continue
        p = os.path.join(d, n)
        try:
            if os.path.getmtime(p) < now - keep_s:
                os.remove(p)
                gone += 1
        except OSError:
            continue
    return gone


# ---- one pass -----------------------------------------------------------------------------------
def build(st, now):
    """The c4.plume/v1 payload from the store, marked onboard. No race_start: an onboard plume is
    never frozen."""
    p = lab.build_plume(st, now)
    if p:
        p["source"] = SOURCE
        p["generated_at"] = int(time.time())
    return p


def waiting_marker(spec, now):
    """The "no onboard forecast for this spot yet" payload the engine stores in place of the old
    onboard plume."""
    return {"schema": lab.SCHEMA, "source": SOURCE, "waiting": True, "models": [],
            "key": spec["key"], "race_id": spec.get("race_id"), "basis": spec.get("basis"),
            "label": spec.get("label"), "generated_at": int(time.time() if now is None else now)}


def run_once(now=None, get=engine_get, post=engine_post, record=None, is_online=online):
    """One pass: route → download new cycles (if online) → plume → engine. Never raises for a
    missing engine, model or network; returns what happened."""
    now = time.time() if now is None else now
    record = record or lab.record
    spec = resolve_spec(get("/playbook/track"), get("/conditions"), now)
    if not spec:
        _log("no route aboard and no boat position — nothing to follow")
        return {"ok": False, "note": "no route and no position"}
    net, stalled = is_online(), False
    if net:
        fetch = BackoffFetch(spec["key"], now)
        try:
            st = fetch.stamp(record(spec, now=now, on_progress=_log, fetch=fetch), now)
        finally:
            fetch.close()                         # always saves the back-off state
        lab.save_store(st)
        stalled = fetch.exhausted
    if not net or stalled:
        # offline (or the downloads stalled) = record from the cache into the current store: any
        # cycle cached for this box that's newer than what the store holds, with no network
        _log("offline — sampling what's in the cache" if not net else "stalled — sampling what's in the cache")
        fetch = CacheOnlyFetch(spec["key"])
        try:
            st = fetch.stamp(record(spec, now=now, on_progress=_log, fetch=fetch), now)
        finally:
            fetch.close()
        lab.save_store(st)
    plume = build(st, now)
    out = {"ok": True, "key": spec["key"], "basis": spec["basis"], "online": net,
           "stalled": bool(net and stalled),
           "models": {m["model"]: len(m["runs"]) for m in (plume or {}).get("models", [])}}
    if not (plume and plume.get("models")):
        # nothing usable for this target (e.g. the boat crossed a grid line while offline): tell the
        # engine, so it stops serving the old target's onboard plume as current
        plume = waiting_marker(spec, now)
        _log("no model runs for this target yet — telling the engine")
    if plume:
        ok, resp = post("/plume/onboard", plume)
        out["push"] = {"ok": bool(ok and (resp or {}).get("loaded")),
                       "detail": None if ok and (resp or {}).get("loaded") else
                       str((resp or {}).get("detail") or (resp or {}).get("error") or resp)[:200]}
        _log(f"pushed to the engine: {out['push']}")
    out["pruned"] = prune_gribs(now=now)
    out["pruned_stores"] = prune_stores(spec["key"], now=now)
    return out


def main():
    t0 = time.time()
    try:
        out = run_once()
    except Exception as exc:  # noqa: BLE001 — a timer job: log and exit cleanly, retry next tick
        _log(f"pass failed: {type(exc).__name__}: {exc}")
        return 1
    newest = dt.datetime.now(dt.timezone.utc).strftime("%H:%MZ")
    _log(f"done at {newest} in {time.time() - t0:.0f} s: {json.dumps(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
