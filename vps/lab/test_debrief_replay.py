"""Debrief replay + strip chart (#373, #374): tracks on one clock, channels with a fallback,
stats that leave faulty stretches out, saved 2-5 min tests, the day's wind kept with the replay.

Run: docker run --rm -v $PWD:/repo -w /repo/vps/lab -e PYTHONPATH=/repo/vps/lab:/repo \
       c4-ci-lab python -m pytest -q test_debrief_replay.py
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import replay as RP
from app import retrostore as RS
from app import track as T

T0 = 1784155457.0           # a wall-clock recording start
GUN = 1784394000.0
POLAR = [(10.0, 90.0, 6.0)]


@pytest.fixture(autouse=True)
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "TRACK_DIR", str(tmp_path / "ingested"))
    monkeypatch.setattr(RS, "RETRO_DB", str(tmp_path / "retro" / "retro.db"))
    monkeypatch.setattr(RP, "boat_polars", lambda boat_id=None: POLAR)
    return tmp_path


def boatlog(n=60, trust=None):
    fixes = [{"t": T0 + i * 5, "lat": 43.0 + i * 1e-4, "lon": -82.4, "sog": 6.0, "cog": 10.0,
              "tws": 10.0, "twa": 90.0 if i < n // 2 else 270.0, "stw": 6.0} for i in range(n)]
    return {"source": "boatlog", "boat": "Ours", "fixes": fixes, "trust": trust,
            "window": {"start_ts": T0, "end_ts": T0 + (n - 1) * 5, "kind": "derived"}}


def gpx(relative=False, n=20):
    base = 0.0 if relative else T0
    return {"source": "gpx", "fixes": [{"t": base + i * 10, "lat": 43.0, "lon": -82.4 + i * 1e-4,
                                        "sog": 5.0, "cog": 90.0} for i in range(n)]}


# ---- tracks on one clock ---------------------------------------------------------------------
def test_own_track_wall_clock_relative_and_unplaceable():
    own = RP.own_track(gpx())
    assert own["available"] and own["t0"] == T0 and own["fixes"][0][:3] == [T0, 43.0, -82.4]
    rel = RP.own_track(gpx(relative=True), gun=GUN)
    assert rel["t0"] == GUN                       # a YB-style clock anchors on the gun
    bad = RP.own_track(gpx(relative=True), gun=None)
    assert not bad["available"] and "start time" in bad["note"]


def test_thin_keeps_last_and_cap():
    seq = list(range(10001))
    out = RP.thin(seq, 100)
    assert len(out) == 100 and out[-1] == 10000 and out[0] == 0


def test_danger_intervals_merge_across_channels():
    trust = {"danger": {"heading": [(10, 20), (40, 50)], "attitude": [(15, 30)]}}
    assert RP.danger_intervals(trust) == [[10.0, 30.0], [40.0, 50.0]]
    assert RP.danger_intervals(None) == []


def test_fleet_from_retro_archive_clipped_and_own_flagged():
    fx = [{"t": T0 - 7200 + i * 600, "lat": 43.0, "lon": -82.0 + i * 1e-3} for i in range(40)]
    RS.upsert_fleet("myrace", {}, T0, "file",
                    [{"id": 1, "name": "Ours", "sail": "X 1"}, {"id": 2, "name": "Rival"},
                     {"id": 3, "name": "No track"}],
                    {1: fx, 2: fx}, [])
    f = RP.fleet_tracks({"race_id": "myrace"}, "myrace", T0, T0 + 600, own_name="ours")
    names = {b["name"]: b for b in f["boats"]}
    assert set(names) == {"Ours", "Rival"} and names["Ours"]["own"] and not names["Rival"]["own"]
    ts = [r[0] for r in names["Rival"]["fixes"]]
    assert min(ts) >= T0 - 3600 and max(ts) <= T0 + 600 + 3600    # clipped to the recording ±1 h


def test_fleet_missing_says_where_to_load_it():
    f = RP.fleet_tracks({"race_id": "nope"}, "nope", T0, T0 + 1)
    assert f["boats"] == [] and "Fleet retro" in f["note"]


def test_build_needs_a_track_then_serves_it_with_saved_state():
    assert not RP.build("r1", T0)["available"]
    T.save_track("r1", boatlog(trust={"available": True, "danger": {"heading": [(T0, T0 + 20)]}}))
    d = RP.build("r1", T0)
    assert d["available"] and d["full_channels"] and d["danger"] == [[T0, T0 + 20]]
    assert d["own"]["boat"] == "Ours" and d["wind_grid"] is None and d["tests"] == []


# ---- channels --------------------------------------------------------------------------------
def test_channels_from_fixes_signs_twa_and_derives_twd():
    s = RP.channels_from_fixes(boatlog()["fixes"])
    assert s[0]["twa"] == 90.0 and s[-1]["twa"] == -90.0          # 270 -> port, negative
    assert s[-1]["twd"] == pytest.approx((10 - 90) % 360)
    assert s[0]["heel"] is None and s[0]["bsp"] == 6.0


def test_enrich_vmg_and_pct_polar():
    s = RP.enrich([{"t": 0, "bsp": 6.0, "sog": 6.5, "tws": 10.0, "twa": -90.0},
                   {"t": 1, "bsp": None, "sog": 5.0, "tws": None, "twa": 0.0}], POLAR)
    assert s[0]["pct_polar"] == pytest.approx(100.0) and s[0]["vmg"] == pytest.approx(0.0, abs=0.01)
    assert s[1]["vmg"] == 5.0 and s[1]["pct_polar"] is None        # SOG when no BSP


def test_channels_boatlog_via_agent():
    T.save_track("r1", boatlog())
    calls = []

    def agent(path, timeout=None):
        calls.append(path)
        return {"step_s": 2, "samples": [{"t": T0, "bsp": 6.0, "tws": 10.0, "twa": 45.0,
                                          "heel": 18.0, "twd": 200.0}]}
    r = RP.channels("r1", T0, agent_get=agent)
    assert r["source"] == "boatlog" and r["note"] == "" and "heel" in r["channels"]
    assert calls and calls[0].startswith(f"/racelog/channels?start={T0}")


def test_channels_falls_back_when_boat_log_is_down():
    T.save_track("r1", boatlog())

    def down(path, timeout=None):
        raise OSError("unreachable")
    r = RP.channels("r1", T0, agent_get=down)
    assert r["available"] and r["source"] == "track" and "heel" in r["note"]
    assert "bsp" in r["channels"] and "heel" not in r["channels"]


def test_channels_falls_back_when_boat_log_has_nothing():
    T.save_track("r1", boatlog())
    r = RP.channels("r1", T0, agent_get=lambda *a, **k: {"step_s": 2, "samples": []})
    assert r["source"] == "track" and "no channels" in r["note"] and r["samples"]


def test_channels_gpx_position_only_and_relative_clock(monkeypatch):
    T.save_track("r2", gpx(relative=True))
    from app import store, racestart
    monkeypatch.setattr(store, "get_race", lambda rid: {"race_id": rid})
    monkeypatch.setattr(racestart, "race_start", lambda d: {"epoch": GUN})
    r = RP.channels("r2", None, agent_get=lambda *a, **k: pytest.fail("no agent for a GPX"))
    assert r["source"] == "track" and "SOG" in r["note"]
    assert r["samples"][0]["t"] == GUN and set(r["channels"]) <= {"sog", "cog", "vmg"}


# ---- stats -----------------------------------------------------------------------------------
def test_window_stats_mean_sd_and_danger_left_out():
    samples = [{"t": i, "bsp": v} for i, v in enumerate([5.0, 7.0, 5.0, 7.0, 100.0])]
    st = RP.window_stats(samples, 0, 4, danger=[[3.5, 4.5]])
    assert st["n"] == 5 and st["left_out"] == 1
    assert st["stats"]["bsp"] == {"mean": 6.0, "sd": 1.0, "n": 4}


def test_window_stats_angles_are_circular():
    samples = [{"t": 0, "twd": 350.0, "twa": -170.0}, {"t": 1, "twd": 10.0, "twa": 170.0}]
    st = RP.window_stats(samples, 1, 0)                # markers in either order
    assert st["stats"]["twd"]["mean"] == pytest.approx(0.0, abs=0.01) or \
        st["stats"]["twd"]["mean"] == pytest.approx(360.0, abs=0.01)
    assert abs(st["stats"]["twa"]["mean"]) == pytest.approx(180.0, abs=0.01)
    assert st["stats"]["twd"]["sd"] == pytest.approx(10.0, abs=0.2)


def test_window_stats_constant_angle_has_zero_sd():
    # float noise puts the mean vector just past length 1 for some runs (20° x 5, 340° x 103)
    for a in (20.0, 340.0, -40.0):
        for n in (5, 10, 19, 36, 103):
            st = RP.window_stats([{"t": i, "cog": a} for i in range(n)], 0, n)
            assert st["stats"]["cog"]["sd"] == pytest.approx(0, abs=0.01)
            assert st["stats"]["cog"]["mean"] == pytest.approx(a % 360)


def test_window_stats_step_overlapping_a_fault_is_left_out():
    # 7 s steps at 100 and 107, fault from 105: the step at 100 mixed healthy and faulty
    # readings and used to count because its start is outside the fault (Codex on #397)
    samples = [{"t": 93, "bsp": 6.0}, {"t": 100, "bsp": 50.0}, {"t": 107, "bsp": 50.0}]
    st = RP.window_stats(samples, 93, 114, danger=[[105, 110]], step=7)
    assert st["left_out"] == 2 and st["stats"]["bsp"]["mean"] == 6.0
    assert RP.window_stats(samples, 93, 107, danger=[[105, 110]])["left_out"] == 1  # points
    assert not RP._in(98, [[105, 110]], 7)            # [98, 105) ends as the fault starts


def test_saved_test_uses_the_agent_step_for_faults():
    # fault 100-110 s in: the 7 s steps at 98 and 105 both touch it (the one at 98 starts outside)
    T.save_track("r1", boatlog(n=80, trust={"available": True,
                                            "danger": {"heading": [(T0 + 100, T0 + 110)]}}))

    def agent(path, timeout=None):
        return {"step_s": 7, "samples": [{"t": T0 + k * 7, "bsp": 60.0 if k in (14, 15) else 6.0}
                                         for k in range(30)]}
    r = RP.save_test("r1", T0, "x", T0, T0 + 200, agent_get=agent)
    assert r["ok"] and r["test"]["left_out"] == 2
    assert r["test"]["stats"]["bsp"]["mean"] == 6.0


def test_window_stats_only_whole_steps_inside():
    samples = [{"t": 0, "bsp": 6.0}, {"t": 7, "bsp": 6.0}, {"t": 14, "bsp": 99.0}]
    st = RP.window_stats(samples, 0, 20, step=7)      # the step at 14 runs to 21: out
    assert st["n"] == 2 and st["stats"]["bsp"]["mean"] == 6.0
    assert RP.window_stats(samples, 0, 14)["n"] == 3  # point samples: ends inclusive


# ---- saved tests -----------------------------------------------------------------------------
def test_save_test_checks_length_name_and_keeps_stats():
    T.save_track("r1", boatlog(n=80))
    down = lambda *a, **k: (_ for _ in ()).throw(OSError("x"))    # noqa: E731
    assert "2 to 5" in RP.save_test("r1", T0, "x", T0, T0 + 60, agent_get=down)["note"]
    assert "name" in RP.save_test("r1", T0, " ", T0, T0 + 150, agent_get=down)["note"]
    r = RP.save_test("r1", T0, "Port, new jib", T0 + 150, T0, agent_get=down)
    assert r["ok"] and r["test"]["seconds"] == 150 and r["test"]["stats"]["bsp"]["mean"] == 6.0
    saved = RP.build("r1", T0)["tests"]
    assert [x["name"] for x in saved] == ["Port, new jib"]
    assert RP.delete_test("r1", T0, saved[0]["id"]) and RP.build("r1", T0)["tests"] == []
    assert not RP.delete_test("r1", T0, "missing")


def test_tests_are_per_recording():
    T.save_track("r1", boatlog(n=80))
    down = lambda *a, **k: (_ for _ in ()).throw(OSError("x"))    # noqa: E731
    RP.save_test("r1", T0, "A", T0, T0 + 150, agent_get=down)
    T.save_track("r1", {**gpx()})                                   # a GPX: no recording key
    assert RP.build("r1", None)["tests"] == []


def test_routes_save_and_delete():
    T.save_track("r1", boatlog(n=80))
    app = FastAPI()
    app.include_router(RP.router)
    import app.monitor as M
    orig = M.agent_json
    M.agent_json = lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
    try:
        c = TestClient(app)
        bad = c.post("/api/debrief/replay/tests", json={"race_id": "r1", "recording": T0,
                                                          "name": "t", "start": T0, "end": T0 + 10})
        assert bad.status_code == 400
        ok = c.post("/api/debrief/replay/tests", json={"race_id": "r1", "recording": T0,
                                                         "name": "t", "start": T0, "end": T0 + 200})
        assert ok.status_code == 200
        tid = ok.json()["test"]["id"]
        got = c.get(f"/api/debrief/replay?race_id=r1&recording={T0}").json()
        assert got["tests"][0]["id"] == tid
        assert c.post("/api/debrief/replay/tests/delete",
                      json={"race_id": "r1", "recording": T0, "id": tid}).json()["ok"]
    finally:
        M.agent_json = orig


def test_wind_status_follows_only_the_callers_job(monkeypatch):
    from app import jobs
    T.save_track("r1", boatlog())
    monkeypatch.setattr(RP, "build_wind", lambda rid, rec, progress=None: {"ok": True, "frames": 1})
    app = FastAPI()
    app.include_router(RP.router)
    c = TestClient(app)
    started = c.post("/api/debrief/replay/wind", json={"race_id": "r1", "recording": T0}).json()
    jid = started["job_id"]
    import time as _t
    for _ in range(100):
        if jobs.status("replaywind")["state"] != "running":
            break
        _t.sleep(0.02)
    mine = c.get(f"/api/debrief/replay/wind/status?job_id={jid}").json()
    assert mine["state"] == "done" and mine["job_id"] == jid
    other = c.get("/api/debrief/replay/wind/status?job_id=someoneelse").json()
    assert other["state"] == "error"                  # never another run's state


# ---- the day's wind --------------------------------------------------------------------------
class FakeWF:
    loaded = True

    def sample_grid(self, t, step, bbox):
        return [{"lat": bbox[0], "lon": bbox[2], "tws": 12.0, "twd": 200.0, "confidence": 1.0}]


def test_build_wind_stores_grid_over_track_area_and_hours():
    T.save_track("r1", boatlog(n=80))
    seen = {}

    def wf(bbox, a, b, log):
        seen.update(bbox=bbox, a=a, b=b)
        return FakeWF()
    r = RP.build_wind("r1", T0, windfield=wf)
    assert r["ok"] and seen["a"] == T0 and seen["b"] == T0 + 79 * 5
    assert seen["bbox"][0] > 43.0 > seen["bbox"][1]
    g = RP.build("r1", T0)["wind_grid"]
    assert g and g["times"][0] == round(T0) and g["frames"][0][0]["tws"] == 12.0


def test_build_wind_says_when_no_wind():
    T.save_track("r1", boatlog())

    class Empty:
        loaded = False
    assert "No wind" in RP.build_wind("r1", T0, windfield=lambda *a: Empty())["note"]


def test_no_race_specific_values():
    src = open(RP.__file__).read().lower()
    for word in ("sr33", "huron", "bayview", "hooligan", "sorc"):
        assert word not in src
