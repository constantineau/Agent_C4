"""Wind forecast (5.4) — gridded wind for routing, from Open-Meteo (free, no key, no GRIB).

Provides a wind field wind_at(lat, lon, epoch) → (tws_kn, twd_deg) that the isochrone router
samples. Forecasts are fetched per coarse grid cell and cached; direction is the
meteorological "from" bearing, matching our TWD. If the network is unavailable, callers fall
back to the live measured wind so routing still runs on the bench.

Note: this is 10 m model wind (not heeled masthead true wind) — guidance-grade for routing,
not trimming. Spatial resolution is the model's; we cache by ~0.1° cell.
"""
import json
import math
import os
import time
import urllib.parse
import urllib.request

API = "https://api.open-meteo.com/v1/forecast"
TTL_S = 1800
RETRY_S = 60           # after a failed refresh, serve the old forecast this long before trying again
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


def fetch_point(lat, lon):
    """Hourly wind forecast at the nearest ~0.1° cell, cached. Returns the cache entry, or no
    hours when the forecast has expired and its refresh failed (never old wind as current)."""
    key = _cell(lat, lon)
    now = time.time()
    c = _cache.get(key)
    if c and now - c["fetched"] < TTL_S:
        return c
    if c and c.get("hours") and now - c.get("failed_at", 0) < RETRY_S:
        return _unavailable(c)        # a refresh just failed: no new timeout yet
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
        if c and c.get("hours"):
            # keep the old forecast, marked: `fetched` stays old so it reads as stale (#157)
            _cache[key] = c = dict(c, error=str(exc), failed_at=now)
            return _unavailable(c)
        c = c or {"fetched": now, "hours": [], "error": str(exc)}
    _cache[key] = c
    return c


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


def _interp(hrs, epoch):
    """(tws, twd) from an hourly series at `epoch` (held at either end), or None if empty."""
    if not hrs:
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
