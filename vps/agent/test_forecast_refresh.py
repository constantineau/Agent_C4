"""#42 forecast: /strategy never waits on the network, failed fetches back off, the background
refresh keeps to a request budget, and drift never compares a time past the forecast's end.

Stubs the network and the clock; no DB, no playbook file.
Run:  PYTHONPATH=vps/agent python3 vps/agent/test_forecast_refresh.py
"""
import json
import time

from app import deviation, drift, weather

ok = True


def check(name, cond):
    global ok
    ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


NOW = [2_000_000_000.0]
time.time = lambda: NOW[0]            # weather and drift share the time module
weather.BACKGROUND = False            # the test drives the refresh by hand
CALLS = []
LINK = {"up": True}


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(url, timeout=None):
    CALLS.append(url)
    if not LINK["up"]:
        raise OSError("no route to host")
    t0 = int(NOW[0]) // 3600 * 3600
    ts = [t0 + h * 3600 for h in range(6)]            # a short forecast: 5 h past the hour
    return _Resp(json.dumps({"hourly": {"time": ts, "wind_speed_10m": [12.0] * 6,
                                        "wind_direction_10m": [200.0] * 6}}).encode())


weather.urllib.request.urlopen = _fake_urlopen


def reset():
    weather._cache.clear()
    weather._want.clear()
    weather._spent.clear()
    weather._link.update(fails=0, failed_at=0.0)
    CALLS.clear()
    LINK["up"] = True


def use_bundle(pts):
    b = {"race_id": "unit", "recommended": "m", "variants": [{"id": "m"}],
         "forecast_fingerprint": {"source": "open-meteo-gfs", "built_at": NOW[0] - 3600,
                                  "points": pts}}
    deviation._load_playbook = lambda: b


def pts_at(hours, n=3):
    return [{"lat": 44.0 + i * 0.1, "lon": -82.0, "t": NOW[0] + hours * 3600, "twd": 200.0,
             "tws": 12.0} for i in range(n)]


print("drift on the /strategy path makes no network call")
reset()
use_bundle(pts_at(2))
drift.reset_state()
d = drift.get_drift()
check(f"no fetch during get_drift (fetches: {len(CALLS)})", len(CALLS) == 0)
check(f"empty cache → na, 'No forecast right now.' ({d.get('sub')!r})",
      d.get("status") == "na" and d.get("sub") == "No forecast right now.")
check(f"the 3 cells are queued for the background refresh ({len(weather._want)})",
      len(weather._want) == 3)
n = weather._refresh_once()
check(f"the refresh fetches them ({n})", n == 3 and not weather._want)
d = drift.get_drift()
check(f"after the refresh drift compares them ({d.get('status')}, n={d.get('n_points')})",
      d.get("status") == "ok" and d.get("n_points") == 3)

print("past the forecast's end drift says so, never the last hour held over")
reset()
use_bundle(pts_at(2))
drift.get_drift()
weather._refresh_once()                               # the 3 cells now hold a fresh forecast
use_bundle(pts_at(20))                                # forecast ends 5 h out
drift.reset_state()
d = drift.get_drift()
check(f"points past the end → na ({d.get('sub')!r})",
      d.get("status") == "na" and d.get("sub") == "Plan runs past the forecast.")
check("wind_now is None past the end", weather.wind_now(44.0, -82.0, NOW[0] + 20 * 3600) is None)
check("wind_now is None before the start", weather.wind_now(44.0, -82.0, NOW[0] - 3 * 3600) is None)
check("wind_now inside the hours is the forecast",
      weather.wind_now(44.0, -82.0, NOW[0] + 3600) == (12.0, 200.0))
check("the router's wind_at still holds the end hour (unchanged)",
      weather.wind_at(44.0, -82.0, NOW[0] + 20 * 3600) == (12.0, 200.0))

print("an expired forecast with the link down reads as old, not as holding")
NOW[0] += weather.TTL_S + 60
LINK["up"] = False
use_bundle(pts_at(2))
drift.reset_state()
CALLS.clear()
d = drift.get_drift()
check(f"no fetch during get_drift ({len(CALLS)})", len(CALLS) == 0)
check(f"na, 'Forecast is old. No new one yet.' ({d.get('sub')!r})",
      d.get("status") == "na" and d.get("sub") == "Forecast is old. No new one yet."
      and d.get("stale") is True)

print("the background refresh backs off after a failure")
n1 = weather._refresh_once()
check(f"first pass: one failed fetch, then it stops ({n1})", n1 == 1)
n2 = weather._refresh_once()
check(f"right after: no fetch ({n2})", n2 == 0)
NOW[0] += weather.RETRY_S + 1
n3 = weather._refresh_once()
check(f"after {weather.RETRY_S} s: tries again ({n3})", n3 == 1)
NOW[0] += weather.RETRY_S + 1
n4 = weather._refresh_once()
check(f"second failure doubles the wait: no fetch after {weather.RETRY_S} s ({n4})", n4 == 0)
NOW[0] += weather.RETRY_S
LINK["up"] = True
n5 = weather._refresh_once()
check(f"after {2 * weather.RETRY_S} s it tries, the link is back: all 3 fetched ({n5})", n5 == 3)
check("the back-off resets on success", weather._link["fails"] == 0)

print("fetch_point backs off too, doubling, capped")
reset()
LINK["up"] = False
weather.fetch_point(45.0, -80.0)
weather.fetch_point(45.0, -80.0)
check(f"a failed cell is not refetched at once ({len(CALLS)})", len(CALLS) == 1)
NOW[0] += weather.RETRY_S + 1
weather.fetch_point(45.0, -80.0)
check(f"after {weather.RETRY_S} s: one more try ({len(CALLS)})", len(CALLS) == 2)
NOW[0] += weather.RETRY_S + 1
weather.fetch_point(45.0, -80.0)
check(f"then it waits {2 * weather.RETRY_S} s ({len(CALLS)})", len(CALLS) == 2)
check("the wait is capped", weather._backoff_s(50) == weather.RETRY_MAX_S)

print("the background refresh keeps to its request budget")
reset()
for i in range(weather.FETCH_BUDGET + 10):
    weather.request_refresh((40.0 + i * 0.1, -80.0))
n = weather._refresh_once()
check(f"one pass fetches at most the budget ({n} ≤ {weather.FETCH_BUDGET})",
      n == weather.FETCH_BUDGET)
check(f"the rest wait ({len(weather._want)})", len(weather._want) == 10)
check("a second pass in the same window fetches none", weather._refresh_once() == 0)
NOW[0] += weather.BUDGET_WINDOW_S + 1
check("the next window fetches the rest", weather._refresh_once() == 10)

print("a cell nobody asks about again drops off the list")
reset()
LINK["up"] = False
weather.request_refresh((41.0, -81.0))
NOW[0] += weather.WANT_TTL_S + 1
weather._refresh_once()
check(f"dropped, not fetched ({len(CALLS)}, {len(weather._want)})",
      not CALLS and not weather._want)

print("\n", "ALL PASS" if ok else "FAILURES ABOVE")
raise SystemExit(0 if ok else 1)
