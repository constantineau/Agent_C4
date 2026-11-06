"""Routes tell the truth about their wind — #156, #157, #158.

  #156  No forecast and no (complete) measured wind → no route, said plainly. Never a made-up
        12 kn northerly, and TWS without TWD no longer crashes the router (engine.md m9).
  #157  One route, one wind source. A forecast cell that fails to load takes the nearest cached
        forecast cell, never the boat's own wind, and the label says gaps were filled. After the
        first failed fetch the run stops fetching, so a dead link costs one timeout, not one per cell.
  #158  Tack labels (and so `tacks` and `first_tack`) come from the wind the route was computed
        in, not the live wind direction (or 0° when it is unknown).

The network is faked at urlopen, so the real weather cache and fetch path run. Polars are stubbed
(6 kn at TWA >= 40°, no-go below). No DB, no network.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_route_wind.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse

os.environ["ENGINE_DB"] = os.path.join(tempfile.mkdtemp(), "engine", "engine.db")

from app import deviation, reoptimize, routing, weather
from app import navigator as NAV
weather.BACKGROUND = False      # no refresh thread: the test drives weather._refresh_once() (#42)

NO_WIND = "No wind data. Can't route."
FCST_GAP = "Forecast incomplete. Can't route."

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


routing._polar_speed = lambda tws, twa: 6.0 if twa >= 40 else 0.0

# ---- a fake Open-Meteo: `GOOD` cells answer with a steady wind, every other cell times out ------
GOOD = {}            # (lat1, lon1) -> (tws, twd); key "*" answers for every cell
CALLS = []           # every cell a fetch was attempted for


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(url, timeout=None):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    cell = (float(q["latitude"][0]), float(q["longitude"][0]))
    CALLS.append(cell)
    if cell not in GOOD and "*" not in GOOD:
        raise TimeoutError("timed out")
    tws, twd = GOOD.get(cell) or GOOD["*"]
    t0 = int(time.time()) - 3600
    ts = [t0 + 3600 * i for i in range(48)]
    return _Resp(json.dumps({"hourly": {"time": ts, "wind_speed_10m": [tws] * 48,
                                        "wind_direction_10m": [twd] * 48}}).encode())


weather.urllib.request.urlopen = _fake_urlopen


def net(good):
    """Reset the forecast cache and the fake network to answer only `good` cells."""
    weather._cache.clear()
    GOOD.clear()
    GOOD.update(good)
    CALLS.clear()
    routing._cache.update(key=None, t=0, val=None)


def boat(lat, lon, tws, twd, marks, next_mark):
    NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                            "next_mark": {"name": next_mark}}
    NAV._marks = lambda route: marks
    NAV._latest = lambda: {"lat": lat, "lon": lon, "tws": tws, "twd": twd, "cog": 0.0, "sog": 6.0}


deviation._load_playbook = lambda: {"race_id": "u", "variants": [{"id": "m"}]}
MARKS_N = [{"seq": 1, "name": "Start", "lat": 44.0, "lon": -82.0},
           {"seq": 2, "name": "A", "lat": 44.3, "lon": -82.0}]


def grid(tws, twd, age_s=0.0, keep=lambda a, b: True):
    """Put forecasts in the cache as if fetched `age_s` ago (past TTL_S = expired), for the cells
    around the test courses that `keep(a, b)` picks (a, b = 0.1° steps from 44.0 N, 82.0 W)."""
    t0 = int(time.time()) - 3600
    for a in range(-6, 10):
        for b in range(-6, 7):
            if keep(a, b):
                weather._cache[(round(44.0 + a * 0.1, 1), round(-82.0 + b * 0.1, 1))] = {
                    "fetched": time.time() - age_s,
                    "hours": [(t0 + 3600 * i, tws, twd) for i in range(48)]}


CHECKER = lambda a, b: (a + b) % 2 == 0      # noqa: E731 — every other cell; holes are ≤ 7.4 nm from one


def raises(fn):
    try:
        fn()
    except Exception as exc:     # noqa: BLE001
        return exc
    return None


# ==== #156: no forecast + no measured wind → refuse ===========================================
print("#156 no wind → no route")
net({})                                                   # the link is down: every fetch times out
boat(44.0, -82.0, None, None, MARKS_N, "A")
r = routing.get_route()
check("/route refuses (not available)", r.get("available") is False)
check(f"/route says '{NO_WIND}'", r.get("note") == NO_WIND)
check("/route carries no route (no path, heading, tack or ETA)",
      not any(k in r for k in ("path", "recommended_heading", "first_tack", "eta_min")))
p = reoptimize.compute()
check("Pi re-route refuses with the same words", p.get("available") is False and p.get("note") == NO_WIND)

wf, use = routing.make_wind_fn(44.0, -82.0, (None, None))
check("make_wind_fn gives no wind field (not a 12 kn northerly)", wf is None and use is False)

print("#156 / m9 TWS without TWD (or TWD without TWS) counts as no wind, no crash")
for live in ((10.0, None), (None, 200.0)):
    net({})
    boat(44.0, -82.0, live[0], live[1], MARKS_N, "A")
    err = raises(routing.get_route)
    check(f"live {live}: /route does not crash", err is None)
    r = routing.get_route() if err is None else {}
    check(f"live {live}: /route refuses with '{NO_WIND}'",
          r.get("available") is False and r.get("note") == NO_WIND)
    err = raises(reoptimize.compute)
    p = reoptimize.compute() if err is None else {}
    check(f"live {live}: Pi re-route refuses with '{NO_WIND}' (no crash)",
          err is None and p.get("available") is False and p.get("note") == NO_WIND)

print("#156 a wind field with nothing for a node → the leg refuses, never invents one")
err = raises(lambda: routing.route_leg(44.0, -82.0, 44.1, -82.0,
                                       lambda la, lo, t: (None, None), 0.0))
check("route_leg raises instead of routing on a made-up wind", err is not None
      and not isinstance(err, TypeError) and str(err) == NO_WIND)

print("#156 measured wind only (no link, full TWS+TWD) still routes, labelled as measured")
net({})
boat(44.0, -82.0, 12.0, 90.0, MARKS_N, "A")
r = routing.get_route()
check("routes on the measured wind", r.get("available") is True)
check("labelled 'live wind (no forecast)'", r.get("wind_source") == "live wind (no forecast)")

# ==== #157: never mix forecast and measured wind ============================================
print("#157 a forecast hole is filled from the nearest forecast, never from the boat's wind")
# the start cell has 20 kn from 090; every other cell fails. Boat measures 8 kn from 270.
net({(44.0, -82.0): (20.0, 90.0)})
wf, use = routing.make_wind_fn(44.0, -82.0, (8.0, 270.0))
check("start cell has a forecast → forecast route", use is True and wf is not None)
w_next = wf(44.1, -82.0, time.time())
check("next cell (fetch fails) takes the start cell's forecast (20 kn / 090), not 8 kn / 270",
      w_next is not None and tuple(round(x) for x in w_next) == (20, 90))
check("label says the forecast had gaps", getattr(wf, "source", None) == "forecast, gaps filled from nearby")
n_calls = len(CALLS)
for la in (43.9, 44.0, 44.1):
    for lo in (-82.1, -81.9):
        wf(la, lo, time.time())
check("after the first failure no more fetches (a dead link costs one timeout per run)",
      len(CALLS) == n_calls)

print("#157 /route and the Pi re-route report the gaps")
net({})
grid(20.0, 90.0, keep=CHECKER)            # every other cell cached fresh, the link down for the rest
boat(44.0, -82.0, 8.0, 270.0, MARKS_N, "A")
r = routing.get_route()
check("/route available", r.get("available") is True)
check("/route wind_source is not plain 'forecast'", r.get("wind_source") == "forecast, gaps filled from nearby")
check("/route counts the filled cells", (r.get("forecast_gaps") or 0) >= 1)
# a reach north in 20 kn from 090: if the 270 boat wind had leaked in, legs would show both tacks
check("one wind across the route → a straight reach, no phantom tacks", r.get("tacks") == 0)
net({})
grid(20.0, 90.0, keep=CHECKER)
p = reoptimize.compute()
check("Pi route available", p.get("available") is True)
check("Pi route labelled with gaps", p.get("wind_source") == "forecast, gaps filled from nearby"
      and (p.get("forecast_gaps") or 0) >= 1)
check("Pi route warns the crew", "Forecast gaps filled from nearby." in (p.get("warnings") or []))

print("#157 a complete forecast is plain 'forecast'")
net({"*": (20.0, 90.0)})
r = routing.get_route()
check("/route wind_source 'forecast', no gaps", r.get("wind_source") == "forecast"
      and not r.get("forecast_gaps"))

print("#157 a hole with no forecast anywhere to fill it → refuse, don't fall back to the boat's wind")
net({(44.0, -82.0): (20.0, 90.0)})
wf, _ = routing.make_wind_fn(44.0, -82.0, (8.0, 270.0))
weather._cache.clear()                                    # nothing cached to borrow from
err = raises(lambda: wf(44.1, -82.0, time.time()))
check(f"the wind field refuses with '{FCST_GAP}'", err is not None and str(err) == FCST_GAP)

print("#157 a hole is filled only from a cell within 10 nm")
net({(44.0, -82.0): (20.0, 90.0)})
wf, _ = routing.make_wind_fn(44.0, -82.0, (8.0, 270.0))
check("6 nm from the forecast cell: filled", wf(44.1, -82.0, time.time()) is not None)
err = raises(lambda: wf(44.2, -82.0, time.time()))
check(f"12 nm away: refused with '{FCST_GAP}'", err is not None and str(err) == FCST_GAP)
net({(44.0, -82.0): (20.0, 90.0)})
boat(44.0, -82.0, 8.0, 270.0, MARKS_N, "A")
r = routing.get_route()
check(f"/route through an 18 nm forecast hole refuses with '{FCST_GAP}'",
      r.get("available") is False and r.get("note") == FCST_GAP)
net({(44.0, -82.0): (20.0, 90.0)})
p = reoptimize.compute()
check("Pi re-route through it refuses the same way", p.get("available") is False and p.get("note") == FCST_GAP)

print("#157 an EXPIRED forecast + no network → one failed fetch per run, labelled 'old forecast'")


def expired(tws, twd, age_s=None):
    """Fill the cache with forecasts past their TTL (fetched before the link died)."""
    grid(tws, twd, weather.TTL_S + 60 if age_s is None else age_s)


net({})
expired(15.0, 90.0)
boat(44.0, -82.0, 8.0, 270.0, MARKS_N, "A")
r = routing.get_route()
check("/route still routes on the old forecast", r.get("available") is True)
check(f"only the start cell's refresh hit the dead link (fetches: {len(CALLS)})", len(CALLS) == 1)
check("labelled 'old forecast'", r.get("wind_source") == "old forecast" and r.get("forecast_old") is True)
check("old forecast, not the boat's 270° wind → a straight reach, no tacks", r.get("tacks") == 0)
net({})
expired(15.0, 90.0)
p = reoptimize.compute()
check(f"Pi re-route: one failed fetch only (fetches: {len(CALLS)})", len(CALLS) == 1)
check("Pi route labelled 'old forecast' and warns 'Old forecast.'",
      p.get("wind_source") == "old forecast" and "Old forecast." in (p.get("warnings") or []))

# fresh start cell (the link answers it), expired cells further out: the first failed refresh
# switches the run to the cache, so the dead link is tried once more, not once per expired cell
net({(44.0, -82.0): (15.0, 90.0)})
expired(15.0, 90.0)
del weather._cache[(44.0, -82.0)]
r = routing.get_route()
check(f"fresh start, expired cells beyond: 1 good + 1 failed fetch (fetches: {len(CALLS)})",
      len(CALLS) == 2)
check("…labelled 'old forecast'", r.get("wind_source") == "old forecast")

print("#157 weather: a failed refresh never hands old wind to other callers as current")
net({})
expired(15.0, 90.0, 3 * 86400)                 # a 3-day-old cache, link down
w1 = weather.wind_at(44.0, -82.0, time.time())
w2 = weather.wind_at(44.0, -82.0, time.time())
check("wind_at says unavailable (None), not the old wind", w1 is None and w2 is None)
check(f"the second read doesn't wait on the network again (fetches: {len(CALLS)})", len(CALLS) == 1)
check("the old hours stay in the cache for the router (cell reads as stale)",
      weather.cell_status(44.0, -82.0) == "stale")
fc = weather.get_forecast(44.0, -82.0)
check("/forecast says unavailable", fc.get("available") is False)
net({})
expired(15.0, 90.0, 3 * 86400)
fc = weather.get_forecast(44.0, -82.0)
check("/forecast says unavailable on the first (failing) read too", fc.get("available") is False)

print("#157 drift: an expired cache + no link reads as old, never 'holding'")
from app import drift  # noqa: E402
fp_t = time.time()
pts = [{"lat": 44.0 + i * 0.1, "lon": -82.0, "t": fp_t + (i + 2) * 3600, "twd": 90.0, "tws": 15.0}
       for i in range(4)]
_bundle = {"race_id": "unit", "recommended": "m", "variants": [{"id": "m"}],
           "forecast_fingerprint": {"source": "open-meteo-gfs", "built_at": fp_t - 3600, "points": pts}}
_saved_pb = deviation._load_playbook
deviation._load_playbook = lambda: _bundle
net({})
expired(15.0, 90.0, 3 * 86400)                 # the old forecast matches the plan exactly
drift.reset_state()
d = drift.get_drift()
deviation._load_playbook = _saved_pb
check(f"drift is not available ({d.get('sub')!r})", d.get("available") is False
      and d.get("sub") == "Forecast is old. No new one yet.")
# control: the same plan with a fresh forecast is compared (so the check above can fail).
# drift reads the cache only (#42): the background refresh fetches the forecast first
net({"*": (15.0, 90.0)})
weather._link.update(fails=0, failed_at=0.0)
deviation._load_playbook = lambda: _bundle
drift.reset_state()
drift.get_drift()
weather._refresh_once()
d = drift.get_drift()
deviation._load_playbook = _saved_pb
check(f"control: with a fresh forecast drift is available ({d.get('status')})", d.get("available") is not False)

print("#157 gap fill skips too-old cells BEFORE picking the nearest")
TOO_OLD_MSG = routing.FCST_TOO_OLD
net({(44.0, -82.0): (15.0, 90.0)})
wf, _ = routing.make_wind_fn(44.0, -82.0, (8.0, 270.0))
weather._cache.clear()
t0h = int(time.time()) - 3600
weather._cache[(44.3, -82.32)] = {"fetched": time.time() - 7 * 3600,        # ~0.9 nm, 7 h old
                                  "hours": [(t0h + 3600 * i, 30.0, 180.0) for i in range(48)]}
weather._cache[(44.38, -82.3)] = {"fetched": time.time() - 5 * 3600,        # ~4.8 nm, 5 h old
                                  "hours": [(t0h + 3600 * i, 15.0, 90.0) for i in range(48)]}
err = raises(lambda: wf(44.3, -82.3, time.time()))
w = wf(44.3, -82.3, time.time()) if err is None else None
check(f"the 5 h cell at 4.8 nm is used, not refused for the 7 h one at 0.9 nm ({err})",
      err is None and w is not None and round(w[0]) == 15)
check("…and the route is labelled old", wf.source.startswith("old forecast"))
# the same for the point's own cell: 7 h old there, a 5 h cell 6 nm away → the 5 h one is used
net({(44.0, -82.0): (15.0, 90.0)})
wf, _ = routing.make_wind_fn(44.0, -82.0, (8.0, 270.0))
weather._cache.clear()
weather._cache[(44.3, -82.3)] = {"fetched": time.time() - 7 * 3600,
                                 "hours": [(t0h + 3600 * i, 30.0, 180.0) for i in range(48)]}
weather._cache[(44.4, -82.3)] = {"fetched": time.time() - 5 * 3600,
                                 "hours": [(t0h + 3600 * i, 15.0, 90.0) for i in range(48)]}
err = raises(lambda: wf(44.3, -82.3, time.time()))
w = wf(44.3, -82.3, time.time()) if err is None else None
check(f"own cell 7 h old: the 5 h cell 6 nm away fills it ({err})",
      err is None and w is not None and round(w[0]) == 15 and wf.source == "old forecast, gaps filled from nearby")
del weather._cache[(44.4, -82.3)]
err = raises(lambda: wf(44.3, -82.3, time.time()))
check(f"own cell 7 h old and nothing younger near: '{TOO_OLD_MSG}'", err is not None and str(err) == TOO_OLD_MSG)

print("#157 a forecast older than 6 h is refused; under 6 h it routes, labelled old")
TOO_OLD = "Forecast too old. Can't route."
net({})
expired(15.0, 90.0, 7 * 3600)
r = routing.get_route()
check(f"7 h old: /route refuses with '{TOO_OLD}'", r.get("available") is False and r.get("note") == TOO_OLD)
net({})
expired(15.0, 90.0, 7 * 3600)
p = reoptimize.compute()
check(f"7 h old: Pi re-route refuses with '{TOO_OLD}'", p.get("available") is False and p.get("note") == TOO_OLD)
net({})
expired(15.0, 90.0, 5 * 3600)
r = routing.get_route()
check("5 h old: routes, labelled 'old forecast'", r.get("available") is True
      and r.get("wind_source") == "old forecast")
# a fresh start, but the hole beside it can only borrow a 7 h old cell → refused too
net({(44.0, -82.0): (15.0, 90.0)})
wf, _ = routing.make_wind_fn(44.0, -82.0, (8.0, 270.0))
weather._cache.clear()
grid(15.0, 90.0, 7 * 3600, keep=lambda a, b: (a, b) == (0, 0))
err = raises(lambda: wf(44.1, -82.0, time.time()))
check("a gap filled from a 7 h old cell is refused too", err is not None and str(err) == TOO_OLD)

print("the limits are set by env (C4_FORECAST_MAX_AGE_H, C4_FORECAST_FILL_NM, C4_REROUTE_BUDGET_S)")
env = dict(os.environ, C4_FORECAST_MAX_AGE_H="2", C4_FORECAST_FILL_NM="4", C4_REROUTE_BUDGET_S="30")
got = subprocess.run([sys.executable, "-c", "from app import weather, reoptimize; "
                      "print(weather.MAX_AGE_S, weather.FILL_NM, reoptimize.BUDGET_S)"],
                     env=env, capture_output=True, text=True).stdout.split()
check(f"env read: {got}", got == ["7200.0", "4.0", "30.0"])
got = subprocess.run([sys.executable, "-c", "from app import weather, reoptimize; "
                      "print(weather.MAX_AGE_S, weather.FILL_NM, reoptimize.BUDGET_S)"],
                     env={k: v for k, v in os.environ.items() if not k.startswith(("C4_FORECAST", "C4_REROUTE"))},
                     capture_output=True, text=True).stdout.split()
check(f"defaults 6 h / 10 nm / 90 s: {got}", got == ["21600.0", "10.0", "90.0"])

print("Pi re-route: 90 s wall-clock limit, then a plain refusal and no half route")
TOO_LONG = "Re-route took too long. Try again."
net({"*": (12.0, 90.0)})
p = reoptimize._compute(NAV.get_navigator(), [MARKS_N[1]], 44.0, -82.0, (12.0, 90.0),
                        None, [], [], deadline=time.time() - 1)
check(f"past the deadline: '{TOO_LONG}', no route", p.get("available") is False
      and p.get("note") == TOO_LONG and "path" not in p)
# the button's job, with a slow wind (each sample 20 ms) and a 0.3 s budget
_real_wind_at = weather.wind_at
weather.wind_at = lambda la, lo, t: (time.sleep(0.02), _real_wind_at(la, lo, t))[1]
reoptimize.BUDGET_S = 0.3
t_start = time.time()
code, body = reoptimize.run(device="test")
if reoptimize._thread:
    reoptimize._thread.join(30)
took = time.time() - t_start
st = reoptimize.status()
weather.wind_at = _real_wind_at
reoptimize.BUDGET_S = 90.0
check("the press started a run", code == 202)
check(f"it stopped near its budget (took {took:.1f} s)", took < 3)
check(f"the job ends in error saying '{TOO_LONG}'", st.get("state") == "error" and st.get("detail") == TOO_LONG)
check("no half route was stored", reoptimize.get_reoptimize().get("available") is False)

# ==== #158: tack labels from the route's wind, not the live TWD ===============================
print("#158 tacks judged against the route's wind")
# forecast from 270 everywhere; the mark is due west (dead upwind). Live wind sensor dead.
WEST = [{"seq": 1, "name": "Start", "lat": 44.0, "lon": -82.0},
        {"seq": 2, "name": "W", "lat": 44.0, "lon": -82.25}]
net({"*": (12.0, 270.0)})
boat(44.0, -82.0, None, None, WEST, "W")
r = routing.get_route()
check("routes (forecast)", r.get("available") is True and r.get("wind_source") == "forecast")


def side(hdg, twd):
    return "stbd" if ((twd - hdg + 180) % 360) - 180 > 0 else "port"


hdg0 = r.get("recommended_heading")
check(f"first tack matches the first heading ({hdg0}°) in a 270° wind",
      hdg0 is not None and r.get("first_tack") == side(hdg0, 270))
check("a dead beat west tacks at least once (not 'tacks: 0')", (r.get("tacks") or 0) >= 1)

# route_leg directly: the wind is 270 but the caller's live TWD says 090 → labels follow the wind
leg = routing.route_leg(44.0, -82.0, 44.0, -82.25, lambda la, lo, t: (12.0, 270.0), 0.0, 90.0)
check("every leg's tack is judged against the 270° wind it was sailed in",
      leg["legs"] and all(lg["tack"] == side(lg["hdg"], 270) for lg in leg["legs"]))

# a wind that shifts along the route: 270 south of 44.05, 300 north of it. Each leg's tack must
# match the wind at the node it left, so labels stay right after the shift.
seen = []


def shifting(la, lo, t):
    return (12.0, 270.0 if la < 44.05 else 300.0)


leg = routing.route_leg(44.0, -82.0, 44.2, -82.2, shifting, 0.0, 0.0)
pts = leg["path"]
for i, lg in enumerate(leg["legs"]):
    twd = shifting(pts[i]["lat"], pts[i]["lon"], 0)[1]
    seen.append(lg["tack"] == side(lg["hdg"], twd))
check("after a wind shift, each leg is labelled against the wind where it starts",
      seen and all(seen))

print("#203 a max age below the 30-min cache TTL is enforced on fresh cells too")
# C4_FORECAST_MAX_AGE_H=0.1 (6 min): a cell fetched 20 min ago is still inside the cache TTL
# ("fresh", never refetched) but older than the limit. Ages are relative to the clock the code
# reads, so nothing here depends on the date.
TOO_OLD = routing.FCST_TOO_OLD
_max_age = weather.MAX_AGE_S
weather.MAX_AGE_S = 0.1 * 3600
net({})
grid(15.0, 90.0, 20 * 60)
wf = routing.WindField(True)
err = raises(lambda: wf(44.1, -82.0, time.time()))
check(f"live path: a 20 min old cell past a 6 min limit is refused (got {err})",
      err is not None and str(err) == TOO_OLD)
wf = routing.WindField(True)
wf._offline = True                           # the cache-only path (after a failed fetch)
err = raises(lambda: wf(44.1, -82.0, time.time()))
check(f"cache-only path: refused too (got {err})", err is not None and str(err) == TOO_OLD)
net({})
grid(15.0, 90.0, 2 * 60)                     # 2 min old: inside the limit
wf = routing.WindField(True)
err = raises(lambda: wf(44.1, -82.0, time.time()))
check(f"(control) a 2 min old cell routes normally ({err})", err is None and wf.source == "forecast")
weather.MAX_AGE_S = _max_age

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
