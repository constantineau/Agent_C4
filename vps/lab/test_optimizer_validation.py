"""#407: optimizer validation, the CI-fast part. Does the router pick the right route, not just a
plausible ETA?

Every case grades the router (`route_leg` / `optimize_course`) against an answer worked out a
second, simpler way in optval_fixtures.py: plain geometry on the polar, or a small brute-force
simulator. Synthetic wind, made-up boats, open water, no network.

  known answers   steady wind: beat, run, reaches (time and tack count against the polar);
                  a persistent shift (the side it shifts to wins, by the textbook amount);
                  an oscillating shift (tack on the headers); a tidal gate (go, or wait);
                  an island, passed on the short side, both ways round
  convergence     finer heading/time steps move the ETA by less than CONVERGE_TOL
  other boats     a keelboat, a heavy displacement cruiser, a multihull and a planing sportboat,
                  through the same cases (testdata/polars/)

A case that shows a real optimizer bug runs every other check as usual (the route completes, the
tack count, the router never beats the polar); only the one assertion the bug breaks becomes an
expected failure, via `_known_bug`. Once the bug is fixed that assertion passes and the test FAILS,
so the entry gets removed with the fix. Run with -s to see the numbers.
"""
import math

import pytest

from app import optimizer as O
from app.geo import obstacles as OB
import optval_fixtures as F

FINE = {k: O.RESOLUTIONS["fine"][k] for k in ("hstep", "dt_cap")}
AUTO = {k: O.RESOLUTIONS["auto"][k] for k in ("hstep", "dt_cap")}
FINER = {"hstep": 4, "dt_cap": 0.15}          # finer than any Lab setting: the convergence target

ETA_TOL = 0.04          # the router may be up to 4% slower than the textbook time...
ETA_FAST = 0.01         # ...and never more than 1% faster (it can't beat the polar)
CONVERGE_TOL = 0.025    # auto / fine / finer ETAs agree within 2.5%
LEG_NM = 6.0

# --- bugs this suite found, now fixed: #410 (hot downwind angles, oscillating shifts) and #411
# (fast reach between resolutions). A new one goes in as (reason, cap) through `_known_bug`.


def _known_bug(bug, value, tol, detail):
    """The one assertion a known bug breaks: `value <= tol`. `bug` is None (a plain assert) or
    (reason, cap): the bug lets `value` exceed `tol` up to `cap`, a little above what it measures
    today, and that is an expected failure (reached only after every other check passed). Past the
    cap it got worse: a hard fail. Within `tol`: the bug no longer shows, so the test fails until
    its entry is removed (a strict xfail)."""
    ok = value <= tol
    if bug is None:
        assert ok, detail
        return
    why, cap = bug
    if ok:
        pytest.fail(f"known bug no longer shows, remove its entry: {why} ({detail})")
    assert value <= cap, f"worse than the known bug allows ({value:.3f} > {cap}): {why} ({detail})"
    pytest.xfail(f"{why} ({detail})")


def _xy_path(leg):
    return [F.to_xy(p["lat"], p["lon"]) for p in leg["path"]]


def _err(eta_h, ref_h):
    return eta_h / ref_h - 1.0


def _leg(wf, P, brg, dist=LEG_NM, res=FINE, **kw):
    mlat, mlon = F.advance(F.LAT0, F.LON0, brg, dist)
    leg = O.route_leg(wf, P, F.LAT0, F.LON0, 0, mlat, mlon, **res, **kw)
    assert leg["reached"], f"short {leg.get('short_nm')} nm ({leg.get('stop')})"
    return leg


# --- known answers: steady wind -----------------------------------------------------------------
# the wind is from 000; the mark sits at `brg` from the start
LEGS = {"beat": 0.0, "tight": 60.0, "reach": 90.0, "broad": 135.0, "run": 180.0}
# known bugs as {(boat, tws, kind): (reason, cap)}; none today (#410 and #411 fixed)
STEADY_XFAIL = {}


def _steady_cases():
    for boat in F.BOATS:
        for tws in (6, 10, 14, 20):
            for kind in LEGS:
                yield pytest.param(boat, tws, kind, id=f"{boat}-{tws}kn-{kind}")


@pytest.mark.parametrize("boat,tws,kind", list(_steady_cases()))
def test_steady_wind_time_and_tacks_match_the_polar(boat, tws, kind):
    P = F.polar(boat)
    brg = LEGS[kind]
    ref_h, ref_tacks, ha, hb = F.two_board_time_h(P, tws, 0.0, LEG_NM, brg, O.tack_cost_s())
    leg = _leg(F.steady(tws, 0.0), P, brg)
    eta = leg["eta"] / 3600.0
    print(f"{boat} {tws} kn {kind}: {eta:.3f} h vs {ref_h:.3f} h ({100 * _err(eta, ref_h):+.1f}%),"
          f" tacks {leg['tacks']} vs {ref_tacks}")
    assert _err(eta, ref_h) >= -ETA_FAST, (eta, ref_h, ha, hb)
    if kind in ("beat", "run"):
        # dead up/downwind: one tack/gybe is the answer; a second one only to tidy the layline
        assert 1 <= leg["tacks"] <= 2, leg["tacks"]
    else:
        assert leg["tacks"] <= 1, leg["tacks"]
    _known_bug(STEADY_XFAIL.get((boat, tws, kind)), _err(eta, ref_h), ETA_TOL,
               f"{eta:.3f} h vs {ref_h:.3f} h")


def _seg_dist(p, a, b):
    """Distance (nm) from point p to the segment a-b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    u = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(a[0] + u * dx - p[0], a[1] + u * dy - p[1])


def _run_twa(leg, twd=0.0):
    """Mean true wind angle of the path's boards (by distance), ignoring the closing board."""
    xy = _xy_path(leg)
    tot = acc = 0.0
    for (x0, y0), (x1, y1) in zip(xy[:-2], xy[1:-1]):
        d = math.hypot(x1 - x0, y1 - y0)
        h = math.degrees(math.atan2(x1 - x0, y1 - y0))
        acc += abs(F.wrap180(h - twd)) * d
        tot += d
    return acc / tot


def test_planing_sportboat_heats_up_when_it_planes():
    # off the plane (10 kn) the best run is deep (~158 TWA); on it (14 kn) it jumps up to ~140.
    # The route has to follow the polar, not a fixed downwind angle: it sails hotter at 14 kn, and
    # at both speeds the angle it sails makes within 2% of the polar's best VMG.
    P = F.polar("planing_sportboat")
    got = {}
    for tws in (10, 14):
        want, best = F.vmg(P, tws, "run")
        got[tws] = twa = _run_twa(_leg(F.steady(tws, 0.0), P, 180.0, dist=12.0))
        made = -F.speed(P, tws, twa) * math.cos(math.radians(twa))
        print(f"sportboat {tws} kn run: sails {twa:.0f} TWA ({100 * made / best:.1f}% of best VMG),"
              f" polar VMG angle {want:.1f}")
        assert made >= 0.98 * best, (twa, want)
    assert F.vmg(P, 10, "run")[0] - F.vmg(P, 14, "run")[0] > 10      # the polar really jumps
    assert got[14] < got[10] - 10, got


# --- known answers: shifts ----------------------------------------------------------------------
@pytest.mark.parametrize("boat", F.BOATS)
@pytest.mark.parametrize("rate", [12.0, -12.0])
def test_persistent_shift_goes_to_the_side_it_shifts_to(boat, rate):
    # the wind swings steadily right (+) or left (-): sail toward the new wind first (right on
    # port tack when it shifts right), and gain what one well-timed tack gains
    P = F.polar(boat)
    tws = 10.0
    mark = F.advance(F.LAT0, F.LON0, 0.0, LEG_NM)
    fn = lambda t: (rate * t / 3600.0) % 360            # noqa: E731
    good = "port" if rate > 0 else "stbd"
    bad = "stbd" if rate > 0 else "port"
    ref_good = F.one_tack_shift_time_h(P, tws, fn, F.to_xy(*mark), O.tack_cost_s(), good)
    ref_bad = F.one_tack_shift_time_h(P, tws, fn, F.to_xy(*mark), O.tack_cost_s(), bad)
    leg = _leg(F.persistent(tws, 0.0, rate), P, 0.0)
    eta = leg["eta"] / 3600.0
    first = F.wrap180(leg["first_heading"])
    print(f"{boat} shift {rate:+.0f}/h: {eta:.3f} h, right side first {ref_good:.3f} h,"
          f" wrong side first {ref_bad:.3f} h, first heading {first:+.0f}")
    assert (first > 0) == (rate > 0), first
    assert -ETA_FAST <= _err(eta, ref_good) <= ETA_TOL
    # the textbook gain is real here (> 4%), and the router keeps most of it
    assert ref_bad - ref_good > 0.04 * ref_good
    assert eta < ref_bad - 0.5 * (ref_bad - ref_good)


OSC = [(10.0, 30.0), (15.0, 40.0)]          # (+- deg, period min)


@pytest.mark.parametrize("amp,period", OSC)
def test_oscillating_shift_tacks_more_than_a_persistent_one(amp, period):
    P = F.polar("keelboat")
    osc = _leg(F.oscillating(10.0, 0.0, amp, period), P, 0.0, dist=12.0)
    per = _leg(F.persistent(10.0, 0.0, 12.0), P, 0.0, dist=12.0)
    uni = _leg(F.steady(10.0, 0.0), P, 0.0, dist=12.0)
    print(f"osc +-{amp:.0f}/{period:.0f} min: {osc['eta'] / 3600:.3f} h, {osc['tacks']} tacks;"
          f" persistent {per['tacks']} tacks; steady {uni['eta'] / 3600:.3f} h")
    assert osc["tacks"] > per["tacks"]
    # playing the shifts beats sailing the same beat in steady wind
    assert osc["eta"] < uni["eta"]


@pytest.mark.parametrize("amp,period", OSC)
def test_oscillating_shift_as_fast_as_tacking_on_headers(amp, period):
    P = F.polar("keelboat")
    mark = F.advance(F.LAT0, F.LON0, 0.0, 12.0)
    fn = lambda t: (amp * math.sin(2 * math.pi * t / (period * 60.0))) % 360     # noqa: E731
    ref_h, ref_tacks = F.header_tacker_time_h(P, 10.0, fn, 0.0, F.to_xy(*mark), O.tack_cost_s())
    leg = _leg(F.oscillating(10.0, 0.0, amp, period), P, 0.0, dist=12.0)
    eta = leg["eta"] / 3600.0
    print(f"osc +-{amp:.0f}/{period:.0f} min: router {eta:.3f} h ({leg['tacks']} tacks),"
          f" tack-on-headers {ref_h:.3f} h ({ref_tacks} tacks)")
    assert ref_h is not None and eta >= ref_h * 0.9      # sanity: nowhere near beating it
    assert leg["tacks"] >= 2, leg["tacks"]               # it does play some shifts
    _known_bug(None, _err(eta, ref_h), 0.01, f"{eta:.3f} h vs {ref_h:.3f} h")


# --- the arrival stop rule -----------------------------------------------------------------------
class _StillWater:
    """A current that never runs: the same water as none, but the router can't bound speed with it."""
    loaded = True

    def current_at(self, lat, lon, t):
        return (0.0, 0.0)


# band (nm) -> the most the route may take (h). A prune on each node's own light-air speed took
# 7.220 and 7.275 h; kept, the boat in the new breeze gets there in 7.172 and 7.257 h.
FILL_MAX_H = {2.0: 7.19, 3.0: 7.265}


@pytest.mark.parametrize("band", sorted(FILL_MAX_H))
def test_arrival_prune_keeps_a_boat_the_wind_fills_in_for(band):
    # 6 kn on a 40 nm reach; at 6.3 h it fills to 20 kn, but only `band` nm or more off the line.
    # A boat first lays the mark in the light air near the line. A boat farther off, in the new
    # breeze one step later, gets there sooner and must not be dropped as "can't beat it" on the
    # light-air speed it has now (#419 review). With a current the router can't bound speed and
    # drops nobody, so the same water with a still current is the unpruned answer.
    P = F.polar("keelboat")
    wf = lambda: F.Field(lambda t, x, y: (20.0 if t >= 6.3 * 3600 and abs(y) >= band else 6.0, 0.0))  # noqa: E731
    plain = _leg(wf(), P, 90.0, dist=40.0, res=AUTO)
    full = _leg(wf(), P, 90.0, dist=40.0, res=AUTO, cur=_StillWater())
    print(f"wind fills {band:.0f} nm off: {plain['eta'] / 3600:.3f} h, unpruned {full['eta'] / 3600:.3f} h")
    assert plain["eta"] <= full["eta"] + 1.0
    assert plain["eta"] / 3600 <= FILL_MAX_H[band]


# --- known answers: tidal gate ------------------------------------------------------------------
def test_tidal_gate_go_when_the_stream_is_beatable():
    # 2 kn foul for an hour, then fair: going now beats waiting, and the router goes
    P = F.polar("keelboat")
    stream = F.TidalStream(270.0, 2.0, 3600.0)
    go = F.stream_go_time_h(P, 8.0, 0.0, stream, 4.0, 90.0)
    wait = F.stream_wait_time_h(P, 8.0, 0.0, stream, 4.0, 90.0)
    leg = _leg(F.steady(8.0, 0.0), P, 90.0, dist=4.0, cur=stream)
    eta = leg["eta"] / 3600.0
    print(f"tide go: router {eta:.3f} h, go {go:.3f} h, wait {wait:.3f} h")
    assert go < wait
    assert -ETA_FAST <= _err(eta, go) <= ETA_TOL


def test_tidal_gate_wait_when_the_stream_is_too_strong():
    # 6 kn foul (faster than the boat) for 90 min, then fair: the right answer is to wait. The
    # router has no anchor, so it can only tread water; it must still get there within a few % of
    # anchoring and turning with the tide, and can't beat it.
    P = F.polar("displacement_cruiser")
    stream = F.TidalStream(270.0, 6.0, 5400.0)
    go = F.stream_go_time_h(P, 6.0, 0.0, stream, 4.0, 90.0)
    wait = F.stream_wait_time_h(P, 6.0, 0.0, stream, 4.0, 90.0)
    leg = _leg(F.steady(6.0, 0.0), P, 90.0, dist=4.0, cur=stream)
    eta = leg["eta"] / 3600.0
    print(f"tide wait: router {eta:.3f} h, wait {wait:.3f} h, plain go {go:.3f} h")
    assert wait < go
    assert -ETA_FAST <= _err(eta, wait) <= 0.05


# --- known answers: an island -------------------------------------------------------------------
@pytest.mark.parametrize("brg", [90.0, 270.0])
@pytest.mark.parametrize("off", [0.3, -0.3])
def test_island_passed_on_the_short_side(brg, off):
    # a 1 nm island 0.3 nm off the line, halfway down an 8 nm reach: pass it on the far side of
    # the line from its centre (the short way), clear of it, in the textbook time; both directions
    P = F.polar("keelboat")
    r = 1.0
    end = F.advance(F.LAT0, F.LON0, brg, 8.0)
    c = F.advance(*F.advance(F.LAT0, F.LON0, brg, 4.0), 0.0, off)
    fld = OB.ObstacleField((44.3, 43.7, -82.4, -81.6))
    fld._fill_disk(c[0], c[1], r, "islands")
    leg = O.route_leg(F.steady(10.0, 0.0), P, F.LAT0, F.LON0, 0, end[0], end[1],
                      obstacles=fld, **FINE)
    assert leg["reached"]
    xy = _xy_path(leg)
    cxy = F.to_xy(*c)
    s, e = (0.0, 0.0), F.to_xy(*end)
    times = {side: F.around_disk_time_h(P, 10.0, 0.0, s, e, cxy, r, side) for side in (1, -1)}
    short = min(times, key=times.get)
    # which side did the route take: the centre is left (+) or right (-) of the boat's track
    k = min(range(len(xy)), key=lambda i: math.hypot(xy[i][0] - cxy[0], xy[i][1] - cxy[1]))
    i, j = (k, k + 1) if k + 1 < len(xy) else (k - 1, k)
    tx, ty = xy[j][0] - xy[i][0], xy[j][1] - xy[i][1]
    left = tx * (cxy[1] - xy[k][1]) - ty * (cxy[0] - xy[k][0]) > 0
    took = 1 if left else -1                        # island on the left = counterclockwise round it
    eta = leg["eta"] / 3600.0
    print(f"island brg {brg:.0f} off {off:+.1f}: router {eta:.3f} h side {took:+d},"
          f" short side {short:+d} {times[short]:.3f} h, long {times[-short]:.3f} h")
    assert took == short
    clear = min(_seg_dist(cxy, a, b) for a, b in zip(xy, xy[1:]))
    assert clear >= r - 0.1, clear
    assert -ETA_FAST <= _err(eta, times[short]) <= ETA_TOL


# --- other boats end to end ---------------------------------------------------------------------
@pytest.mark.parametrize("boat", F.BOATS)
def test_windward_leeward_course_finishes_on_every_boat(boat):
    # a whole course through optimize_course: up 3 nm, down 3 nm, 12 kn. Total within the tolerance
    # of the two legs' textbook times (each leg's own errors are tested above).
    P = F.polar(boat)
    w = F.advance(F.LAT0, F.LON0, 0.0, 3.0)
    d = F.course([w, (F.LAT0, F.LON0)])
    r = O.optimize_course(d, "c", 0, F.steady(12.0, 0.0), polar=P, avoid=False,
                          emit_exploration=False, resolution="fine", time_budget_s=120)
    assert all(leg["reached"] for leg in r["legs"]), [leg.get("short_nm") for leg in r["legs"]]
    up = F.two_board_time_h(P, 12.0, 0.0, 3.0, 0.0, O.tack_cost_s())[0]
    down = F.two_board_time_h(P, 12.0, 0.0, 3.0, 180.0, O.tack_cost_s())[0]
    total = (r["finish_epoch"] - r["start_epoch"]) / 3600.0
    print(f"{boat} W/L: {total:.3f} h vs {up + down:.3f} h")
    assert -ETA_FAST <= _err(total, up + down) <= ETA_TOL


# --- convergence --------------------------------------------------------------------------------
def _conv_cases():
    kb, mh, sb = "keelboat", "multihull", "planing_sportboat"
    # last field: a known bug as (reason, cap on the spread), else None
    cases = [
        ("keelboat beat 10", kb, lambda: F.steady(10, 0.0), 0.0, 6.0, None),
        ("keelboat run 10", kb, lambda: F.steady(10, 0.0), 180.0, 6.0, None),
        ("keelboat reach 10", kb, lambda: F.steady(10, 0.0), 90.0, 6.0, None),
        ("cruiser beat 14", "displacement_cruiser", lambda: F.steady(14, 0.0), 0.0, 6.0, None),
        ("keelboat persistent shift", kb, lambda: F.persistent(10, 0.0, 12.0), 0.0, 6.0, None),
        ("keelboat oscillating shift", kb, lambda: F.oscillating(10, 0.0, 10.0, 30.0), 0.0, 12.0, None),
        ("multihull reach 10", mh, lambda: F.steady(10, 0.0), 90.0, 6.0, None),
        ("multihull reach 20", mh, lambda: F.steady(20, 0.0), 90.0, 6.0, None),
        ("multihull run 10", mh, lambda: F.steady(10, 0.0), 180.0, 6.0, None),
        ("sportboat run 14", sb, lambda: F.steady(14, 0.0), 180.0, 6.0, None),
    ]
    for name, boat, wf, brg, dist, why in cases:
        yield pytest.param(boat, wf, brg, dist, why, id=name)


@pytest.mark.parametrize("boat,wf,brg,dist,why", list(_conv_cases()))
def test_finer_steps_barely_move_the_eta(boat, wf, brg, dist, why):
    P = F.polar(boat)
    etas = {name: _leg(wf(), P, brg, dist=dist, res=res)["eta"] / 3600.0
            for name, res in (("auto", AUTO), ("fine", FINE), ("finer", FINER))}
    spread = max(etas.values()) / min(etas.values()) - 1.0
    print(f"{boat} brg {brg:.0f}: " + ", ".join(f"{k} {v:.3f} h" for k, v in etas.items())
          + f"; spread {100 * spread:.1f}%")
    _known_bug(why, spread, CONVERGE_TOL, f"spread {100 * spread:.1f}%")
