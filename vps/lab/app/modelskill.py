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


def fetch_ndbc_historical(station, year, timeout=60):
    """A buoy's full-year archived stdmet (gzip). {epoch: (tws_kn, twd_deg)}. WSPD m/s → kn."""
    import gzip
    url = f"https://www.ndbc.noaa.gov/data/historical/stdmet/{station}h{year}.txt.gz"
    try:
        raw = gzip.decompress(_get(url, timeout))
        text = raw.decode("utf-8", "replace")
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
            out[dt.datetime(yy, mm, dd, hh, mn, tzinfo=dt.timezone.utc).timestamp()] = \
                (float(wspd) * 1.943844, float(wdir))
        except (ValueError, IndexError):
            continue
    return out


def fetch_ndbc_window(station, sd, ed):
    """Observed buoy wind over [sd, ed] (YYYY-MM-DD). Uses the per-year historical archive, falling
    back to the realtime feed for the recent ~45 days it doesn't yet cover. Filtered to the window."""
    lo = dt.datetime.fromisoformat(sd).replace(tzinfo=dt.timezone.utc).timestamp()
    hi = dt.datetime.fromisoformat(ed).replace(tzinfo=dt.timezone.utc).timestamp() + 86400
    obs = {}
    for y in range(int(sd[:4]), int(ed[:4]) + 1):
        obs.update(fetch_ndbc_historical(station, y))
    obs.update(fetch_ndbc_realtime(station))            # recent tail the archive lacks
    return {e: v for e, v in obs.items() if lo <= e <= hi}


# ---------------------------------------------------- deep history (pre-2021): reforecast / GRIB archive
REFORECAST_READY = False   # flipped on once the heavier pipeline lands (see docs/MODEL_SKILL_WEIGHTING.md)


def fetch_reforecast(lat, lon, sd, ed, model, year):
    """DEEP (pre-2021) archived-forecast provider — the heavier GRIB pipeline (GEFS Reforecast v12 on
    AWS ≈ 2000-2019; HRRR archive ≈ 2014+; ECMWF reforecasts). Returns {epoch: (tws_kn, twd_deg)}.
    STUB: returns {} until the pipeline is built, so pre-2021 seasonal windows currently no-op and the
    recency-weighted score rests on the Open-Meteo era. Wiring this is the next increment."""
    return {}


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


def _acc_new():
    return {"W": 0.0, "SSE": 0.0, "SB": 0.0, "SIN": 0.0, "COS": 0.0, "n": 0}


def _acc_add(acc, pairs, w=1.0):
    """Fold matched (obs, forecast) pairs into a weighted accumulator (w = per-sample weight, e.g. a
    recency weight so newer years count more). n counts actual pairs; W is the weight sum."""
    for to, do, tf, df in pairs:
        uo, vo = _uv(to, do)
        uf, vf = _uv(tf, df)
        acc["SSE"] += w * ((uf - uo) ** 2 + (vf - vo) ** 2)   # weighted squared vector error
        acc["SB"] += w * (tf - to)
        de = math.radians(_wrap180(df - do))
        acc["SIN"] += w * math.sin(de)
        acc["COS"] += w * math.cos(de)
        acc["W"] += w
        acc["n"] += 1
    return acc


def _acc_final(acc):
    if acc["n"] == 0 or acc["W"] <= 0:
        return None
    W = acc["W"]
    return {
        "n": acc["n"],
        "vector_rmse_kn": round(math.sqrt(acc["SSE"] / W), 2),
        "speed_bias_kn": round(acc["SB"] / W, 2),                            # + = over-forecasts speed
        "dir_bias_deg": round(math.degrees(math.atan2(acc["SIN"], acc["COS"])), 1),  # + = veered vs obs
    }


def score(obs, fc):
    """Per-model error from a single window's matched pairs (unweighted). None if nothing matched."""
    return _acc_final(_acc_add(_acc_new(), _match(obs, fc)))


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


# =========================================================================== Phase 2: store + weights
import os
import sqlite3

SKILL_DB = os.environ.get("LEARNING_DB", "/srv/learning/learning.db")   # share the Lab-4 volume
WEIGHTING_ON = os.environ.get("MODEL_SKILL_WEIGHTING", "on").lower() not in ("off", "0", "false")
SKILL_TTL_S = float(os.environ.get("MODEL_SKILL_TTL_S", str(6 * 3600)))  # re-score at most this often
MIN_N = int(os.environ.get("MODEL_SKILL_MIN_N", "12"))                  # need this many matches to trust
SHRINK_N = float(os.environ.get("MODEL_SKILL_SHRINK_N", "30"))          # pseudo-count: shrink to priors
CAP_LO = float(os.environ.get("MODEL_SKILL_CAP_LO", "0.5"))
CAP_HI = float(os.environ.get("MODEL_SKILL_CAP_HI", "2.0"))
DEFAULT_MODELS = ("gfs", "hrrr", "ecmwf", "icon", "gem")

# --- seasonal, multi-year, recency-weighted sampling -------------------------------------------
# Instead of one recent window, score the RACE's calendar window (±SEASON_PAD_DAYS) in EVERY year we
# can reach, and weight each year by recency (models change over time → recent years count more).
SEASON_PAD_DAYS = int(os.environ.get("MODEL_SKILL_SEASON_PAD_DAYS", "21"))   # ± around the race date
FIRST_YEAR = int(os.environ.get("MODEL_SKILL_FIRST_YEAR", "2010"))           # deepest year to attempt
OPENMETEO_FIRST_YEAR = 2021        # Open-Meteo historical-forecast archive floor (verified empirically)
RECENCY_HALFLIFE_Y = float(os.environ.get("MODEL_SKILL_RECENCY_HALFLIFE_Y", "8"))  # yr for weight to halve


def _conn():
    os.makedirs(os.path.dirname(SKILL_DB), exist_ok=True)
    c = sqlite3.connect(SKILL_DB, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS model_skill (
        venue_key TEXT, model TEXT, station TEXT, obs_source TEXT,
        n INTEGER, vector_rmse_kn REAL, speed_bias_kn REAL, dir_bias_deg REAL,
        window_start TEXT, window_end TEXT, updated_at REAL,
        n_years INTEGER, deep INTEGER,
        PRIMARY KEY (venue_key, model))""")
    for col, decl in (("n_years", "INTEGER"), ("deep", "INTEGER")):   # forward-compat for older tables
        try:
            c.execute(f"ALTER TABLE model_skill ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    return c


def save_scores(venue_key, station, obs_source, window, scores, n_years=1, deep=False):
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    with _conn() as c:
        for m, s in scores.items():
            if not s:
                continue
            c.execute("""INSERT OR REPLACE INTO model_skill VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (venue_key, m, station, obs_source, s["n"], s["vector_rmse_kn"],
                       s["speed_bias_kn"], s["dir_bias_deg"], window[0], window[1], now,
                       n_years, 1 if deep else 0))


def load_scores(venue_key):
    with _conn() as c:
        rows = c.execute("""SELECT model,n,vector_rmse_kn,speed_bias_kn,dir_bias_deg,updated_at,
                            window_start,window_end,station,obs_source,n_years,deep FROM model_skill
                            WHERE venue_key=?""", (venue_key,)).fetchall()
    return {r[0]: {"n": r[1], "vector_rmse_kn": r[2], "speed_bias_kn": r[3], "dir_bias_deg": r[4],
                   "updated_at": r[5], "window": [r[6], r[7]], "station": r[8], "obs_source": r[9],
                   "n_years": r[10], "deep": bool(r[11])}
            for r in rows}


def _fresh(scores):
    if not scores:
        return False
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    return all((now - s["updated_at"]) < SKILL_TTL_S for s in scores.values())


def _season_windows(center_md, first_year, last_year, pad_days, yesterday):
    """The race's calendar window (±pad_days around month/day = center_md) in each year first..last.
    Returns [(year, sd, ed)] with the end capped at `yesterday`; a window entirely in the future is
    skipped. So we score the same SEASON across many years, not one recent stretch."""
    mo, dy = center_md
    out = []
    for y in range(first_year, last_year + 1):
        try:
            c = dt.date(y, mo, dy)
        except ValueError:
            c = dt.date(y, mo, min(dy, 28))
        start, end = c - dt.timedelta(days=pad_days), c + dt.timedelta(days=pad_days)
        if start > yesterday:
            continue
        if end > yesterday:
            end = yesterday
        out.append((y, start.isoformat(), end.isoformat()))
    return out


def _recency_weight(year, ref_year):
    """Newer years count more (models change over time). Exponential decay by RECENCY_HALFLIFE_Y."""
    return 0.5 ** ((ref_year - year) / max(0.5, RECENCY_HALFLIFE_Y))


def forecast_series(lat, lon, sd, ed, model, year):
    """Each model's PAST forecast for a window — dispatched by year. Open-Meteo for the archive era
    (>=2021); the heavier reforecast/GRIB-archive pipeline for older years (deep history)."""
    if year >= OPENMETEO_FIRST_YEAR:
        return fetch_forecast(lat, lon, sd, ed, model)
    return fetch_reforecast(lat, lon, sd, ed, model, year)


def obs_series(station, sd, ed):
    """Observed wind for a window from the venue station (METAR range, or NDBC historical/realtime)."""
    if station["kind"] == "ndbc":
        return fetch_ndbc_window(station["id"], sd, ed)
    return fetch_metar(station["id"], sd, ed)


def refresh_venue_skill(venue, models=DEFAULT_MODELS, race_date=None, force=False):
    """Score each model over the venue's SEASONAL window across MANY years, recency-weighted, at the
    station (co-located with the obs — a shore forecast point vs shore obs; never mid-lake vs shore).
    Persists + caches (skips within SKILL_TTL_S unless force). Returns {} if the venue has no station.

    `race_date` (a datetime.date) centres the seasonal window on the race's time of year; without it we
    centre on today. Years reach back to FIRST_YEAR — pre-2021 needs the deep reforecast provider
    (fetch_reforecast); until that's wired those years simply contribute nothing."""
    st = (venue or {}).get("station")
    if not st:
        return {}
    if not force:
        cached = load_scores(venue["key"])
        if _fresh(cached):
            return cached
    yesterday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).date()
    center = race_date or yesterday
    windows = _season_windows((center.month, center.day), FIRST_YEAR, yesterday.year,
                              SEASON_PAD_DAYS, yesterday)
    lat, lon = st["lat"], st["lon"]
    accs = {m: _acc_new() for m in models}
    years_used, deep_used = set(), False
    for (year, sd, ed) in windows:
        obs = obs_series(st, sd, ed)
        if not obs:
            continue
        w = _recency_weight(year, yesterday.year)
        for m in models:
            fc = forecast_series(lat, lon, sd, ed, m, year)
            if fc:
                before = accs[m]["n"]
                _acc_add(accs[m], _match(obs, fc), w)
                if accs[m]["n"] > before:
                    years_used.add(year)
                    if year < OPENMETEO_FIRST_YEAR:
                        deep_used = True
    scores = {m: _acc_final(a) for m, a in accs.items()}
    yrs = sorted(years_used)
    span = [f"{yrs[0]}" if yrs else "", f"{yrs[-1]}" if yrs else ""]
    save_scores(venue["key"], st["id"], st["kind"], span, scores,
                n_years=len(yrs), deep=deep_used)
    return load_scores(venue["key"])


def derive_weights(scores):
    """Turn per-model error into blend weights. Returns {model: {weight, bias_speed_kn, bias_dir_deg,
    rmse, n}} for models with enough data (>=MIN_N); others are omitted (caller treats as identity).

    weight = geomean-normalized inverse-variance (1/rmse^2), SHRUNK toward 1.0 by sample count, capped.
    bias = the measured speed/dir offset, likewise shrunk. Needs >=2 scored models to have a reference."""
    scored = {m: s for m, s in scores.items() if s and s.get("n", 0) >= MIN_N and s.get("vector_rmse_kn")}
    if len(scored) < 2:
        return {}
    inv = {m: 1.0 / max(0.25, s["vector_rmse_kn"]) ** 2 for m, s in scored.items()}
    geo = math.exp(sum(math.log(v) for v in inv.values()) / len(inv))
    out = {}
    for m, s in scored.items():
        n = s["n"]
        shrink = n / (n + SHRINK_N)                       # 0 (no data) .. 1 (lots)
        factor = inv[m] / geo                             # >1 better than typical here, <1 worse
        weight = 1.0 + (factor - 1.0) * shrink
        weight = max(CAP_LO, min(CAP_HI, weight))
        out[m] = {"weight": round(weight, 3),
                  "bias_speed_kn": round(s["speed_bias_kn"] * shrink, 2),
                  "bias_dir_deg": round(s["dir_bias_deg"] * shrink, 1),
                  "rmse": s["vector_rmse_kn"], "n": n}
    return out


def venue_weights(venue, models=DEFAULT_MODELS, race_date=None, refresh=True):
    """Top-level: (optionally) refresh venue skill, derive weights, and return the full display payload
    used by the optimizer + the Lab panel. `model_weights`/`model_bias` are the drop-ins for
    build_windfield; `table` is the sorted per-model scorecard for the UI. enabled=False => identity."""
    if not WEIGHTING_ON or not venue or not venue.get("station"):
        return {"enabled": False, "reason": ("disabled" if not WEIGHTING_ON else "no obs station"),
                "venue_key": (venue or {}).get("key"), "model_weights": {}, "model_bias": {}, "table": []}
    scores = (refresh_venue_skill(venue, models=models, race_date=race_date) if refresh
              else load_scores(venue["key"]))
    derived = derive_weights(scores)
    model_weights = {m: d["weight"] for m, d in derived.items()}
    model_bias = {m: (d["bias_speed_kn"], d["bias_dir_deg"]) for m, d in derived.items()}
    table = sorted(
        [{"model": m, **scores[m], **derived.get(m, {"weight": 1.0})} for m in scores if scores[m]],
        key=lambda r: r["vector_rmse_kn"])
    st = venue["station"]
    any_score = next((s for s in scores.values() if s), None)
    return {"enabled": bool(model_weights), "venue_key": venue["key"], "venue_label": venue.get("label"),
            "station": st["id"], "station_name": st.get("name"), "obs_source": st["kind"],
            "window": any_score["window"] if any_score else None,
            "n_years": any_score.get("n_years") if any_score else 0,
            "deep": any_score.get("deep") if any_score else False,
            "recency_halflife_y": RECENCY_HALFLIFE_Y,
            "model_weights": model_weights, "model_bias": model_bias, "table": table,
            "note": None if model_weights else "not enough matched obs yet — routing on static priors"}


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
