"""#258 — the router never leaves the obstacle map's box.

Outside the box nothing was checked (no land, zones or depths), so a route there can't be proven
safe. `ObstacleField.blocked` / `crosses` read outside the box as blocked, and the router tests
every step with `crosses`, so it is kept inside. Locked here at the router level (constant wind,
an injected polar, no network):
  1) a beat in a narrow box: unconstrained, its best route tacks far outside the box; with the
     obstacle map it short-tacks inside it, every point inside, and still reaches the mark;
  2) the course optimizer: the same beat comes back complete with no "Route leaves the chart
     area" problem;
  3) open water (an obstacle map with nothing in it, or none at all): the route is unchanged.
"""
import math
import os

import pytest

os.environ.setdefault("DEPTH_GRIDS", "none")

from app import optimizer as O  # noqa: E402
from app.geo import obstacles as OB  # noqa: E402


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


# a generic keelboat polar at 8.5 kn (beat VMG ~42 deg)
ROWS = {30: 2.8, 40: 4.2, 42: 4.4, 50: 4.9, 60: 5.3, 75: 5.7, 90: 5.9,
        110: 5.8, 135: 5.2, 150: 4.6, 165: 4.0, 180: 3.2}
P = [(8.5, a, s) for a, s in ROWS.items()]
LAT0, LON0 = 44.0, -82.3
DLON = 1.0 / (60.0 * math.cos(math.radians(LAT0)))     # deg of longitude per nm
WIND = Steady(8.5, 0.0)                                 # from the north: the leg is a dead beat
MARK = (LAT0 + 4 / 60.0, LON0)                          # 4 nm dead upwind
HALF_W = 0.6                                            # the box is 1.2 nm wide (nm each side)
BBOX = (LAT0 + 5 / 60.0, LAT0 - 1 / 60.0, LON0 - HALF_W * DLON, LON0 + HALF_W * DLON)


def _east_nm(lon):
    return (lon - LON0) / DLON


def _rect(s_nm, n_nm, w_nm, e_nm):
    return {"name": "z", "type": "exclusion", "geometry": {
        "north": LAT0 + n_nm / 60.0, "south": LAT0 + s_nm / 60.0,
        "west": LON0 + w_nm * DLON, "east": LON0 + e_nm * DLON}}


def _definition(zones):
    return {"race_id": "t258", "zones": zones, "courses": [{
        "id": "c", "start": {"lat": LAT0, "lon": LON0},
        "marks": [{"name": "W", "type": "mark", "lat": MARK[0], "lon": MARK[1]}],
        "finish": {"points": [{"lat": MARK[0] + 0.0001, "lon": MARK[1]}]}}]}


# a small zone in a far corner, off the route: the map is loaded (active) but open in the middle
D = _definition([_rect(-0.9, -0.7, 0.4, 0.55)])


@pytest.fixture(scope="module")
def field():
    f = OB.build_for_course(D, "c", BBOX, coastline_on=False, use_cache=False)
    assert f.active and f.bbox == BBOX
    return f


def _leg(obstacles):
    return O.route_leg(WIND, P, LAT0, LON0, 0, MARK[0], MARK[1], obstacles=obstacles)


def test_unconstrained_beat_leaves_the_box():
    leg = _leg(None)
    assert leg["reached"]
    widest = max(abs(_east_nm(p["lon"])) for p in leg["path"])
    assert widest > HALF_W + 0.3, f"the repro needs a route that leaves the box ({widest:.2f} nm)"


def test_beat_stays_inside_the_box_and_reaches_the_mark(field):
    leg = _leg(field)
    assert leg["reached"], leg.get("short_nm")
    outside = [p for p in leg["path"] if not field.inside(p["lat"], p["lon"])]
    assert not outside, f"{len(outside)} points outside the box"
    pts = [(p["lat"], p["lon"]) for p in leg["path"]]
    assert not any(field.crosses(*a, *b) for a, b in zip(pts, pts[1:]))


def test_optimizer_route_is_complete_and_inside(field):
    r = O.optimize_course(D, "c", 0, WIND, polar=P, obstacles=field,
                          emit_exploration=False, time_budget_s=60)
    assert r["complete"] and not r.get("incomplete"), r.get("incomplete")
    codes = {p["code"] for p in (r.get("safety") or {}).get("problems", [])}
    assert "box" not in codes
    assert all(field.inside(p["lat"], p["lon"]) for p in r["path"])


def test_open_water_route_unchanged():
    empty = OB.build_for_course(_definition([]), "c", BBOX, coastline_on=False, use_cache=False)
    assert not empty.active                      # nothing loaded: no box limit
    a, b = _leg(None), _leg(empty)
    assert [(p["lat"], p["lon"]) for p in a["path"]] == [(p["lat"], p["lon"]) for p in b["path"]]
    assert a["eta"] == b["eta"]
