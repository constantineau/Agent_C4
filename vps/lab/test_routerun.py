"""#124 — the Lab's route run, factored out so the boat's Orin runs the SAME code.

`main._run_optimize` now calls `routerun.run_course` (shared with pi/orin/reroute). This pins that
the Lab's result did not change: a verbatim copy of the pre-#124 `_run_optimize` body (below,
`legacy_run`) and the new path must produce byte-identical results on the same inputs — a real
two-model WindField built from synthetic frames, the active boat's overlays (helm, jibs, Code 0,
an approved polar overlay), with and without current/sea state. Also pinned:
  * boats: the new per-boat helpers equal the old active_* reads;
  * windfield: `fallback_tries` (0 = no older-cycle retry) and duck-typed sources;
  * grib: `crop` copies (no view keeping the whole parsed field alive), same values;
  * obstacles: a built field survives `field_to_dict` → `field_from_dict` unchanged.
"""
import datetime
import json
import os
import sys
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

from shared import boat_profile  # noqa: E402
from app import main, boats, optimizer, current, wave, venue, modelskill  # noqa: E402
from app import wind as WIND  # noqa: E402
from app.wind import grib, windfield, models  # noqa: E402
from app.geo import obstacles as geo  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---- the inputs -------------------------------------------------------------------------------
T0 = 1_793_534_400
BBOX = (45.6, 43.4, -83.6, -81.4)
COURSE = {"race_id": "t", "name": "Test race", "courses": [{
    "id": "x", "start": {"lat": 43.9, "lon": -82.4},
    "marks": [{"name": "Gate", "type": "buoy", "lat": 44.6, "lon": -82.0, "rounding": "port"}],
    "finish": {"points": [{"lat": 45.1, "lon": -82.9}]}}]}
BOAT = {"boat_id": "sr33", "name": "SR33", "draft_m": 2.1336, "helm_factor": 0.95,
        "jib_crossovers": [{"sail": "J1", "tws_max": 14}, {"sail": "J2", "tws_max": 20}],
        "code0": {"enabled": True, "tws_max": 9, "twa_min": 55, "twa_max": 110},
        "main_reefs": {"r1_tws_kn": 20, "r1_a3_slot_tws_kn": 16},
        "polar_adjustments": [{"tws": 10, "twa": 90, "mult": 1.05}]}


def frames(model, u0, v0):
    lat = np.arange(43.0, 46.01, 0.25)
    lon = np.arange(-84.0, -80.99, 0.25)
    out = []
    for k in range(0, 40, 3):
        t = T0 + k * 3600
        u = np.full((lat.size, lon.size), u0 + 0.05 * k) + np.linspace(0, 1, lon.size)[None, :]
        v = np.full((lat.size, lon.size), v0 - 0.04 * k) + np.linspace(0, 1, lat.size)[:, None]
        out.append(grib.GribFrame(model, "det", float(t), lat, lon, u, v, True))
    return out


def fake_build_windfield(bbox, t_start, t_end, models=(), ensemble_members=0, on_progress=None,
                         model_weights=None, model_bias=None, fallback_tries=None):
    series = {("gfs", "det"): frames("gfs", -3.0, -5.0), ("nam", "det"): frames("nam", -2.0, -6.0)}
    meta = [{"model": m, "cycle": "2026-11-01 12Z", "members": 1, "frames": 14,
             "expected_frames": 14, "cycle_fallbacks": 0, "priority": 1.0, "kind": "deterministic"}
            for m in ("gfs", "nam")]
    if on_progress:
        on_progress("gfs: 14/14 frames @ 2026-11-01 12Z")
    return windfield.WindField(series, meta, bbox, t_start, t_end, model_weights=model_weights,
                               model_bias=model_bias)



WIND.build_windfield = fake_build_windfield
current.build_currentfield = lambda *a, **k: current.ZeroCurrent()
wave.build_wavefield = lambda *a, **k: wave.ZeroWave()
boats.active_boat = lambda: BOAT
SKILL = {"on": False}            # no network: the venue's model skill is stubbed (on or off)
venue.resolve = lambda *a, **k: {"key": "huron"} if SKILL["on"] else None
modelskill.venue_weights = lambda v, models=None, race_date=None: {
    "enabled": True, "station": "45003", "model_weights": {"gfs": 1.3, "nam": 0.7},
    "model_bias": {"nam": (0.8, 4.0)}}


# ---- the Lab's _run_optimize before #124, verbatim (the reference) ------------------------------
def legacy_run(definition, course_id, start_epoch, model_names, ensemble_members, avoid=True,
               per_model=False, resolution="auto", use_waves=True, use_current=True,
               on_progress=None):
    from app.wind import build_windfield
    bbox = optimizer.course_bbox(definition, course_id)
    if not bbox:
        return {"available": False, "note": "No mark positions yet. Fix them in Course & Marks."}
    hours = optimizer.estimate_hours(definition, course_id)
    t_end = start_epoch + hours * 3600

    class _Log(list):
        def append(self, msg):
            list.append(self, msg)
            if on_progress:
                try:
                    on_progress(msg)
                except Exception:
                    pass
    log = _Log()
    skill = {"enabled": False}
    mweights = mbias = None
    try:
        from app import venue as venue_mod, modelskill
        v = venue_mod.resolve(definition, course_id, bbox=bbox)
        race_date = datetime.datetime.fromtimestamp(start_epoch, datetime.timezone.utc).date()
        skill = (modelskill.venue_weights(v, models=model_names, race_date=race_date)
                 if v else {"enabled": False})
        if skill.get("enabled"):
            mweights, mbias = skill["model_weights"], skill["model_bias"]
            log.append(f"model-skill: {v['key']} via {skill['station']} → "
                       + ", ".join(f"{m}×{w}" for m, w in mweights.items()))
    except Exception as exc:
        log.append(f"model-skill: skipped ({exc})")
    wf = build_windfield(bbox, start_epoch, t_end, models=model_names,
                         ensemble_members=ensemble_members, on_progress=log.append,
                         model_weights=mweights, model_bias=mbias)
    if not wf.loaded:
        return {"available": False, "note": "No forecast could be loaded (not out yet, or no "
                "internet).", "windfield": wf.status(), "log": log}
    cur = (current.build_currentfield(bbox, start_epoch, t_end, on_progress=log.append)
           if use_current else current.ZeroCurrent())
    waves = (wave.build_wavefield(bbox, start_epoch, t_end, on_progress=log.append)
             if use_waves else wave.ZeroWave())
    cert_polar = (boats.active_boat() or {}).get("cert_polar") or None
    b = boats.active_boat() or {}
    result = optimizer.optimize_course(definition, course_id, start_epoch, wf, avoid=avoid,
                                       source=main.chart_source(), polar=cert_polar,
                                       safety_depth=boats.active_safety_depth_m(),
                                       jib_crossovers=b.get("jib_crossovers") or [],
                                       sail_config=boats.active_sail_config(), per_model=per_model,
                                       resolution=resolution, cur=cur,
                                       waves=waves, helm_factor=boats.active_helm_factor(),
                                       polar_adjustments=boats.active_polar_adjustments(),
                                       wave_coeffs=boats.active_wave_coeffs())
    result["current"] = cur.status()
    result["waves"] = waves.status()
    result["model_skill"] = skill
    result["briefing"] = optimizer.briefing(result, definition.get("name", ""))
    result["boat"] = boat_profile.summary(boats.active_boat()) if boats.active_boat() else None
    wg = main_wind_grid(wf, bbox, start_epoch, result.get("finish_epoch", t_end))
    result["wind_grid"] = wg
    result["log"] = log
    return result


def main_wind_grid(wf, bbox, start_epoch, finish_epoch, n_cells=16):
    """The pre-#124 main._wind_grid, verbatim."""
    n, s, w, e = bbox
    step = max(0.05, max(n - s, e - w) / n_cells)
    span = max(1.0, float(finish_epoch) - float(start_epoch))
    step_s = max(900.0, 1.0 * 3600.0)
    n_times = min(72, max(2, int(span / step_s) + 1))
    times = [round(start_epoch + min(span, i * step_s)) for i in range(n_times)]
    if times[-1] < round(start_epoch + span):
        times.append(round(start_epoch + span))
    frames_ = [wf.sample_grid(t, step, bbox) for t in times]
    return {"step_deg": round(step, 3), "step_h": 1.0, "bbox": [n, s, w, e],
            "times": times, "frames": frames_}


def dump(r):
    return json.dumps(r, sort_keys=True, default=str)


print("1) the Lab's route is unchanged by the factoring")
for label, kw in (("no current/sea state", dict(use_waves=False, use_current=False)),
                  ("current + sea state on (both empty)", dict(use_waves=True, use_current=True)),
                  ("fast resolution", dict(resolution="fast", use_waves=False, use_current=False)),
                  ("venue model skill on", dict(use_waves=False, use_current=False, skill=True))):
    SKILL["on"] = kw.pop("skill", False)
    seen_a, seen_b = [], []
    a = legacy_run(COURSE, "x", T0, ["gfs", "nam"], 0, avoid=False, on_progress=seen_a.append, **kw)
    b = main._run_optimize(COURSE, "x", T0, ["gfs", "nam"], 0, avoid=False,
                           on_progress=seen_b.append, **kw)
    check(f"{label}: identical result", a.get("available") and dump(a) == dump(b))
    check(f"{label}: identical progress lines", seen_a == seen_b)
SKILL["on"] = False
check("the skill weights reached the wind field", "model-skill: huron via 45003" in " ".join(seen_b))
check("the boat overlays reached the optimizer (helm 0.95 in the result)",
      (b.get("realized") or {}).get("helm_factor") == 0.95)
check("no mark positions → the same note",
      main._run_optimize({"courses": []}, None, T0, ["gfs"], 0) ==
      legacy_run({"courses": []}, None, T0, ["gfs"], 0))

print("2) per-boat helpers = the old active_* reads")
check("safety depth", boats.safety_depth_m(BOAT) == boat_profile.safety_depth_m(BOAT)
      == boats.active_safety_depth_m())
check("helm factor (and its band)", boats.helm_factor(BOAT) == 0.95
      and boats.helm_factor({"helm_factor": 3}) == 1.0 and boats.helm_factor(None) == 1.0)
check("sail config", boats.sail_config(BOAT) == {"code0": BOAT["code0"],
                                                 "main_reefs": BOAT["main_reefs"]}
      and boats.sail_config({}) is None)
check("adjustments / wave coeffs", boats.polar_adjustments(BOAT) == BOAT["polar_adjustments"]
      and boats.wave_coeffs(BOAT) is None and boats.polar_adjustments(None) == [])

print("3) windfield: fallback_tries + duck-typed sources")


class Sparse(models.GFS):
    """Nothing posted: every fetch misses, so the loader steps back cycles."""

    def __init__(self):
        self.picked = []

    def fetch(self, cycle, fhr, member, bbox, timeout=60):
        self.picked.append(cycle)
        return None


s = Sparse()
windfield.build_windfield(BBOX, T0, T0 + 6 * 3600, models=[s])
check("default: tries older cycles (Lab behaviour)", len(set(s.picked)) == windfield.CYCLE_FALLBACK_TRIES + 1)
s = Sparse()
windfield.build_windfield(BBOX, T0, T0 + 6 * 3600, models=[s], fallback_tries=0)
check("fallback_tries=0: one cycle only", len(set(s.picked)) == 1)


class Wrapped:
    def __init__(self, src):
        self._src = src

    def __getattr__(self, k):
        return getattr(self._src, k)


s = Sparse()
wf = windfield.build_windfield(BBOX, T0, T0 + 6 * 3600, models=[Wrapped(s)], fallback_tries=0)
check("a wrapper around a source is used, not dropped", s.picked and wf.meta[0]["model"] == "gfs")

print("4) grib crop copies")
lat = np.arange(-90, 90.01, 0.25)[::-1]
lon = np.arange(-180, 180, 0.25)
u = np.random.default_rng(1).normal(size=(lat.size, lon.size))
big = grib.GribFrame("ecmwf", "det", float(T0), lat, lon, u, -u, True)
small = big.crop(BBOX)
check("cropped to the box (+0.5)", small.u.shape[0] < 20 and small.u.shape[1] < 20)
check("owns its data (no view on the global field)", small.u.base is None and small.lat.base is None)
check("same values", small.sample_uv(44.3, -82.2) == big.sample_uv(44.3, -82.2))

print("5) obstacle field round trip")
d = {"race_id": "t", "zones": [{"name": "Z", "type": "exclusion", "geometry": {
    "north": 44.5, "south": 44.4, "west": -82.3, "east": -82.1}}],
     "courses": [dict(COURSE["courses"][0], marks=COURSE["courses"][0]["marks"] + [
         {"name": "Isle", "type": "island", "lat": 44.2, "lon": -82.6, "rounding": "starboard"}])]}
f = geo.build_for_course(d, "x", optimizer.course_bbox(d, "x"), coastline_on=False, use_cache=False)
g = geo.field_from_dict(json.loads(json.dumps(geo.field_to_dict(f))))
check("same mask, box and layers", g.mask == f.mask and g.bbox == f.bbox and g.layers == f.layers
      and g.active and g.blocked(44.45, -82.2) and not g.blocked(43.95, -82.4))
try:
    geo.field_from_dict({"schema": "nope"})
    check("a bad payload is refused", False)
except ValueError:
    check("a bad payload is refused", True)

print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
