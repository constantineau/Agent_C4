"""#141 — the Lab routes each leg on its own obstacle mask.

Land, zones and island disks are on every leg; an island's rounding-side barrier only on the leg
that rounds it. Before, one course-wide mask carried every barrier on every leg, so a course that
passes the same island twice, once each way, walled itself off. Pinned here (no network, no
shoreline data: the race's own zones/islands/barriers only):
  1) the island-twice course: the old course-wide mask can't reach the far mark; the Lab's run
     (main._run_optimize) completes it, leaving the island to port both times;
  2) normal courses are unchanged: on Bayview Mackinac, each leg's mask is byte-identical to the
     old course-wide mask wherever that leg rounds an island, and the routes are identical;
  3) the cache: the base is built once, a leg with a barrier adds one entry, repeat runs reuse the
     same objects, the cache is bounded, and a changed rounding side rebuilds;
  4) the summary still draws every barrier; pace plays (from_mark) get their own legs' masks;
  5) the playbook downsample checks each shortcut on its own leg's mask.
"""
import json
import math
import os
import tempfile

import numpy as np

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

from app import main, boats, optimizer as OPT, current, wave, synthesis as SYN, venue  # noqa: E402
from app import wind as WIND  # noqa: E402
from app.wind import grib, windfield  # noqa: E402
from app.geo import obstacles as OB  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# no shoreline data in CI: the race's own zones, island disks and barriers only
OB._fill_global_backstop = lambda field, cache_dir=None: {}


class WF:
    loaded = True

    def __init__(self, tws=12.0, twd=0.0):
        self.tws, self.twd = tws, twd

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}

    def status(self):
        return {"models": []}


def side_of(path, clat, clon, lo, hi):
    """Which hand the island is on as the boat passes closest, within path[lo:hi]."""
    k = min(range(lo, hi), key=lambda i: OPT._hav_nm(path[i]["lat"], path[i]["lon"], clat, clon))
    i, j = (k, k + 1) if k + 1 < hi else (k - 1, k)
    h = math.radians(OPT._bearing(path[i]["lat"], path[i]["lon"], path[j]["lat"], path[j]["lon"]))
    ce = (clon - path[k]["lon"]) * 60 * math.cos(math.radians(path[k]["lat"]))
    cn = (clat - path[k]["lat"]) * 60
    return "port" if math.sin(h) * cn - math.cos(h) * ce > 0 else "starboard"


# Start → X (port) → A → X (port) → Finish: east past X, back west past X
X = (44.0, -82.0)
TWICE = {"race_id": "twice", "name": "Island twice", "zones": [], "courses": [{
    "id": "c", "start": {"lat": 44.0, "lon": -82.3},
    "marks": [{"name": "X", "type": "island", "lat": X[0], "lon": X[1], "radius_nm": 1.0,
               "rounding": "port"},
              {"name": "A", "type": "buoy", "lat": 44.0, "lon": -81.7, "rounding": "none"},
              {"name": "X", "type": "island", "lat": X[0], "lon": X[1], "radius_nm": 1.0,
               "rounding": "port"}],
    "finish": {"points": [{"lat": 44.03, "lon": -82.3}]}}]}
BBOX = OPT.course_bbox(TWICE, "c")

# ---- 1) the island-twice course ------------------------------------------------------------------
print("1) a course that passes the same island twice, once each way")
legs_of = OB.island_legs(TWICE, "c")
check("the first X is on leg 0, the second on leg 1", legs_of == {0: 0, 2: 1})
whole = OB.build_for_course(TWICE, "c", BBOX, use_cache=False)
old = OPT.optimize_course(TWICE, "c", 0, WF(), obstacles=whole, emit_exploration=False,
                          time_budget_s=60, resolution="fast")
new = OPT.optimize_course(TWICE, "c", 0, WF(), emit_exploration=False, time_budget_s=60,
                          resolution="fast")
print(f"     course-wide mask: {old.get('total_sailed_nm')} nm, complete={old.get('complete')}; "
      f"per-leg: {new.get('total_sailed_nm')} nm")
# both walls together close the island's meridian across the whole chart: the old route either
# stalls or sails off the chart (where nothing is checked) round the end of the wall
off_chart = any(p["lat"] < BBOX[1] or p["lat"] > BBOX[0] for p in old.get("path") or [])
check("before: the course-wide mask walls the course off (stalls, or detours off the chart)",
      not old.get("complete") or (off_chart and old["total_sailed_nm"] > 3 * new["total_sailed_nm"]))
check("per-leg masks: the route completes on the chart",
      new.get("complete") and not new.get("degraded")
      and all(BBOX[1] <= p["lat"] <= BBOX[0] for p in new["path"]))


def frames(model):
    lat = np.arange(43.0, 45.01, 0.25)
    lon = np.arange(-83.5, -80.49, 0.25)
    out = []
    for k in range(0, 40, 3):
        u = np.zeros((lat.size, lon.size))           # wind FROM the north at ~11.7 kn
        v = np.full((lat.size, lon.size), -6.0)
        out.append(grib.GribFrame(model, "det", float(T0 + k * 3600), lat, lon, u, v, True))
    return out


def fake_build_windfield(bbox, t_start, t_end, models=(), ensemble_members=0, on_progress=None,
                         model_weights=None, model_bias=None, fallback_tries=None):
    meta = [{"model": "gfs", "cycle": "2026-11-01 12Z", "members": 1, "frames": 14,
             "expected_frames": 14, "cycle_fallbacks": 0, "priority": 1.0,
             "kind": "deterministic"}]
    return windfield.WindField({("gfs", "det"): frames("gfs")}, meta, bbox, t_start, t_end)


T0 = 1_793_534_400
WIND.build_windfield = fake_build_windfield
current.build_currentfield = lambda *a, **k: current.ZeroCurrent()
wave.build_wavefield = lambda *a, **k: wave.ZeroWave()
boats.active_boat = lambda: None
venue.resolve = lambda *a, **k: None           # no venue model skill (it needs the network)
main.chart_source = lambda: "natural_earth"
lab = main._run_optimize(TWICE, "c", T0, ["gfs"], 0, use_waves=False, use_current=False,
                         resolution="fast")
print(f"     Lab run: {lab.get('total_hours')} h, {lab.get('total_sailed_nm')} nm, "
      f"complete={lab.get('complete')}")
check("the Lab's Optimize run completes the course",
      lab.get("available") and lab.get("complete") and len(lab.get("legs") or []) == 2)
path, ends = lab.get("path") or [], lab.get("leg_end_idx") or []
if len(ends) == 2:
    check("leg to A leaves X to port", side_of(path, *X, 0, ends[0] + 1) == "port")
    check("leg back leaves X to port", side_of(path, *X, ends[0], ends[1] + 1) == "port")
    check("the route never enters X's disk",
          all(OPT._hav_nm(p["lat"], p["lon"], *X) > 0.9 for p in path))
check("the map still draws both barriers",
      len((lab.get("obstacles") or {}).get("geometry", {}).get("rounding_barriers") or []) == 2)

# ---- 2) normal courses are unchanged (Bayview Mackinac) ------------------------------------------
print("2) Bayview Mackinac (each island rounded once): same masks, same routes")
BAY = None
for _p in (os.path.join(HERE, "races", "bayview_mackinac_2026.json"),
           "/srv/races/bayview_mackinac_2026.json"):
    if os.path.exists(_p):
        BAY = json.load(open(_p))
        break
check("Bayview fixture found", BAY is not None)
for cid in ("cove_island", "shore"):
    bb = OPT.course_bbox(BAY, cid)
    OB._FIELD_CACHE.clear()
    wide = OB.build_for_course(BAY, cid, bb, use_cache=False)
    base, legs = OB.build_leg_fields(BAY, cid, bb)
    rounds = {OB.island_legs(BAY, cid)[r["mark_idx"]] for r in OB._island_rounding_marks(BAY, cid)}
    check(f"{cid}: the base has no barrier", base.layers["rounding_barrier"] == 0
          and base.mask != wide.mask)
    for i, f in enumerate(legs):
        if i in rounds:
            check(f"{cid} leg {i} (rounds its islands): mask identical to the course-wide one",
                  f.mask == wide.mask)
        else:
            check(f"{cid} leg {i} (rounds none): routes on the base", f is base)
    # two winds a course keep CI quick; the PR has all four on the real shoreline
    for tws, twd in (((12, 0), (8, 225)) if cid == "cove_island" else ((12, 135), (16, 315))):
        a = OPT.optimize_course(BAY, cid, 0, WF(tws, twd), obstacles=wide, emit_exploration=False,
                                time_budget_s=600, resolution="fast")
        b = OPT.optimize_course(BAY, cid, 0, WF(tws, twd), emit_exploration=False,
                                time_budget_s=600, resolution="fast")
        same = a["path"] == b["path"] and a["total_minutes"] == b["total_minutes"]
        check(f"{cid} {tws} kn from {twd}: identical route ({b['total_hours']} h, "
              f"{b['total_sailed_nm']} nm)", same and b.get("complete"))

# ---- 3) the cache ----------------------------------------------------------------------------------
print("3) the obstacle cache")
bb = OPT.course_bbox(BAY, "cove_island")
OB._FIELD_CACHE.clear()
b1, l1 = OB.build_leg_fields(BAY, "cove_island", bb)
n1 = len(OB._FIELD_CACHE)
check(f"one base + one leg with barriers = {n1} entries", n1 == 2)
b2, l2 = OB.build_leg_fields(BAY, "cove_island", bb)
check("a second scenario reuses the same objects (no rebuild)",
      b1 is b2 and all(x is y for x, y in zip(l1, l2)) and len(OB._FIELD_CACHE) == n1)
r = OPT.optimize_course(BAY, "cove_island", 0, WF(), time_budget_s=1, emit_exploration=False)
check("optimize_course builds through the same cache", len(OB._FIELD_CACHE) == n1)
OB.build_leg_fields(TWICE, "c", BBOX)
check("the twice course: base + one per leg = 3 more", len(OB._FIELD_CACHE) == n1 + 3)
_max = OB.FIELD_CACHE_MAX
OB.FIELD_CACHE_MAX = 3
try:
    OB.build_leg_fields(BAY, "shore", OPT.course_bbox(BAY, "shore"))
    check("bounded: least-recently-used entries go", len(OB._FIELD_CACHE) == 3)
finally:
    OB.FIELD_CACHE_MAX = _max
flip = json.loads(json.dumps(TWICE))
flip["courses"][0]["marks"][0]["rounding"] = "starboard"
OB._FIELD_CACHE.clear()
w1 = OB.build_for_course(TWICE, "c", BBOX)
w2 = OB.build_for_course(flip, "c", BBOX)
check("a changed rounding side builds a new course-wide mask (was served the old one)",
      w1 is not w2 and w1.mask != w2.mask)
_, lf = OB.build_leg_fields(flip, "c", BBOX)
_, lt = OB.build_leg_fields(TWICE, "c", BBOX)
check("…and a new leg mask", lf[0].mask != lt[0].mask and lf[1].mask == lt[1].mask)

# ---- 4) summary + pace plays -----------------------------------------------------------------------
print("4) summary and from_mark")
s = new["obstacles"]
check("summary: both legs' barriers counted",
      s["layers"]["rounding_barrier"] == sum(f.layers["rounding_barrier"] for f in lt)
      and s["cells_blocked"] == sum(s["layers"].values()))
seen = []
_orig_route_leg = OPT.route_leg


def spy(*a, **k):
    seen.append(k.get("obstacles"))
    return _orig_route_leg(*a, **k)


OPT.route_leg = spy
try:
    rest = OPT.optimize_course(TWICE, "c", 0, WF(), from_mark=1, emit_exploration=False,
                               time_budget_s=60, resolution="fast")
finally:
    OPT.route_leg = _orig_route_leg
check("a pace play from A routes its one leg on the course's leg-1 mask",
      rest.get("complete") and len(seen) == 1 and seen[0] is lt[1])

# ---- 5) playbook downsample ------------------------------------------------------------------------
print("5) the play-route downsample checks each shortcut on its own leg's mask")


class Rec:
    def __init__(self, name):
        self.name, self.calls = name, []

    def crosses(self, la1, lo1, la2, lo2):
        self.calls.append((la1, la2))
        return False


dense = [{"lat": 44.0 + i * 1e-3, "lon": -82.0, "t": float(i)} for i in range(400)]
m0, m1, fb = Rec("leg0"), Rec("leg1"), Rec("base")
SYN._thin_route({"path": dense, "legs": [], "leg_end_idx": [150, 399]}, fb, max_pts=40,
                leg_obstacles=[m0, m1])
check("leg 0 shortcuts on leg 0's mask, leg 1's on leg 1's, none on the base",
      m0.calls and m1.calls and not fb.calls
      and all(b <= dense[150]["lat"] for _a, b in m0.calls)
      and all(a >= dense[150]["lat"] for a, _b in m1.calls))
fb2 = Rec("base")
SYN._thin_route({"path": dense, "legs": []}, fb2, max_pts=40, leg_obstacles=[m0, m1])
check("no leg_end_idx: falls back to the base mask", fb2.calls)
pb = SYN._play_obstacles(TWICE, "c")
check("_play_obstacles gives the base + per-leg masks", pb[0] is not None and len(pb[1]) == 2)

print("\nALL OK" if ok else "\nFAILURES")
raise SystemExit(0 if ok else 1)
