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
import os
import threading
import time
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

# Where each model has data. A model missing here is global. Approximate boxes are enough: a point
# inside the box but off the real grid just comes back empty and is skipped like any other gap.
# (A step toward the per-area source registry, #309.)
MODEL_COVERAGE = {
    "hrrr": {"box": (21.1, 52.7, -134.1, -60.9), "label": "US only"},   # (s, n, w, e)
}


def covers(model, lat, lon):
    """True if `model` has forecasts at (lat, lon). Global models always do."""
    c = MODEL_COVERAGE.get(model)
    if not c:
        return True
    s, n, w, e = c["box"]
    return s <= lat <= n and w <= lon <= e


def coverage_note(model):
    c = MODEL_COVERAGE.get(model)
    return f"{model.upper()}: not here ({c['label']})" if c else f"{model.upper()}: not here"


# Open-Meteo's free tier allows ~600 calls/min and 5,000/hour; a long study makes a few hundred.
# Space the calls out so a study never trips the limit (429s are retried anyway, see _get).
OPENMETEO_MIN_GAP_S = float(os.environ.get("OPENMETEO_MIN_GAP_S", "0.5"))
_om_lock = threading.Lock()
_om_last = [0.0]


def _om_wait():
    with _om_lock:
        gap = OPENMETEO_MIN_GAP_S - (time.monotonic() - _om_last[0])
        if gap > 0:
            time.sleep(gap)
        _om_last[0] = time.monotonic()


# ---------------------------------------------------------------------------- wind vector helpers
def _uv(tws_kn, twd_deg):
    """(speed kn, direction-FROM deg) -> (u, v) in kn (meteorological: wind blowing FROM twd)."""
    r = math.radians(twd_deg)
    return (-tws_kn * math.sin(r), -tws_kn * math.cos(r))


def _wrap180(d):
    return (d + 180.0) % 360.0 - 180.0


_RETRY_CODES = {429, 500, 502, 503, 504}   # transient — throttling under a heavy backfill


def _get(url, timeout=45, retries=5):
    """HTTP GET with retry+backoff on transient failures (429/5xx/timeout) so a throttled call under a
    heavy multi-year backfill isn't silently dropped as no-data. 404 raises immediately."""
    import time
    import urllib.error
    req = urllib.request.Request(url, headers={"User-Agent": "agent-c4-modelskill/1"})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in _RETRY_CODES and attempt < retries - 1:
                last = e
            else:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(min(8.0, 0.5 * (2 ** attempt)))
    raise last


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
    _om_wait()
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
# Iowa State answers parallel requests from one address with errors (seen live: 4 of 7 airports
# came back empty when asked 6 at a time, all fine one at a time). Every METAR fetch in the Lab
# (Find stations, a venue study, a race-time refresh) goes through this one gate.
_IEM_ONE_AT_A_TIME = threading.Semaphore(1)


def fetch_metar(station, start_date, end_date, timeout=60, deadline=None):
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
    # `deadline` (time.monotonic()): a caller that stops waiting then (Find stations) doesn't queue
    # behind other METAR requests, or send one, after it has given up
    wait_s = None if deadline is None else deadline - time.monotonic()
    if wait_s is not None and (wait_s <= 0 or not _IEM_ONE_AT_A_TIME.acquire(timeout=wait_s)):
        return {}
    if wait_s is None:
        _IEM_ONE_AT_A_TIME.acquire()
    try:
        if deadline is not None and time.monotonic() >= deadline:
            return {}
        text = _get(url, min(timeout, max(1.0, deadline - time.monotonic())) if deadline else timeout,
                    retries=1 if deadline else 5).decode("utf-8", "replace")
    except Exception:
        return {}
    finally:
        _IEM_ONE_AT_A_TIME.release()
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
    # the archive's file names are lower case (LKWF1 -> lkwf1h2025.txt.gz); upper case is a 404
    url = f"https://www.ndbc.noaa.gov/data/historical/stdmet/{str(station).lower()}h{year}.txt.gz"
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
# Maps our model -> (first available year, deep series provider in deepfc). Deep coverage is uneven:
# HRRR archive from 2015 (extended runs); GEFS Reforecast v12 is its OWN 'gefs' line back to ~2005 (NOT
# a gfs proxy — that contaminated gfs). 'gfs'/ECMWF/ICON/GEM have no deep archive → Open-Meteo (2021+).
def _deep_providers():
    from . import deepfc
    return {"hrrr": (2015, deepfc.hrrr_series), "gefs": (2005, deepfc.gefs_series)}


def fetch_reforecast(lat, lon, sd, ed, model, year):
    """DEEP (pre-2021) archived-forecast provider via the AWS GRIB byte-range pipeline (`deepfc`).
    Returns {epoch: (tws_kn, twd_deg)}, or {} if this model has no deep source for `year`. HEAVY — only
    reached when a caller explicitly opts into deep (the backfill), never on the inline optimize path."""
    try:
        prov = _deep_providers().get(model)
        if not prov:
            return {}
        first, series = prov
        return series(lat, lon, sd, ed) if year >= first else {}
    except Exception:
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
import sqlite3

SKILL_DB = os.environ.get("LEARNING_DB", "/srv/learning/learning.db")   # share the Lab-4 volume
WEIGHTING_ON = os.environ.get("MODEL_SKILL_WEIGHTING", "on").lower() not in ("off", "0", "false")
SKILL_TTL_S = float(os.environ.get("MODEL_SKILL_TTL_S", str(6 * 3600)))  # re-score at most this often
MIN_N = int(os.environ.get("MODEL_SKILL_MIN_N", "12"))                  # need this many matches to trust
SHRINK_N = float(os.environ.get("MODEL_SKILL_SHRINK_N", "30"))          # pseudo-count: shrink to priors
CAP_LO = float(os.environ.get("MODEL_SKILL_CAP_LO", "0.5"))
CAP_HI = float(os.environ.get("MODEL_SKILL_CAP_HI", "2.0"))
DEFAULT_MODELS = ("gfs", "hrrr", "ecmwf", "icon", "gem", "gefs")
# tracked for reference but NOT blended → kept out of the weight-derivation geomean so it can't distort
# the models that ARE routed on. (GEFS reforecast is our deep NCEP-global history; it isn't in the blend.)
REFERENCE_ONLY = {"gefs"}

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
        n_years INTEGER, deep INTEGER, study INTEGER,
        PRIMARY KEY (venue_key, model))""")
    for col, decl in (("n_years", "INTEGER"), ("deep", "INTEGER"), ("study", "INTEGER")):   # forward-compat for older tables
        try:
            c.execute(f"ALTER TABLE model_skill ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    return c


def save_scores(venue_key, station, obs_source, window, scores, study=False, conn=None):
    """Persist one score row per model. Each score dict carries its OWN `n_years` (count of distinct
    seasons that model actually matched) and `deep` (did it use a pre-2021 archive). `study`=True
    marks rows a Lab venue study wrote: kept until the study is run again (see _fresh). `conn`: write
    inside the caller's transaction (the venue study saves its record in the same one)."""
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    if conn is None:
        with _conn() as c:                       # its own transaction
            return save_scores(venue_key, station, obs_source, window, scores, study, conn=c)
    c = conn                                     # the caller's transaction: no commit here
    c.execute("DELETE FROM model_skill WHERE venue_key=?", (venue_key,))   # replace the venue set
    for m, s in scores.items():
        if not s:
            continue
        c.execute("""INSERT OR REPLACE INTO model_skill (venue_key, model, station, obs_source,
                     n, vector_rmse_kn, speed_bias_kn, dir_bias_deg, window_start, window_end,
                     updated_at, n_years, deep, study) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (venue_key, m, station, obs_source, s["n"], s["vector_rmse_kn"],
                   s["speed_bias_kn"], s["dir_bias_deg"], window[0], window[1], now,
                   s.get("n_years", 1), 1 if s.get("deep") else 0, 1 if study else 0))


def load_scores(venue_key):
    with _conn() as c:
        rows = c.execute("""SELECT model,n,vector_rmse_kn,speed_bias_kn,dir_bias_deg,updated_at,
                            window_start,window_end,station,obs_source,n_years,deep,study FROM model_skill
                            WHERE venue_key=?""", (venue_key,)).fetchall()
    return {r[0]: {"n": r[1], "vector_rmse_kn": r[2], "speed_bias_kn": r[3], "dir_bias_deg": r[4],
                   "updated_at": r[5], "window": [r[6], r[7]], "station": r[8], "obs_source": r[9],
                   "n_years": r[10], "deep": bool(r[11]), "study": bool(r[12])}
            for r in rows}


def _fresh(scores):
    if not scores:
        return False
    if any(s.get("deep") or s.get("study") for s in scores.values()):
        return True                                  # deep backfill is historical + static → permanent;
                                                     # a venue study stands until it is run again
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


def _span_windows(start_md, end_md, first_year, last_year, pad_days, yesterday):
    """Like _season_windows but for a date RANGE (race dates, or a whole month): in each year,
    [start_md - pad, end_md + pad]. An end before the start wraps into the next year (a race over
    New Year). Windows wholly in the future are skipped; the end is capped at `yesterday`."""
    def _d(y, md):
        try:
            return dt.date(y, md[0], md[1])
        except ValueError:
            return dt.date(y, md[0], min(md[1], 28))
    out = []
    for y in range(first_year, last_year + 1):
        s0 = _d(y, start_md)
        e0 = _d(y + 1 if tuple(end_md) < tuple(start_md) else y, end_md)
        start, end = s0 - dt.timedelta(days=pad_days), e0 + dt.timedelta(days=pad_days)
        if start > yesterday:
            continue
        out.append((y, start.isoformat(), min(end, yesterday).isoformat()))
    return out


def _recency_weight(year, ref_year):
    """Newer years count more (models change over time). Exponential decay by RECENCY_HALFLIFE_Y."""
    return 0.5 ** ((ref_year - year) / max(0.5, RECENCY_HALFLIFE_Y))


def forecast_series(lat, lon, sd, ed, model, year, include_deep=False):
    """Each model's PAST forecast for a window — dispatched by year. Open-Meteo for the archive era
    (>=2021); the heavier reforecast/GRIB-archive pipeline for older years, but ONLY when include_deep
    (the offline backfill) — the inline optimize path never triggers the heavy deep pulls."""
    if year >= OPENMETEO_FIRST_YEAR:
        return fetch_forecast(lat, lon, sd, ed, model)
    return fetch_reforecast(lat, lon, sd, ed, model, year) if include_deep else {}


def obs_series(station, sd, ed, deadline=None):
    """Observed wind for a window from the venue station (METAR range, or NDBC historical/realtime).
    `deadline` bounds the wait for a METAR request (Find stations)."""
    if station["kind"] == "ndbc":
        return fetch_ndbc_window(station["id"], sd, ed)
    return fetch_metar(station["id"], sd, ed, deadline=deadline)


def score_stations(stations, windows, models, ref_year, include_deep=False, log=None, on_obs=None):
    """Score each model against the observed wind at each station over `windows` [(year, sd, ed)],
    recency-weighted. The forecast is always sampled AT the station (co-located). A model with no
    forecasts at a station (outside its area, #309) is skipped there with a note, never an error.
    `on_obs(station, year, obs)` sees each window's observations (the venue study builds its
    wind summary from them). Returns (scores {model: score|None}, years {model: sorted years},
    notes [plain text])."""
    accs = {m: _acc_new() for m in models}
    model_years = {m: set() for m in models}          # per-model distinct seasons matched
    model_deep = {m: False for m in models}           # per-model: did it use a pre-2021 archive
    notes = []
    for m in models:
        if not any(covers(m, st["lat"], st["lon"]) for st in stations):
            notes.append(coverage_note(m))
    for st in stations:
        lat, lon = st["lat"], st["lon"]
        for (year, sd, ed) in windows:
            if year < OPENMETEO_FIRST_YEAR and not include_deep:
                continue                              # skip deep years on the fast inline path
            obs = obs_series(st, sd, ed)
            if not obs:
                if log:
                    log(f"{st['id']} {year}: no obs")
                continue
            if on_obs:
                on_obs(st, year, obs)
            w = _recency_weight(year, ref_year)
            got = {}
            for m in models:
                if not covers(m, lat, lon):
                    continue
                fc = forecast_series(lat, lon, sd, ed, m, year, include_deep=include_deep)
                if not fc:
                    continue
                before = accs[m]["n"]
                _acc_add(accs[m], _match(obs, fc), w)
                added = accs[m]["n"] - before
                if added:
                    got[m] = added
                    model_years[m].add(year)
                    if year < OPENMETEO_FIRST_YEAR:
                        model_deep[m] = True
            if log:
                log(f"{st['id']} {year}: obs={len(obs)} " + " ".join(f"{m}+{n}" for m, n in got.items()))
    scores = {}
    for m, a in accs.items():
        sc = _acc_final(a)
        if sc:
            sc["n_years"], sc["deep"] = len(model_years[m]), model_deep[m]
        scores[m] = sc
    return scores, {m: sorted(ys) for m, ys in model_years.items()}, notes


def refresh_venue_skill(venue, models=DEFAULT_MODELS, race_date=None, force=False, include_deep=False,
                        log=None):
    """Score each model over the venue's SEASONAL window across MANY years, recency-weighted, POOLING
    every anchor station (a shore METAR + an over-water buoy). The forecast is always sampled AT the
    station being scored against (co-located obs — never mid-lake forecast vs shore obs). Persists +
    caches (skips within SKILL_TTL_S unless force; a deep result is permanent). {} if no station.

    `race_date` (a datetime.date) centres the seasonal window on the race's time of year; without it we
    centre on today. `include_deep`=False (the inline optimize path) scores only the Open-Meteo era
    (>=2021), fast; the offline backfill sets it True to fold in the heavy pre-2021 GRIB archives."""
    stations = (venue or {}).get("stations") or ([venue["station"]] if (venue or {}).get("station") else [])
    if not stations:
        return {}
    if not force:
        cached = load_scores(venue["key"])
        if _fresh(cached):
            return cached
    yesterday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).date()
    center = race_date or yesterday
    windows = _season_windows((center.month, center.day), FIRST_YEAR, yesterday.year,
                              SEASON_PAD_DAYS, yesterday)
    scores, _years, _notes = score_stations(stations, windows, models, yesterday.year,
                                            include_deep=include_deep, log=log)
    all_years = sorted({y for ys in _years.values() for y in ys})
    span = [f"{all_years[0]}" if all_years else "", f"{all_years[-1]}" if all_years else ""]
    label = "+".join(s["id"] for s in stations)
    src = "+".join(sorted({s["kind"] for s in stations}))
    with _conn() as c:
        # one write transaction: if a venue study (or a deep backfill) saved while this refresh
        # was scoring, keep its rows rather than overwrite them with this quicker race-date score
        c.execute("BEGIN IMMEDIATE")
        # (a study always wins, even over a forced backfill: a studied venue deepens through the
        # study itself; a forced backfill may replace an older deep score)
        kept = c.execute(
            "SELECT 1 FROM model_skill WHERE venue_key=? AND (study=1 OR (deep=1 AND ?)) LIMIT 1",
            (venue["key"], 0 if force else 1)).fetchone()
        if not kept:
            save_scores(venue["key"], label, src, span, scores, conn=c)
    return load_scores(venue["key"])


def backfill_deep(venue, models=DEFAULT_MODELS, race_date=None, log=None):
    """Explicit OFFLINE deep backfill: re-score the venue INCLUDING the pre-2021 GRIB archives (HRRR
    2015+, GEFS reforecast 2005+), recency-weighted, and persist. Heavy (thousands of byte-range GRIB
    gets, retried with backoff) — run on demand, not inline. The result is marked deep=True so the fast
    optimize path then reads it as permanent and never clobbers it. `log` gets per-station-year progress."""
    return refresh_venue_skill(venue, models=models, race_date=race_date,
                               force=True, include_deep=True, log=log)


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
    # derive weights only over BLENDED models (exclude reference-only lines like gefs so they can't
    # shift the geomean that sets the routed models' weights); reference lines still show in the table.
    derived = derive_weights({m: s for m, s in scores.items() if m not in REFERENCE_ONLY})
    model_weights = {m: d["weight"] for m, d in derived.items()}
    model_bias = {m: (d["bias_speed_kn"], d["bias_dir_deg"]) for m, d in derived.items()}
    table = sorted(
        [{"model": m, "reference": m in REFERENCE_ONLY, **scores[m],
          **derived.get(m, {"weight": None if m in REFERENCE_ONLY else 1.0})}
         for m in scores if scores[m]],
        key=lambda r: r["vector_rmse_kn"])
    stations = venue.get("stations") or ([venue["station"]] if venue.get("station") else [])
    any_score = next((s for s in scores.values() if s), None)
    return {"enabled": bool(model_weights), "venue_key": venue["key"], "venue_label": venue.get("label"),
            "station": "+".join(s["id"] for s in stations),
            "station_name": ", ".join(s.get("name", s["id"]) for s in stations),
            "obs_source": "+".join(sorted({s["kind"] for s in stations})),
            "window": any_score["window"] if any_score else None,
            "n_years": max((s.get("n_years", 0) for s in scores.values() if s), default=0),
            "deep": any(s.get("deep") for s in scores.values() if s),
            "recency_halflife_y": RECENCY_HALFLIFE_Y,
            "model_weights": model_weights, "model_bias": model_bias, "table": table,
            "note": None if model_weights else "Not enough measured wind here yet"}


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
