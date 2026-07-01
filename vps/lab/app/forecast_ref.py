"""Forecast reference fingerprint — the common-data forecast the playbook was BUILT on, frozen
into the bundle so the onboard executor can measure FORECAST-DRIFT (Lab-3 branch trigger b).

The playbook's routes are optimized on the multi-model GRIB blend, but that blend can't be re-fetched
onboard the Pi (no cfgrib/eccodes on the resource-constrained Tier-1 image). So the drift trigger uses
a **same-source** reference instead: at freeze time the Lab samples **Open-Meteo** (the free, keyless
10 m GFS forecast the onboard engine ALSO serves at `/forecast` — common data, available to all boats)
along the recommended variant's route at each waypoint's ETA, and freezes those (tws, twd) into the
bundle. Onboard, `drift.py` re-samples live Open-Meteo for the SAME (place, target-time) and compares —
so the signal is a clean "how has the common forecast for the race moved since we froze the plan",
with no cross-model bias to subtract. (A GRIB-native fingerprint is a heavier future upgrade.)

Pure-stdlib Open-Meteo access (mirrors `vps/agent/app/weather.py`), so the lab image needs nothing new.
Best-effort: if the network is down at freeze the fingerprint is simply omitted and the onboard drift
tile reports "no forecast reference" — the rest of the bundle is unaffected.
"""
import json
import math
import time
import urllib.parse
import urllib.request

API = "https://api.open-meteo.com/v1/forecast"
MODEL = "open-meteo-gfs"
_cache = {}     # (rlat, rlon) -> [(epoch, tws_kn, twd_deg)]


def _cell(lat, lon):
    return (round(lat, 1), round(lon, 1))


def _fetch_point(lat, lon):
    key = _cell(lat, lon)
    if key in _cache:
        return _cache[key]
    q = urllib.parse.urlencode({
        "latitude": key[0], "longitude": key[1],
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn", "forecast_days": 2, "timeformat": "unixtime", "timezone": "GMT",
    })
    try:
        with urllib.request.urlopen(f"{API}?{q}", timeout=10) as r:
            d = json.loads(r.read())
        h = d["hourly"]
        hours = [(int(t), float(s), float(w)) for t, s, w in
                 zip(h["time"], h["wind_speed_10m"], h["wind_direction_10m"])]
    except Exception:
        hours = []
    _cache[key] = hours
    return hours


def _circ_interp(a, b, f):
    d = ((b - a + 180) % 360) - 180
    return (a + d * f) % 360


def _wind_at(lat, lon, epoch):
    """(tws_kn, twd_deg) at a position/time, or None if unavailable / beyond the 2-day horizon."""
    hrs = _fetch_point(lat, lon)
    if not hrs or epoch > hrs[-1][0] + 3600:      # outside the forecast horizon → no honest sample
        return None
    if epoch <= hrs[0][0]:
        return (hrs[0][1], hrs[0][2])
    for (t0, s0, w0), (t1, s1, w1) in zip(hrs, hrs[1:]):
        if t0 <= epoch <= t1:
            f = (epoch - t0) / max(1, (t1 - t0))
            return (s0 + (s1 - s0) * f, _circ_interp(w0, w1, f))
    return (hrs[-1][1], hrs[-1][2])


def build_fingerprint(path, max_points=12):
    """Sample the common forecast along a route (`[{lat,lon,t}]`, t = epoch ETA) → the reference the
    onboard drift trigger compares against. Downsamples to ~`max_points` evenly across the route and
    keeps only points still in the forecast horizon. Returns the fingerprint dict, or None if the
    route is empty / the forecast can't be reached (caller omits it — drift degrades gracefully)."""
    pts = [p for p in (path or []) if p.get("lat") is not None and p.get("t") is not None]
    if len(pts) < 2:
        return None
    _cache.clear()
    step = max(1, len(pts) // max_points)
    sampled, seen = [], set()
    for i in range(0, len(pts), step):
        idx = min(i, len(pts) - 1)
        if idx in seen:
            continue
        seen.add(idx)
        p = pts[idx]
        w = _wind_at(p["lat"], p["lon"], p["t"])
        if w is None:
            continue
        sampled.append({"lat": round(p["lat"], 4), "lon": round(p["lon"], 4),
                        "t": int(round(p["t"])), "tws": round(w[0], 1), "twd": round(w[1], 1)})
    # always include the last route point if it's in horizon and not already there
    if pts and (len(pts) - 1) not in seen:
        p = pts[-1]
        w = _wind_at(p["lat"], p["lon"], p["t"])
        if w is not None:
            sampled.append({"lat": round(p["lat"], 4), "lon": round(p["lon"], 4),
                            "t": int(round(p["t"])), "tws": round(w[0], 1), "twd": round(w[1], 1)})
    if len(sampled) < 2:
        return None
    return {"source": MODEL, "built_at": int(round(time.time())),
            "note": "common Open-Meteo GFS forecast the playbook was built on; onboard drift "
                    "re-samples the same source live and compares.",
            "points": sampled}
