"""#379: reverse isochrones and route criticality (the time-loss band on the Gameplan map).

Made-up wind (no network, no shoreline data), sr33's polar via polar_fixture:
  1) uniform wind, beat and run: the band mirrors exactly about the rhumb line, the rhumb is in
     it, and water well outside the laylines is not;
  2) land across one side of the beat: no band on the land, the open side keeps its band;
  2b) a building, rotating, uneven wind: the reverse run (from the forward ETA) gets back to the
     start when the forward run left it, i.e. both price a step on the wind where it leaves;
  2c) tacks cost time: inside the laylines the band is one tack worse than on them;
  3) optimize_course: off by default (no key, nothing computed); on, every routed leg gets a grid.
"""
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("INGESTED_DIR", _tmp)
os.environ.pop("ANTHROPIC_API_KEY", None)

from app import criticality as CRIT  # noqa: E402
from app import optimizer as OPT  # noqa: E402
from app import polars as POL  # noqa: E402
from app.geo import obstacles as OB  # noqa: E402
import polar_fixture  # noqa: E402

polar_fixture.install()
P = POL.polars_stw(polar_fixture.sr33())
T0 = 1_780_000_000.0
S = (44.0, -82.0)
M = (44.25, -82.0)          # 15 nm due north


class WF:
    loaded = True

    def __init__(self, tws=12.0, twd=0.0):
        self.tws, self.twd = tws, twd

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}

    def status(self):
        return {"models": []}


def at(b, i, j):
    return b["loss_min"][j * b["nx"] + i]


def band(twd, obstacles=None):
    b = CRIT.leg_band(WF(12.0, twd), P, *S, T0, *M, T0 + 4 * 3600, obstacles=obstacles)
    assert b is not None
    return b


@pytest.mark.parametrize("twd,what", [(0.0, "beat"), (180.0, "run")])
def test_uniform_band_mirrors_about_the_rhumb(twd, what):
    b = band(twd)
    nx, ny = b["nx"], b["ny"]
    mid = nx // 2
    assert abs(b["lon0"] + mid * b["dlon"] - S[1]) < 1e-6, "the rhumb is the middle column"
    for j in range(ny):
        for i in range(nx):
            a, c = at(b, i, j), at(b, nx - 1 - i, j)
            assert (a is None) == (c is None), (what, i, j, a, c)
            if a is not None:
                assert abs(a - c) < 0.05, (what, i, j, a, c)
    # the rhumb (start to mark) is inside the 5-min band
    j_s = round((S[0] - b["lat0"]) / b["dlat"])
    j_m = round((M[0] - b["lat0"]) / b["dlat"])
    rhumb = [at(b, mid, j) for j in range(j_s, j_m + 1)]
    assert all(v is not None and v <= 5 for v in rhumb), rhumb
    assert min(v for v in b["loss_min"] if v is not None) == 0.0
    # the 5-min band is the layline box: at the leg's midpoint it reaches about half the leg's
    # length off to each side (beat/run angles of ~40-50 deg off the wind), not to the grid's edge
    j_mid = (j_s + j_m) // 2
    inside = [i for i in range(nx) if at(b, i, j_mid) is not None and at(b, i, j_mid) <= 5]
    half_nm = (max(inside) - mid) * b["cell_nm"]
    assert 0.4 * 7.5 <= half_nm <= 1.3 * 7.5, half_nm
    assert at(b, 0, j_mid) is None or at(b, 0, j_mid) > 5
    assert 0 < b["width_nm"]["5"] <= b["width_nm"]["10"] <= b["width_nm"]["20"]


def _land_field(n, s, w, e):
    """An obstacle field over a wide box, open water except one blocked rectangle."""
    f = OB.ObstacleField((44.8, 43.5, -82.8, -81.2))
    f.active = True
    f.land = "ok"
    for iy in range(f.ny):
        for ix in range(f.nx):
            lat = f.s + (iy + 0.5) * f.res
            lon = f.w + (ix + 0.5) * f.res
            if s <= lat <= n and w <= lon <= e:
                f.mask[iy * f.nx + ix] = 1
    return f


def test_land_on_one_side_shows_no_band_there():
    land = (44.20, 44.05, -82.12, -82.03)             # west of the rhumb, inside the laylines
    obs = _land_field(*land)
    b = band(0.0, obstacles=obs)
    open_b = band(0.0)
    nx = b["nx"]
    on_land = mirrored_open = 0
    for j in range(b["ny"]):
        for i in range(nx):
            lat, lon = b["lat0"] + j * b["dlat"], b["lon0"] + i * b["dlon"]
            if obs.blocked(lat, lon):
                assert at(b, i, j) is None, ("band on land", lat, lon, at(b, i, j))
                if at(open_b, i, j) is not None:
                    on_land += 1
                    # the same water on the open (east) side still has its band
                    if at(b, nx - 1 - i, j) is not None:
                        mirrored_open += 1
    assert on_land > 10, "the land really covers part of the open-water band"
    assert mirrored_open == on_land
    # a corridor blocked by land: the band is no longer symmetric
    assert any((at(b, i, j) is None) != (at(b, nx - 1 - i, j) is None)
               for j in range(b["ny"]) for i in range(nx))


class Shifty:
    """6 kn building 3 kn an hour, veering 20 deg an hour, and 30 deg more per degree east."""
    loaded = True

    def wind_at(self, lat, lon, t):
        h = (t - T0) / 3600.0
        return (6.0 + 3.0 * h, (20.0 * h + 30.0 * (lon - S[1])) % 360)


def test_reverse_run_sails_the_forward_physics_on_a_changing_wind():
    first = CRIT.leg_band(Shifty(), P, *S, T0, *M, T0 + 3 * 3600, tack_s=30)
    fwd = first["grid_minutes"]
    b = CRIT.leg_band(Shifty(), P, *S, T0, *M, T0 + fwd * 60, tack_s=30)
    # pricing each reverse step on the wind at its END (the old way) came back ~9 min short here
    assert abs(b["reverse_minutes"] - fwd) <= 0.5, (b["reverse_minutes"], fwd)
    assert min(v for v in b["loss_min"] if v is not None) == 0.0


class Step:
    """18 kn from the north, dying at T0+1 h to 5 kn from the north-west. A step that straddles
    the drop flip-flops a plain fixed-point solve (leave late: light air, so leave early: fresh
    breeze, so leave late...) and four passes stopped on a departure that arrives too late."""
    loaded = True

    def wind_at(self, lat, lon, t):
        return (18.0, 0.0) if t < T0 + 3600 else (5.0, 315.0)


def test_reverse_departures_are_checked_on_a_step_change():
    """Every latest departure the reverse run keeps must be sailable: leaving that node then,
    priced like the forward run, some step reaches a kept state of the next node in time."""
    g = CRIT._Grid(*S, *M)
    at_, speed = CRIT._physics(Step(), P, None, None, 1.0, None, None)
    env = CRIT._cached(g, at_)
    edges = CRIT._edges(g, None)
    dst = g.snap(*M)
    eta = T0 + 2.0 * 3600
    bw = CRIT._reverse(edges, env, speed, dst, eta, eta - 6 * 3600, 30.0)
    assert len(bw) > 500
    for (v, side), tv in bw.items():
        if v == dst:
            continue
        e = env(v, tv)
        ok_ = False
        for u, nm, brg in edges[v]:
            got = speed(e, brg) if e else None
            if got is None:
                continue
            sp, hdg = got
            sd = CRIT._side(e, hdg)
            if (sd or side) != side:
                continue
            arr = tv + nm / sp * 3600.0
            for s2 in (-1, 0, 1):
                tu = bw.get((u, s2))
                if tu is not None and arr + (30.0 if s2 and sd and sd != s2 else 0.0) <= tu + 1.0:
                    ok_ = True
                    break
            if ok_:
                break
        assert ok_, (v, side, tv)


def test_back_search_tries_the_window_end():
    """The only departure that makes it is right at the start of the search window (`floor`),
    past where the doubling steps land: it must still be found, not dropped."""
    F, t = T0, T0 + 2000.0

    def env(v, x):            # wind only for the first 100 s of the window
        return (6.0, 0.0, 0.0, 0.0, 0.0) if F <= x <= F + 100 else None

    got = CRIT._latest_departure(env, lambda e, trk: (6.0, trk), (0, 0), 90.0, 1.0, t, 0, 30.0, F)
    assert got is not None, "the window's end was never tried"
    assert F <= got[0] <= F + 100


class Cross:
    """A 2 kn current setting west, everywhere."""
    loaded = True

    def current_at(self, lat, lon, t):
        return 270.0, 2.0


def test_cross_current_reads_the_polar_at_the_crab_heading():
    """As the router's closing board: the heading through the water that holds the track
    (`optimizer._crab_onto`), the polar read at that heading, the ground speed along the track."""
    import math
    _at, speed = CRIT._physics(WF(12.0, 90.0), P, Cross(), None, 1.0, None, None)
    e = _at(*S, T0)
    gs, hdg = speed(e, 0.0)                   # due north, wind from the east, current to the west
    crab = OPT._wrap180(hdg - 0.0)
    assert 5.0 < crab < 30.0, "crabs up-current (to the east)"
    tws, twd = e[0], e[1]                     # the wind over the water
    sp = OPT._polar_speed(P, tws, abs(OPT._wrap180(hdg - twd)))
    assert abs(math.sin(math.radians(crab)) * sp - 2.0) < 0.05, "the crab cancels the set"
    assert abs(gs - sp * math.cos(math.radians(crab))) < 1e-6
    # the old shortcut (polar at the track) would have given a different speed here
    sp_track = OPT._polar_speed(P, tws, abs(OPT._wrap180(0.0 - twd)))
    assert abs(math.sqrt(sp_track ** 2 - 4.0) - gs) > 0.01


def test_fast_changing_wind_is_flagged_rough():
    steady = CRIT.leg_band(WF(12.0, 0.0), P, *S, T0, *M, T0 + 3 * 3600)
    assert steady["rough"] is False and steady["wind_change"] == {"dir_deg": 0, "kn": 0.0}
    b = CRIT.leg_band(Shifty(), P, *S, T0, *M, T0 + 3 * 3600)
    assert b["rough"] is True and b["wind_change"]["dir_deg"] >= 25


def test_tacks_cost_time_inside_the_laylines():
    free = CRIT.leg_band(WF(12.0, 0.0), P, *S, T0, *M, T0 + 4 * 3600, tack_s=0)
    tk = CRIT.leg_band(WF(12.0, 0.0), P, *S, T0, *M, T0 + 4 * 3600, tack_s=120)
    mid = tk["nx"] // 2
    j_mid = round(((S[0] + M[0]) / 2 - tk["lat0"]) / tk["dlat"])
    assert at(free, mid, j_mid) == 0.0, "no tack cost: the whole layline box is as good"
    # mid-rhumb needs a tack to get there and another to leave: one more than the best route
    assert 1.5 <= at(tk, mid, j_mid) <= 2.5, at(tk, mid, j_mid)
    # on the layline (where the one-tack route sails) nothing is lost
    row = [at(tk, i, j_mid) for i in range(tk["nx"])]
    assert min(v for v in row[:mid] if v is not None) == 0.0
    assert tk["tack_s"] == 120


def test_tack_cost_defaults_to_the_boats():
    with OPT.boat_tack_cost(75):
        b = CRIT.leg_band(WF(12.0, 0.0), P, *S, T0, *M, T0 + 4 * 3600)
    assert b["tack_s"] == 75


COURSE = {"race_id": "crit", "name": "Crit", "zones": [], "courses": [{
    "id": "c", "start": {"lat": S[0], "lon": S[1]},
    "marks": [{"name": "Top", "type": "buoy", "lat": M[0], "lon": M[1], "rounding": "none"}],
    "finish": {"points": [{"lat": 44.25, "lon": -81.8}]}}]}


def test_optimize_course_opt_in():
    off = OPT.optimize_course(COURSE, "c", T0, WF(12.0, 0.0), avoid=False, emit_exploration=False)
    assert off["available"] and off["complete"]
    assert "criticality" not in off
    on = OPT.optimize_course(COURSE, "c", T0, WF(12.0, 0.0), avoid=False, emit_exploration=False,
                             criticality=True)
    assert on["finish_epoch"] == off["finish_epoch"], "the route itself doesn't change"
    crit = on["criticality"]
    assert crit["bands_min"] == [5, 10, 20]
    assert [lg["to"] for lg in crit["legs"]] == [lg["to"] for lg in on["legs"]]
    for lg in crit["legs"]:
        assert len(lg["loss_min"]) == lg["nx"] * lg["ny"]
        # the grid route is close to the router's: no tack cost makes it quicker, headings only
        # every ~18 deg (a beat sails 45 deg, not its best ~40) make it slower
        router = next(x for x in on["legs"] if x["to"] == lg["to"])["leg_minutes"]
        assert 0.8 * router <= lg["grid_minutes"] <= 1.2 * router, (lg["grid_minutes"], router)
