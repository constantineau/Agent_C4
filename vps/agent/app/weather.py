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
import time
import urllib.parse
import urllib.request

API = "https://api.open-meteo.com/v1/forecast"
TTL_S = 1800
_cache = {}            # (rlat, rlon) -> {"fetched": ts, "hours": [(epoch, tws, twd)]}


def _cell(lat, lon):
    return (round(lat, 1), round(lon, 1))


def fetch_point(lat, lon):
    """Hourly wind forecast at the nearest ~0.1° cell, cached. Returns the cache entry."""
    key = _cell(lat, lon)
    now = time.time()
    c = _cache.get(key)
    if c and now - c["fetched"] < TTL_S:
        return c
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
        c = c or {"fetched": now, "hours": [], "error": str(exc)}
    _cache[key] = c
    return c


def _circ_interp(a, b, f):
    """Interpolate between two bearings (deg) by fraction f, shortest way around."""
    d = ((b - a + 180) % 360) - 180
    return (a + d * f) % 360


def wind_at(lat, lon, epoch):
    """(tws_kn, twd_deg) forecast at a position/time, or None if unavailable."""
    c = fetch_point(lat, lon)
    hrs = c.get("hours") or []
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
        return {"available": False, "note": "forecast unavailable (no network?)",
                "error": c.get("error")}
    now = time.time()
    up = [{"in_h": round((t - now) / 3600, 1), "tws": round(s), "twd": round(w)}
          for t, s, w in hrs if now <= t <= now + hours * 3600]
    return {"available": True, "lat": round(lat, 3), "lon": round(lon, 3),
            "source": "Open-Meteo GFS 10 m", "hours": up[:hours]}
