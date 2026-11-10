"""#203: every playbook entry keeps its route's health, and the bundle shows it.

  1) A robustness run (a scenario or pace run whose route holds the nominal line) used to drop
     its route's warnings: a +2 h pace run that sailed past the forecast on held wind read as
     "plan HOLDS" and the bundle looked clean. Now the robustness entry keeps degraded/warnings
     and the bundle is degraded, with the run named in its warning.
  2) The "rejoin or continue?" table play had no forecast-end check: its rows are routed from
     mid-leg, so they can run past the forecast when the nominal doesn't. Now a table whose
     routes sailed on held wind marks the play degraded with the plain warning.

No network: wind frames are built in memory; part 1 stubs the router.
"""
import os
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["INGESTED_DIR"] = tempfile.mkdtemp()

from app import current as currentmod, optimizer as OPT, playbook, wave as wavemod  # noqa: E402
from app.geo import obstacles as OB  # noqa: E402
from app.wind import grib  # noqa: E402
from app.wind.windfield import WindField  # noqa: E402
import polar_fixture  # noqa: E402
polar_fixture.install()     # bare optimizer/sail-plan calls route on sr33's own polar (#303)

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1_800_000_000.0          # a fixed epoch: nothing here reads the clock
H = 3600.0
LAT = np.arange(42.5, 45.51, 0.25)
LON = np.arange(-83.5, -80.49, 0.25)


def field(hours, u=6.0, v=0.0):
    """GFS hourly to +`hours` h, steady wind (default ~12 kn from the west: a beam reach north)."""
    frames = [grib.GribFrame("gfs", "det", T0 + k * H, LAT, LON, np.full((LAT.size, LON.size), u),
                             np.full((LAT.size, LON.size), v), True) for k in range(hours + 1)]
    return WindField({("gfs", "det"): frames}, [{"model": "gfs"}], (45.5, 42.5, -83.5, -80.5),
                     T0, T0 + hours * H)


OB._fill_global_backstop = lambda field, cache_dir=None: {}   # open water, loaded (no data in CI)
currentmod.build_currentfield = lambda *a, **k: currentmod.ZeroCurrent()
wavemod.build_wavefield = lambda *a, **k: wavemod.ZeroWave()

# ---- 1) robustness runs keep their warnings ---------------------------------------------------
print("1) a pace run that holds the line but sails past the forecast: the bundle says so")
DEF = {"race_id": "p", "name": "P", "zones": [], "courses": [{
    "id": "c", "start": {"lat": 43.5, "lon": -82.0},
    "marks": [{"name": "M", "type": "buoy", "lat": 44.0, "lon": -82.0, "rounding": "none"}],
    "finish": {"points": [{"lat": 44.5, "lon": -82.0}]}}]}


def _line(lat0, lat1, t0, n=21, kn=7.0):
    out = []
    for i in range(n):
        la = lat0 + (lat1 - lat0) * i / (n - 1)
        out.append({"lat": round(la, 5), "lon": -82.0, "t": t0 + abs(la - lat0) * 60 / kn * H})
    return out


def _route(path, from_mark=0, degraded=False):
    t0, t1 = path[0]["t"], path[-1]["t"]
    legs = [{"to": "M", "leg_minutes": 30 / 7 * 60, "eta_epoch": t0 + 30 / 7 * H, "first_heading": 0,
             "direct_nm": 30.0, "sail": "J1", "point_of_sail": "reaching"},
            {"to": "Finish", "leg_minutes": 30 / 7 * 60, "eta_epoch": t1, "first_heading": 0,
             "direct_nm": 30.0, "sail": "J1", "point_of_sail": "reaching"}][from_mark:]
    return {"available": True, "complete": True, "course_id": "c", "path": path, "legs": legs,
            "leg_end_idx": [], "total_hours": (t1 - t0) / H, "total_tacks": 0, "total_peels": 0,
            "sail_plan": [{"sail": "J1"}], "degraded": degraded,
            "warnings": [OPT.FORECAST_ENDS] if degraded else []}


def fake_optimize(definition, course_id, start_epoch, wf, from_mark=0, **kw):
    if from_mark:      # a pace run: the same line and sails from M, but past the forecast
        return _route(_line(44.0, 44.5, start_epoch, n=11), from_mark=from_mark, degraded=True)
    return _route(_line(43.5, 44.5, start_epoch))


_real_opt, _real_tab = playbook.optimizer.optimize_course, playbook._rejoin_tab
playbook.optimizer.optimize_course = fake_optimize
playbook._rejoin_tab = lambda *a, **k: []
playbook.build_windfield = lambda *a, **k: field(48)
pb = playbook.build_playbook(DEF, "c", T0, ["gfs"], model_skill=False, v2_scenarios=True,
                             use_waves=False, fan_depth="quick", time_budget_s=60,
                             scenario_budget_s=60)
playbook.optimizer.optimize_course, playbook._rejoin_tab = _real_opt, _real_tab
robust = (pb.get("v2") or {}).get("robustness") or []
pace = [e for e in robust if str(e.get("scenario", "")).startswith("pace_")]
print(f"     {len(robust)} robustness entries, {len(pace)} pace")
check("(control) the nominal is clean", not pb["consensus"].get("degraded")
      and not pb["consensus"].get("warnings"))
check("(control) the pace runs count as robustness (same line, same sails)", len(pace) == 3)
check("each pace robustness entry keeps its route's health",
      pace and all(e.get("degraded") and OPT.FORECAST_ENDS in e.get("warnings", []) for e in pace))
check("the clean ones stay clean", all(not e.get("degraded") for e in robust if e not in pace))
check("the bundle is degraded because of them", pb.get("degraded") is True)
check("…and names the run in its warning",
      bool(pace) and f"{pace[0]['name']}: {OPT.FORECAST_ENDS}" in pb.get("warnings", []))

# ---- 2) the rejoin table: a forecast-end check of its own -------------------------------------
print("2) the rejoin table routed past the forecast end is marked degraded")
DEF2 = {"race_id": "r", "name": "R", "zones": [], "courses": [{
    "id": "c", "start": {"lat": 43.5, "lon": -82.0}, "marks": [],
    "finish": {"points": [{"lat": 44.5, "lon": -82.0}]}}]}
MARKS2 = [("Start", 0, 43.5, -82.0), ("Finish", 1, 44.5, -82.0)]
nominal = _route(_line(43.5, 44.5, T0, n=41))
nominal["legs"] = [dict(nominal["legs"][0], to="Finish", direct_nm=60.0,
                        leg_minutes=60 / 7 * 60, eta_epoch=T0 + 60 / 7 * H)]
nominal["start_epoch"] = T0
playbook.optimizer.optimize_course = lambda *a, **k: {"available": False, "note": "stubbed"}


def rejoin_play(hours):
    routes, _robust = playbook._internal_fan(DEF2, "c", T0, field(hours), nominal,
                                             currentmod.ZeroCurrent(), wavemod.ZeroWave(), MARKS2,
                                             per_budget_s=30)
    return next((r for r in routes if r.get("id") == "rejoin_vs_continue"), None)


inside = rejoin_play(24)          # the forecast reaches well past the finish (~+8.6 h)
past = rejoin_play(2)             # it ends at +2 h; the table is routed from mid-leg (~+4.3 h)
playbook.optimizer.optimize_course = _real_opt
check("(control) the table is built in both", inside and past and inside.get("table") and past.get("table"))
check("(control) inside the forecast: not degraded, no warning",
      inside and not inside.get("degraded") and not inside.get("warnings"))
check("past the forecast end: degraded", past and past.get("degraded") is True)
check("…with the plain warning", past and OPT.FORECAST_ENDS in (past.get("warnings") or []))

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
