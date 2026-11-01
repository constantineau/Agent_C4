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
        return write_grib(self._cache_path(cycle, fhr, member, bbox), u=-(5.0 + fhr / 3.0), v=0.0)


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
      and out["key"].startswith("onboard__r1__left__"))
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


FETCHED.clear()
out = ob.run_once(now=NOW + 1800, get=get, post=post, record=record, is_online=lambda: False)
check("no download attempted (cache only)", FETCHED == [])
check("last runs pushed", out["ok"] and out["online"] is False
      and POSTS[-1][1]["models"][0]["runs"][0]["cycle"] == int(CYCLE.timestamp()))
check("its age keeps counting (built_at unchanged)", POSTS[-1][1]["built_at"] == NOW)

print("offline, and the target changed (fell another hour behind → new key):")
FETCHED.clear()
behind = dict(TRACK, points=[dict(pt, t=pt["t"] - 3600) for pt in TRACK["points"]])   # ETAs → new key
s_new = ob.resolve_spec(behind, COND, NOW + 2400)
check("a key with no store yet", ob.lab.load_store(s_new["key"]) is None)
ENGINE["/playbook/track"] = behind
_g = os.path.join(os.environ["GRIB_CACHE"], "gfs")
for _n in os.listdir(_g):                                   # downloaded half an hour before NOW
    os.utime(os.path.join(_g, _n), (NOW - 1800, NOW - 1800))
n_posts = len(POSTS)
out = ob.run_once(now=NOW + 2400, get=get, post=post, record=record, is_online=lambda: False)
pb = POSTS[-1][1] if len(POSTS) > n_posts else None
check("still pushes: resampled from the cached GRIBs", pb is not None and pb["key"] == s_new["key"]
      and pb["models"][0]["runs"][0]["cycle"] == int(CYCLE.timestamp()))
check("no download attempted (cache only)", FETCHED == [])
check("the age is when the files were downloaded, not now", pb is not None and pb["built_at"] == NOW - 1800)
check("the new store is kept for the next pass", ob.lab.load_store(s_new["key"]) is not None)
ENGINE["/playbook/track"] = TRACK

print("online, the target changed, its GRIBs already cached:")


class CachedGFS(FixtureGFS):
    """The real fetch's cache hit: a cached file is returned as is, nothing new is written."""

    def fetch(self, cycle, fhr, member, bbox, timeout=60):
        FETCHED.append(fhr)
        path = self._cache_path(cycle, fhr, member, bbox)
        return path if os.path.exists(path) else None


models.MODELS["gfs"] = CachedGFS()
six = dict(TRACK, points=[dict(pt, t=pt["t"] - 6 * 3600) for pt in TRACK["points"]])   # ETAs → new key
ENGINE["/playbook/track"] = six
n_posts = len(POSTS)
out = ob.run_once(now=NOW + 2450, get=get, post=post, record=record, is_online=lambda: True)
po = POSTS[-1][1] if len(POSTS) > n_posts else {}
check("resampled from the cache for the new key", po.get("key") == out["key"] and po.get("models")
      and po["models"][0]["runs"][0]["cycle"] == int(CYCLE.timestamp()))
check("the age is when the files were downloaded, not now", po.get("built_at") == NOW - 1800)
check("...and saved that way", ob.lab.load_store(out["key"])["runs"]["gfs"][0]["at"] == NOW - 1800)
models.MODELS["gfs"] = FixtureGFS()
ENGINE["/playbook/track"] = TRACK

print("offline, a store that exists but is empty or older than the cache:")
FETCHED.clear()
two = dict(TRACK, points=[dict(pt, t=pt["t"] - 2 * 3600) for pt in TRACK["points"]])   # same box, new key
k2 = ob.resolve_spec(two, COND, NOW + 2600)["key"]
ob.lab.save_store({"key": k2, "runs": {}})                    # e.g. left behind by a "waiting" pass
ENGINE["/playbook/track"] = two
ob.run_once(now=NOW + 2600, get=get, post=post, record=record, is_online=lambda: False)
pe = POSTS[-1][1]
check("empty store: the cached run is sampled, not 'waiting'", not pe.get("waiting") and pe["key"] == k2
      and pe["models"][0]["runs"][0]["cycle"] == int(CYCLE.timestamp()))
check("...without a download", FETCHED == [])
three = dict(TRACK, points=[dict(pt, t=pt["t"] - 3 * 3600) for pt in TRACK["points"]])
k3 = ob.resolve_spec(three, COND, NOW + 2700)["key"]
OLDC = int(CYCLE.timestamp()) - 6 * 3600
ob.lab.save_store({"key": k3, "runs": {"gfs": [{"cycle": OLDC, "at": NOW - 9000,
                                                "pts": [[NOW + 2700, 5.0, 10]]}]}})
ENGINE["/playbook/track"] = three
ob.run_once(now=NOW + 2700, get=get, post=post, record=record, is_online=lambda: False)
runs3 = POSTS[-1][1]["models"][0]["runs"]
check("older store: the newer cached cycle is added on top", runs3[0]["cycle"] == int(CYCLE.timestamp())
      and any(r["cycle"] == OLDC for r in runs3))
ENGINE["/playbook/track"] = TRACK

print("online but stalling (the probe passes, then the downloads hang):")
import time as _time  # noqa: E402


class StallGFS(FixtureGFS):
    """A newer GFS cycle is due; every download of it hangs far past the budget."""

    def pick_cycle(self, now=None, min_horizon_h=0):
        return CYCLE + dt.timedelta(hours=6)

    def fetch(self, cycle, fhr, member, bbox, timeout=60):
        STALLS.append(timeout)
        _time.sleep(4)
        return None


STALLS = []
models.MODELS["gfs"] = StallGFS()
ob.DOWNLOAD_BUDGET_S = 1.5
four = dict(TRACK, points=[dict(pt, t=pt["t"] - 4 * 3600) for pt in TRACK["points"]])   # same box, new key
ENGINE["/playbook/track"] = four
misses = os.path.join(os.environ["PLUME_DIR"], "misses.state")
if os.path.exists(misses):
    os.remove(misses)
n_posts = len(POSTS)
t0 = _time.monotonic()
out = ob.run_once(now=NOW + 2800, get=get, post=post, record=record, is_online=lambda: True)
took = _time.monotonic() - t0
check("the pass ends on the budget, not the stall (or the 600 s service kill)", took < 3.5)
check("each request asked for a short timeout", STALLS and max(STALLS) <= ob.REQUEST_TIMEOUT_S)
ps = POSTS[-1][1] if len(POSTS) > n_posts else {}
check("falls through to the cache: the new target's plume is posted", out["stalled"] and ps.get("key") == out["key"]
      and ps["models"][0]["runs"][0]["cycle"] == int(CYCLE.timestamp()))
check("back-off state saved, with no 'missing' recorded for a stall",
      os.path.exists(misses) and "gfs" not in open(misses).read())
ENGINE["/playbook/track"] = {"available": False}
ENGINE["/conditions"] = {"available": True, "lat": 40.0, "lon": -95.0}     # nothing cached here
n_posts = len(POSTS)
ob.run_once(now=NOW + 2900, get=get, post=post, record=record, is_online=lambda: True)
check("stalled with nothing cached for the spot → the waiting marker is posted",
      len(POSTS) > n_posts and POSTS[-1][1].get("waiting") is True)
models.MODELS["gfs"] = FixtureGFS()
ob.DOWNLOAD_BUDGET_S = 240.0
ENGINE["/playbook/track"] = TRACK
ENGINE["/conditions"] = COND

print("offline, crossed a grid line with no playbook, nothing cached for the new spot:")
ENGINE["/playbook/track"] = {"available": False}
ENGINE["/conditions"] = {"available": True, "lat": 41.0, "lon": -90.0}    # a box nothing was fetched for
n_posts = len(POSTS)
out = ob.run_once(now=NOW + 2500, get=get, post=post, record=record, is_online=lambda: False)
mk = POSTS[-1][1] if len(POSTS) > n_posts else {}
check("the engine is told: a waiting marker for the new spot", mk.get("waiting") is True and mk.get("models") == []
      and mk.get("source") == "onboard" and mk.get("key") == out["key"])
check("the engine's validator takes the marker",
      engine_plume.is_waiting(mk) and engine_plume.validate_waiting(mk) == (True, ""))
ENGINE["/playbook/track"] = TRACK
ENGINE["/conditions"] = COND

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
check("route ETAs over, boat off the route → at the boat's position (snapped to 0.25°)",
      s["basis"] == "point" and s["key"] == "onboard__pt-44.50_-86.50" and s["path"][0]["lat"] == 44.5)
check("no playbook, no position → None", ob.resolve_spec({"available": False}, {"available": False}, NOW) is None)
check("a track without ETAs (older engine) → the boat's position",
      ob.resolve_spec(dict(TRACK, points=[{"lat": 44, "lon": -87}, {"lat": 45, "lon": -85}]), COND, NOW)["basis"] == "point")

print("store keys follow what the samples depend on:")
FAR = {"available": True, "lat": 42.0, "lon": -80.0}            # nowhere near the route
k1 = ob.resolve_spec({"available": False}, {"available": True, "lat": 44.0, "lon": -87.0}, NOW)["key"]
k2 = ob.resolve_spec({"available": False}, {"available": True, "lat": 45.0, "lon": -85.0}, NOW)["key"]
k1b = ob.resolve_spec({"available": False}, {"available": True, "lat": 44.05, "lon": -87.05}, NOW)["key"]
check("boat moved 44N 87W → 45N 85W: a different key", k1 != k2)
check("a few hundred metres of drift: the same key (same grid point)", k1 == k1b)
r1 = ob.resolve_spec(TRACK, FAR, NOW)["key"]
moved = dict(TRACK, points=[TRACK["points"][0], dict(TRACK["points"][1], lat=45.5)])
retimed = dict(TRACK, points=[TRACK["points"][0], dict(TRACK["points"][1], t=NOW + 40 * 3600)])
check("same race + variant ids, new route geometry: a different key", ob.resolve_spec(moved, FAR, NOW)["key"] != r1)
check("same race + variant ids, new ETAs: a different key", ob.resolve_spec(retimed, FAR, NOW)["key"] != r1)
check("same route: the same key", ob.resolve_spec(dict(TRACK), FAR, NOW)["key"] == r1)

print("the boat moved before the next cycle (no skip, no stale samples):")
ENGINE["/playbook/track"] = {"available": False}
ENGINE["/conditions"] = {"available": True, "lat": 44.0, "lon": -87.0}
FETCHED.clear()
a1 = ob.run_once(now=NOW, get=get, post=post, record=record, is_online=lambda: True)
ENGINE["/conditions"] = {"available": True, "lat": 45.0, "lon": -85.0}
a2 = ob.run_once(now=NOW + 900, get=get, post=post, record=record, is_online=lambda: True)
p2 = POSTS[-1][1]
check("a new key after the move", a1["key"] != a2["key"] and p2["key"] == a2["key"])
check("the same cycle is sampled again at the new position (not skipped)",
      p2["models"] and p2["models"][0]["runs"][0]["cycle"] == int(CYCLE.timestamp()))
check("history doesn't mix positions: one run in the new store", len(p2["models"][0]["runs"]) == 1)

print("ETAs follow the boat's real progress:")
# plan: 44N 87W at NOW-1h → 45N 85W at NOW+30h. The boat is only 1/31 of the way along at NOW+3h,
# where the plan put it at NOW (3 h behind).
mid = {"available": True, "lat": 44.0 + 1 / 31, "lon": -87.0 + 2 / 31}
s3 = ob.resolve_spec(TRACK, mid, NOW + 3 * 3600)
check("3 h behind plan → ETAs shifted +3 h, in the key", s3["shift_s"] == 3 * 3600 and s3["key"].endswith("__s+3h")
      and s3["path"][0]["t"] == TRACK["points"][0]["t"] + 3 * 3600)
check("off the route (dock, delivery) → no shift", ob.resolve_spec(TRACK, FAR, NOW + 3 * 3600)["shift_s"] == 0)
check("before the planned start → no shift",
      ob.resolve_spec(TRACK, mid, TRACK["points"][0]["t"] - 60)["shift_s"] == 0)
near_end = {"available": True, "lat": 44.97, "lon": -85.06}
over = dict(TRACK, points=[dict(pt, t=pt["t"] - 35 * 3600) for pt in TRACK["points"]])   # plan ended NOW-5h
late = ob.resolve_spec(over, near_end, NOW)
check("plan ETAs over but the boat is still finishing → keeps the route, shifted",
      late["basis"] == "route" and 3 * 3600 <= late["shift_s"] <= 6 * 3600)
check("more than 12 h off plan → no shift (old playbook, boat moored on the course)",
      ob.progress_shift(ob.lab.clean_path(TRACK["points"]), (44.97, -85.06), NOW + 5 * 86400) == 0)

print("a due cycle that isn't posted yet is re-probed at most hourly:")
DUE = CYCLE + dt.timedelta(hours=6)
NAMCALLS = []


class LateNAM(models.NAM):
    """NAM whose due cycle (12Z) isn't posted: every fetch of it misses, 06Z is there."""

    def pick_cycle(self, now=None, min_horizon_h=0):
        return DUE

    def fetch(self, cycle, fhr, member, bbox, timeout=60):
        NAMCALLS.append(cycle)
        if cycle == DUE:
            return None
        return write_grib(os.path.join(os.environ["GRIB_CACHE"], "nam", f"f{fhr:03d}.grib2"), u=-4.0, v=0.0)


models.MODELS["nam"] = LateNAM()


def rec_nam(spec, **kw):
    return ob.lab.record(spec, models=("nam",), **kw)


ENGINE["/conditions"] = {"available": True, "lat": 43.5, "lon": -86.0}
ob.run_once(now=NOW, get=get, post=post, record=rec_nam, is_online=lambda: True)
check("first pass probes 12Z, falls back to 06Z", DUE in NAMCALLS and CYCLE in NAMCALLS
      and POSTS[-1][1]["models"][0]["runs"][0]["cycle"] == int(CYCLE.timestamp()))
NAMCALLS.clear()
ob.run_once(now=NOW + 900, get=get, post=post, record=rec_nam, is_online=lambda: True)
check("15 min later: no probe at all", NAMCALLS == [])
ENGINE["/conditions"] = {"available": True, "lat": 45.0, "lon": -84.5}   # a new target meanwhile
ob.run_once(now=NOW + 1200, get=get, post=post, record=rec_nam, is_online=lambda: True)
check("a new box gets its own probe of 12Z, then 06Z",
      DUE in NAMCALLS and POSTS[-1][1]["models"][0]["runs"][0]["cycle"] == int(CYCLE.timestamp()))
NAMCALLS.clear()
ENGINE["/conditions"] = {"available": True, "lat": 43.5, "lon": -86.0}  # the first box again
ob.run_once(now=NOW + 3700, get=get, post=post, record=rec_nam, is_online=lambda: True)
check("after an hour: 12Z probed again", DUE in NAMCALLS)

print("no run outside a model's coverage doesn't block it inside (HRRR):")
EDGE = []


class EdgeNAM(models.NAM):
    """A regional model whose grid ends at 44N: nothing for a box north edge below that."""

    def pick_cycle(self, now=None, min_horizon_h=0):
        return CYCLE

    def fetch(self, cycle, fhr, member, bbox, timeout=60):
        EDGE.append(bbox[0])
        if bbox[0] < 44:
            return None
        return write_grib(self._cache_path(cycle, fhr, member, bbox), u=-4.0, v=0.0)


models.MODELS["nam"] = EdgeNAM()
ENGINE["/conditions"] = {"available": True, "lat": 42.0, "lon": -86.0}       # outside coverage
ob.run_once(now=NOW + 600, get=get, post=post, record=rec_nam, is_online=lambda: True)
check("outside coverage: no run", EDGE and POSTS[-1][1].get("waiting") is True)
EDGE.clear()
ENGINE["/conditions"] = {"available": True, "lat": 44.5, "lon": -86.0}       # inside, 10 min later
ob.run_once(now=NOW + 1200, get=get, post=post, record=rec_nam, is_online=lambda: True)
check("moved into coverage within the hour: fetched, not backed off",
      EDGE and not POSTS[-1][1].get("waiting") and POSTS[-1][1]["models"][0]["model"] == "nam")

print("pruning:")
store_dir = os.environ["PLUME_DIR"]
oldkey = os.path.join(store_dir, "onboard__pt-40.00_-80.00.json")
with open(oldkey, "w") as f:
    f.write("{}")
os.utime(oldkey, (NOW - 50 * 3600, NOW - 50 * 3600))
cur = ob.lab._safe(a2["key"]) + ".json"
os.utime(os.path.join(store_dir, cur), (NOW - 50 * 3600, NOW - 50 * 3600))
n = ob.prune_stores(keep_key=a2["key"], now=NOW)
check("old targets' stores pruned after 48 h, the current one kept",
      n >= 1 and not os.path.exists(oldkey) and os.path.exists(os.path.join(store_dir, cur)))
check("the back-off state is never pruned as a store", os.path.exists(os.path.join(store_dir, "misses.state")))
gdir = os.path.join(os.environ["GRIB_CACHE"], "gfs")
old = os.path.join(gdir, sorted(os.listdir(gdir))[0])
os.utime(old, (NOW - 40 * 3600, NOW - 40 * 3600))
n = ob.prune_gribs(now=NOW)
check("GRIBs older than GRIB_KEEP_H deleted, newer kept",
      n >= 1 and not os.path.exists(old) and os.listdir(os.path.join(os.environ["GRIB_CACHE"], "gfs")))
check("online probe is a fast no on a closed port", ob.online("127.0.0.1:9", timeout=1) is False)


# long ids must not truncate away the route hash in the store filename
_t0 = NOW + 3600
_long = {"available": True, "race_id": "r" * 100, "variant": "left-route"}
_a = ob.resolve_spec(dict(_long, points=[{"lat": 44, "lon": -87, "t": _t0}, {"lat": 45, "lon": -86, "t": _t0 + 3600}]), None, NOW)
_b = ob.resolve_spec(dict(_long, points=[{"lat": 44, "lon": -87, "t": _t0}, {"lat": 45.5, "lon": -86, "t": _t0 + 3600}]), None, NOW)
check("long ids keep distinct store keys under 120 chars",
      _a and _b and len(_a["key"]) <= 120 and len(_b["key"]) <= 120 and _a["key"] != _b["key"])

print("\nALL OK" if ok else "\nFAILURES")
raise SystemExit(0 if ok else 1)
