"""#257 + #65: the layline commit, on a beat in light air and on a run.

#257: at 5 kn with the mark 42-44 deg off the wind, the commit's slack plus the "don't sail below
the mark" filter left only headings inside the no-go zone, all at speed 0, and the leg stranded
(the 5 kn W/L with the wind from 045 stopped at the leeward mark). The commit must never leave a
node with only zero-speed headings.
#65: the run commit compared the angle off dead downwind with the run TWA (measured from the
wind), so it never fired.

Steady wind, injected polars, no network. The wind is from 045 so the angles wrap off north.
"""
import math

import pytest

from app import optimizer as O

TWD = 45.0
LAT0, LON0 = 44.0, -82.3

# light air: nothing sails below 44 deg TWA, best beat VMG right on that edge
P5 = [(5.0, a, s) for a, s in {44: 3.0, 60: 3.2, 90: 3.6, 135: 3.3, 180: 2.6}.items()]
# a moderate breeze: beat VMG ~42, run VMG ~150
P8 = [(8.5, a, s) for a, s in {30: 2.8, 40: 4.2, 42: 4.4, 50: 4.9, 60: 5.3, 75: 5.7, 90: 5.9,
                               110: 5.8, 135: 5.2, 150: 4.6, 165: 4.0, 180: 3.2}.items()]


class Steady:
    def __init__(self, tws, twd):
        self.tws, self.twd = tws, twd
        self.loaded = True
        self.series, self.meta = {}, []

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}

    def status(self):
        return {"models": []}


@pytest.fixture(autouse=True)
def _commit_on(monkeypatch):
    monkeypatch.setattr(O, "LAYLINE_COMMIT", True)


def _speed(P, tws, h):
    return O._polar_speed(P, tws, abs(O._wrap180(h - TWD)))


@pytest.mark.parametrize("off", [40.0, 42.0, 42.5, 43.0, 43.5, 44.0, 45.0, 46.0,
                                 -42.0, -43.0, -44.0])
def test_light_air_beat_commit_keeps_a_sailable_heading(off):
    fan = list(range(0, 360, 5))
    kept = O._layline_fan(P5, 5.0, TWD, "beat", (TWD + off) % 360, fan)
    assert kept
    assert any(_speed(P5, 5.0, h) >= 0.3 for h in kept), kept


def test_commit_never_leaves_only_zero_speed_headings():
    # a fan that holds nothing sailable on the laying board: the commit falls back to the best
    # heading the boat can sail rather than hand back only dead headings
    bmark = (TWD + 43.0) % 360
    dead = [(TWD + d) % 360 for d in (10, 20, 30, 40, 43)]
    sailable = [(TWD - 60) % 360, (TWD - 90) % 360]
    kept = O._layline_fan(P5, 5.0, TWD, "beat", bmark, dead + sailable)
    assert any(_speed(P5, 5.0, h) >= 0.3 for h in kept), kept


@pytest.mark.parametrize("off", [43.0, -43.0])
def test_light_air_beat_leg_reaches_the_mark(off):
    # the #257 strand: 5 kn, wind from 045, the windward mark 43 deg off the wind, 3 nm away
    mlat, mlon = O._advance(LAT0, LON0, (TWD + off) % 360, 3.0)
    leg = O.route_leg(Steady(5.0, TWD), P5, LAT0, LON0, 0, mlat, mlon, deadline=None)
    assert leg["reached"], f"short {leg.get('short_nm')} nm"


def test_run_commit_fires_past_the_layline():
    # 8.5 kn: run VMG ~150 TWA, so the run layline is ~30 deg off dead downwind. A mark 35 deg off
    # dead downwind is past it: only the gybe that lays it stays.
    down = (TWD + 180.0) % 360
    fan = list(range(0, 360, 12))
    kept = O._layline_fan(P8, 8.5, TWD, "run", (down + 35.0) % 360, fan)
    assert kept and len(kept) < len(fan)
    assert all(O._wrap180(h - down) >= 0 for h in kept), kept
    kept = O._layline_fan(P8, 8.5, TWD, "run", (down - 35.0) % 360, fan)
    assert kept and all(O._wrap180(h - down) < 0 for h in kept), kept


def test_run_commit_leaves_both_gybes_inside_the_layline():
    down = (TWD + 180.0) % 360
    fan = list(range(0, 360, 12))
    assert O._layline_fan(P8, 8.5, TWD, "run", (down + 15.0) % 360, fan) == fan


def test_run_leg_past_the_layline_is_one_gybe():
    down = (TWD + 180.0) % 360
    mlat, mlon = O._advance(LAT0, LON0, (down + 35.0) % 360, 4.0)
    leg = O.route_leg(Steady(8.5, TWD), P8, LAT0, LON0, 0, mlat, mlon, deadline=None)
    assert leg["reached"]
    assert leg["tacks"] == 0, leg["tacks"]


def test_commit_fires_at_all_on_a_run_leg(monkeypatch):
    # route_leg actually reaches the run branch of the commit (not just the helper)
    seen = []
    real = O._layline_fan

    def spy(P, tws, twd, pos, bmark, fan):
        out = real(P, tws, twd, pos, bmark, fan)
        seen.append((pos, len(out) < len(fan)))
        return out

    monkeypatch.setattr(O, "_layline_fan", spy)
    down = (TWD + 180.0) % 360
    mlat, mlon = O._advance(LAT0, LON0, (down + 35.0) % 360, 4.0)
    O.route_leg(Steady(8.5, TWD), P8, LAT0, LON0, 0, mlat, mlon, deadline=None)
    assert any(pos == "run" and dropped for pos, dropped in seen), seen[:5]


def test_light_air_course_wind_045_finishes():
    # the #257 shape end to end: a W/L at 5 kn, wind from 045, leeward mark set so the beat back
    # up sits 43 deg off the wind
    wlat, wlon = O._advance(LAT0, LON0, (TWD + 43.0) % 360, 2.0)
    d = {"race_id": "t257", "zones": [], "courses": [{
        "id": "c", "start": {"lat": LAT0, "lon": LON0},
        "marks": [{"name": "W", "type": "mark", "lat": wlat, "lon": wlon},
                  {"name": "L", "type": "mark", "lat": LAT0, "lon": LON0 + 0.0002}],
        "finish": {"points": [{"lat": wlat, "lon": wlon + 0.0002}]}}]}
    r = O.optimize_course(d, "c", 0, Steady(5.0, TWD), polar=P5, avoid=False,
                          emit_exploration=False, time_budget_s=120)
    assert all(leg["reached"] for leg in r["legs"]), [leg.get("short_nm") for leg in r["legs"]]
    assert not math.isnan(r.get("total_tacks", 0))
