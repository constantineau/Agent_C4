"""Tack and gybe loss (#375) — `app/maneuvers.py` on synthetic tracks with KNOWN losses.

The generator sails a boat at a fixed true wind angle and speed, turns it through the wind at
known times, drops the speed through the turn and builds it back. The true loss is integrated
from the same per-second velocities that lay down the positions, so the test knows exactly how
many metres (and seconds) each manoeuvre cost. The module must find each turn, call it a tack or
a gybe, and get the loss close to that number — with noise, with the wind channels missing
(GPS-only, as from a GPX or YB track), with a hole in the log, with an instrument fault, and on a
dead run whose wind angle flickers across 180° without anybody gybing.

Run: docker run --rm -v $PWD:/repo -w /repo/vps/lab -e PYTHONPATH=/repo/vps/lab:/repo c4-ci-lab \
       python -m pytest -q test_maneuvers.py
"""
import math
import random

import pytest

from app import maneuvers as M

T0 = 1_790_000_000.0
LAT0, LON0 = 26.1, -80.0
M_PER_DEG_LAT = 111_195.0
KN = 1852.0 / 3600.0


def _wrap180(d):
    return ((d + 180.0) % 360.0) - 180.0


def sail(plan, twd=0.0, v=6.5, twa=42.0, dt=1.0, turn_s=8.0, vmin_frac=0.55, rec_s=25.0,
         current=(0.0, 0.0), twd_drift_deg_per_h=0.0, seed=None, noise=None, start_side=1):
    """A synthetic sail. `plan` = list of (t_offset_s, kind) turns, kind 'tack' or 'gybe'; the track
    ends 240 s after the last turn. Returns (fixes, truth) where truth[i] = {t, kind, loss_m,
    loss_s} for each turn, integrated from the same velocities the positions came from.

    Upwind for tacks, downwind for gybes (`twa` is the upwind angle; gybes sail at 180-twa+...
    set by the caller through `twa`). `start_side` +1 = starboard (wind over the right side)."""
    rng = random.Random(seed)
    end = (plan[-1][0] if plan else 0) + 240.0
    n = int(end / dt) + 1
    side = start_side
    x = y = 0.0
    fixes, truth = [], []
    turns = list(plan)
    # per-turn state
    cur_turn = None
    vmg_true = []                 # (t, vmg along the progress axis, m/s) per step
    for i in range(n):
        t = i * dt
        w = twd + twd_drift_deg_per_h * t / 3600.0
        if turns and cur_turn is None and t >= turns[0][0]:
            cur_turn = {"t": turns[0][0], "kind": turns[0][1], "from": side}
            turns.pop(0)
        speed, ang = v, twa
        if cur_turn is not None:
            k = (t - cur_turn["t"]) / turn_s           # 0..1 through the turn
            if k <= 1.0:
                # heading swings linearly from the old side through the wind to the new side
                a_from, a_to = twa * cur_turn["from"], -twa * cur_turn["from"]
                signed = a_from + (a_to - a_from) * k if cur_turn["kind"] == "tack" else None
                if cur_turn["kind"] == "gybe":
                    # through 180: angle magnitude goes twa -> 180 -> twa, side flips at k=0.5
                    mag = twa + (180.0 - twa) * (1 - abs(2 * k - 1))
                    signed = mag * (cur_turn["from"] if k < 0.5 else -cur_turn["from"])
                ang_signed = signed
                speed = v * (1.0 - (1.0 - vmin_frac) * k)
            else:
                side = -cur_turn["from"]
                ang_signed = twa * side
                r = (t - cur_turn["t"] - turn_s) / rec_s
                speed = v * (vmin_frac + (1.0 - vmin_frac) * min(1.0, r))
                if r >= 1.0:
                    cur_turn = None
        else:
            ang_signed = twa * side
        heading = (w - ang_signed) % 360.0            # signed TWA = TWD - heading
        vx = speed * KN * math.sin(math.radians(heading)) + current[0]
        vy = speed * KN * math.cos(math.radians(heading)) + current[1]
        # progress axis: toward the wind upwind, away from it downwind
        up = abs(_wrap180(ang_signed)) < 90 if cur_turn is None else (twa < 90)
        ux, uy = math.sin(math.radians(w)), math.cos(math.radians(w))
        if not up:
            ux, uy = -ux, -uy
        vmg_true.append((t, (speed * KN * math.sin(math.radians(heading))) * ux
                         + (speed * KN * math.cos(math.radians(heading))) * uy))
        sog_x, sog_y = vx, vy
        sog = math.hypot(sog_x, sog_y) / KN
        cog = math.degrees(math.atan2(sog_x, sog_y)) % 360.0
        f = {"t": T0 + t,
             "lat": LAT0 + y / M_PER_DEG_LAT,
             "lon": LON0 + x / (M_PER_DEG_LAT * math.cos(math.radians(LAT0))),
             "sog": round(sog, 2), "cog": round(cog, 1),
             "tws": 12.0, "twa": round(ang_signed % 360.0, 1), "stw": round(speed, 2)}
        if noise:
            f["cog"] = round((f["cog"] + rng.gauss(0, noise.get("cog", 0))) % 360.0, 1)
            f["twa"] = round((f["twa"] + rng.gauss(0, noise.get("twa", 0))) % 360.0, 1)
            f["stw"] = round(max(0.0, f["stw"] + rng.gauss(0, noise.get("spd", 0))), 2)
            f["sog"] = round(max(0.0, f["sog"] + rng.gauss(0, noise.get("spd", 0))), 2)
            jit = noise.get("pos_m", 0)
            if jit:
                f["lat"] += rng.gauss(0, jit) / M_PER_DEG_LAT
                f["lon"] += rng.gauss(0, jit) / (M_PER_DEG_LAT * math.cos(math.radians(LAT0)))
        fixes.append(f)
        x += vx * dt
        y += vy * dt
    # truth: steady VMG minus actual, integrated over turn start .. recovered (+ margins)
    base = v * KN * math.cos(math.radians(twa if twa < 90 else 180 - twa))
    for (tt, kind) in plan:
        lost = sum((base - g) * dt for (t, g) in vmg_true if tt - 5 <= t < tt + turn_s + rec_s + 5)
        truth.append({"t": T0 + tt, "kind": kind, "loss_m": lost, "loss_s": lost / base})
    return fixes, truth


def strip_wind(fixes):
    return [{**f, "tws": None, "twa": None, "stw": None} for f in fixes]


def thin(fixes, every):
    return fixes[::every]


def counted(res):
    return [m for m in res["maneuvers"] if m["counted"]]


# ---- the basics -------------------------------------------------------------------------------
def test_clean_tacks_found_typed_and_loss_close_to_truth():
    fixes, truth = sail([(300, "tack"), (600, "tack"), (900, "tack")])
    res = M.analyze(fixes)
    assert res["available"] is True
    assert res["basis"] == "instruments"
    ms = res["maneuvers"]
    assert [m["type"] for m in ms] == ["tack"] * 3
    for m, tr in zip(ms, truth):
        assert abs(m["t"] - tr["t"]) < 8          # the head-to-wind moment, within a few seconds
        assert m["loss_m"] == pytest.approx(tr["loss_m"], rel=0.12, abs=3)
        assert m["loss_s"] == pytest.approx(tr["loss_s"], rel=0.12, abs=1)
        assert m["counted"] is True
    assert [m["from_side"] for m in ms] == ["starboard", "port", "starboard"]
    assert [m["to_side"] for m in ms] == ["port", "starboard", "port"]


def test_entry_min_exit_speed_and_recovery_time():
    fixes, _ = sail([(300, "tack")], v=6.5, vmin_frac=0.5, turn_s=8, rec_s=30)
    m = M.analyze(fixes)["maneuvers"][0]
    assert m["entry_kn"] == pytest.approx(6.5, abs=0.3)
    assert m["min_kn"] == pytest.approx(3.25, abs=0.3)
    assert m["exit_kn"] < 4.5                       # still slow when the turn is over
    # 90% of 6.5 is reached 0.8 of the way through a 30 s build, which starts 8 s after the
    # tack began; head to wind is 4 s in -> about 4 + 24 = 28 s after head to wind
    assert m["recover_s"] == pytest.approx(28, abs=5)
    assert m["speed_source"] == "stw"


def test_gybes_downwind():
    fixes, truth = sail([(300, "gybe"), (600, "gybe")], twa=150.0, v=8.0, vmin_frac=0.75,
                        turn_s=10, rec_s=15)
    res = M.analyze(fixes)
    ms = res["maneuvers"]
    assert [m["type"] for m in ms] == ["gybe", "gybe"]
    for m, tr in zip(ms, truth):
        assert m["loss_m"] == pytest.approx(tr["loss_m"], rel=0.15, abs=3)
        assert m["vmg_before_kn"] > 0                # progress downwind counts positive


def test_bigger_loss_reads_bigger():
    small, _ = sail([(300, "tack")], vmin_frac=0.8, rec_s=10)
    big, _ = sail([(300, "tack")], vmin_frac=0.4, rec_s=40)
    a = M.analyze(small)["maneuvers"][0]["loss_m"]
    b = M.analyze(big)["maneuvers"][0]["loss_m"]
    assert b > a * 2


def test_current_cancels_out_of_the_loss():
    """A steady current moves the baseline and the turn alike — the loss stays the same."""
    still, truth = sail([(300, "tack")])
    tide, _ = sail([(300, "tack")], current=(0.4, -0.3))
    a = M.analyze(still)["maneuvers"][0]["loss_m"]
    b = M.analyze(tide)["maneuvers"][0]["loss_m"]
    assert b == pytest.approx(a, abs=4)
    assert a == pytest.approx(truth[0]["loss_m"], rel=0.12, abs=3)


# ---- robustness ------------------------------------------------------------------------------
def test_noisy_instruments_still_find_every_tack_and_nothing_else():
    fixes, truth = sail([(300, "tack"), (600, "tack"), (900, "tack"), (1200, "tack")], seed=7,
                        noise={"cog": 4, "twa": 5, "spd": 0.25, "pos_m": 1.5})
    res = M.analyze(fixes)
    assert [m["type"] for m in res["maneuvers"]] == ["tack"] * 4
    for m, tr in zip(res["maneuvers"], truth):
        assert m["loss_m"] == pytest.approx(tr["loss_m"], rel=0.15, abs=4)


def test_dead_run_flicker_is_not_a_gybe():
    """Running square, the wind angle wanders across 180° — no turn, so no gybe."""
    fixes, _ = sail([(10_000, "gybe")], twa=176.0, v=7.0)   # plan only sets the length
    fixes = fixes[:1500]
    rng = random.Random(3)
    for f in fixes:
        f["twa"] = round((180.0 + rng.gauss(0, 6)) % 360.0, 1)
        f["cog"] = round((180.0 + rng.gauss(0, 4)) % 360.0, 1)
    res = M.analyze(fixes)
    assert res["maneuvers"] == []


def test_sparse_boat_log_three_second_fixes():
    """The boat log is thinned to a few thousand points — about 3 s apart on a long race."""
    fixes, truth = sail([(300, "tack"), (600, "tack")])
    res = M.analyze(thin(fixes, 3))
    assert [m["type"] for m in res["maneuvers"]] == ["tack", "tack"]
    for m, tr in zip(res["maneuvers"], truth):
        assert m["loss_m"] == pytest.approx(tr["loss_m"], rel=0.2, abs=5)


def test_tracker_fixes_too_far_apart_say_so():
    fixes, _ = sail([(300, "tack"), (600, "tack")])
    res = M.analyze(thin(fixes, 120))           # a YB-style fix every 2 min
    assert res["available"] is False
    assert "apart" in res["note"]


def test_gap_in_the_log_keeps_the_turn_but_not_its_loss():
    fixes, truth = sail([(300, "tack"), (600, "tack")])
    t_hole = (T0 + 600 + 5, T0 + 600 + 35)
    holed = [f for f in fixes if not (t_hole[0] <= f["t"] <= t_hole[1])]
    res = M.analyze(holed)
    ms = res["maneuvers"]
    assert len(ms) == 2
    assert ms[0]["loss_m"] is not None and ms[0]["counted"]
    assert ms[1]["loss_m"] is None and not ms[1]["counted"]
    assert any("gap" in fl for fl in ms[1]["flags"])


def test_instrument_fault_window_is_flagged_and_left_out_of_averages():
    fixes, _ = sail([(300, "tack"), (600, "tack")])
    trust = {"available": True, "danger": {"heading": [(T0 + 580, T0 + 640)], "attitude": []}}
    res = M.analyze(fixes, trust=trust)
    a, b = res["maneuvers"]
    assert a["counted"] and a["trusted"]
    assert not b["trusted"] and not b["counted"]
    assert any("instrument" in fl for fl in b["flags"])
    assert res["summary"]["tack"]["n"] == 1


def test_missing_channels_on_some_fixes():
    """The paddlewheel and the wind drop out for a stretch: speed falls back to SOG, the wind
    angle to the course against the wind direction measured either side."""
    fixes, truth = sail([(300, "tack"), (600, "tack")])
    for f in fixes:
        if T0 + 500 <= f["t"] <= T0 + 700:
            f["twa"] = f["tws"] = f["stw"] = None
    res = M.analyze(fixes)
    assert [m["type"] for m in res["maneuvers"]] == ["tack", "tack"]
    assert res["maneuvers"][1]["loss_m"] == pytest.approx(truth[1]["loss_m"], rel=0.15, abs=4)
    assert res["maneuvers"][1]["speed_source"] == "sog"


def test_turns_too_close_together_have_no_loss():
    fixes, _ = sail([(300, "tack"), (330, "tack"), (700, "tack")])
    res = M.analyze(fixes)
    ms = res["maneuvers"]
    assert len(ms) == 3
    assert ms[2]["counted"]
    # the quick pair can't have a steady stretch between them
    assert any(not m["counted"] for m in ms[:2])


def test_tack_into_bear_away_is_a_rounding_not_a_loss():
    """Beat to the mark, tack and bear away onto a run on the other gybe: one side change, but
    upwind before and downwind after — VMG can't measure it, so no loss is claimed."""
    up, _ = sail([(10_000, "tack")], v=6.5)
    up = up[:300]
    last = up[-1]
    down, _ = sail([(10_000, "gybe")], twa=150, v=8.0, start_side=-1)
    down = down[:300]
    dx_lat, dx_lon = last["lat"] - LAT0, last["lon"] - LON0
    for i, f in enumerate(down):
        f["t"] = last["t"] + 1 + i
        f["lat"] += dx_lat
        f["lon"] += dx_lon
    res = M.analyze(up + down)
    assert len(res["maneuvers"]) == 1
    m = res["maneuvers"][0]
    assert m["loss_m"] is None and not m["counted"]
    assert any("mark" in fl for fl in m["flags"])


# ---- GPS only (GPX / YB) ----------------------------------------------------------------------
def test_gps_only_finds_tacks_with_low_confidence():
    fixes, truth = sail([(300, "tack"), (600, "tack"), (900, "tack"), (1200, "tack")])
    res = M.analyze(strip_wind(fixes))
    assert res["basis"] == "gps"
    assert res["confidence"] == "low"
    assert res["note"]
    ms = res["maneuvers"]
    assert len(ms) == 4
    assert all(m["type"] == "tack" for m in ms)
    for m, tr in zip(ms, truth):
        assert m["loss_m"] == pytest.approx(tr["loss_m"], rel=0.2, abs=5)
        assert m["confidence"] == "low"


def test_gps_only_with_a_wind_hint_types_gybes():
    fixes, _ = sail([(300, "gybe"), (600, "gybe"), (900, "gybe")], twa=150.0, v=8.0,
                    vmin_frac=0.75, turn_s=10, rec_s=15)
    res = M.analyze(strip_wind(fixes), twd_hint=10.0)
    assert [m["type"] for m in res["maneuvers"]] == ["gybe"] * 3


def test_gps_only_follows_a_slow_wind_shift():
    fixes, _ = sail([(t, "tack") for t in range(300, 3900, 300)], twd_drift_deg_per_h=30.0)
    res = M.analyze(strip_wind(fixes))
    assert len(res["maneuvers"]) == 12
    assert all(m["type"] == "tack" for m in res["maneuvers"])


# ---- summary ---------------------------------------------------------------------------------
def test_summary_by_side_and_sail_setup():
    fixes, _ = sail([(300, "tack"), (600, "tack"), (900, "tack"), (1200, "tack")])
    sail_log = [{"t": T0, "flying": ["M", "J1"]}, {"t": T0 + 750, "flying": ["M", "J2"]}]
    res = M.analyze(fixes, sail_log=sail_log)
    assert [m["config"] for m in res["maneuvers"]] == ["J1+M", "J1+M", "J2+M", "J2+M"]
    s = res["summary"]
    assert s["tack"]["n"] == 4
    assert s["tack"]["loss_s"] > 0
    groups = {(g["type"], g["by"], g["key"]): g for g in s["groups"]}
    assert groups[("tack", "side", "port")]["n"] == 2
    assert groups[("tack", "side", "starboard")]["n"] == 2
    assert groups[("tack", "config", "J1+M")]["n"] == 2
    assert groups[("tack", "config", "J2+M")]["n"] == 2


def test_too_little_data():
    res = M.analyze([])
    assert res["available"] is False
    res = M.analyze([{"t": T0, "lat": LAT0, "lon": LON0}])
    assert res["available"] is False


def test_shape_is_json_clean():
    import json
    fixes, _ = sail([(300, "tack")])
    res = M.analyze(fixes)
    json.dumps(res, allow_nan=False)
    m = res["maneuvers"][0]
    for k in ("id", "type", "t", "t_start", "t_end", "window", "lat", "lon", "from_side",
              "to_side", "loss_m", "loss_s", "entry_kn", "min_kn", "exit_kn", "recover_s",
              "vmg_before_kn", "vmg_after_kn", "twd", "config", "basis", "confidence",
              "trusted", "counted", "flags"):
        assert k in m, k


# ---- the endpoint ----------------------------------------------------------------------------
def test_endpoint_reads_the_stored_track(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import auth, main, track
    monkeypatch.setattr(track, "TRACK_DIR", str(tmp_path))
    fixes, _ = sail([(300, "tack"), (600, "tack")])
    win = {"start_ts": T0, "end_ts": T0 + 900}
    track.save_track("t-race", {"source": "boatlog", "fixes": fixes, "window": win,
                                "sail_log": [{"t": T0, "flying": ["M", "J1"]}]})
    client = TestClient(main.app)
    hdr = {"Authorization": "Bearer " + auth.issue_token()}
    r = client.get(f"/api/debrief/maneuvers?race_id=t-race&recording={T0}", headers=hdr).json()
    assert r["available"] and r["recording"] == T0 and r["source"] == "boatlog"
    assert [m["type"] for m in r["maneuvers"]] == ["tack", "tack"]
    assert r["maneuvers"][0]["config"] == "J1+M"
    r = client.get("/api/debrief/maneuvers?race_id=nothing", headers=hdr).json()
    assert r["available"] is False
    assert client.get("/api/debrief/maneuvers", headers=hdr).status_code == 422
    # a practice sail (no race) is filed under its own key
    track.save_track(None, {"source": "boatlog", "fixes": fixes, "window": win})
    r = client.get(f"/api/debrief/maneuvers?no_race=1&recording={T0}", headers=hdr).json()
    assert r["available"] and r["race_id"] is None and len(r["maneuvers"]) == 2
