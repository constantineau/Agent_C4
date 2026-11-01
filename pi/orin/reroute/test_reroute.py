"""Onboard re-route (pi/orin/reroute) — weather rules, one run end to end, the job lifecycle.

Pins:
  * weather: the plume's cached files are used first and nothing downloads offline or once the
    budget is spent; offline picks the cached cycle that covers the run best; the run uses a box
    the cache already holds when one covers the course;
  * the remaining course: from the boat, next mark on, islands already passed dropped;
  * one full run, offline, from a small fixture GRIB: the Lab's optimizer routes it, the result
    has the route with ETAs, the gain vs the plan, the models + cycles + weather age, who asked,
    run time, "Off the playbook"; no chart aboard → it says land isn't checked; a chart file → used;
  * the service: one run at a time (busy + progress), done → last result kept, error and
    too-long runs reported in a short sentence, peers outside the allow list refused, and a real
    spawned job.py reporting a run that can't start.

Runs in the Lab image (eccodes/cfgrib): tools/ci/run.sh, or
  PYTHONPATH=vps/lab:. python3 pi/orin/reroute/test_reroute.py
"""
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
_tmp = tempfile.mkdtemp()
os.environ["GRIB_CACHE"] = os.path.join(_tmp, "gribcache")
os.environ["REROUTE_DIR"] = os.path.join(_tmp, "reroute")
os.environ["PLUME_DIR"] = os.path.join(_tmp, "plume")
os.environ["GRIB_ISOLATE_PARSE"] = "0"
os.environ["INGESTED_DIR"] = os.path.join(_tmp, "ingested")
for _seed in (os.path.join(REPO, "vps", "db", "seed"), "/srv"):
    if os.path.exists(os.path.join(_seed, "polars_sr33.sql")):
        os.environ["POLARS_FILE"] = os.path.join(_seed, "polars_sr33.sql")
        os.environ["SAIL_POLARS_FILE"] = os.path.join(_seed, "sr33_sail_polars.json")
        os.environ["CROSSOVERS_FILE"] = os.path.join(_seed, "sr33_crossovers.json")
        break
for _b in (os.path.join(REPO, "vps", "lab", "boats"), "/srv/boats"):
    if os.path.isdir(_b):
        os.environ["BOATS_DIR"] = _b
        break
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "plume"))

import eccodes  # noqa: E402
import numpy as np  # noqa: E402

import job  # noqa: E402
import orin_weather as W  # noqa: E402
import service  # noqa: E402
from app import plume as lab_plume  # noqa: E402
from app.geo import obstacles as geo  # noqa: E402
from app.wind import models, windfield  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def write_grib(path, u, v):
    """A tiny 10 m wind GRIB2: 0.5° grid 42–47N, 86–80W, uniform (u, v) m/s."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ni, nj = 13, 11
    with open(path, "wb") as f:
        for short, val in (("10u", u), ("10v", v)):
            h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
            for k, x in (("Ni", ni), ("Nj", nj), ("latitudeOfFirstGridPointInDegrees", 47.0),
                         ("longitudeOfFirstGridPointInDegrees", 274.0),
                         ("latitudeOfLastGridPointInDegrees", 42.0),
                         ("longitudeOfLastGridPointInDegrees", 280.0),
                         ("iDirectionIncrementInDegrees", 0.5), ("jDirectionIncrementInDegrees", 0.5)):
                eccodes.codes_set(h, k, x)
            eccodes.codes_set(h, "shortName", short)
            eccodes.codes_set_values(h, np.full(ni * nj, float(val)))
            eccodes.codes_write(h, f)
            eccodes.codes_release(h)
    return path


NOW = 1_793_552_400                                    # 2026-11-01T17:00Z
CYCLE = dt.datetime(2026, 11, 1, 12, tzinfo=dt.timezone.utc)
OLD = dt.datetime(2026, 11, 1, 6, tzinfo=dt.timezone.utc)
NET = []


class FixtureGFS(models.GFS):
    """GFS whose 'network' writes a fixture file (and is counted): a 6 m/s south-westerly."""

    def fetch(self, cycle, fhr, member, bbox, timeout=60):
        NET.append((cycle, fhr))
        return write_grib(self._cache_path(cycle, fhr, member, bbox), u=4.2, v=4.2)


models.MODELS["gfs"] = FixtureGFS()
GFS = models.MODELS["gfs"]

# ---- 1) weather ---------------------------------------------------------------------------------
print("1) weather: cache first, downloads only when allowed")
BOX = (46.0, 43.0, -84.0, -81.0)
T1 = NOW + 30 * 3600
off = W.GribSource(GFS, W.Budget(False), BOX, NOW, T1)
check("offline + nothing cached → None, no network", off.fetch(CYCLE, 6, "det", BOX) is None and not NET)
on = W.GribSource(GFS, W.Budget(True), BOX, NOW, T1)
p = on.fetch(CYCLE, 6, "det", BOX)
check("online → downloaded once, counted", p and len(NET) == 1 and on._budget.downloads == 1)
check("cached → returned with no network (even offline)", off.fetch(CYCLE, 6, "det", BOX) == p and len(NET) == 1)
spent = W.GribSource(GFS, W.Budget(True, budget_s=0), BOX, NOW, T1)
check("budget spent → no download, flagged", spent.fetch(CYCLE, 9, "det", BOX) is None
      and len(NET) == 1 and spent._budget.exhausted)
# cache: the old cycle complete over the window, the new one with a single frame
for f in windfield._fhrs_for_cycle(GFS, OLD, NOW, T1):
    write_grib(GFS._cache_path(OLD, f, "det", BOX), 4.2, 4.2)
check("offline picks the cycle the cache covers best", off.best_cached_cycle() == OLD
      and off.pick_cycle() == OLD)
for f in windfield._fhrs_for_cycle(GFS, CYCLE, NOW, T1):
    write_grib(GFS._cache_path(CYCLE, f, "det", BOX), 4.2, 4.2)
check("a newer cycle with as many frames wins", off.best_cached_cycle() == CYCLE)
api = W.ApiSource(models.MODELS["icon"], W.Budget(False))
series, meta = api.load_series(BOX, NOW, T1)
check("ICON/GEM offline → skipped, no call", series == {} and meta["skipped"] == "offline")
need = (45.5, 43.5, -83.5, -81.5)
check("a cached box that holds the course is used", W.choose_bbox(need, [BOX, (50, 40, -90, -75)], False)
      == (BOX, "cache"))
check("none holds it, online → the course box", W.choose_bbox((47, 43, -84, -81), [BOX], True)[0]
      == (47, 43, -84, -81))
check("none holds it, offline → the best overlap", W.choose_bbox((46.5, 43, -84, -81), [BOX], False)[0] == BOX)
W.remember_box(os.environ["REROUTE_DIR"], BOX)
check("boxes remembered for the next run", W.load_boxes(os.environ["REROUTE_DIR"]) == [BOX])

# ---- 2) the remaining course ----------------------------------------------------------------------
print("2) the remaining course")
DEF = {"race_id": "huron", "name": "Huron race", "zones": [], "courses": [{
    "id": "c", "start": {"lat": 43.07, "lon": -82.42},
    "marks": [{"name": "North", "type": "buoy", "lat": 44.0, "lon": -82.1, "rounding": "port"},
              {"name": "Rock", "type": "island", "lat": 44.3, "lon": -82.6, "rounding": "starboard"},
              {"name": "Gate", "type": "gate", "lat": 44.9, "lon": -82.3, "lat2": 44.9,
               "lon2": -82.2, "rounding": "gate"},
              {"name": "Isle", "type": "island", "lat": 45.3, "lon": -82.9, "rounding": "port"}],
    "finish": {"points": [{"lat": 45.3, "lon": -82.4}]}}]}
d, cid, names = job.remaining_definition(DEF, "c", 2, 44.2, -82.2)
check("from the boat, next mark on", d["courses"][0]["start"]["lat"] == 44.2 and names == ["Gate", "Finish"])
check("islands on this leg and ahead kept", [m["name"] for m in d["courses"][0]["marks"]] == ["Rock", "Gate", "Isle"])
d, cid, names = job.remaining_definition(DEF, "c", 3, 45.0, -82.5)
check("last leg: only the finish to route to", names == ["Finish"])
check("…every island still aboard; the rounded one without its side",
      [(m["name"], m["rounding"]) for m in d["courses"][0]["marks"]]
      == [("Rock", "none"), ("Isle", "port")])
# out and back past an island (Codex repro): Start → island → A → Finish, the way home crosses it
BACK = {"race_id": "back", "zones": [{"name": "Old no-go", "type": "exclusion", "geometry": {
    "north": 44.25, "south": 44.15, "west": -82.3, "east": -82.2}}],
    "courses": [{"id": "c", "start": {"lat": 44.0, "lon": -82.0},
                 "marks": [{"name": "Rocky", "type": "island", "lat": 44.5, "lon": -82.0,
                            "rounding": "starboard", "radius_nm": 1.5},
                           {"name": "A", "type": "buoy", "lat": 45.0, "lon": -82.0,
                            "rounding": "port"}],
                 "finish": {"points": [{"lat": 44.0, "lon": -82.02}]}}]}
d6, c6, n6 = job.remaining_definition(BACK, "c", 2, 44.95, -82.0)     # A rounded, heading home
f6, note6 = job.load_obstacles(d6, c6, None, None, job._get_boat())
check("way home: the rounded island is still solid", n6 == ["Finish"] and f6.blocked(44.5, -82.0))
check("…but leaves no rounding barrier", f6.layers["rounding_barrier"] == 0)
check("…and every zone stays, even on a sailed leg", f6.blocked(44.2, -82.25))
d7, c7, _n = job.remaining_definition(BACK, "c", 1, 44.1, -82.0)      # before rounding it
f7, _ = job.load_obstacles(d7, c7, None, None, job._get_boat())
check("before rounding it: disk and barrier both", f7.blocked(44.5, -82.0)
      and f7.layers["rounding_barrier"] > 0)
NOSTART = {"courses": [{"id": "c", "start": {},
                       "marks": [{"name": "Rocky", "type": "island", "lat": 44.5, "lon": -82.0,
                                  "rounding": "port", "radius_nm": 1.5},
                                 {"name": "A", "type": "buoy", "lat": 45.0, "lon": -82.0,
                                  "rounding": "port"}],
                       "finish": {"points": [{"lat": 44.0, "lon": -82.02}]}}]}
d9, _c9, n9 = job.remaining_definition(NOSTART, "c", 1, 44.95, -82.0)   # no start: A is index 0, rounded
r9 = {m["name"]: m["rounding"] for m in d9["courses"][0]["marks"]}
check("no start, island before A: rounded island keeps no barrier", n9 == ["Finish"] and r9.get("Rocky") == "none")
d10, _c10, _n10 = job.remaining_definition(NOSTART, "c", 0, 44.1, -82.0)   # still heading to A
check("no start, before A: island keeps its side",
      {m["name"]: m["rounding"] for m in d10["courses"][0]["marks"]}.get("Rocky") == "port")
# the current leg's island keeps its DEPLOYED rounding side (Codex repro): Start (44,-83) → island
# (44,-82) left to port → Finish (44,-81); the boat south of the island, mid-leg
PORT = {"race_id": "port", "zones": [], "courses": [{"id": "c", "start": {"lat": 44.0, "lon": -83.0},
        "marks": [{"name": "Isl", "type": "island", "lat": 44.0, "lon": -82.0, "rounding": "port"}],
        "finish": {"points": [{"lat": 44.0, "lon": -81.0}]}}]}
deployed = geo.build_for_course(PORT, "c", job.optimizer.course_bbox(PORT, "c"), coastline_on=False,
                                use_cache=False, source="natural_earth")
check("(the deployed barrier blocks 44.1N 82W)", deployed.blocked(44.1, -82.0))
dP, cP, _n = job.remaining_definition(PORT, "c", 1, 43.5, -82.0)
fP, _ = job.load_obstacles(dP, cP, None, None, job._get_boat())
check("after trimming (boat as start) it's still blocked", fP.blocked(44.1, -82.0))
check("…the island carries the deployed leg's bearing (~090)",
      abs(dP["courses"][0]["marks"][0]["transit_brg"] - 90) < 1.5)
_unpinned = json.loads(json.dumps(dP))
_unpinned["courses"][0]["marks"][0].pop("transit_brg")
check("(without the pinned bearing that water would open)", not job.load_obstacles(
    _unpinned, cP, None, None, job._get_boat())[0].blocked(44.1, -82.0))
os.makedirs(job.CHART_DIR, exist_ok=True)
_land = geo.build_for_course(PORT, "c", (44.6, 42.9, -83.6, -80.4), coastline_on=False,
                             use_cache=False, zones_on=False, islands_on=False)
_land._fill_polygon([[(-83.4, 43.1), (-83.3, 43.1), (-83.3, 43.2), (-83.4, 43.2)]], 1, "coastline")
_land.active = True
with open(job.chart_path("port", "c"), "w") as fh:
    json.dump(dict(geo.field_to_dict(_land), kind="land",
                   fingerprint=geo.course_fingerprint(PORT, "c", DEPTH_P := job.boats.safety_depth_m(
                       job._get_boat()))), fh)
fC, nC = job.load_obstacles(dP, cP, PORT, "c", job._get_boat())
check("chart path: same, the deployed side holds", nC is None and fC.blocked(44.1, -82.0))
# the same island passed twice, once each way: each occurrence keeps its own deployed bearing
TWICE = {"race_id": "twice", "zones": [], "courses": [{"id": "c", "start": {"lat": 44.0, "lon": -83.0},
         "marks": [{"name": "X", "type": "island", "lat": 44.0, "lon": -82.0, "rounding": "port"},
                   {"name": "A", "type": "buoy", "lat": 44.0, "lon": -81.0, "rounding": "port"},
                   {"name": "X", "type": "island", "lat": 44.0, "lon": -82.0, "rounding": "port"}],
         "finish": {"points": [{"lat": 44.05, "lon": -83.0}]}}]}
dT, cT, _n = job.remaining_definition(TWICE, "c", 1, 43.9, -82.6)
brgs = [m.get("transit_brg") for m in dT["courses"][0]["marks"] if m["type"] == "island"]
check("island passed twice: two bearings, one each way (~090, ~270)", len(brgs) == 2
      and abs(brgs[0] - 90) < 4 and abs(brgs[1] - 270) < 4)
_src = job.geo.COASTLINE_SOURCE
job.geo.COASTLINE_SOURCE = "enc"                       # the Lab's ENC setting, no chart aboard
f8, _ = job.load_obstacles(d6, c6, None, None, job._get_boat())
job.geo.COASTLINE_SOURCE = _src
check("coastline source ENC, no chart: the island is still solid", f8.blocked(44.5, -82.0))
try:
    job.remaining_definition(DEF, "c", 4, 45.3, -82.4)
    check("past the finish → refused", False)
except job.RunError as e:
    check("past the finish → refused, short", str(e) == "Course finished. Nothing to re-route.")
d, cid, names = job.minimal_definition([{"name": "Start", "lat": 43.07, "lon": -82.42},
                                        {"name": "North", "lat": 44.0, "lon": -82.1},
                                        {"name": "Finish", "lat": 45.3, "lon": -82.4}], 1, 43.5, -82.3)
check("no definition aboard → the navigator's marks", names == ["North", "Finish"])
NOSTART = {"race_id": "n", "zones": [], "courses": [{"id": "c", "marks": [
    {"name": "First", "type": "buoy", "lat": 44.0, "lon": -82.1, "rounding": "none"},
    {"name": "Second", "type": "buoy", "lat": 44.5, "lon": -82.2, "rounding": "none"}],
    "finish": {"points": [{"lat": 45.0, "lon": -82.3}]}}]}
d, cid, names = job.remaining_definition(NOSTART, "c", 0, 43.8, -82.0)
check("no positioned start, next index 0 → no mark skipped", names == ["First", "Second", "Finish"])
d, cid, names = job.remaining_definition(NOSTART, "c", None, 43.8, -82.0)
check("…and the default with no start is the first mark", names == ["First", "Second", "Finish"])
d, cid, names = job.remaining_definition(DEF, "c", None, 43.0, -82.5)
check("default with a start: the first mark after it", names[0] == "North")
d, cid, names = job.remaining_definition(DEF, "c", 0, 43.0, -82.5)
check("before the start (index 0): the start line first", names[:2] == ["Start", "North"])

# ---- 3) one run, end to end, offline -----------------------------------------------------------
print("3) one full run, offline, from the cache")
NET.clear()
PLAN = {"variant": "left", "variant_label": "Left side",
        "points": [{"lat": 43.07, "lon": -82.42, "t": NOW - 8 * 3600},
                   {"lat": 44.0, "lon": -82.1, "t": NOW - 2 * 3600},
                   {"lat": 44.9, "lon": -82.25, "t": NOW + 6 * 3600},
                   {"lat": 45.3, "lon": -82.4, "t": NOW + 9 * 3600}]}
plume_box = lab_plume._bbox_for({"path": lab_plume.clean_path(PLAN["points"])})
for f in windfield._fhrs_for_cycle(GFS, CYCLE, NOW, NOW + 24 * 3600):
    write_grib(GFS._cache_path(CYCLE, f, "det", plume_box), 4.2, 4.2)   # what the plume left
REQ = {"device": "iPad 1", "asked_at": NOW, "job_id": "j-test",
       "position": {"lat": 44.2, "lon": -82.2, "t": NOW}, "next_index": 2,
       "definition": DEF, "course_id": "c", "plan": PLAN, "models": ["gfs"]}
job.STATE_DIR = os.path.join(_tmp, "fresh")          # no remembered boxes: only the plume's
phases = []
out = job.run(REQ, now=NOW, on_phase=phases.append, is_online=lambda: False)
check("a route, complete", out["available"] and out["complete"] and not out["degraded"])
check("off the playbook, from the Orin", out["label"] == "Off the playbook" and out["off_playbook"]
      and out["schema"] == "c4.reroute/v1")
check("who asked + run time + memory", out["asked_by"] == "iPad 1" and out["run_s"] >= 0
      and out["peak_rss_mb"]["process"] > 0)
check("points with ETAs, from the boat", len(out["points"]) >= 3 and out["points"][0]["lat"] == 44.2
      and all(p["t"] >= NOW for p in out["points"]))
check("legs to Gate then Finish, with ETAs", [lg["to"] for lg in out["legs"]] == ["Gate", "Finish"]
      and out["legs"][-1]["eta"] == out["finish_eta"])
w = out["weather"]
check("weather: gfs 12Z, 5 h old, offline, no downloads", [m["model"] for m in w["models"]] == ["gfs"]
      and w["models"][0]["cycle"] == "2026-11-01 12Z" and w["newest_age_h"] == 5.0
      and w["online"] is False and w["downloads"] == 0 and not NET)
check("used the plume's cached box", w["box"] == "cache")
v = out["vs_plan"]
check("gain vs the plan, in minutes", v["available"] and isinstance(v["gain_min"], int)
      and v["gain_min"] == v["plan_left_min"] - v["new_left_min"])
check("no chart aboard → it says so, short", out["warnings"][0] == "No chart aboard. Route doesn't check the shore."
      and out["chart"]["land"] is False)
check("phases in order", phases[:3] == ["chart", "weather", "routing"] and phases[-1] == "saving")

os.makedirs(job.CHART_DIR, exist_ok=True)
DEPTH = job.boats.safety_depth_m(job._get_boat())


LAND = (44.0, 43.9, -83.0, -82.9)                       # a fake "land" block, away from the route


def write_chart(defn, fingerprint=True, kind="land", full=False):
    """A carried chart like prep_chart.py writes: land only (here one fake block), or — `full` —
    the old style with the whole race's zones and barriers baked in."""
    f = geo.build_for_course(defn, "c", (46.0, 42.5, -83.5, -81.5), coastline_on=False,
                             use_cache=False, zones_on=full, islands_on=full)
    n, s_, w, e = LAND
    f._fill_polygon([[(w, s_), (e, s_), (e, n), (w, n)]], 1, "coastline")
    f.active = True
    d = geo.field_to_dict(f)
    if kind:
        d["kind"] = kind
    if fingerprint:
        d["fingerprint"] = geo.course_fingerprint(defn, "c", DEPTH)
    with open(job.chart_path("huron", "c"), "w") as fh:
        json.dump(d, fh)


write_chart(DEF)
out2 = job.run(REQ, now=NOW, is_online=lambda: False)
check("with a matching chart aboard: used, no warning", out2["chart"]["land"] is True
      and job.NO_CHART not in out2["warnings"] and job.OLD_CHART not in out2["warnings"])

print("3b) a chart built for an older version of the course is refused")


def lo(defn):
    """load_obstacles for the boat at 44.2N 82.2W, next mark the gate."""
    d_, c_, _n = job.remaining_definition(defn, "c", 2, 44.2, -82.2)
    return job.load_obstacles(d_, c_, defn, "c", job._get_boat())

def _lon_at(pts, lat):
    """Where the route crosses `lat` (first crossing)."""
    for a, b in zip(pts, pts[1:]):
        if (a["lat"] - lat) * (b["lat"] - lat) <= 0 and a["lat"] != b["lat"]:
            return a["lon"] + (b["lon"] - a["lon"]) * (lat - a["lat"]) / (b["lat"] - a["lat"])
    return -82.2


ZLON = _lon_at(out["points"], 44.55)                # a zone right on the plain route
ZB = (44.65, 44.45, ZLON - 0.15, ZLON + 0.15)       # n, s, w, e
ZONE = {"name": "No-go", "type": "exclusion",
        "geometry": {"north": ZB[0], "south": ZB[1], "west": ZB[2], "east": ZB[3]}}


def in_zone(pts):
    """Any point, or any point along a leg between them, inside ZONE."""
    for a, b in zip(pts, pts[1:]):
        for k in range(21):
            la, lo = a["lat"] + (b["lat"] - a["lat"]) * k / 20, a["lon"] + (b["lon"] - a["lon"]) * k / 20
            if ZB[1] < la < ZB[0] and ZB[2] < lo < ZB[3]:
                return True
    return False


check("(the plain route does cross that water)", in_zone(out["points"]))
DEF2 = dict(DEF, zones=[ZONE])                      # re-deployed with a new exclusion zone
field, note = lo(DEF2)
check("out-of-date chart → not used, said plainly", note == "Chart is out of date. Route doesn't check the shore.")
check("…and the new zone is still avoided", field.blocked(44.55, ZLON) and not field.blocked(44.2, -82.6))
out3 = job.run(dict(REQ, definition=DEF2), now=NOW, is_online=lambda: False)
check("the run says so", out3["warnings"][0] == job.OLD_CHART and out3["chart"]["land"] is False)
check("the route keeps out of the zone", out3["complete"] and not in_zone(out3["points"]))
good = json.load(open(job.chart_path("huron", "c")))
for bad_mask in ("not base64 !!", "aGVsbG8gd29ybGQ="):              # bad base64; not zlib data
    with open(job.chart_path("huron", "c"), "w") as fh:
        json.dump(dict(good, mask_zb64=bad_mask), fh)
    try:
        fb_field, fb_note = lo(DEF)
        check(f"corrupt mask ({bad_mask[:8]}…) → the fallback, not a crash",
              fb_note == job.OLD_CHART and fb_field is not None)
    except Exception as exc:  # noqa: BLE001
        check(f"corrupt mask ({bad_mask[:8]}…) → the fallback, not a crash ({type(exc).__name__})", False)
try:
    geo.field_from_dict(dict(good, mask_zb64="aGVsbG8gd29ybGQ="))
    check("field_from_dict: corrupt mask → ValueError", False)
except ValueError:
    check("field_from_dict: corrupt mask → ValueError", True)
good_ch = json.load(open(job.chart_path("huron", "c")))
empty = geo.ObstacleField(tuple(good_ch["bbox"]), res_deg=good_ch["res_deg"])
with open(job.chart_path("huron", "c"), "w") as fh:
    json.dump(dict(good_ch, mask_zb64=geo.field_to_dict(empty)["mask_zb64"]), fh)
field_e, note_e = lo(DEF)
check("a chart with an empty land mask → refused, said plainly",
      note_e == "Chart has no land in it. Route doesn't check the shore.")
with open(job.chart_path("huron", "c"), "w") as fh:
    json.dump(dict(good_ch, mask_zb64=geo.field_to_dict(empty)["mask_zb64"], open_water=True), fh)
check("…unless it was built as open water on purpose", lo(DEF)[1] is None)

print("3b1) a boat in a shoreline cell gets the Lab's start pocket, nothing more")
# the shoreline raster has the boat (44.2N 82.2W) just "on land": 0.2 nm inside the edge
LAND_NEAR = (44.2035, 44.10, -82.35, -82.05)
f_near = geo.build_for_course(DEF, "c", (46.0, 42.5, -83.5, -81.5), coastline_on=False,
                              use_cache=False, zones_on=False, islands_on=False)
n_, s__, w_, e_ = LAND_NEAR
f_near._fill_polygon([[(w_, s__), (e_, s__), (e_, n_), (w_, n_)]], 1, "coastline")
f_near.active = True
with open(job.chart_path("huron", "c"), "w") as fh:
    json.dump(dict(geo.field_to_dict(f_near), kind="land",
                   fingerprint=geo.course_fingerprint(DEF, "c", DEPTH)), fh)
fN, nN = lo(DEF)
check("chart used; the boat's own cell is open", nN is None and not fN.blocked(44.2, -82.2))
check("…land beyond the pocket stays", fN.blocked(44.17, -82.2) and fN.blocked(44.2, -82.1))
check("(without the pocket the boat's cell is land)", f_near.blocked(44.2, -82.2))
outN = job.run(REQ, now=NOW, is_online=lambda: False)
check("…and the route leaves from the boat", outN["available"] and outN["complete"])
write_chart(DEF)

print("3b1b) a zone right next to the boat is never carved; a boat inside one doesn't route")
NEAR = {"race_id": "near", "courses": [{"id": "c", "start": {"lat": 43.5, "lon": -82.5}, "marks": [],
        "finish": {"points": [{"lat": 45.0, "lon": -81.5}]}}],
        "zones": [{"name": "Tiny no-go", "type": "exclusion", "geometry": {
            "north": 44.005, "south": 43.995, "west": -81.998, "east": -81.990}}]}
dZ, cZ, _n = job.remaining_definition(NEAR, "c", 1, 44.0, -82.0)       # Codex repro
fZ, nZ = job.load_obstacles(dZ, cZ, None, None, job._get_boat())
check("fallback: the zone 0.15 nm from the boat stays blocked", fZ.blocked(44.001, -81.994))
landZ = geo.build_for_course(NEAR, "c", (45.6, 42.9, -83.1, -80.9), coastline_on=False,
                             use_cache=False, zones_on=False, islands_on=False)
landZ._fill_polygon([[(-83.0, 43.0), (-82.9, 43.0), (-82.9, 43.1), (-83.0, 43.1)]], 1, "coastline")
landZ.active = True
with open(job.chart_path("near", "c"), "w") as fh:
    json.dump(dict(geo.field_to_dict(landZ), kind="land",
                   fingerprint=geo.course_fingerprint(NEAR, "c", DEPTH)), fh)
fZ2, nZ2 = job.load_obstacles(dZ, cZ, NEAR, "c", job._get_boat())
check("chart path: same", nZ2 is None and fZ2.blocked(44.001, -81.994))
check("(the Lab's own build still opens its start pocket: unchanged)", not geo.build_for_course(
    dict(NEAR, courses=[dict(NEAR["courses"][0], start={"lat": 44.0, "lon": -82.0})]), "c",
    (45.6, 42.9, -83.1, -80.9), coastline_on=False, use_cache=False).blocked(44.001, -81.994))
REQ_Z = {"device": "iPad 1", "position": {"lat": 44.0, "lon": -81.994}, "next_index": 1,
         "definition": NEAR, "course_id": "c", "models": ["gfs"]}
try:
    job.run(REQ_Z, now=NOW, is_online=lambda: False)
    check("boat inside the zone → not routed", False)
except job.RunError as e:
    check("boat inside the zone → not routed, said plainly",
          str(e) == "Boat is inside an exclusion zone. Re-route not run.")
try:
    job.run(dict(REQ, position={"lat": 44.31, "lon": -82.6}), now=NOW, is_online=lambda: False)
    check("boat inside an island's disk → not routed", False)
except job.RunError as e:
    check("boat inside an island's disk → not routed, said plainly",
          str(e) == "Boat is inside an island's keep-out. Re-route not run.")

print("3b2) prep_chart refuses a mask with no land")
import io  # noqa: E402
import contextlib  # noqa: E402

import prep_chart  # noqa: E402
prep_chart.store.get_race = lambda rid: DEF
_bfc = prep_chart.geo.build_for_course
prep_chart.geo.build_for_course = lambda *a, **k: geo.ObstacleField((46.0, 42.5, -83.5, -81.5))
buf, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
    rc = prep_chart.main(["x", "huron", "c"])
check("shoreline didn't load → exit 1, nothing written", rc == 1 and buf.getvalue() == ""
      and "shoreline didn't load" in err.getvalue())
buf = io.StringIO()
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
    rc = prep_chart.main(["x", "huron", "c", "--open-water"])
check("--open-water → written, flagged", rc == 0 and json.loads(buf.getvalue())["open_water"] is True)
prep_chart.geo.build_for_course = _bfc
write_chart(DEF, fingerprint=False)
check("a chart with no fingerprint → out of date",
      lo(DEF)[1] == job.OLD_CHART)
write_chart(DEF)
moved = json.loads(json.dumps(DEF))
moved["courses"][0]["marks"][2]["lat"] = 44.95
check("a moved mark → out of date", lo(moved)[1]
      == job.OLD_CHART)

print("3c) only the remaining course's barriers block water")
# the boat past Rock (rounded to starboard on leg 1), sailing the gate leg
BAR = json.loads(json.dumps(DEF))
whole = geo.build_for_course(BAR, "c", (46.0, 42.5, -83.5, -81.5), coastline_on=False,
                             use_cache=False)
d4, c4, _n = job.remaining_definition(BAR, "c", 3, 45.0, -82.5)
check("(the whole race has rounding barriers)", whole.layers["rounding_barrier"] > 0)
write_chart(BAR)
field, note = job.load_obstacles(d4, c4, BAR, "c", job._get_boat())
check("the chart is used, its land kept", note is None and field.blocked(43.95, -82.95))
rock = [(la, lo) for la in np.arange(43.5, 45.0, 0.01) for lo in np.arange(-83.4, -81.6, 0.01)
        if whole.blocked(la, lo) and not geo.build_for_course(
            BAR, "c", (46.0, 42.5, -83.5, -81.5), coastline_on=False, use_cache=False,
            islands_on=False).blocked(la, lo) and abs(la - 44.3) + abs(lo + 82.6) > 0.1][:50]
check("(found water blocked only by Rock's barrier)", len(rock) > 0)
check("chart path: a rounded island's barrier no longer blocks", not any(
    field.blocked(la, lo) for la, lo in rock))
fb, note = job.load_obstacles(d4, c4, None, None, job._get_boat())
check("fallback path: the same", note == job.NO_CHART and not any(fb.blocked(la, lo) for la, lo in rock))
d5, c5, _n = job.remaining_definition(BAR, "c", 1, 43.2, -82.4)
field5, _ = job.load_obstacles(d5, c5, BAR, "c", job._get_boat())
check("before rounding Rock its barrier still applies", field5.layers["rounding_barrier"] > 0
      and any(field5.blocked(la, lo) for la, lo in rock))
write_chart(BAR, full=True, kind=None)
check("an old full-race chart (no 'land' kind) → out of date",
      job.load_obstacles(d4, c4, BAR, "c", job._get_boat())[1] == job.OLD_CHART)
write_chart(DEF)

print("3d) the boat outside the chart's box")
far = dict(REQ, position={"lat": 44.2, "lon": -80.6, "t": NOW})      # east of the mask (-81.5)
DEF3 = json.loads(json.dumps(DEF))
DEF3["zones"] = [{"name": "Far no-go", "type": "exclusion", "geometry": {
    "north": 44.3, "south": 44.1, "west": -80.9, "east": -80.8}}]
write_chart(DEF3)
d3, c3, _n = job.remaining_definition(DEF3, "c", 2, 44.2, -80.6)
field, note = job.load_obstacles(d3, c3, DEF3, "c", job._get_boat())
check("chart doesn't cover the boat → not used, said plainly",
      note == "Chart doesn't cover the boat. Route doesn't check the shore.")
check("the fallback covers the boat's area (zone there blocked)", field.blocked(44.2, -80.85))
need3 = job.optimizer.course_bbox(d3, c3)              # weather cached for that box too
for f in windfield._fhrs_for_cycle(GFS, CYCLE, NOW, NOW + 30 * 3600):
    write_grib(GFS._cache_path(CYCLE, f, "det", need3), 4.2, 4.2)
W.remember_box(job.STATE_DIR, need3)
out5 = job.run(dict(far, definition=DEF3), now=NOW, is_online=lambda: False)
check("the run doesn't claim land", out5["chart"]["land"] is False and out5["warnings"][0] == job.NO_COVER)
write_chart(DEF)

print("3e) online, but the downloads fail: the forecasts aboard are used")
_cache0 = models.CACHE
models.CACHE = os.path.join(_tmp, "gribcache-fail")      # a cache with only yesterday's run
OLD2 = dt.datetime(2026, 10, 31, 12, tzinfo=dt.timezone.utc)   # 29 h old, past the 2-cycle step-back
for f in windfield._fhrs_for_cycle(GFS, OLD2, NOW, NOW + 30 * 3600):
    write_grib(GFS._cache_path(OLD2, f, "det", plume_box), 4.2, 4.2)
TRIED = []


class DownGFS(models.GFS):
    """The probe passed, but every download fails."""

    def fetch(self, cycle, fhr, member, bbox, timeout=60):
        TRIED.append(cycle)
        return None


models.MODELS["gfs"] = DownGFS()
job.STATE_DIR = os.path.join(_tmp, "fresh-fail")
out6 = job.run(REQ, now=NOW, is_online=lambda: True)
models.MODELS["gfs"], models.CACHE = GFS, _cache0
w6 = out6.get("weather") or {}
check("downloads were tried", len(TRIED) > 0)
check("a route anyway, from the saved run", out6["available"] and out6["complete"])
check("its age said honestly (29 h)", [m["cycle"] for m in w6["models"]] == ["2026-10-31 12Z"]
      and w6["newest_age_h"] == 29.0)
check("online, no downloads, cache used for gfs", w6["online"] is True and w6["downloads"] == 0
      and w6["from_cache_after_failed_download"] == ["gfs"])

print("3e2) gfs fails, icon loads: both kept, one route")
from app.wind import grib as G  # noqa: E402


class FakeICON(models.ICON):
    """Open-Meteo ICON that answers (online): a steady 6 m/s south-westerly series."""

    def load_series(self, bbox, t_start, t_end, on_progress=None):
        lat, lon = np.arange(42.0, 47.01, 0.25), np.arange(-86.0, -79.99, 0.25)
        u = np.full((lat.size, lon.size), 4.0)
        fr = [G.GribFrame("icon", "det", float(t), lat, lon, u, u.copy(), True)
              for t in range(int(t_start) - 3600, int(t_end) + 7200, 3 * 3600)]
        return {("icon", "det"): fr}, {"model": "icon", "cycle": "open-meteo icon_global",
                                       "members": 1, "frames": len(fr), "expected_frames": len(fr),
                                       "cycle_fallbacks": 0, "priority": 1.15,
                                       "kind": "deterministic"}


print("3e1b) a stalled ICON gets its own timeout, not the budget")


class SlowICON(models.ICON):
    def load_series(self, bbox, t_start, t_end, on_progress=None):
        time.sleep(5)
        return {}, {}


t0 = time.time()
check("ICON/GEM get their own per-load timeout, 60 s by default", W.Budget(True).api_s == 60.0)
b = W.Budget(True, budget_s=180, request_s=20, api_s=0.5)
ser, meta = W.ApiSource(SlowICON(), b).load_series(BOX, NOW, NOW + 3600)
check("gave up after ~the API timeout", time.time() - t0 < 2.5 and meta["skipped"] == "timed out")
check("…and the budget is still there for the rest", not b.exhausted and b.left() > 170)

_icon0 = models.MODELS["icon"]
models.MODELS["gfs"], models.MODELS["icon"] = DownGFS(), FakeICON()
models.CACHE = os.path.join(_tmp, "gribcache-fail")
out7 = job.run(dict(REQ, models=["gfs", "icon"]), now=NOW, is_online=lambda: True)
models.MODELS["gfs"], models.MODELS["icon"], models.CACHE = GFS, _icon0, _cache0
w7 = out7["weather"]
check("both models in the route", sorted(m["model"] for m in w7["models"]) == ["gfs", "icon"]
      and out7["complete"])
check("gfs from its saved run, icon kept as loaded", w7["from_cache_after_failed_download"] == ["gfs"]
      and [m for m in w7["models"] if m["model"] == "icon"][0]["frames"] > 0)

print("3e3) a forecast that ends before the finish says so")
models.CACHE = os.path.join(_tmp, "gribcache-short")
for f in windfield._fhrs_for_cycle(GFS, CYCLE, NOW, NOW + 3 * 3600):
    write_grib(GFS._cache_path(CYCLE, f, "det", plume_box), 4.2, 4.2)
job.STATE_DIR = os.path.join(_tmp, "fresh-short")
out8 = job.run(REQ, now=NOW, is_online=lambda: False)
models.CACHE = _cache0
w8 = out8["weather"]
check("forecast_until reported", w8["forecast_until"] and w8["forecast_until"] <= NOW + 9 * 3600)
check("…and the warning, when the finish is later", out8["finish_eta"] > w8["forecast_until"] + 3600
      and job.SHORT_FORECAST in out8["warnings"])
check("a model per frame count, marked partial", w8["models"][0]["partial"] is True)

print("3e4) a requested model with nothing aboard is named")
job.STATE_DIR = os.path.join(_tmp, "fresh")
out9 = job.run(dict(REQ, models=["gfs", "nam"]), now=NOW, is_online=lambda: False)
check("missing: nam, said in a warning", out9["weather"]["missing"] == ["nam"]
      and "No forecast from nam." in out9["warnings"])

print("3e5) no boat profile is said, not hidden")
_bid = job.BOAT_ID
job.BOAT_ID = "no-such-boat"
out10 = job.run(REQ, now=NOW, is_online=lambda: False)
job.BOAT_ID = _bid
check("warning + no boat block", out10["boat"] is None and job.NO_BOAT in out10["warnings"])
check("with the profile: which one it used", out["boat"]["boat_id"] == "sr33")

print("3f) an incomplete route makes no finish claim")
_orig = job.routerun.run_course


def stalled(*a, **k):
    r = _orig(*a, **k)
    r.update(complete=False, finish_epoch=NOW + 3600, total_hours=1.0)
    return r


job.routerun.run_course = stalled
out4 = job.run(REQ, now=NOW, is_online=lambda: False)
job.routerun.run_course = _orig
check("no finish time, no total", out4["finish_eta"] is None and out4["total_hours"] is None
      and out4["complete"] is False)
check("no gain vs the plan", out4["vs_plan"] == {"available": False, "note": "Route incomplete."})
check("said plainly, first", out4["warnings"][0] == "Route incomplete.")
try:
    job.run(dict(REQ, position={}), now=NOW, is_online=lambda: False)
    check("no position → refused", False)
except job.RunError as e:
    check("no position → refused, short", str(e) == "No boat position yet.")

# ---- 4) the service: one run at a time ------------------------------------------------------------
print("4) the service: job lifecycle")


class FakeProc:
    def __init__(self, job_dir):
        self.dir, self.rc, self.killed = job_dir, None, False

    def poll(self):
        return self.rc

    def finish(self, rc, result=None, error=None):
        if result is not None:
            json.dump(result, open(os.path.join(self.dir, "result.json"), "w"))
        if error is not None:
            json.dump(error, open(os.path.join(self.dir, "error.json"), "w"))
        self.rc = rc

    def terminate(self):
        self.killed, self.rc = True, -15

    kill = terminate

    def wait(self, timeout=None):
        return self.rc


PROCS = []
CLOCK = [NOW]


def runner(job_dir):
    PROCS.append(FakeProc(job_dir))
    with open(os.path.join(job_dir, "progress.jsonl"), "w") as fh:
        fh.write(json.dumps({"phase": "weather"}) + "\n" + json.dumps({"phase": "routing"}) + "\n")
    return PROCS[-1]


jobs = service.Jobs(state_dir=os.path.join(_tmp, "svc"), runner=runner, timeout_s=600,
                    clock=lambda: CLOCK[0])
check("idle at first", jobs.status()["state"] == "idle" and jobs.result() is None)
code, body = jobs.start({"device": "iPad 1", "position": {"lat": 1, "lon": 2}})
check("run → 202 + job id", code == 202 and body["ok"] and body["job_id"])
jid = body["job_id"]
CLOCK[0] += 30
code, busy = jobs.start({"device": "iPad 2"})
check("second run while one is going → 409 busy", code == 409 and busy["busy"]
      and busy["detail"] == "A re-route is already running." and busy["job_id"] == jid)
check("…with progress", busy["progress"]["step"] == "Finding the fastest route"
      and busy["progress"]["elapsed_s"] == 30 and 0 < busy["progress"]["pct"] <= 95)
st = jobs.status()
check("status: running, who asked", st["state"] == "running" and st["asked_by"] == "iPad 1")
PROCS[-1].finish(0, result={"schema": "c4.reroute/v1", "job_id": jid, "run_s": 42.0,
                            "available": True})
st = jobs.status()
check("done → state done, last result kept", st["state"] == "done" and st["last"]["run_s"] == 42.0
      and jobs.result()["job_id"] == jid)
check("the next run's expected time = the last run's", jobs.start({})[0] == 202
      and jobs._progress()["expected_s"] == 42)
PROCS[-1].finish(1, error={"detail": "Re-route failed. Try again.", "error": "KeyError"})
st = jobs.status()
check("error → state error, short reason; last result untouched", st["state"] == "error"
      and st["last"]["detail"] == "Re-route failed. Try again." and jobs.result()["job_id"] == jid)
check("status still names the job behind /result", st["result_job_id"] == jid
      and st["last"]["job_id"] != jid)
jobs.start({})
CLOCK[0] += 601
st = jobs.status()
check("too long → killed, said plainly", PROCS[-1].killed and st["state"] == "error"
      and st["last"]["detail"] == "Re-route took too long. Try again.")
check("it survives a restart (last result on disk)",
      service.Jobs(state_dir=os.path.join(_tmp, "svc"), runner=runner).status()["last"]["state"] == "error")

print("5) the HTTP API")
srv = service.ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(jobs, allow_from={"127.0.0.1"}))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"


def call(method, path, body=None, base=base):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


check("bad request → 400", call("POST", "/run", {"device": "x"})[0] == 400)
check("no position → 400, plain", call("POST", "/run", {"marks": [1]})[1]["detail"] == "No boat position yet.")
code, body = call("POST", "/run", {"device": "iPad 1", "position": {"lat": 44, "lon": -82},
                                   "marks": [{"name": "A", "lat": 44, "lon": -82}]})
check("POST /run → 202", code == 202 and body["ok"])
code, body = call("POST", "/run", {"position": {"lat": 44, "lon": -82}, "marks": [{}]})
check("POST /run while running → 409 busy", code == 409 and body["busy"])
check("GET /status → running", call("GET", "/status")[1]["state"] == "running")
PROCS[-1].finish(0, result={"schema": "c4.reroute/v1", "job_id": "x", "run_s": 1})
check("GET /result → the route", call("GET", "/result")[1]["job_id"] == "x")
srv2 = service.ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(jobs, allow_from={"10.10.10.1"}))
threading.Thread(target=srv2.serve_forever, daemon=True).start()
check("a caller outside the allow list → 403",
      call("GET", "/status", base=f"http://127.0.0.1:{srv2.server_address[1]}")[0] == 403)

print("6) a real job.py child: a run that can't start says why")
real = service.Jobs(state_dir=os.path.join(_tmp, "real"))
code, body = real.start({"device": "iPad 1", "position": {"lat": 44.2, "lon": -82.2},
                         "definition": {"courses": [{"id": "c", "start": {"lat": 44.0, "lon": -82.0},
                                                    "marks": []}]}, "next_index": 1})
deadline = time.time() + 120
while real.status()["state"] == "running" and time.time() < deadline:
    time.sleep(0.3)
st = real.status()
check("child exits, reason passed on", st["state"] == "error" and st["last"]["detail"] == "No marks left to route to.")

print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
