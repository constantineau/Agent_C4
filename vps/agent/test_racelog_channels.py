"""#374: logged channels over a window, one sample per step, best-ranked source per step.

Run: docker run --rm -v $PWD:/repo -w /repo/vps/agent -e PYTHONPATH=/repo/vps/agent:/repo \
       c4-ci-agent python -m pytest -q test_racelog_channels.py
"""
import math

import pytest

from app import racelog_channels as RC

KN, DEG = RC.KN, RC.DEG


def row(b, path, source, value=None, ang=None):
    if ang is not None:
        r = math.radians(ang)
        return {"b": b, "path": path, "source": source, "m": r, "s": math.sin(r), "c": math.cos(r)}
    return {"b": b, "path": path, "source": source, "m": value, "s": None, "c": None}


def no_rank(_path, _src):
    return None


def test_pick_step_keeps_points_under_cap():
    assert RC.pick_step(0, 7 * 3600, 4000) == 7          # 25200 s / 4000 -> 6.3 -> 7
    assert RC.pick_step(0, 60, 4000) == 1                # never below a second
    assert RC.pick_step(0, 7 * 3600, 4000, step_s=2.4) == 2


def test_units_and_one_row_per_step():
    rows = [row(100, "navigation.speedThroughWater", "a", 3.0),
            row(100, "environment.wind.speedTrue", "a", 5.0),
            row(100, "navigation.attitude.roll", "a", math.radians(-12)),
            row(105, "navigation.speedThroughWater", "a", 3.5)]
    out = RC.assemble(rows, no_rank)
    assert [s["t"] for s in out] == [100.0, 105.0]
    assert out[0]["bsp"] == round(3.0 * KN, 2) and out[0]["tws"] == round(5.0 * KN, 2)
    assert out[0]["heel"] == pytest.approx(-12, abs=0.05)
    assert out[1]["tws"] is None                         # missing in that step, not carried


def test_best_ranked_source_wins_unranked_is_kept():
    ranks = {"lead": 0, "backup": 1}
    rows = [row(0, "navigation.speedThroughWater", "backup", 2.0),
            row(0, "navigation.speedThroughWater", "lead", 3.0),
            row(5, "navigation.speedThroughWater", "mystery", 4.0)]
    out = RC.assemble(rows, lambda p, s: ranks.get(s))
    assert out[0]["bsp"] == round(3.0 * KN, 2)
    assert out[1]["bsp"] == round(4.0 * KN, 2)           # unranked still counts when alone


def test_angles_signed_twa_and_wrapped_directions():
    rows = [row(0, "environment.wind.angleTrueWater", "a", ang=-40),
            row(0, "navigation.headingTrue", "a", ang=350),
            row(0, "navigation.courseOverGroundTrue", "a", ang=-10)]
    out = RC.assemble(rows, no_rank)[0]
    assert out["twa"] == pytest.approx(-40, abs=0.05)    # port tack stays negative
    assert out["cog"] == pytest.approx(350, abs=0.05)    # 0..360
    # no directionTrue on the bus: TWD = heading + TWA
    assert out["twd"] == pytest.approx(310, abs=0.05)


def test_instrument_twd_preferred_over_derived():
    rows = [row(0, "environment.wind.angleTrueWater", "a", ang=40),
            row(0, "navigation.headingTrue", "a", ang=0),
            row(0, "environment.wind.directionTrue", "a", ang=45)]
    assert RC.assemble(rows, no_rank)[0]["twd"] == pytest.approx(45, abs=0.05)


def test_cancelled_direction_is_none_and_unknown_paths_ignored():
    rows = [{"b": 0, "path": "navigation.headingTrue", "source": "a", "m": 0, "s": 0.0, "c": 0.0},
            {"b": 0, "path": "some.other.path", "source": "a", "m": 1, "s": None, "c": None},
            row(0, "navigation.speedOverGround", "a", 1.0)]
    out = RC.assemble(rows, no_rank)[0]
    assert out["hdg"] is None and out["sog"] == round(KN, 2)
