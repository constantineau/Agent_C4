"""Venue-specific weather-model skill — verification substrate (Phase 1).

Look back at what each weather model ACTUALLY forecast for a past window at a venue, compare it to
what was actually OBSERVED (independent of any model), and score each model's real error. The scores
become blend weights in Phase 2 (see docs/MODEL_SKILL_WEIGHTING.md). This module is the measurement:

  observed wind  (METAR / NDBC — independent truth)
  forecast wind  (Open-Meteo Historical Forecast API — each model's real PAST forecast, per model)
      → match on time → per-model bias + vector RMSE

Headline metric = VECTOR RMSE (kn): the magnitude of (forecast wind vector − observed wind vector),
which folds speed AND direction error into one number. Plus speed bias + circular direction bias for
interpretability (direction bias feeds de-biasing in Phase 2).

Pure stdlib (urllib, json, math). Self-test: `python3 -m app.modelskill` (from vps/lab, on PYTHONPATH).
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
import datetime as dt

# our model name -> Open-Meteo historical-forecast model id (see the doc's map). Best-effort:
# a model the archive doesn't carry for a venue simply returns no series and is skipped.
OM_MODELS = {
    "gfs": "gfs_global",
    "hrrr": "gfs_hrrr",
    "ecmwf": "ecmwf_ifs025",
    "icon": "icon_global",
    "gem": "gem_global",
}

_HIST_FC = "https://historical-forecast-api.open-meteo.com/v1/forecast"
_METAR = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_NDBC_RT = "https://www.ndbc.noaa.gov/data/realtime2/{station}.txt"

MATCH_TOL_S = 1800          # match a forecast hour to an obs within ±30 min


# ---------------------------------------------------------------------------- wind vector helpers
def _uv(tws_kn, twd_deg):
    """(speed kn, direction-FROM deg) -> (u, v) in kn (meteorological: wind blowing FROM twd)."""
    r = math.radians(twd_deg)
    return (-tws_kn * math.sin(r), -tws_kn * math.cos(r))


def _wrap180(d):
    return (d + 180.0) % 360.0 - 180.0


def _get(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": "agent-c4-modelskill/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ------------------------------------------------------------------------------------- forecast
def fetch_forecast(lat, lon, start_date, end_date, our_model, timeout=45):
    """Each hour a model FORECAST for [start_date, end_date] at (lat, lon) — day-ahead horizon the
    archive serves. Returns {epoch: (tws_kn, twd_deg)}. Unknown/absent model -> {}."""
    om = OM_MODELS.get(our_model)
    if not om:
        return {}
    qs = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "start_date": start_date, "end_date": end_date,
        "hourly": "wind_speed_10m,wind_direction_10m", "models": om,
        "wind_speed_unit": "kn", "timezone": "UTC",
    })
    try:
        payload = json.loads(_get(f"{_HIST_FC}?{qs}", timeout))
    except Exception:
        return {}
    h = (payload or {}).get("hourly") or {}
    times, spd, drc = h.get("time") or [], h.get("wind_speed_10m") or [], h.get("wind_direction_10m") or []
    out = {}
    for t, s, d in zip(times, spd, drc):
        if s is None or d is None:
            continue
        ep = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp()
        out[ep] = (float(s), float(d))
    return out


# ------------------------------------------------------------------------------------- observed
def fetch_metar(station, start_date, end_date, timeout=60):
    """Observed wind from the Iowa State ASOS/METAR archive. `station` = ICAO (KAPN) or 3-char (APN).
    Returns {epoch: (tws_kn, twd_deg)} over [start_date, end_date] UTC. Calm/variable/missing dropped."""
    sid = station[1:] if len(station) == 4 and station[0].upper() == "K" else station
    y1, m1, d1 = start_date.split("-")
    y2, m2, d2 = end_date.split("-")
    qs = urllib.parse.urlencode({
        "station": sid, "data": "drct", "tz": "Etc/UTC", "format": "onlycomma",
        "missing": "M", "trace": "T", "year1": y1, "month1": m1, "day1": d1,
        "year2": y2, "month2": m2, "day2": d2,
    })
    # request drct + sknt together (two data= params)
    url = f"{_METAR}?{qs}&data=sknt"
    try:
        text = _get(url, timeout).decode("utf-8", "replace")
    except Exception:
        return {}
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("station"):
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        _, valid, drct, sknt = parts[0], parts[1], parts[-2], parts[-1]
        if "M" in (drct, sknt) or drct == "" or sknt == "":
            continue
        try:
            d, s = float(drct), float(sknt)
        except ValueError:
            continue
        try:
            ep = dt.datetime.strptime(valid.strip(), "%Y-%m-%d %H:%M").replace(
                tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            continue
        out[ep] = (s, d)
    return out


def fetch_ndbc_realtime(station, timeout=45):
    """Observed wind from an NDBC buoy's realtime2 feed (last ~45 days). {epoch: (tws_kn, twd_deg)}.
    WSPD is m/s in the feed → converted to kn. For older windows use the historical stdmet archive."""
    try:
        text = _get(_NDBC_RT.format(station=station), timeout).decode("utf-8", "replace")
    except Exception:
        return {}
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        c = line.split()
        if len(c) < 7:
            continue
        try:
            yy, mm, dd, hh, mn = (int(c[i]) for i in range(5))
            wdir, wspd = c[5], c[6]
            if wdir in ("MM", "999") or wspd in ("MM", "99.0"):
                continue
            ep = dt.datetime(yy, mm, dd, hh, mn, tzinfo=dt.timezone.utc).timestamp()
            out[ep] = (float(wspd) * 1.943844, float(wdir))   # m/s -> kn
        except (ValueError, IndexError):
            continue
    return out


# --------------------------------------------------------------------------------------- scoring
def _match(obs, fc, tol_s=MATCH_TOL_S):
    """Pair each forecast hour with the nearest obs within tol_s. -> [(tws_o,twd_o,tws_f,twd_f)]."""
    if not obs:
        return []
    otimes = sorted(obs)
    pairs = []
    import bisect
    for ep, (tf, df) in fc.items():
        i = bisect.bisect_left(otimes, ep)
        best, bd = None, tol_s + 1
        for j in (i - 1, i):
            if 0 <= j < len(otimes):
                d = abs(otimes[j] - ep)
                if d < bd:
                    bd, best = d, otimes[j]
        if best is not None:
            to, do = obs[best]
            pairs.append((to, do, tf, df))
    return pairs


def score(obs, fc):
    """Per-model error from matched (obs, forecast) pairs. None if nothing matched."""
    pairs = _match(obs, fc)
    if not pairs:
        return None
    n = len(pairs)
    sq = spd_bias = 0.0
    sin_e = cos_e = 0.0
    for to, do, tf, df in pairs:
        uo, vo = _uv(to, do)
        uf, vf = _uv(tf, df)
        sq += (uf - uo) ** 2 + (vf - vo) ** 2        # squared vector error
        spd_bias += (tf - to)
        de = math.radians(_wrap180(df - do))
        sin_e += math.sin(de)
        cos_e += math.cos(de)
    return {
        "n": n,
        "vector_rmse_kn": round(math.sqrt(sq / n), 2),
        "speed_bias_kn": round(spd_bias / n, 2),                        # + = model over-forecasts speed
        "dir_bias_deg": round(math.degrees(math.atan2(sin_e, cos_e)), 1),  # + = model veered vs obs
    }


def verify_venue(lat, lon, station, start_date, end_date, models=("gfs", "hrrr", "ecmwf", "icon", "gem"),
                 obs_source="metar"):
    """Standalone 'which model was right here' report: observed vs each model's past forecast over a
    venue window. Returns {station, obs_n, models: {name: score|None}} sorted by vector RMSE."""
    obs = (fetch_metar(station, start_date, end_date) if obs_source == "metar"
           else fetch_ndbc_realtime(station))
    scores = {}
    for m in models:
        scores[m] = score(obs, fetch_forecast(lat, lon, start_date, end_date, m))
    return {"station": station, "window": [start_date, end_date],
            "obs_source": obs_source, "obs_n": len(obs), "models": scores}


# ------------------------------------------------------------------------------------- self-test
if __name__ == "__main__":
    # Alpena KAPN (shore of N Lake Huron, on the Mackinac course) vs each model's PAST forecast,
    # over the 2025 Bayview-Mackinac week. Real endpoints, real numbers.
    r = verify_venue(45.07, -83.56, "KAPN", "2025-07-12", "2025-07-14")
    print(f"\nVENUE VERIFICATION  station={r['station']}  window={r['window']}  "
          f"obs(METAR)={r['obs_n']} points\n")
    print(f"  {'model':6} {'n':>4}  {'vec RMSE':>9}  {'spd bias':>9}  {'dir bias':>9}")
    print("  " + "-" * 44)
    ranked = sorted(((m, s) for m, s in r["models"].items() if s), key=lambda x: x[1]["vector_rmse_kn"])
    for m, s in ranked:
        print(f"  {m:6} {s['n']:>4}  {s['vector_rmse_kn']:>7} kn  "
              f"{s['speed_bias_kn']:>+7} kn  {s['dir_bias_deg']:>+7}°")
    for m, s in r["models"].items():
        if not s:
            print(f"  {m:6}    —  (no forecast series / no match)")
    if ranked:
        best, worst = ranked[0], ranked[-1]
        print(f"\n  → best here: {best[0].upper()} ({best[1]['vector_rmse_kn']} kn) · "
              f"worst: {worst[0].upper()} ({worst[1]['vector_rmse_kn']} kn) — "
              f"this is what Phase 2 turns into blend weights.\n")
