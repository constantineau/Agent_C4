"""Wind forecast (5.4) — gridded wind for routing, from Open-Meteo (free, no key, no GRIB).

Provides a wind field wind_at(lat, lon, epoch) → (tws_kn, twd_deg) that the isochrone router
samples. Forecasts are fetched per coarse grid cell and cached; direction is the
meteorological "from" bearing, matching our TWD. If the network is unavailable, callers fall
back to the live measured wind so routing still runs on the bench.

Note: this is 10 m model wind (not heeled masthead true wind) — guidance-grade for routing,
not trimming. Spatial resolution is the model's; we cache by ~0.1° cell.
"""
import collections
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request

API = "https://api.open-meteo.com/v1/forecast"
TTL_S = 1800
RETRY_S = 60           # after a failed fetch, wait this long before the next one ...
RETRY_MAX_S = float(os.environ.get("C4_FORECAST_RETRY_MAX_S", "1800"))   # ... doubling up to this
# the background refresh (#42): at most FETCH_BUDGET network fetches per BUDGET_WINDOW_S
FETCH_BUDGET = int(os.environ.get("C4_FORECAST_FETCH_BUDGET", "30"))
BUDGET_WINDOW_S = 600
WANT_TTL_S = 3600      # a cell nobody asked about for this long drops off the refresh list
TICK_S = 10
BACKGROUND = True      # tests set False and drive _refresh_once() themselves
# a route refuses a forecast older than this (h), and fills a hole only from a cell this close (nm)
MAX_AGE_S = float(os.environ.get("C4_FORECAST_MAX_AGE_H", "6")) * 3600
FILL_NM = float(os.environ.get("C4_FORECAST_FILL_NM", "10"))
_cache = {}            # (rlat, rlon) -> {"fetched": ts, "hours": [(epoch, tws, twd)]}


def _cell(lat, lon):
    return (round(lat, 1), round(lon, 1))


def _unavailable(c):
    """What a caller sees for an expired forecast whose refresh failed: no hours. The old hours
    stay in the cache, where only the router reads them (cached_wind_at(stale_ok=True)), labelled."""
    return {"fetched": c["fetched"], "hours": [], "error": c.get("error"), "stale": True}


def _backoff_s(fails):
    """Wait after `fails` failed fetches in a row: RETRY_S, doubling, capped at RETRY_MAX_S."""
    return min(RETRY_MAX_S, RETRY_S * 2 ** (max(1, fails) - 1))


def _due(c, now):
    """True when this cell needs a network fetch now: no fresh forecast, and not inside the
    back-off after a failed one."""
    if _fresh(c, now):
        return False
    return not (c and c.get("failed_at") and now - c["failed_at"] < _backoff_s(c.get("fails", 1)))


def _fetch(key, now):
    """One network fetch for a cell; stores and returns the cache entry. A failure keeps any old
    hours (with `fetched` left old, so they read as stale, #157) and counts toward the back-off."""
    c = _cache.get(key)
    q = urllib.parse.urlencode({
        "latitude": key[0], "longitude": key[1],
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn", "forecast_days": 2, "timeformat": "unixtime",
        "timezone": "GMT",
    })
    try:
        with urllib.request.urlopen(f"{API}?{q}", timeout=10) as r:
            d = json.loads(r.read())
        h = d["hourly"]
        hours = [(int(t), float(s), float(w)) for t, s, w in
                 zip(h["time"], h["wind_speed_10m"], h["wind_direction_10m"])]
        c = {"fetched": now, "hours": hours}
    except Exception as exc:
        base = c if c and c.get("hours") else {"fetched": now, "hours": []}
        c = dict(base, error=str(exc), failed_at=now, fails=(c or {}).get("fails", 0) + 1)
    _cache[key] = c
    return c


def fetch_point(lat, lon):
    """Hourly wind forecast at the nearest ~0.1° cell, cached. Returns the cache entry, or no
    hours when the forecast has expired and its refresh failed (never old wind as current).
    May block on the network (10 s timeout): not for a request path that must stay fast; those
    use `wind_now`. After a failure the cell waits out a doubling back-off before the next try."""
    key = _cell(lat, lon)
    now = time.time()
    c = _cache.get(key)
    if not _due(c, now):
        return c if _fresh(c, now) or not c.get("hours") else _unavailable(c)
    c = _fetch(key, now)
    return _unavailable(c) if c.get("hours") and not _fresh(c, now) else c


# --- background refresh (#42): /strategy never waits on the network --------------------------
_want = {}             # cell -> last time someone asked for it
_spent = collections.deque()   # times of recent background fetches (the request budget)
_link = {"fails": 0, "failed_at": 0.0}   # back-off for the whole worker after a failed fetch
_lock = threading.Lock()
_wake = threading.Event()
_worker = []


def request_refresh(key):
    """Queue a cell for the background refresh. Never blocks."""
    _want[key] = time.time()
    if BACKGROUND:
        with _lock:
            if not _worker or not _worker[0].is_alive():
                t = threading.Thread(target=_run, name="forecast-refresh", daemon=True)
                _worker[:] = [t]
                t.start()
        _wake.set()


def _refresh_once(now=None):
    """One pass of the refresh: fetch the asked-for cells that are due, within the request budget,
    and stop at the first failure (the link is likely down; the worker backs off). Returns the
    number of network fetches made."""
    now = time.time() if now is None else now
    if _link["fails"] and now - _link["failed_at"] < _backoff_s(_link["fails"]):
        return 0
    while _spent and now - _spent[0] > BUDGET_WINDOW_S:
        _spent.popleft()
    n = 0
    for key, asked in list(_want.items()):
        c = _cache.get(key)
        if now - asked > WANT_TTL_S or _fresh(c, now):
            _want.pop(key, None)
            continue
        if not _due(c, now):
            continue
        if len(_spent) >= FETCH_BUDGET:
            break
        _spent.append(now)
        n += 1
        c = _fetch(key, now)
        if not _fresh(c, now):
            _link.update(fails=_link["fails"] + 1, failed_at=now)
            break
        _link.update(fails=0, failed_at=0.0)
        _want.pop(key, None)
    return n


def _run():
    while True:
        try:
            _refresh_once()
        except Exception:      # a bad pass must not kill the worker
            pass
        _wake.wait(TICK_S)
        _wake.clear()


def wind_now(lat, lon, epoch):
    """Fresh forecast (tws_kn, twd_deg) at a place and time, from the cache only, or None. Never
    touches the network, so it is safe inside /strategy: a cell with no fresh forecast is queued
    for the background refresh. Also None outside the forecast's hours (past its last hour, or
    before its first), never the end hour held over (#42). `cell_status` says why it was None."""
    key = _cell(lat, lon)
    c = _cache.get(key)
    if not _fresh(c, time.time()):
        request_refresh(key)
        return None
    return _interp(c["hours"], epoch, hold=False)


def _circ_interp(a, b, f):
    """Interpolate between two bearings (deg) by fraction f, shortest way around."""
    d = ((b - a + 180) % 360) - 180
    return (a + d * f) % 360


def wind_at(lat, lon, epoch):
    """(tws_kn, twd_deg) forecast at a position/time, or None if unavailable."""
    return _interp(fetch_point(lat, lon).get("hours") or [], epoch)


def _fresh(c, now):
    return bool(c and c.get("hours")) and now - c["fetched"] < TTL_S


def cell_status(lat, lon):
    """'fresh', 'stale' (a forecast older than TTL_S: its refresh failed) or 'missing'. Never fetches."""
    c = _cache.get(_cell(lat, lon))
    if not (c and c.get("hours")):
        return "missing"
    return "fresh" if _fresh(c, time.time()) else "stale"


def cached_wind_at(lat, lon, epoch, stale_ok=False):
    """Like `wind_at`, but from the cache only: never fetches, so it never blocks. None when this
    cell has no fresh forecast cached (or none at all, with `stale_ok`)."""
    c = _cache.get(_cell(lat, lon))
    if not (c and c.get("hours")):
        return None
    return _interp(c["hours"], epoch) if stale_ok or _fresh(c, time.time()) else None


def cell_age_s(lat, lon):
    """Seconds since this cell's cached forecast was fetched, or None if none is cached."""
    c = _cache.get(_cell(lat, lon))
    return time.time() - c["fetched"] if c and c.get("hours") else None


def nearest_cached_wind(lat, lon, epoch, max_nm=None):
    """(wind, fresh, age_s): among cached cells within `max_nm` (default FILL_NM) and no older
    than MAX_AGE_S, the nearest fresh one, else the nearest old one (fresh False). Never fetches.
    The router fills a forecast gap with it, so a route never mixes forecast and measured wind
    (#157). With none usable: (None, False, age_s) when only too-old cells were in reach (age of
    the youngest), else (None, False, None)."""
    now = time.time()
    lim = FILL_NM if max_nm is None else max_nm
    coslat = math.cos(math.radians(lat))
    best = {True: None, False: None}
    too_old = None
    for (clat, clon), c in list(_cache.items()):
        if not (c and c.get("hours")):
            continue
        d_nm = 60.0 * math.hypot(clat - lat, (clon - lon) * coslat)
        if d_nm > lim:
            continue
        age = now - c["fetched"]
        if age > MAX_AGE_S:                   # too old to use: skip it before choosing the nearest
            too_old = age if too_old is None else min(too_old, age)
            continue
        fresh = _fresh(c, now)
        if best[fresh] is None or d_nm < best[fresh][0]:
            best[fresh] = (d_nm, c)
    for fresh in (True, False):
        if best[fresh]:
            c = best[fresh][1]
            return _interp(c["hours"], epoch), fresh, now - c["fetched"]
    return None, False, too_old


def _interp(hrs, epoch, hold=True):
    """(tws, twd) from an hourly series at `epoch`, or None if empty. Outside the series it holds
    the end hour, or with hold=False returns None."""
    if not hrs:
        return None
    if not hold and not hrs[0][0] <= epoch <= hrs[-1][0]:
        return None
    if epoch <= hrs[0][0]:
        return (hrs[0][1], hrs[0][2])
    for (t0, s0, w0), (t1, s1, w1) in zip(hrs, hrs[1:]):
        if t0 <= epoch <= t1:
            f = (epoch - t0) / max(1, (t1 - t0))
            return (s0 + (s1 - s0) * f, _circ_interp(w0, w1, f))
    return (hrs[-1][1], hrs[-1][2])


def get_forecast(lat: float, lon: float, hours: int = 12):
    """Human/agent-facing forecast summary for a position: next N hours of wind."""
    c = fetch_point(lat, lon)
    hrs = c.get("hours") or []
    if not hrs:
        return {"available": False, "note": "No forecast. No internet?",
                "error": c.get("error")}
    now = time.time()
    up = [{"in_h": round((t - now) / 3600, 1), "tws": round(s), "twd": round(w)}
          for t, s, w in hrs if now <= t <= now + hours * 3600]
    return {"available": True, "lat": round(lat, 3), "lon": round(lon, 3),
            "source": "Open-Meteo GFS 10 m", "hours": up[:hours]}
