"""#378 — the Gameplan's Study batch (app/study.py).

Pinned, on a synthetic two-model wind field (no network):
  * the axes are cleaned (baseline values dropped, limits held, text or lists);
  * the plan varies one thing at a time: 1 + the values on each axis, never a cross product;
  * a plan over the cap is refused BEFORE any download;
  * the weather is loaded ONCE for every run, over the widest window the start offsets need;
  * each model routed alone carries its model, cycle and blend weight;
  * a slower boat (Boat 90%) takes longer; a wind shift moves the route;
  * the results: time vs the baseline, ties inside the threshold, the spread, the sides, and a
    route fan for the map on the baseline;
  * `run_course(windfield=...)` with the field it would have built routes the SAME as before.
"""
import os
import sys
import tempfile

import numpy as np

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["GRIB_CACHE"] = os.path.join(_tmp, "gribcache")
os.environ.pop("ANTHROPIC_API_KEY", None)

from app import boats, current, wave, venue, routerun, study  # noqa: E402
from app import wind as WIND  # noqa: E402
from app.wind import grib, windfield  # noqa: E402
import polar_fixture  # noqa: E402
polar_fixture.install()

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1_793_534_400
COURSE = {"race_id": "t", "name": "Test race", "courses": [{
    "id": "x", "start": {"lat": 43.9, "lon": -82.4},
    "marks": [{"name": "Gate", "type": "buoy", "lat": 44.6, "lon": -82.0, "rounding": "port"}],
    "finish": {"points": [{"lat": 45.1, "lon": -82.9}]}}]}
BOAT = {"boat_id": "testboat", "name": "Test boat", "draft_m": 2.0, "helm_factor": 1.0}


def frames(model, u0, v0):
    lat = np.arange(43.0, 46.01, 0.25)
    lon = np.arange(-84.0, -80.99, 0.25)
    out = []
    for k in range(-6, 52, 3):
        t = T0 + k * 3600
        u = np.full((lat.size, lon.size), u0 + 0.05 * k) + np.linspace(0, 1, lon.size)[None, :]
        v = np.full((lat.size, lon.size), v0 - 0.04 * k) + np.linspace(0, 1, lat.size)[:, None]
        out.append(grib.GribFrame(model, "det", float(t), lat, lon, u, v, True))
    return out


BUILDS = []


def fake_build_windfield(bbox, t_start, t_end, models=(), ensemble_members=0, on_progress=None,
                         model_weights=None, model_bias=None, fallback_tries=None, **_kw):
    BUILDS.append((t_start, t_end))
    series = {("gfs", "det"): frames("gfs", -3.0, -5.0), ("rrfs", "det"): frames("rrfs", -1.0, -6.5)}
    meta = [{"model": m, "cycle": c, "members": 1, "frames": 20, "expected_frames": 20,
             "cycle_fallbacks": 0, "priority": 1.0, "kind": "deterministic"}
            for m, c in (("gfs", "2026-11-01 12Z"), ("rrfs", "2026-11-01 15Z"))]
    return windfield.WindField(series, meta, bbox, t_start, t_end, model_weights=model_weights,
                               model_bias=model_bias)


WIND.build_windfield = fake_build_windfield
current.build_currentfield = lambda *a, **k: current.ZeroCurrent()
wave.build_wavefield = lambda *a, **k: wave.ZeroWave()
boats.active_boat = lambda: BOAT
venue.resolve = lambda *a, **k: None

print("axes")
ax = study.parse_axes({"models": 1, "start_min": "-30, 0, 30 30 abc", "polar_pct": [90, 100, 300],
                       "tws_pct": "120", "twd_deg": [-10, 10, 0, 200]})
check("start offsets: baseline 0 dropped, duplicates and junk gone", ax["start_min"] == [-30, 30])
check("boat %: 100 is the baseline, 300 is out of range", ax["polar_pct"] == [90])
check("wind direction: 0 dropped, 200 out of range", ax["twd_deg"] == [-10, 10])
check("ensemble off unless asked", ax["members"] is False and ax["models"] is True)

print("plan")
check("one thing at a time: 1 + 2 models + 2 + 1 + 1 + 2",
      study.run_count(ax, 2, 0) == 9)
check("one model: no 'alone' runs (it IS the baseline)", study.run_count(ax, 1, 0) == 7)
p = study.plan(ax, ["gfs", "rrfs"])
check("baseline first", p[0]["id"] == "base")
check("labels are plain", [r["label"] for r in p][1:] == [
    "GFS alone", "RRFS alone", "Start -30 min", "Start +30 min", "Boat 90%", "Wind 120%",
    "Wind 10° left", "Wind 10° right"])

print("cap")
BUILDS.clear()
r = study.run(COURSE, "x", T0, ["gfs", "rrfs"], ax, cap=5, boat=BOAT, avoid=False)
check("over the cap: refused, says how many", not r["available"] and r["planned"] == 9
      and "limit of 5" in r["note"])
check("…before any download", BUILDS == [])
big = study.parse_axes({"start_min": list(range(-600, 601, 30))})      # 40 offsets + baseline
r = study.run(COURSE, "x", T0, ["gfs"], big, cap=500, boat=BOAT, avoid=False)
check("the cap itself is held to the hard ceiling", not r["available"]
      and r["cap"] == study.MAX_RUNS and r["planned"] == 41 and BUILDS == [])
BUILDS.clear()

print("views")
wf = fake_build_windfield(None, T0, T0 + 40 * 3600)
wf.bbox = (45.6, 43.4, -83.6, -81.4)
base = wf.detail_at(44.0, -82.0, T0 + 3600)
n = study.Nudged(wf, tws_scale=1.2, twd_shift=-10)
d = n.detail_at(44.0, -82.0, T0 + 3600)
check("wind 120%", abs(d["tws"] - base["tws"] * 1.2) < 0.02)
check("wind shifted left 10°", abs(((d["twd"] - base["twd"] + 540) % 360) - 180 + 10) < 0.11)
check("wind_at agrees", n.wind_at(44.0, -82.0, T0 + 3600) == (d["tws"], d["twd"]))
g = n.sample_grid(T0 + 3600, 0.5)
check("the map grid is nudged too", g and abs(g[0]["tws"] - wf.sample_grid(T0 + 3600, 0.5)[0]["tws"] * 1.2) < 0.03)
check("status names the nudge", n.status()["study"] == {"tws_scale": 1.2, "twd_shift": -10.0})
s = study.subset(wf, lambda k: k[0] == "gfs")
check("a model alone: only its series and meta", list(s.series) == [("gfs", "det")]
      and [m["model"] for m in s.meta] == ["gfs"] and len(wf.series) == 2)
pm, ps = study.blend_weights(wf)
check("blend weights add to 100 %", abs(sum(pm.values()) - 100) < 0.2 and pm["gfs"] == ps[("gfs", "det")])

print("run_course with a prebuilt field routes the same")
a = routerun.run_course(COURSE, "x", T0, ["gfs", "rrfs"], avoid=False, boat=BOAT, model_skill=False,
                        resolution="fast", briefing=False, grids=False, use_waves=False,
                        use_current=False)
built = BUILDS[-1]
wf2 = fake_build_windfield(a["windfield"]["bbox"], built[0], built[1])
b = routerun.run_course(COURSE, "x", T0, ["gfs", "rrfs"], avoid=False, boat=BOAT, model_skill=False,
                        resolution="fast", briefing=False, grids=False, use_waves=False,
                        use_current=False, windfield=wf2, currents=current.ZeroCurrent("off"),
                        seastate=wave.ZeroWave("off"))
check("same path and time", a["path"] == b["path"] and a["total_minutes"] == b["total_minutes"])
c = routerun.run_course(COURSE, "x", T0, ["gfs", "rrfs"], avoid=False, boat=BOAT, model_skill=False,
                        resolution="fast", briefing=False, grids=False, use_waves=False,
                        use_current=False, windfield=wf2, currents=current.ZeroCurrent("off"),
                        seastate=wave.ZeroWave("off"), speed_scale=0.9)
check("speed_scale 0.9: slower", c["total_minutes"] > a["total_minutes"])

print("the batch")
BUILDS.clear()
prog = []
ax2 = study.parse_axes({"models": True, "start_min": [60], "polar_pct": [90],
                        "twd_deg": [-20, 20], "tws_pct": [130]})
r = study.run(COURSE, "x", T0, ["gfs", "rrfs"], ax2, boat=BOAT, avoid=False, resolution="fast",
              use_waves=False, use_current=False, on_progress=prog.append, model_skill=False)
check("available, 8 rows", r["available"] and len(r["rows"]) == 8)
check("weather loaded ONCE", len(BUILDS) == 1)
check("…over the window the +60 min start needs", BUILDS[0][0] == T0
      and BUILDS[0][1] >= T0 + 3600 + 3600)
rows = {x["id"]: x for x in r["rows"]}
check("every run finished", all(x["ok"] for x in r["rows"]), )
g = rows["model:gfs"]
check("a model alone shows model, cycle and weight", g["model"] == "gfs"
      and g["cycle"] == "2026-11-01 12Z" and g["weight_pct"] == 50.0)
check("Boat 90%: longer on the course", rows["polar_pct:90"]["delta_min"] > 0)
check("a wind shift moves the route", rows["twd_deg:20"]["path"] != rows["base"]["path"])
check("baseline: 0 min, a tie with itself", rows["base"]["delta_min"] == 0 and rows["base"]["tie"])
st = rows["start_min:60"]
check("start +60: compared on time on the course, not clock",
      st["delta_min"] == round(st["minutes"] - rows["base"]["minutes"], 1)
      and st["finish_epoch"] - rows["base"]["finish_epoch"] != st["delta_min"] * 60)
check("sides counted", sum(r["sides"].values()) == r["finished"] == 8)
check("spread over same-start runs", r["spread_min"] is not None and r["spread_min"] >= 0)
check("tie threshold and its caveat ride along", r["tie_min"] == study.TIE_MIN
      and "Rough" in r["tie_note"] and "Agreement" in r["agreement_note"])
fanned = r["baseline"]["candidate_paths"]
check("baseline carries the fan: every run but itself", len(fanned) == 7
      and all(f["path"] and f["label"] for f in fanned))
check("baseline has the map's wind grid; the others don't carry one",
      "wind_grid" in r["baseline"] and "path" in rows["base"])
check("progress says which run", any("Study run 3 of 8" in m for m in prog))

print("ties and spread")
fake = [{"id": "base", "ok": True, "finish_epoch": 1000 * 60, "minutes": 1000, "side": "left"},
        {"id": "a", "ok": True, "finish_epoch": 1004 * 60, "minutes": 1004, "side": "left"},
        {"id": "b", "ok": True, "finish_epoch": 1030 * 60, "minutes": 1030, "side": "right"},
        {"id": "c", "ok": False, "note": "No full route."}]
sm = study.summarize(fake, tie_min=5)
check("4 min off: a tie; 30 min: not", fake[1]["tie"] and not fake[2]["tie"])
check("spread 30 min, 1 failed, sides 2/1", sm["spread_min"] == 30 and sm["failed"] == 1
      and sm["sides"] == {"left": 2, "right": 1})

print("the endpoint: /api/study runs in the optimize job slot")
import asyncio  # noqa: E402
import time  # noqa: E402
from app import main, jobs, store  # noqa: E402
store.get_race = lambda rid: COURSE if rid == "t" else None
check("unknown race: 404", asyncio.run(main.study_start({"race_id": "nope"})).status_code == 404)
st = asyncio.run(main.study_start({"race_id": "t", "course_id": "x", "start_epoch": T0,
                                   "models": ["gfs", "rrfs"], "avoid_land": False,
                                   "resolution": "fast", "use_waves": False, "use_current": False,
                                   "axes": {"twd_deg": "10"}, "cap": "junk"}))
check("started as a study in the optimize slot", st["ok"] and st["kind"] == "study")
busy = asyncio.run(main.optimize({"race_id": "t", "course_id": "x", "start_epoch": T0}))
check("Find route waits while a study runs", busy["ok"] is False)
for _ in range(240):
    js = jobs.status("optimize", job_id=st["job_id"])
    if js["state"] != "running":
        break
    time.sleep(0.5)
res = js.get("result") or {}
check("done: baseline + Wind 10° right, start and race named", js["state"] == "done"
      and [x["label"] for x in res.get("rows", [])] == ["Baseline", "Wind 10° right"]
      and res.get("race_id") == "t" and res.get("start", {}).get("epoch") == T0)

print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
