"""#51: a model's forecast that has ENDED no longer feeds the route as if it were current.

Before, each model series held its last frame forever (and its first frame before it began):
HRRR's +18 h frame kept blending at priority 1.2 through a 40 h race, and a route that ran past
every model's end sailed on the last frame with no warning. Now (Cole, 2026-11-02):
  1) a model counts only within one frame step of its own frames; while another model still
     covers the hour, the ended one drops out of the blend;
  2) when NO model reaches the hour, the last frame still stands in (the route is kept), but the
     route is marked degraded with a plain warning, first or last hours alike.

No network: the frames are built in memory.
"""
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
os.environ["INGESTED_DIR"] = tempfile.mkdtemp()

from app import optimizer as OPT  # noqa: E402
from app.scenarios import TransformedWind  # noqa: E402
from app.wind import grib  # noqa: E402
from app.wind.windfield import WindField  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1_800_000_000.0
H = 3600.0
LAT = np.arange(43.0, 45.01, 0.25)
LON = np.arange(-83.5, -80.49, 0.25)


def series(model, hours, step, u, v):
    return [grib.GribFrame(model, "det", T0 + k * H, LAT, LON, np.full((LAT.size, LON.size), u),
                           np.full((LAT.size, LON.size), v), True) for k in range(0, hours + 1, step)]


# HRRR to +18 h (hourly) from the WEST at ~12 kn; GFS to +72 h (3-hourly) from the NORTH
HRRR = series("hrrr", 18, 1, 6.0, 0.0)
GFS = series("gfs", 72, 3, 0.0, -6.0)
wf = WindField({("hrrr", "det"): HRRR, ("gfs", "det"): GFS},
               [{"model": "hrrr"}, {"model": "gfs"}], (45, 43, -83.5, -80.5), T0, T0 + 72 * H)

print("1) an ended model drops out while another still covers the hour")
d10 = wf.detail_at(44.0, -82.0, T0 + 10 * H)
check("+10 h: both models blend", d10["models"] == ["gfs", "hrrr"] and not d10.get("held"))
d19 = wf.detail_at(44.0, -82.0, T0 + 18.5 * H)
check("+18.5 h (within one HRRR step of its end): HRRR still counts", d19["models"] == ["gfs", "hrrr"])
for h in (24, 48, 72):
    d = wf.detail_at(44.0, -82.0, T0 + h * H)
    check(f"+{h} h: GFS only (HRRR ended at +18 h), not held",
          d["models"] == ["gfs"] and d["n_samples"] == 1 and not d.get("held"))
check("…and the wind is GFS's northerly, not dragged west by HRRR's last frame",
      abs(((wf.detail_at(44.0, -82.0, T0 + 48 * H)["twd"] + 180) % 360) - 180) < 1.0)

print("2) past every model's end: the last frame stands in, flagged")
d = wf.detail_at(44.0, -82.0, T0 + 120 * H)
check("+120 h: still a wind (GFS's last frame)", d is not None and d["models"] == ["gfs"])
check("…flagged as held", d.get("held") is True)
check("+74 h (within one GFS step): a real forecast, not held",
      not wf.detail_at(44.0, -82.0, T0 + 74 * H).get("held"))
LATE = WindField({("gfs", "det"): [f for f in GFS if f.valid_time >= T0 + 6 * H]},
                 [{"model": "gfs"}], (45, 43, -83.5, -80.5), T0, T0 + 72 * H)
check("before the first frame by more than a step: held too",
      LATE.detail_at(44.0, -82.0, T0).get("held") is True)
check("a scenario's shifted view keeps the flag",
      TransformedWind(wf, rot_deg=10).detail_at(44.0, -82.0, T0 + 120 * H).get("held") is True)

# Codex on #200: the stand-in must come from a model that covers the POSITION. GFS (wide grid)
# ends +1 h; HRRR ends +2 h but its grid stops short of the boat: at +2.2 h GFS's last frame must
# stand in, not "no wind" (which drops the router to its made-up fallback)
NARROW_LON = np.arange(-81.5, -80.49, 0.25)                 # HRRR misses -82.0
NARROW = [grib.GribFrame("hrrr", "det", T0 + k * H, LAT, NARROW_LON,
                         np.full((LAT.size, NARROW_LON.size), 6.0),
                         np.full((LAT.size, NARROW_LON.size), 0.0), True) for k in range(0, 3)]
SHORT = series("gfs", 1, 1, 0.0, -6.0)
CODEX = WindField({("hrrr", "det"): NARROW, ("gfs", "det"): SHORT},
                  [{"model": "hrrr"}, {"model": "gfs"}], (45, 43, -83.5, -80.5), T0, T0 + 3 * H)
dc = CODEX.detail_at(44.0, -82.0, T0 + 2.2 * H)
check("(control) HRRR doesn't cover the boat", NARROW[-1].sample_uv(44.0, -82.0) is None)
check("a nearer-ending model off the boat's position doesn't hide GFS: wind, held, from GFS",
      dc is not None and dc.get("held") and dc["models"] == ["gfs"])

print("3) the route: kept, degraded, says so plainly")
DEF = {"race_id": "long", "name": "Long", "zones": [], "courses": [{
    "id": "c", "start": {"lat": 43.3, "lon": -82.0}, "marks": [],
    "finish": {"points": [{"lat": 44.8, "lon": -82.0}]}}]}       # ~90 nm dead upwind in GFS's N
r = OPT.optimize_course(DEF, "c", T0 + 60 * H, wf, avoid=False, emit_exploration=False,
                        time_budget_s=120, resolution="fast")
print(f"     start +60 h, finish +{(r['finish_epoch'] - T0) / H:.0f} h, complete={r['complete']}")
check("(control) the route runs past GFS's +72 h end", r["finish_epoch"] > T0 + 75 * H)
check("the route is kept", r["available"] and r["complete"] and r["path"])
check("degraded", r["degraded"] is True)
check("with the plain warning", OPT.FORECAST_ENDS in r["warnings"])

r2 = OPT.optimize_course(DEF, "c", T0 + 2 * H, wf, avoid=False, emit_exploration=False,
                         time_budget_s=120, resolution="fast")
check("(control) a route inside the forecast: no such warning",
      r2["finish_epoch"] < T0 + 70 * H and OPT.FORECAST_ENDS not in r2["warnings"]
      and OPT.FORECAST_STARTS_LATE not in r2["warnings"])
r3 = OPT.optimize_course(DEF, "c", T0, LATE, avoid=False, emit_exploration=False,
                         time_budget_s=120, resolution="fast")
check("a forecast that starts after the gun: degraded, says so",
      r3["degraded"] and OPT.FORECAST_STARTS_LATE in r3["warnings"])

print("4) the playbook carries the warning to the Lab, before Deploy (Codex on #200)")
from app import current as currentmod, pbstore, playbook, synthesis, wave as wavemod  # noqa: E402

playbook.build_windfield = lambda *a, **k: wf
from app.geo import obstacles as OB  # noqa: E402
OB._fill_global_backstop = lambda field, cache_dir=None: {}   # open water, loaded (no data in CI)
currentmod.build_currentfield = lambda *a, **k: currentmod.ZeroCurrent()
wavemod.build_wavefield = lambda *a, **k: wavemod.ZeroWave()
_real_build = playbook.build_playbook
PB = {}


def _pb(*a, **k):
    k.update(model_skill=False, v2_scenarios=False, time_budget_s=120)
    PB.update(_real_build(*a, **k))
    return dict(PB)


playbook.build_playbook = _pb
synthesis.pb.build_playbook = _pb
synthesis._buoy_stations = lambda *a, **k: []          # no network
synthesis._venue_stats = lambda: None
bundle = synthesis.synthesize(DEF, "c", T0 + 60 * H, ["gfs", "hrrr"], use_waves=False)
print(f"     playbook: available={PB.get('available')} variants={len(PB.get('variants') or [])}")
check("(control) a playbook was built", PB.get("available") and PB.get("variants"))
check("the consensus carries degraded + the forecast warning",
      PB["consensus"].get("degraded") and OPT.FORECAST_ENDS in PB["consensus"].get("warnings", []))
check("every variant carries it", all(v.get("degraded") and OPT.FORECAST_ENDS in v.get("warnings", [])
                                      for v in PB["variants"]))
check("the bundle: degraded, warning listed once",
      bundle.get("degraded") and bundle.get("warnings", []).count(OPT.FORECAST_ENDS) == 1)
check("…and on each bundle variant", all(OPT.FORECAST_ENDS in v.get("warnings", [])
                                          for v in bundle.get("variants") or [{}]))
pbstore.PLAYBOOK_DIR = tempfile.mkdtemp()
pbstore.save(bundle)
row = pbstore.list_bundles()[0]
check("the stored summary (what Deploy lists) carries it",
      row.get("degraded") and OPT.FORECAST_ENDS in row.get("warnings", []))

print("5) the v2 plays' health reaches the bundle too (Codex on #200, round 3)")
# the nominal finishes ~+11.6 h inside a forecast to +11 h (hourly, so it counts to +12 h); a
# slower-pressure play (~+12.4 h) runs past it. Nominal clean, the bundle must still say so.
LAT5 = np.arange(43.0, 45.51, 0.25)
FR5 = [grib.GribFrame("gfs", "det", T0 + k * H, LAT5, LON, np.full((LAT5.size, LON.size), 0.0),
                      np.full((LAT5.size, LON.size), -6.0), True) for k in range(12)]
WF5 = WindField({("gfs", "det"): FR5}, [{"model": "gfs"}], (45.5, 43, -83.5, -80.5), T0, T0 + 11 * H)
DEF5 = {"race_id": "p", "name": "P", "zones": [], "courses": [{
    "id": "c", "start": {"lat": 43.5, "lon": -82.6},
    "marks": [{"name": "M", "type": "buoy", "lat": 44.0, "lon": -82.0, "rounding": "none"}],
    "finish": {"points": [{"lat": 44.5, "lon": -82.6}]}}]}
playbook.build_windfield = lambda *a, **k: WF5


def _pb5(*a, **k):
    k.update(model_skill=False, v2_scenarios=True, time_budget_s=60, fan_depth="quick",
             scenario_budget_s=240)
    PB.clear()
    PB.update(_real_build(*a, **k))
    return dict(PB)


synthesis.pb.build_playbook = _pb5
b5 = synthesis.synthesize(DEF5, "c", T0, ["gfs"], use_waves=False)
plays5 = (PB.get("v2") or {}).get("scenario_routes") or []
bad = [e for e in plays5 if e.get("degraded")]
print(f"     consensus finish +{(PB['consensus'].get('total_hours') or 0):.1f} h; "
      f"{len(plays5)} plays, {len(bad)} past the forecast: {[e['id'] for e in bad]}")
check("(control) the nominal stays inside the forecast", not PB["consensus"].get("degraded"))
check("(control) at least one play runs past it", bad)
check("each play keeps its own health", all(OPT.FORECAST_ENDS in e.get("warnings", []) for e in bad))
check("the bundle is degraded because of it", b5.get("degraded") is True)
check("…and names the play in its warning",
      any(w.startswith(bad[0]["name"] + ":") and OPT.FORECAST_ENDS in w for w in b5.get("warnings", []))
      if bad else False)
check("the bundle's play carries it", any(p.get("degraded") and OPT.FORECAST_ENDS in p.get("warnings", [])
                                          for p in b5.get("plays") or []))

print("6) the held side comes from the series actually used (Codex on #200, round 3)")
# GFS covers the boat and ends +18 h; HRRR misses the boat but runs to +48 h
WIDE_HRRR = [grib.GribFrame("hrrr", "det", T0 + k * H, LAT, NARROW_LON,
                            np.full((LAT.size, NARROW_LON.size), 6.0),
                            np.full((LAT.size, NARROW_LON.size), 0.0), True) for k in range(49)]
G18 = series("gfs", 18, 1, 0.0, -6.0)
WF6 = WindField({("hrrr", "det"): WIDE_HRRR, ("gfs", "det"): G18},
                [{"model": "hrrr"}, {"model": "gfs"}], (45, 43, -83.5, -80.5), T0, T0 + 48 * H)
d6 = WF6.detail_at(44.0, -82.0, T0 + 30 * H)
check("+30 h at the boat: GFS's last frame, held at its END", d6 and d6.get("held")
      and d6["models"] == ["gfs"] and d6.get("held_side") == "end")
r6 = OPT.optimize_course(DEF, "c", T0 + 16 * H, WF6, avoid=False, emit_exploration=False,
                         time_budget_s=120, resolution="fast")
check("the route says the forecast ENDS (not that it starts late)",
      OPT.FORECAST_ENDS in r6["warnings"] and OPT.FORECAST_STARTS_LATE not in r6["warnings"])

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
