"""#149: the frozen playbook routes on the Lab's chart and the active boat's draft.

The Gameplan routes on the Lab's selected chart (`chart_source()`) and the active boat's no-go
depth, and Deploy sends the Orin a chart built the same way. The playbook used to call the
optimizer and the obstacle builder with neither, so it routed on the env default chart and draft:
on ENC with a 2.1 m draft the Gameplan stayed off a shoal the signed playbook went over.

Pinned:
  * every route in build_playbook (consensus, per-model scenarios, the scenario fan, pace,
    gear-loss, low-maneuver) and the rejoin table's obstacle fields get the chart and depth;
  * the play thinning's obstacle fields get them too;
  * synthesize records them in the bundle (and the env defaults when none are given);
  * the Lab snapshots its chart and the active boat's depth when synthesis starts;
  * Deploy builds the Orin's chart on the playbook's chart, not on what the Lab has selected now.

No network: the router, wind field and obstacle builder are stubbed.
"""
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ.pop("ANTHROPIC_API_KEY", None)

import numpy as np  # noqa: E402

from app import current as currentmod, geo, labstate, playbook, wave as wavemod  # noqa: E402
from app.geo import obstacles as OB  # noqa: E402
from app.wind import grib  # noqa: E402
from app.wind.windfield import WindField  # noqa: E402
import polar_fixture  # noqa: E402
polar_fixture.install()     # bare optimizer/sail-plan calls route on sr33's own polar (#303)

labstate._PATH = os.path.join(_tmp, "_labstate.json")   # never the image's real state file

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1_800_000_000.0
H = 3600.0
LAT = np.arange(42.5, 45.51, 0.25)
LON = np.arange(-83.5, -80.49, 0.25)
CHART = {"source": "enc", "safety_depth": 2.1}


def field(hours):
    frames = [grib.GribFrame(m, "det", T0 + k * H, LAT, LON, np.full((LAT.size, LON.size), 6.0),
                             np.zeros((LAT.size, LON.size)), True)
              for m in ("gfs", "hrrr") for k in range(hours + 1)]
    return WindField({("gfs", "det"): frames[:hours + 1], ("hrrr", "det"): frames[hours + 1:]},
                     [{"model": "gfs"}, {"model": "hrrr"}], (45.5, 42.5, -83.5, -80.5),
                     T0, T0 + hours * H)


DEF = {"race_id": "p", "name": "P", "zones": [], "courses": [{
    "id": "c", "start": {"lat": 43.5, "lon": -82.0},
    "marks": [{"name": "M", "type": "buoy", "lat": 44.0, "lon": -82.0, "rounding": "none"}],
    "finish": {"points": [{"lat": 44.5, "lon": -82.0}]}}]}


def _line(lat0, lat1, t0, n=21, kn=7.0):
    return [{"lat": round(lat0 + (lat1 - lat0) * i / (n - 1), 5), "lon": -82.0,
             "t": t0 + abs((lat1 - lat0) * i / (n - 1)) * 60 / kn * H} for i in range(n)]


def _route(path, from_mark=0, sails=("A3",)):
    t0, t1 = path[0]["t"], path[-1]["t"]
    legs = [{"to": "M", "leg_minutes": 30 / 7 * 60, "eta_epoch": t0 + 30 / 7 * H,
             "first_heading": 0, "direct_nm": 30.0, "sail": sails[0], "point_of_sail": "running"},
            {"to": "Finish", "leg_minutes": 30 / 7 * 60, "eta_epoch": t1, "first_heading": 0,
             "direct_nm": 30.0, "sail": sails[0], "point_of_sail": "running"}][from_mark:]
    return {"available": True, "complete": True, "course_id": "c", "path": path, "legs": legs,
            "leg_end_idx": [], "total_hours": (t1 - t0) / H, "total_tacks": 0, "total_peels": 0,
            "sail_plan": [{"sail": s} for s in sails], "degraded": False, "warnings": []}


calls = []          # (kind, source, safety_depth) per optimizer / obstacle-builder call


def fake_optimize(definition, course_id, start_epoch, wf, from_mark=0, **kw):
    kind = ("pace" if from_mark else "gear" if kw.get("exclude_sails")
            else "lowman" if (kw.get("maneuver_prune_mult") or 1) != 1 else "route")
    calls.append((kind, kw.get("source"), kw.get("safety_depth")))
    if from_mark:
        return _route(_line(44.0, 44.5, start_epoch, n=11), from_mark=from_mark)
    return _route(_line(43.5, 44.5, start_epoch))


def fake_leg_fields(definition, course_id, bbox, *, source=None, safety_depth=None, **kw):
    calls.append(("leg_fields", source, safety_depth))
    return None, []


OB._fill_global_backstop = lambda field, cache_dir=None: {}
currentmod.build_currentfield = lambda *a, **k: currentmod.ZeroCurrent()
wavemod.build_wavefield = lambda *a, **k: wavemod.ZeroWave()
_real_opt, _real_lf = playbook.optimizer.optimize_course, geo.build_leg_fields
playbook.optimizer.optimize_course = fake_optimize
geo.build_leg_fields = fake_leg_fields
playbook.build_windfield = lambda *a, **k: field(48)

# ---- 1) build_playbook ---------------------------------------------------------------------------
print("1) every route and obstacle field in the playbook uses the given chart and depth")
pb = playbook.build_playbook(DEF, "c", T0, ["gfs", "hrrr"], model_skill=False, v2_scenarios=True,
                             use_waves=False, fan_depth="quick", time_budget_s=60,
                             scenario_budget_s=60, chart=CHART)
kinds = {k for k, _s, _d in calls}
print(f"     {len(calls)} calls: {sorted(kinds)}")
check("(control) the playbook was built", pb.get("available") is True)
check("(control) consensus, scenarios, pace, gear-loss, low-maneuver and the rejoin table ran",
      {"route", "pace", "gear", "lowman", "leg_fields"} <= kinds)
check("every call got the chart", calls and all(s == "enc" for _k, s, _d in calls))
check("every call got the depth", calls and all(d == 2.1 for _k, _s, d in calls))

# ---- 2) the play thinning's obstacle fields --------------------------------------------------
print("2) the play thinning checks land on the same chart")
from app import synthesis  # noqa: E402

calls.clear()
synthesis._play_obstacles(DEF, "c", CHART)
check("the obstacle fields got the chart and depth", calls == [("leg_fields", "enc", 2.1)])

# ---- 3) synthesize records the chart in the bundle -------------------------------------------
print("3) synthesize passes the chart on and records it in the bundle")
seen = {}


def fake_build_playbook(*a, **kw):
    seen.update(kw)
    return {"available": True, "course_id": "c", "start_epoch": T0, "variants": [],
            "consensus": {"favored_side": "middle", "total_hours": 8.0}, "windfield": {}}


_real_bp = synthesis.pb.build_playbook
synthesis.pb.build_playbook = fake_build_playbook
synthesis._venue_stats = lambda: None
synthesis._buoy_stations = lambda *a, **k: []
synthesis._fingerprint = lambda *a, **k: None
synthesis._boat_model = lambda *a, **k: {}
bundle = synthesis.synthesize(DEF, "c", T0, ["gfs"], chart=CHART)
check("build_playbook got the chart", seen.get("chart") == CHART)
check("the bundle records it", bundle.get("chart") == {"source": "enc", "safety_depth_m": 2.1})
bundle = synthesis.synthesize(DEF, "c", T0, ["gfs"])
check("no chart given: the bundle records the defaults it routed on",
      bundle.get("chart") == {"source": OB.COASTLINE_SOURCE.lower(),
                              "safety_depth_m": OB.safety_depth_m()})
synthesis.pb.build_playbook = _real_bp
playbook.optimizer.optimize_course, geo.build_leg_fields = _real_opt, _real_lf

# ---- 4) the Lab snapshots its own chart and the active boat --------------------------------------
print("4) the Lab hands synthesis its chart and the active boat's depth")
from app import boats, main  # noqa: E402
from shared import chartid  # noqa: E402

labstate.set("coastline_source", "enc")
_real_active = boats.active_boat
boats.active_boat = lambda: {"boat_id": "x", "draft_m": 1.6}
snap = main._playbook_chart()
boats.active_boat = _real_active
check("the Lab's chart", snap["source"] == "enc")
check("the active boat's depth", snap["safety_depth"] == chartid.safety_depth_m({"draft_m": 1.6}))

# ---- 5) Deploy sends the chart the playbook was routed on -------------------------------------
print("5) Deploy builds the Orin's chart on the playbook's chart")
from app import orinassets  # noqa: E402

labstate.set("coastline_source", "natural_earth")          # changed after the freeze
got = {}


def fake_for_course(definition, course_id, bbox, source=None, **kw):
    got["source"] = source
    raise RuntimeError("stop")                               # the step reports a failed build


_real_bfc = orinassets.geo.build_for_course
orinassets.geo.build_for_course = fake_for_course
orinassets.boats.get_boat = lambda bid: {"boat_id": bid, "draft_m": 2.1336}
orinassets.boat_profile.validate = lambda b: ([], [])
DEPTH = chartid.safety_depth_m({"draft_m": 2.1336})
_, probs = orinassets.build(DEF, "c", bundle={"boat_model": {"boat_id": "sr33"},
                                             "chart": {"source": "enc", "safety_depth_m": DEPTH}})
check("the playbook's chart, not the Lab's current one", got.get("source") == "enc")
check("(control) same draft: no draft problem", orinassets.DRAFT_CHANGED not in probs)
got.clear()
orinassets.boats.get_boat = lambda bid: {"boat_id": bid, "draft_m": 1.5}     # draft lowered
payload, probs = orinassets.build(DEF, "c", bundle={
    "boat_model": {"boat_id": "sr33"}, "chart": {"source": "enc", "safety_depth_m": DEPTH}})
check("draft changed since the playbook: a problem, in plain words",
      probs == [orinassets.DRAFT_CHANGED])
check("…and no chart at the new draft is sent", payload["chart"] is None and "source" not in got)
orinassets.boats.get_boat = lambda bid: {"boat_id": bid, "draft_m": 2.1336}
got.clear()
orinassets.build(DEF, "c", bundle={"boat_model": {"boat_id": "sr33"}})
check("an older bundle with no chart: the Lab's chart, as before",
      got.get("source") == "natural_earth")
orinassets.geo.build_for_course = _real_bfc

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
