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
import time
import urllib.parse

from app import deviation, reoptimize, routing, weather
from app import navigator as NAV

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
for k in range(2, 12):
    wf(44.0 + k * 0.1, -82.0, time.time())
check("after the first failure no more fetches (a dead link costs one timeout per run)",
      len(CALLS) == n_calls)

print("#157 /route and the Pi re-route report the gaps")
net({(44.0, -82.0): (20.0, 90.0)})
boat(44.0, -82.0, 8.0, 270.0, MARKS_N, "A")
r = routing.get_route()
check("/route available", r.get("available") is True)
check("/route wind_source is not plain 'forecast'", r.get("wind_source") == "forecast, gaps filled from nearby")
check("/route counts the filled cells", (r.get("forecast_gaps") or 0) >= 1)
# a reach north in 20 kn from 090: if the 270 boat wind had leaked in, legs would show both tacks
check("one wind across the route → a straight reach, no phantom tacks", r.get("tacks") == 0)
net({(44.0, -82.0): (20.0, 90.0)})
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

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
