"""Onboard forecast plume (pi/orin/plume/onboard_plume.py) — built from a small fixture GRIB.

Pins: the route + ETAs come from the Pi engine's /playbook/track; one model run is downloaded,
sampled along the route and posted to /plume/onboard in the exact c4.plume/v1 shape the engine
validates, marked onboard, with no race_start; a second pass with nothing new downloads nothing;
offline keeps the last runs and still pushes them; no route → the boat's position; the engine
down never crashes a pass; old GRIBs are pruned.

Runs in the Lab image (it has eccodes/cfgrib): tools/ci/run.sh, or
  PYTHONPATH=vps/lab python3 pi/orin/plume/test_onboard_plume.py
"""
import datetime as dt
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
_tmp = tempfile.mkdtemp()
os.environ["GRIB_CACHE"] = os.path.join(_tmp, "gribcache")
os.environ["PLUME_DIR"] = os.path.join(_tmp, "store")
os.environ["GRIB_ISOLATE_PARSE"] = "0"
sys.path.insert(0, HERE)

import eccodes  # noqa: E402
import numpy as np  # noqa: E402

import onboard_plume as ob  # noqa: E402
from app.wind import models  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# the engine's own validator, loaded by path (the Lab and the agent both call their package `app`)
_spec = importlib.util.spec_from_file_location("engine_plume", os.path.join(REPO, "vps", "agent", "app", "plume.py"))
engine_plume = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine_plume)


def write_grib(path, u, v):
    """A tiny 10 m wind GRIB2: 1° grid 43–46N, 88–84W, uniform (u, v) m/s."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        for short, val in (("10u", u), ("10v", v)):
            h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
            for k, x in (("Ni", 5), ("Nj", 4), ("latitudeOfFirstGridPointInDegrees", 46.0),
                         ("longitudeOfFirstGridPointInDegrees", 272.0),
                         ("latitudeOfLastGridPointInDegrees", 43.0),
                         ("longitudeOfLastGridPointInDegrees", 276.0),
                         ("iDirectionIncrementInDegrees", 1.0), ("jDirectionIncrementInDegrees", 1.0)):
                eccodes.codes_set(h, k, x)
            eccodes.codes_set(h, "shortName", short)
            eccodes.codes_set_values(h, np.full(20, float(val)))
            eccodes.codes_write(h, f)
            eccodes.codes_release(h)
    return path


NOW = 1_793_534_400                                   # 2026-11-01T12:00Z
CYCLE = dt.datetime(2026, 11, 1, 6, tzinfo=dt.timezone.utc)
FETCHED = []


class FixtureGFS(models.GFS):
    """GFS with the network swapped for the fixture: wind from the east, building 1/3 m/s per hour."""

    def pick_cycle(self, now=None, min_horizon_h=0):
        return CYCLE

    def fetch(self, cycle, fhr, member, bbox, timeout=60):
        FETCHED.append(fhr)
        return write_grib(os.path.join(os.environ["GRIB_CACHE"], "gfs", f"f{fhr:03d}.grib2"),
                          u=-(5.0 + fhr / 3.0), v=0.0)


models.MODELS["gfs"] = FixtureGFS()


def record(spec, **kw):
    return ob.lab.record(spec, models=("gfs",), **kw)


TRACK = {"available": True, "race_id": "r1", "variant": "left", "variant_label": "Left side",
         "points": [{"lat": 44.0, "lon": -87.0, "t": NOW - 3600},
                    {"lat": 45.0, "lon": -85.0, "t": NOW + 30 * 3600}]}
COND = {"available": True, "lat": 44.5, "lon": -86.5}
ENGINE = {"/playbook/track": TRACK, "/conditions": COND}
POSTS = []


def get(path):
    return ENGINE.get(path)


def post(path, body):
    POSTS.append((path, body))
    return True, {"loaded": True}


print("first pass, online:")
out = ob.run_once(now=NOW, get=get, post=post, record=record, is_online=lambda: True)
check("pass ok, following the playbook route", out["ok"] and out["basis"] == "route"
      and out["key"] == "onboard__r1__left")
check("downloaded the run's frames over the plume window", FETCHED and min(FETCHED) <= 6 and max(FETCHED) >= 30)
check("posted once, to /plume/onboard", len(POSTS) == 1 and POSTS[0][0] == "/plume/onboard")
p = POSTS[0][1]
check("the engine's own validator takes it (no race start needed)",
      engine_plume.validate(p, need_race_start=False) == (True, ""))
check("marked onboard, no race_start (never frozen)", p["source"] == "onboard" and "race_start" not in p)
check("labelled with the plan's name", p["label"] == "along the Left side route")
run = p["models"][0]["runs"][0]
check("one gfs run, the fixture cycle", p["models"][0]["model"] == "gfs" and run["cycle"] == int(CYCLE.timestamp()))
by_t = {pt[0]: pt for pt in run["pts"]}
# at NOW the run is 6 h old: u = -(5 + 6/3) = -7 m/s from the east → 13.6 kn, 090
check("sampled from the GRIB: 13.6 kn from 090 at now", by_t.get(NOW) == [NOW, 13.6, 90])
# halfway between f06 and f09 frames → u = -7.5 m/s → 14.6 kn
check("interpolated between frames (+1.5 h)", abs(by_t[NOW + 3600][1] - round(7.333 * 1.9438445, 1)) <= 0.1)
check("the whole window, hourly", len(run["pts"]) >= 25)
check("data age = when it was recorded aboard", p["built_at"] == NOW)

print("second pass, nothing new posted:")
FETCHED.clear()
out = ob.run_once(now=NOW + 900, get=get, post=post, record=record, is_online=lambda: True)
check("no download (the cycle on file is the newest due)", FETCHED == [])
check("still pushed (keeps the engine's copy current)", len(POSTS) == 2 and out["push"]["ok"])
check("still one run", len(POSTS[1][1]["models"][0]["runs"]) == 1)

print("offline:")


def boom(*a, **k):
    raise AssertionError("must not download offline")


out = ob.run_once(now=NOW + 1800, get=get, post=post, record=boom, is_online=lambda: False)
check("no download attempted, last runs pushed", out["ok"] and out["online"] is False
      and POSTS[-1][1]["models"][0]["runs"][0]["cycle"] == int(CYCLE.timestamp()))
check("its age keeps counting (built_at unchanged)", POSTS[-1][1]["built_at"] == NOW)

print("engine trouble never crashes a pass:")
out = ob.run_once(now=NOW, get=lambda _p: None, post=post, record=record, is_online=lambda: True)
check("engine unreachable → nothing to follow, clean return", out["ok"] is False)
out = ob.run_once(now=NOW + 2000, get=get, post=lambda *_a: (False, {"error": "URLError"}),
                  record=record, is_online=lambda: True)
check("push failure is reported, not raised", out["push"]["ok"] is False and "URLError" in out["push"]["detail"])
out = ob.run_once(now=NOW + 2100, get=get, post=lambda *_a: (True, {"loaded": False, "detail": "nope"}),
                  record=record, is_online=lambda: True)
check("engine refusal is reported", out["push"]["ok"] is False and out["push"]["detail"] == "nope")

print("no current route:")
ENGINE["/playbook/track"] = dict(TRACK, points=[dict(pt, t=pt["t"] - 10 * 86400) for pt in TRACK["points"]])
s = ob.resolve_spec(ENGINE["/playbook/track"], COND, NOW)
check("route ETAs over → at the boat's position", s["basis"] == "point" and s["key"] == "onboard__boat"
      and s["path"][0]["lat"] == 44.5)
check("no playbook, no position → None", ob.resolve_spec({"available": False}, {"available": False}, NOW) is None)
check("a track without ETAs (older engine) → the boat's position",
      ob.resolve_spec(dict(TRACK, points=[{"lat": 44, "lon": -87}, {"lat": 45, "lon": -85}]), COND, NOW)["basis"] == "point")

print("pruning:")
old = os.path.join(os.environ["GRIB_CACHE"], "gfs", "f000.grib2")
os.utime(old, (NOW - 40 * 3600, NOW - 40 * 3600))
n = ob.prune_gribs(now=NOW)
check("GRIBs older than GRIB_KEEP_H deleted, newer kept",
      n >= 1 and not os.path.exists(old) and os.listdir(os.path.join(os.environ["GRIB_CACHE"], "gfs")))
check("online probe is a fast no on a closed port", ob.online("127.0.0.1:9", timeout=1) is False)

print("\nALL OK" if ok else "\nFAILURES")
raise SystemExit(0 if ok else 1)
