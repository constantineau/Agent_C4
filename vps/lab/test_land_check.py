"""#51 and #145: the router must never take "land not checked" for "no land".

#51 — a shoreline that failed to load used to come back as an EMPTY land layer: cached for the
process, no warning, and a route straight over the Lower Peninsula looked complete and clean.
Pinned here:
  1) the coastline loader tells "unavailable" from "empty": missing files raise, a failed load is
     not cached, a cut-off GSHHG clip is dropped (never read as open water), an ogr2ogr timeout
     leaves no partial file behind;
  2) the obstacle build marks the field `land = "failed"` and doesn't cache it;
  3) the review's repro: Lake Michigan → Lake Huron across the Lower Peninsula, one island mark,
     Natural Earth download failed → the route is degraded, says so first, plainly; the map note
     says land wasn't checked. A real (loaded) shoreline gives no such warning;
  4) the Deploy chart and the playbook refuse to build on it.
#145 — the obstacle check SAMPLED each step every half cell, so a step clipping a blocked cell's
corner passed. Pinned: the clipped corner on the field itself, the router's own steps and the
playbook path thinning.

No network, no shoreline data: every load here is stubbed or a temp file.
"""
import json
import math
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
for _seed in (os.path.join(HERE, "..", "db", "seed"), "/srv"):
    if os.path.exists(os.path.join(_seed, "polars_sr33.sql")):
        os.environ["POLARS_FILE"] = os.path.join(_seed, "polars_sr33.sql")
        os.environ["SAIL_POLARS_FILE"] = os.path.join(_seed, "sr33_sail_polars.json")
        os.environ["CROSSOVERS_FILE"] = os.path.join(_seed, "sr33_crossovers.json")
        break
_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["GRIB_CACHE"] = os.path.join(_tmp, "gribcache")
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["DEPTH_GRIDS"] = "none"     # stub charts only: no real depth grid over the test water (#292)

from app import optimizer as OPT, orinassets, playbook, synthesis as SYN  # noqa: E402
from app import current as currentmod, wave as wavemod  # noqa: E402
from app.geo import coastline as C, obstacles as OB  # noqa: E402
from shared import gridwalk  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class WF:
    loaded = True
    series, meta = {}, []

    def __init__(self, tws=12.0, twd=0.0):
        self.tws, self.twd = tws, twd

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}

    def status(self):
        return {"models": []}


def fresh_cache():
    d = tempfile.mkdtemp(dir=_tmp)
    C._LAYERS.clear()
    OB._FIELD_CACHE.clear()
    return d


def write_fc(path, polys):
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in ring] for ring in poly]}}
            for poly in polys]}, f)


# no downloads, ever: GSHHG absent, Natural Earth "download failed"
C._ensure_gshhg = lambda cache_dir: False
_real_ensure_ne = C._ensure_ne
C._ensure_ne = lambda cache_dir: {}
C.GLOBAL_SOURCE = "gshhg"

# ---- 1) the loader -------------------------------------------------------------------------------
print("1) the coastline loader tells 'unavailable' from 'empty'")
BOX = (44.5, 43.5, -86.0, -83.0)
cache = fresh_cache()
try:
    got = C.layers_in_bbox(BOX, cache)
    raised = False
except C.CoastlineUnavailable:
    got, raised = None, True
check("no data at all → CoastlineUnavailable (not an empty layer)", raised)
check("…and nothing cached for the process", not C._LAYERS)

# the files arrive later (the next download works): the same process now loads them
LAND = [[(-86.5, 43.0), (-82.5, 43.0), (-82.5, 46.0), (-86.5, 46.0), (-86.5, 43.0)]]
for role, fn in C.NE_SOURCES.items():
    write_fc(os.path.join(cache, fn), [LAND] if role == "land" else
             [[[(-170, -10), (-169, -10), (-169, -9), (-170, -10)]]])
got = C.layers_in_bbox(BOX, cache)
check("a later good load is used (the failure wasn't cached)", len(got["land"]) == 1)

# a cut-off NE file (download died mid-way) is unreadable → unavailable, and deleted for a refetch
cache = fresh_cache()
for fn in C.NE_SOURCES.values():
    write_fc(os.path.join(cache, fn), [LAND])
with open(os.path.join(cache, C.NE_SOURCES["land"]), "w") as f:
    f.write('{"type": "FeatureCollection", "features": [{"type": "Feat')
try:
    C.layers_in_bbox(BOX, cache)
    raised = False
except C.CoastlineUnavailable:
    raised = True
check("a cut-off Natural Earth file → unavailable", raised)
check("…and it is deleted, so the next run fetches it again",
      not os.path.exists(os.path.join(cache, C.NE_SOURCES["land"])))

# GSHHG: a cut-off cached clip must not read as "no land"
cache = fresh_cache()
C._ensure_gshhg = lambda cache_dir: True
clip_dir = os.path.join(cache, C.GSHHG_DIR, "clip")
os.makedirs(clip_dir)
import hashlib  # noqa: E402
key = hashlib.sha1(repr((C.GSHHG_RES, tuple(round(x, 4) for x in BOX))).encode()).hexdigest()[:12]
for L in ("L1", "L2", "L3"):
    write_fc(os.path.join(clip_dir, f"{key}_{L}.json"), [])
with open(os.path.join(clip_dir, f"{key}_L1.json"), "w") as f:
    f.write('{"type": "FeatureCollection", "features": [{"type": "Feature", "geom')
check("a cut-off GSHHG clip → GSHHG unusable (falls back), not empty land",
      C._gshhg_layers_in_bbox(BOX, cache) is None)
check("…and the broken clip is deleted (re-clipped next time)",
      not os.path.exists(os.path.join(clip_dir, f"{key}_L1.json")))
# a genuinely empty clip (open water) is still a real answer
write_fc(os.path.join(clip_dir, f"{key}_L1.json"), [])
got = C._gshhg_layers_in_bbox(BOX, cache)
check("a readable empty clip is real open water ([] land), not a failure",
      got is not None and got["land"] == [])
C._ensure_gshhg = lambda cache_dir: False

# ogr2ogr times out half-way: nothing may be left under the clip's name
out = os.path.join(cache, "clip_timeout.json")
_run = subprocess.run


def _slow_ogr(args, **kw):
    with open(args[-2], "w") as f:          # ogr2ogr writes its output, then is killed
        f.write('{"type": "FeatureCollection", "features": [')
    raise subprocess.TimeoutExpired(args, kw.get("timeout"))


C.subprocess.run = _slow_ogr
wrote = C._clip_shp("/nonexistent.shp", out, BOX)
C.subprocess.run = _run
check("an ogr2ogr timeout → no clip", not wrote)
check("…and no partial file left behind (under any name)",
      not [f for f in os.listdir(cache) if f.startswith(("clip_timeout", ".clip-"))])

# two Lab jobs build the same new box at once: each must get a good clip (Codex on #200: a shared
# temp name let one delete or overwrite the other's half-written file → a false "didn't load")
import threading  # noqa: E402

GOOD = json.dumps({"type": "FeatureCollection", "features": []})


def _slow_ok(args, **kw):
    with open(args[-2], "w") as f:
        f.write(GOOD[:20])
        f.flush()
        time.sleep(0.4)
        f.write(GOOD[20:])
    return subprocess.CompletedProcess(args, 0, b"", b"")


import time  # noqa: E402
C.subprocess.run = _slow_ok
both = os.path.join(cache, "clip_both.json")
res = []
ts = [threading.Thread(target=lambda: res.append(C._clip_shp("/x.shp", both, BOX))) for _ in range(2)]
for t in ts:
    t.start()
    time.sleep(0.15)
for t in ts:
    t.join()
C.subprocess.run = _run
check("two clips of the same box at once: both succeed", res == [True, True])
check("…and the clip is whole", C._parse_geojson(both) == [])

# same for a Natural Earth download
_ur = C.urllib.request.urlretrieve
NE_OK = json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}]})
NE_OK += " " * (1200 - len(NE_OK))           # over the 1000-byte "already have it" floor


_n_get = []


def _slow_get(url, path):
    # each download writes a DIFFERENT (valid) body, so two writers mixing one file shows up
    _n_get.append(1)
    body = NE_OK if len(_n_get) % 2 else json.dumps(
        {"type": "FeatureCollection", "name": "x" * 200, "features": json.loads(NE_OK)["features"]})
    body += " " * (1200 - len(body))
    first = len(_n_get) % 2 == 1               # the first download is the slow one: the second
    with open(path, "w") as f:                  # finishes and lands while the first still writes
        f.write(body[:100])
        f.flush()
        time.sleep(0.6 if first else 0.1)
        f.write(body[100:])


C.urllib.request.urlretrieve = _slow_get
ne_dir = fresh_cache()
ts = [threading.Thread(target=_real_ensure_ne, args=(ne_dir,)) for _ in range(2)]
for t in ts:
    t.start()
    time.sleep(0.15)
for t in ts:
    t.join()
C.urllib.request.urlretrieve = _ur
check("two Natural Earth downloads at once: every file whole",
      all(C._parse_geojson(os.path.join(ne_dir, fn)) for fn in C.NE_SOURCES.values()))
check("…and no temp files left", not [f for f in os.listdir(ne_dir) if f.endswith(".part")])

# ---- 2) the obstacle build -------------------------------------------------------------------------
print("2) the obstacle build marks a failed shoreline and doesn't cache it")
# Lake Michigan → Lake Huron across the Lower Peninsula, one island mark (the review's repro)
CROSS = {"race_id": "lp", "name": "Across the peninsula", "zones": [], "courses": [{
    "id": "c", "start": {"lat": 44.0, "lon": -86.6},
    "marks": [{"name": "Rock", "type": "island", "lat": 44.3, "lon": -86.4, "radius_nm": 0.5,
               "rounding": "none"}],
    "finish": {"points": [{"lat": 44.0, "lon": -82.6}]}}]}
BBOX = OPT.course_bbox(CROSS, "c")
OB.coastline.CACHE = fresh_cache()
f = OB.build_for_course(CROSS, "c", BBOX)
check("land = 'failed' on the field", f.land == "failed" and f.land_error)
check("the island still makes the field active (so 'active' alone can't tell)", f.active)
check("the summary says so", f.summary()["land"] == "failed")
check("a failed build is not cached", not OB._FIELD_CACHE)
base, legs = OB.build_leg_fields(CROSS, "c", BBOX)
check("per-leg fields carry it, still uncached",
      base.land == "failed" and all(x.land == "failed" for x in legs) and not OB._FIELD_CACHE)
f_off = OB.build_for_course(CROSS, "c", BBOX, coastline_on=False, use_cache=False)
check("coastline not asked for → land 'off', not 'failed'", f_off.land == "off")

# ---- 3) the route -----------------------------------------------------------------------------------
print("3) the route: degraded, and says so first")
r = OPT.optimize_course(CROSS, "c", 0, WF(12, 0), emit_exploration=False, time_budget_s=60,
                        resolution="fast")
print(f"     complete={r.get('complete')} degraded={r.get('degraded')} "
      f"sailed={r.get('total_sailed_nm')} direct={r.get('total_direct_nm')}")
check("degraded", r.get("degraded") is True)
check("the first warning is the plain land one", (r.get("warnings") or [None])[0] == OPT.LAND_FAILED)
check("the map note's data says land failed", r["obstacles"].get("land") == "failed")

# the build itself blows up (not just the shoreline): same answer
_blf = OB.build_leg_fields


def _boom(*a, **k):
    raise RuntimeError("disk full")


import app.geo as G  # noqa: E402
G.build_leg_fields = _boom
r2 = OPT.optimize_course(CROSS, "c", 0, WF(12, 0), emit_exploration=False, time_budget_s=60,
                         resolution="fast")
G.build_leg_fields = _blf
check("an obstacle build that crashes: degraded + the land warning",
      r2.get("degraded") and r2["warnings"][0] == OPT.LAND_FAILED
      and r2["obstacles"] == {"active": False, "land": "failed"})

r3 = OPT.optimize_course(CROSS, "c", 0, WF(12, 0), emit_exploration=False, time_budget_s=60,
                         resolution="fast", avoid=False)
check("avoid=False (land deliberately off) is not reported as a failure",
      OPT.LAND_FAILED not in r3.get("warnings", []) and r3["obstacles"].get("land") != "failed")

# control: the shoreline loads (Natural Earth files present) → land checked, no warning, no crossing
cache = fresh_cache()
PEN = [[(-86.0, 41.5), (-83.0, 41.5), (-83.0, 44.45), (-86.0, 44.45), (-86.0, 41.5)]]   # "Michigan"
TINY = [[[(-170, -10), (-169, -10), (-169, -9), (-170, -10)]]]
for role, fn in C.NE_SOURCES.items():
    write_fc(os.path.join(cache, fn), [PEN] if role == "land" else TINY)
OB.coastline.CACHE = cache
C.GLOBAL_SOURCE = "natural_earth"
r4 = OPT.optimize_course(CROSS, "c", 0, WF(12, 0), emit_exploration=False, time_budget_s=120,
                         resolution="fast")
on_land = [p for p in r4.get("path") or [] if -86.0 < p["lon"] < -83.0 and 41.5 < p["lat"] < 44.45]
print(f"     loaded: complete={r4.get('complete')} sailed={r4.get('total_sailed_nm')} "
      f"points on the peninsula={len(on_land)}")
check("(control) a loaded shoreline: land 'ok', no land warning",
      r4["obstacles"].get("land") == "ok" and OPT.LAND_FAILED not in r4.get("warnings", []))
check("(control) …and the route never crosses it", r4.get("path") and not on_land)
C.GLOBAL_SOURCE = "gshhg"
OB.coastline.CACHE = fresh_cache()

# ---- 4) Deploy chart + playbook ---------------------------------------------------------------------
print("4) the Deploy chart and the playbook refuse")
# ENC gave some US land, the global shoreline (Canada) failed: land cells > 0, still refused
_enc_ensure, _enc_layers = OB.enc.ensure_bbox, OB.enc.layers_in_bbox
OB.enc.ensure_bbox = lambda bbox, cache_dir=None: None
OB.enc.layers_in_bbox = lambda bbox, cache_dir=None, depth=None: {"land": [PEN], "shoal": []}
_ls = orinassets.labstate.get
orinassets.labstate.get = lambda k, *a: "enc" if k == "coastline_source" else _ls(k, *a)
chart, why = orinassets.build_chart(CROSS, "c", {"draft_m": 2.1336})
orinassets.labstate.get = _ls
OB.enc.ensure_bbox, OB.enc.layers_in_bbox = _enc_ensure, _enc_layers
check("ENC land but no global shoreline → no chart, plain reason",
      chart is None and why == orinassets.NO_LAND)

playbook.build_windfield = lambda *a, **k: WF(12, 0)
currentmod.build_currentfield = lambda *a, **k: currentmod.ZeroCurrent()
wavemod.build_wavefield = lambda *a, **k: wavemod.ZeroWave()
pb = playbook.build_playbook(CROSS, "c", 0, ["gfs"], time_budget_s=60, v2_scenarios=False,
                             model_skill=False)
check("no playbook is built on it, and it says why",
      pb.get("available") is False and "Shoreline didn't load" in (pb.get("note") or ""))

# ---- 5) #145: the exact segment check ---------------------------------------------------------------
print("5) #145: a step that clips a blocked cell's corner is caught")
RES = 0.01
fld = OB.ObstacleField((44.1, 44.0, -82.1, -82.0), res_deg=RES)
fld.mask[5 * fld.nx + 5] = 1                 # one cell: lon [-82.05, -82.04], lat [44.05, 44.06]
fld.active = True


def g2ll(x, y):
    return 44.0 + y * RES, -82.1 + x * RES


def sampled(f, la1, lo1, la2, lo2):          # the old check, for the control
    steps = max(2, int(math.hypot(la2 - la1, lo2 - lo1) / (f.res * 0.5)) + 1)
    return any(f.blocked(la1 + (la2 - la1) * k / steps, lo1 + (lo2 - lo1) * k / steps)
               for k in range(steps + 1))


A, B = g2ll(5.09, 4.1), g2ll(7.09, 6.1)       # clips the cell's lower-right corner
check("(control) the old sampled check misses it", not sampled(fld, *A, *B))
check("crosses() catches it", fld.crosses(*A, *B) and fld.crosses(*B, *A))
check("a clear step stays clear", not fld.crosses(*g2ll(6.2, 4.1), *g2ll(8.2, 6.1)))

# the playbook's path thinning: the shortcut A → B clips the corner, so the middle point stays
P3 = [{"lat": A[0], "lon": A[1], "t": 0}, {"lat": g2ll(7.2, 4.0)[0], "lon": g2ll(7.2, 4.0)[1], "t": 1},
      {"lat": B[0], "lon": B[1], "t": 2}]
check("path thinning keeps the vertex round the corner", SYN._downsample_idx(P3, 2, obstacles=fld) == [0, 1, 2])

# the router: a short leg whose straight line clips a blocked block's corner
RES2 = 0.005
rf = OB.ObstacleField((44.2, 43.9, -82.3, -81.9), res_deg=RES2)
for iy in range(30, 34):                     # a 4 x 4 cell "rock", lat 44.05-44.07, lon -82.13..-82.11
    for ix in range(34, 38):
        rf.mask[iy * rf.nx + ix] = 1
rf.active = True
cx, cy = -82.3 + 38 * RES2, 43.9 + 30 * RES2     # its lower-right (south-east) corner
S0 = (cy - 0.05 + 0.0003, cx - 0.05 - 0.0003)    # the rhumb line clips the corner by ~0.0003°
F0 = (cy + 0.05 + 0.0003, cx + 0.05 - 0.0003)


def touches(f, path):
    """Path segments touching a blocked cell: the shared exact walk directly (independent of the
    field's own crosses(), which is the code under test)."""
    def g(la, lo):
        return (lo - f.w) / f.res, (la - f.s) / f.res
    return [i for i in range(len(path) - 1)
            if gridwalk.any_set(f.mask, f.nx, f.ny, *g(path[i]["lat"], path[i]["lon"]),
                                *g(path[i + 1]["lat"], path[i + 1]["lon"]))]


ROCK = {"race_id": "rock", "name": "Rock", "zones": [], "courses": [{
    "id": "c", "start": {"lat": S0[0], "lon": S0[1]}, "marks": [],
    "finish": {"points": [{"lat": F0[0], "lon": F0[1]}]}}]}
check("(control) the old check calls the rhumb line clear", not sampled(rf, *S0, *F0))
check("…the exact one does not", rf.crosses(*S0, *F0))
rr = OPT.optimize_course(ROCK, "c", 0, WF(10, 315), obstacles=rf, emit_exploration=False,
                         time_budget_s=60, resolution="fast")
hits = touches(rf, rr.get("path") or [])
print(f"     router: complete={rr.get('complete')} sailed={rr.get('total_sailed_nm')} "
      f"direct={rr.get('total_direct_nm')} segments on the rock={hits}")
check("the routed path never touches the rock", rr.get("complete") and not hits)

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
