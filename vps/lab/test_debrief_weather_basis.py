"""#404 — the debrief says which weather its best route used, and splits regret.

Before: the best route ("the wind that actually blew") was, for an older race, routed on the
forecast cycle out at the gun, and "time lost" was the plan's FORECAST ETA minus that route's
time, so forecast error read as a tactical loss. Locked here, no network:

  1) a re-timer sails a fixed route on a given wind with the router's boat model;
  2) the plan's own route re-timed on the oracle wind splits time lost into the route call
     (same wind) and forecast error; with no saved route it falls back and says so;
  3) the report names the weather (hourly analyses / forecast from the start / latest) and its
     age at every hour;
  4) a race that is over prefers hourly analyses, falls back to the forecasts when the archive
     has too few, and never asks HRRR for a box outside it.
"""
import math
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["PLAYBOOK_DIR"] = os.path.join(_tmp, "playbooks")
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest  # noqa: E402

from app import judge as JD, retime as RT, oraclewind as OW            # noqa: E402
from app import current as CURMOD, wave as WAVEMOD                      # noqa: E402
from app.wind import archive as AR                                      # noqa: E402
import polar_fixture                                                    # noqa: E402

polar_fixture.install()
JD.API_KEY = None
POL = polar_fixture.sr33()
P = RT.boat_table(POL)
T0 = 1.75e9
NM_LAT = 1 / 60.0


class WF:
    loaded = True

    def __init__(self, tws=12.0, twd=270.0):
        self.tws, self.twd = tws, twd

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}

    def status(self):
        return {"models": []}


def north(nm, lat0=44.0, lon=-82.0):
    return [{"lat": lat0, "lon": lon}, {"lat": lat0 + nm * NM_LAT, "lon": lon}]


# ---- 1) the re-timer ------------------------------------------------------------------------
def test_beam_reach_is_distance_over_polar_speed():
    r = RT.sail_path(north(8), WF(12, 270), P, T0)
    sp = JD.optimizer._polar_speed(P, 12, 90)
    assert r["hours"] == pytest.approx(8 / sp, rel=0.01)
    assert r["missing"] == 0


def test_helm_and_current_change_the_time():
    base = RT.sail_path(north(8), WF(12, 270), P, T0)["hours"]
    helm = RT.sail_path(north(8), WF(12, 270), P, T0, helm_factor=0.9)["hours"]
    assert helm == pytest.approx(base / 0.9, rel=0.01)
    fair = RT.sail_path(north(8), WF(12, 270), P, T0, cur=CURMOD.ConstantCurrent(0.0, 1.0))
    assert fair["hours"] < base


def test_dead_upwind_is_beaten_at_vmg():
    r = RT.sail_path(north(4), WF(12, 0), P, T0)
    beat = JD.optimizer._vmg_twa_cached(P, 12, "beat")
    vmg = JD.optimizer._polar_speed(P, 12, beat) * math.cos(math.radians(beat))
    assert r["hours"] == pytest.approx(4 / vmg, rel=0.02)


def test_no_wind_refuses():
    class Calm(WF):
        def wind_at(self, *a):
            return None
    assert RT.sail_path(north(4), Calm(), P, T0) is None


def test_calibration_cancels_the_re_timer():
    path = north(8)
    h, why = RT.calibrated_hours(path, path, 1.3, WF(), P, T0)
    assert why is None and h == pytest.approx(1.3)
    dogleg = [path[0], {"lat": 44.0 + 4 * NM_LAT, "lon": -81.9}, path[1]]
    h2, _ = RT.calibrated_hours(dogleg, path, 1.3, WF(), P, T0)
    assert h2 > 1.3
    assert RT.calibrated_hours(None, path, 1.3, WF(), P, T0)[0] is None


# ---- 2) the regret split in the debrief -------------------------------------------------------
NS = {"courses": [{"id": "x", "start": {"lat": 44.0, "lon": -82.0},
                   "finish": {"points": [{"lat": 44.1333, "lon": -82.0}]}}]}
PATH = [{"lat": 44.0, "lon": -82.0, "t": T0}, {"lat": 44.1333, "lon": -82.0, "t": T0 + 3600}]
ORACLE = {"available": True, "complete": True, "total_hours": 1.0, "path": PATH, "legs": []}


def _pb(path):
    v = {"id": "middle", "total_hours": 1.25}
    if path is not None:
        v["route"] = {"path": path}
    return {"course_id": "x", "start_epoch": T0, "recommended": "middle", "variants": [v],
            "routing": {"helm_factor": 1.0, "polar_adjustments": [], "wave_coeffs": None,
                        "jib_crossovers": [], "sail_config": None, "use_waves": True}}


@pytest.fixture
def tactics(monkeypatch):
    monkeypatch.setattr(JD, "_oracle_windfield", lambda *a, **k: (WF(), "analyses"))
    monkeypatch.setattr(JD.optimizer, "optimize_course", lambda *a, **k: dict(ORACLE))
    monkeypatch.setattr(CURMOD, "build_currentfield", lambda *a, **k: CURMOD.ConstantCurrent(0, 0))
    monkeypatch.setattr(WAVEMOD, "build_wavefield", lambda *a, **k: WAVEMOD.ConstantWave(0.0))
    return lambda pb: JD._tactics(NS, "r", pb, None, lambda *_: None, boat={"boat_id": "c4"})


def test_plan_sailed_the_best_route_so_all_loss_is_forecast(tactics):
    reg = tactics(_pb(PATH))["regret"]
    assert reg["kind"] == "same_wind"
    assert reg["minutes"] == 0                       # same route, same wind: no route loss
    assert reg["forecast_error_minutes"] == -15      # forecast said 1.25 h; it took 1.0 h
    assert reg["vs_forecast_minutes"] == 15          # the old number, kept for comparison
    assert reg["split_note"] is None


def test_a_worse_route_shows_as_route_loss(tactics):
    dogleg = [PATH[0], {"lat": 44.066, "lon": -81.85}, PATH[1]]
    reg = tactics(_pb(dogleg))["regret"]
    assert reg["kind"] == "same_wind" and reg["minutes"] > 0
    assert reg["plan_on_actual_hours"] > 1.0


def test_no_saved_route_falls_back_and_says_so(tactics):
    tac = tactics(_pb(None))
    reg = tac["regret"]
    assert reg["kind"] == "vs_forecast" and reg["minutes"] == 15
    assert reg["forecast_error_minutes"] is None
    assert "forecast error" in reg["split_note"] and "forecast error" in tac["caveat"]


def test_report_carries_the_weather(tactics):
    tac = tactics(_pb(PATH))
    assert tac["weather"]["kind"] == "analyses"
    assert tac["weather"]["label"] == "Hourly analyses"
    assert "analysis" in tac["caveat"]


def test_deterministic_critique_keeps_them_apart(tactics):
    dogleg = [PATH[0], {"lat": 44.066, "lon": -81.85}, PATH[1]]
    reg = tactics(_pb(dogleg))["regret"]
    rep = {"tactics_available": True, "race_name": "Test", "actual_track": {},
           "regret": {**reg, "side_matched": False, "recommended_side": "left",
                      "side_paid": "right"}}
    c = JD._deterministic_critique(rep)
    assert "on the same wind" in c["assessment"]
    assert "forecast was off" in c["assessment"]
    assert "on the same wind" in c["proposed_learnings"]
    assert "forecast off by" in c["proposed_learnings"]


# ---- 3) which weather, how old ---------------------------------------------------------------
class Frame:
    def __init__(self, t):
        self.valid_time = t


def _cyc(epoch):
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d %HZ")


def test_forecast_ages_count_from_the_cycle():
    start = 1_752_840_000.0                         # on the hour
    cyc = start - 3 * 3600
    wf = WF()
    wf.meta = [{"model": "gfs", "cycle": _cyc(cyc), "frames": 9},
               {"model": "hrrr", "cycle": None, "frames": 0, "skipped": "not_here"}]
    wf.series = {("gfs", "det"): [Frame(start + h * 3600) for h in range(0, 7)]}
    d = OW.describe(wf, "archive", start, start + 6 * 3600)
    assert d["label"] == "Forecast from the start"
    assert [m["model"] for m in d["models"]] == ["gfs"]       # a skipped model isn't listed
    assert d["models"][0]["age_min_h"] == 3 and d["models"][0]["age_max_h"] == 9
    assert [r["age_h"]["gfs"] for r in d["hourly"]] == [3, 4, 5, 6, 7, 8, 9]
    assert "GFS" in d["summary"] and "3–9 h old" in d["summary"]
    assert d["hours_uncovered"] == 0


def test_analyses_are_fresh_and_gaps_are_counted():
    start = 1_752_840_000.0
    wf = WF()
    wf.meta = [{"model": "hrrr", "cycle": AR.ANALYSIS_CYCLE, "frames": 3}]
    wf.series = {("hrrr", "det"): [Frame(start + h * 3600) for h in (0, 1, 2)]}
    d = OW.describe(wf, "analyses", start, start + 10 * 3600)
    ages = [r["age_h"]["hrrr"] for r in d["hourly"]]
    assert ages[:3] == [0, 0, 0]
    assert ages[-1] is None                        # 8 h past the last analysis: not covered
    assert d["hours_uncovered"] > 0 and "No wind for" in d["summary"]
    assert d["summary"].startswith("Hourly analyses: HRRR analyses")


def test_stub_field_is_safe():
    d = OW.describe(WF(), "live", T0, T0 + 3600)
    assert d["models"] == [] and d["summary"] == "Latest forecast."


# ---- 4) analyses first for a race that is over --------------------------------------------------
class Loaded(WF):
    def __init__(self, frames, expected):
        super().__init__()
        self.meta = [{"model": "hrrr", "cycle": AR.ANALYSIS_CYCLE, "frames": frames,
                      "expected_frames": expected}]
        self.series = {("hrrr", "det"): [Frame(T0)]} if frames else {}

    @property
    def loaded(self):
        return bool(self.series)


BBOX = (45.0, 43.5, -83.0, -81.0)


def test_a_finished_race_uses_hourly_analyses(monkeypatch):
    monkeypatch.setattr(AR, "analysis_field", lambda *a, **k: Loaded(30, 32))
    wf, kind = JD._oracle_windfield(BBOX, T0, T0 + 30 * 3600, None, "r", lambda *_: None)
    assert kind == "analyses"


def test_too_few_analyses_fall_back_to_the_forecasts(monkeypatch):
    import app.wind as W
    monkeypatch.setattr(AR, "analysis_field", lambda *a, **k: Loaded(3, 32))
    monkeypatch.setattr(W, "build_windfield", lambda *a, **k: WF())
    wf, kind = JD._oracle_windfield(BBOX, T0, T0 + 30 * 3600, None, "r", lambda *_: None)
    assert kind == "archive"


def test_analyses_off_by_switch(monkeypatch):
    import app.wind as W
    monkeypatch.setattr(JD, "ORACLE_ANALYSES", False)
    monkeypatch.setattr(AR, "analysis_field", lambda *a, **k: pytest.fail("should not be asked"))
    monkeypatch.setattr(W, "build_windfield", lambda *a, **k: WF())
    assert JD._oracle_windfield(BBOX, T0, T0 + 3600, None, "r", lambda *_: None)[1] == "archive"


def test_hrrr_analyses_outside_its_area_fetch_nothing(monkeypatch):
    monkeypatch.setattr(AR.ArchiveHRRR, "fetch", lambda *a, **k: pytest.fail("no fetch"))
    channel = (51.0, 49.5, -3.0, 1.0)
    wf = AR.analysis_field(channel, T0, T0 + 6 * 3600)
    assert not wf.loaded and wf.meta[0].get("skipped")
    # partly inside is not enough: half a course of analyses would leave the rest borrowed
    straddle = (54.0, 50.0, -70.0, -58.0)
    assert not AR.analysis_field(straddle, T0, T0 + 3600).loaded
