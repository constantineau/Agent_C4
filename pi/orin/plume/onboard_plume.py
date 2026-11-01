"""Onboard forecast plume — computed ON THE BOAT, on the Orin, from public model data.

The Lab's plume (vps/lab/app/plume.py) freezes at the gun: it is shore-computed and
route-specific, so under RRS 41 it is pre-start homework only. This service computes the SAME
plume aboard — every router weather model sampled along the planned route at the boat's ETAs,
run by run — from public model data the Orin downloads itself over Starlink. Own computer +
public data is legal in-race, so this one keeps updating through the race.

One pass (`run_once`, run by a systemd timer every 15 min as a short-lived process):
  1. Ask the Pi engine (over the direct Pi↔Orin cable) which route the boat holds:
     GET /playbook/track (the active playbook variant, with ETAs). No route, or its ETAs are over →
     the boat's own position from GET /conditions.
  2. If there's internet, download any NEW model cycle for that route's box (bbox-subset NOMADS
     GRIBs, ECMWF open data, Open-Meteo ICON/GEM — the Lab's own sources and cache layout) and
     record it. No internet → skip the downloads and keep the last runs.
  3. Build the plume (the exact c4.plume/v1 shape the engine and console already use), mark it
     `source: onboard`, POST it to the engine's /plume/onboard. The engine only takes that from
     the Orin's cable address (10.10.10.2).
  4. Prune GRIB files older than GRIB_KEEP_H.

It never runs the route optimizer. The GRIB cache it keeps (GRIB_CACHE, the Lab's layout) is the
download layer #124's onboard optimizer can reuse later.

Run:  PYTHONPATH=<repo>/vps/lab python3 pi/orin/plume/onboard_plume.py   (see README.md)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sys
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
def resolve_spec(track, conditions, now):
    """The spec `lab.record` follows: the playbook route the boat holds (with ETAs), else the boat's
    position. None when there's neither."""
    pts = lab.clean_path((track or {}).get("points")) if (track or {}).get("available") else []
    if len(pts) >= 2 and pts[-1]["t"] >= now - lab.BACK_H * 3600:
        race = lab._safe((track or {}).get("race_id")) or "race"
        variant = lab._safe((track or {}).get("variant")) or "plan"
        name = (track or {}).get("variant_label") or "planned"
        return {"key": f"onboard__{race}__{variant}", "race_id": (track or {}).get("race_id"),
                "basis": "route", "path": pts, "label": f"along the {name} route"}
    c = conditions or {}
    if c.get("available") and c.get("lat") is not None and c.get("lon") is not None:
        lat, lon = float(c["lat"]), float(c["lon"])
        return {"key": "onboard__boat", "race_id": (track or {}).get("race_id"), "basis": "point",
                "path": [{"lat": lat, "lon": lon, "t": 0.0}], "label": "at the boat"}
    return None


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


# ---- one pass -----------------------------------------------------------------------------------
def build(st, now):
    """The c4.plume/v1 payload from the store, marked onboard. No race_start: an onboard plume is
    never frozen."""
    p = lab.build_plume(st, now)
    if p:
        p["source"] = SOURCE
        p["generated_at"] = int(time.time())
    return p


def run_once(now=None, get=engine_get, post=engine_post, record=None, is_online=online):
    """One pass: route → download new cycles (if online) → plume → engine. Never raises for a
    missing engine, model or network; returns what happened."""
    now = time.time() if now is None else now
    record = record or lab.record
    spec = resolve_spec(get("/playbook/track"), get("/conditions"), now)
    if not spec:
        _log("no route aboard and no boat position — nothing to follow")
        return {"ok": False, "note": "no route and no position"}
    net = is_online()
    if net:
        st = record(spec, now=now, on_progress=_log)
    else:
        _log("offline — keeping the last runs")
        st = lab.load_store(spec["key"])
        if st:
            lab.prune(st, now)
    plume = build(st, now)
    out = {"ok": True, "key": spec["key"], "basis": spec["basis"], "online": net,
           "models": {m["model"]: len(m["runs"]) for m in (plume or {}).get("models", [])}}
    if plume and plume.get("models"):
        ok, resp = post("/plume/onboard", plume)
        out["push"] = {"ok": bool(ok and (resp or {}).get("loaded")),
                       "detail": None if ok and (resp or {}).get("loaded") else
                       str((resp or {}).get("detail") or (resp or {}).get("error") or resp)[:200]}
        _log(f"pushed to the engine: {out['push']}")
    else:
        _log("no model runs on file yet — nothing to push")
    out["pruned"] = prune_gribs(now=now)
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
