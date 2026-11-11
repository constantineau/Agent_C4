"""Venue study: score the weather models at a place ahead of any race there (the Lab "Venue study" page).

Before, a venue was only studied as a side effect of routing a race there, and only where the
curated Great Lakes station list reached. A study takes a place anywhere (lat/lon + radius), the
race dates (or a month), and the observation stations the user ticked (found by obsstations), then:

  1. scores each weather model against the measured wind at those stations, over that window in
     every year the forecast archive reaches (recency-weighted: modelskill.score_stations);
  2. sums up what the wind is usually like in that window, from the same observations
     (direction x speed table, and by time of day);
  3. saves both under the venue key a race there resolves to (venue._cell_key of the study's
     location), writes the model scores into model_skill as study rows (kept until the study is
     run again), and remembers the stations, so venue.resolve() for a race within MATCH_NM of the
     study uses the study's key and stations. A race can also point at a study by setting its
     `venue_tag` to the study's key.

Models that don't cover the place (HRRR outside the US) are skipped with a plain note.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3

from . import modelskill

MATCH_NM = float(os.environ.get("VENUE_STUDY_MATCH_NM", "30"))   # a race this close uses the study
STUDY_MODELS = ("gfs", "hrrr", "ecmwf", "icon", "gem")
DIRS8 = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
SPEED_BINS = ((0, 6, "0–6"), (6, 12, "6–12"), (12, 18, "12–18"), (18, 25, "18–25"), (25, 999, "25+"))
CALM_KN = 1.0
TOD = ((0, 6, "Night"), (6, 12, "Morning"), (12, 18, "Afternoon"), (18, 24, "Evening"))


def _db_path():
    return modelskill.SKILL_DB              # same learning.db as the model_skill rows


def _conn(create=True):
    p = _db_path()
    if not create and not os.path.exists(p):
        return None
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    c = sqlite3.connect(p, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS venue_study (
        key TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL, radius_nm REAL,
        spec TEXT, stations TEXT, result TEXT, updated_at REAL)""")
    return c


# --------------------------------------------------------------------------------- window
def window_spec(body):
    """Normalise the study window from the page: race dates {start, end} (YYYY-MM-DD; the year is
    ignored, the season is what counts) or {month: 1..12}. Raises ValueError with a plain message."""
    body = body or {}
    if body.get("month"):
        m = int(body["month"])
        if not 1 <= m <= 12:
            raise ValueError("Pick a month.")
        last = (dt.date(2001 if m < 12 else 2002, m % 12 + 1, 1) - dt.timedelta(days=1)).day
        return {"start_md": [m, 1], "end_md": [m, last], "pad_days": 0,
                "label": dt.date(2001, m, 1).strftime("%B")}
    try:
        s = dt.date.fromisoformat(str(body.get("start") or ""))
        e = dt.date.fromisoformat(str(body.get("end") or body.get("start") or ""))
    except ValueError:
        raise ValueError("Enter the race dates or pick a month.")
    if e < s:
        raise ValueError("The end date is before the start.")
    if (e - s).days > 60:
        raise ValueError("Race dates longer than 60 days. Pick a month instead.")
    pad = modelskill.SEASON_PAD_DAYS
    return {"start_md": [s.month, s.day], "end_md": [e.month, e.day], "pad_days": pad,
            "label": f"{s:%b %-d}–{e:%b %-d}, ±{pad} days"}


def windows_for(spec, include_deep=False, today=None):
    yesterday = (today or dt.datetime.now(dt.timezone.utc).date()) - dt.timedelta(days=1)
    first = modelskill.FIRST_YEAR if include_deep else modelskill.OPENMETEO_FIRST_YEAR
    return modelskill._span_windows(tuple(spec["start_md"]), tuple(spec["end_md"]), first,
                                    yesterday.year, spec["pad_days"], yesterday), yesterday


def probe_window(spec, today=None):
    """The window the station check looks at: the latest past year whose whole window is over
    (this year's may be only days old), else the latest there is. None when there is none."""
    wins, yesterday = windows_for(spec, today=today)
    full = modelskill._span_windows(tuple(spec["start_md"]), tuple(spec["end_md"]),
                                    wins[0][0] if wins else yesterday.year, yesterday.year,
                                    spec["pad_days"], dt.date.max - dt.timedelta(days=400))
    done = [w for w, f in zip(wins, full) if w == f]
    return (done or wins or [None])[-1]


# -------------------------------------------------------------------------------- climate
def _dir8(d):
    return DIRS8[int(((d % 360) + 22.5) // 45) % 8]


def _hourly(obs):
    """One observation per hour (the one nearest the top of the hour): METARs come several an hour
    in bad weather and buoys every 10 min, which would over-count those hours."""
    best = {}
    for ep, v in obs.items():
        h = int(round(ep / 3600.0))
        off = abs(ep - h * 3600)
        if h not in best or off < best[h][0]:
            best[h] = (off, v)
    return {h * 3600: v for h, (_, v) in best.items()}


def _utc_offset_fn(tz, lon):
    """epoch -> local hour. The station's own time zone when known, else sun time from longitude."""
    if tz:
        try:
            from zoneinfo import ZoneInfo
            z = ZoneInfo(tz)
            return (lambda ep: dt.datetime.fromtimestamp(ep, z).hour), "local time"
        except Exception:      # noqa: BLE001 — unknown zone: fall back to sun time
            pass
    off = round(lon / 15.0)
    return (lambda ep: int(((ep / 3600.0) + off) % 24)), "sun time"


def climate(obs, tz=None, lon=0.0):
    """What the wind is usually like, from observations {epoch: (kn, deg_from)}: share of hours by
    direction x speed, calm share, average, and by time of day. None when there is nothing."""
    hrs = _hourly(obs)
    if not hrs:
        return None
    hour_of, tz_label = _utc_offset_fn(tz, lon)
    n = len(hrs)
    grid = {d: [0] * len(SPEED_BINS) for d in DIRS8}
    calm = 0
    tod = [{"label": lab, "hours": f"{a:02d}–{b:02d}", "n": 0, "sum": 0.0, "dirs": {}} for a, b, lab in TOD]
    total = 0.0
    years = set()
    for ep, (kn, deg) in hrs.items():
        total += kn
        years.add(dt.datetime.fromtimestamp(ep, dt.timezone.utc).year)
        t = tod[hour_of(ep) // 6]
        t["n"] += 1
        t["sum"] += kn
        if kn < CALM_KN:
            calm += 1
            continue
        d = _dir8(deg)
        t["dirs"][d] = t["dirs"].get(d, 0) + 1
        for i, (lo, hi, _) in enumerate(SPEED_BINS):
            if lo <= kn < hi:
                grid[d][i] += 1
                break
    pct = lambda k: round(100.0 * k / n, 1)
    rose = [{"dir": d, "pct": pct(sum(grid[d])), "bins": [pct(k) for k in grid[d]]} for d in DIRS8]
    tod_out = [{"label": t["label"], "hours": t["hours"], "n": t["n"],
                "mean_kn": round(t["sum"] / t["n"], 1) if t["n"] else None,
                "top_dir": max(t["dirs"], key=t["dirs"].get) if t["dirs"] else None} for t in tod]
    speeds = [v[0] for v in hrs.values()]
    return {"hours": n, "years": sorted(years), "mean_kn": round(total / n, 1), "calm_pct": pct(calm),
            "light_pct": pct(sum(1 for s in speeds if s < 6)),
            "strong_pct": pct(sum(1 for s in speeds if s >= 18)),
            "bins": [b[2] for b in SPEED_BINS], "rose": rose, "tod": tod_out, "time_label": tz_label}


def climate_line(c):
    """One plain sentence: 'Mostly E and NE. 11 kn on average. Windiest in the afternoon.'"""
    if not c:
        return "No measured wind in this window."
    top = sorted(c["rose"], key=lambda r: -r["pct"])
    dirs = [r["dir"] for r in top[:2] if r["pct"] >= 15] or [top[0]["dir"]]
    out = f"Mostly {' and '.join(dirs)}. {c['mean_kn']:g} kn on average."
    tods = [t for t in c["tod"] if t["mean_kn"] is not None and t["n"] >= 10]
    if len(tods) >= 2:
        hi = max(tods, key=lambda t: t["mean_kn"])
        lo = min(tods, key=lambda t: t["mean_kn"])
        if hi["mean_kn"] - lo["mean_kn"] >= 2:
            out += f" Windiest in the {hi['label'].lower()}."
    return out


# -------------------------------------------------------------------------------- verdict
def verdict(table):
    """'ICON was closest; GFS runs 2 kn light.' from the per-model rows (routed models only)."""
    rows = [r for r in table if not r.get("reference") and r.get("vector_rmse_kn") is not None]
    if not rows:
        return "Not enough data to compare the models here."
    best = min(rows, key=lambda r: r["vector_rmse_kn"])
    out = f"{best['model'].upper()} was closest"
    if len(rows) == 1:
        out = f"Only {best['model'].upper()} could be checked here"
    biased = [r for r in rows if abs(r.get("speed_bias_kn") or 0) >= 1.0]
    if biased:
        b = max(biased, key=lambda r: abs(r["speed_bias_kn"]))
        out += f"; {b['model'].upper()} runs {abs(b['speed_bias_kn']):.1f} kn " \
               f"{'light' if b['speed_bias_kn'] < 0 else 'windy'}"
    return out + "."


# ------------------------------------------------------------------------------------ run
def venue_key(lat, lon):
    from . import venue
    return venue._cell_key(lat, lon)


def run(name, lat, lon, radius_nm, spec, stations, include_deep=False, log=None, today=None):
    """Run a study (blocking; the Lab runs it as a background job). Returns the saved study."""
    if not stations:
        raise ValueError("Tick at least one station.")
    log = log or (lambda m: None)
    key = venue_key(lat, lon)
    windows, yesterday = windows_for(spec, include_deep, today)
    if not windows:
        raise ValueError("No past years in that window yet.")
    models = STUDY_MODELS + (("gefs",) if include_deep else ())
    obs_by_station = {}
    stations = [dict(st) for st in stations]
    for st in stations:              # a buoy has no clock of its own: borrow the nearest airport's
        if not st.get("tz"):
            near = [m for m in stations if m.get("tz")]
            if near:
                from . import venue
                st["tz"] = min(near, key=lambda m: venue._haversine_nm(st["lat"], st["lon"],
                                                                        m["lat"], m["lon"]))["tz"]

    def keep(st, year, obs):
        obs_by_station.setdefault(st["id"], {}).update(obs)

    log(f"Scoring {len(models)} models at {len(stations)} station(s), {len(windows)} years…")
    scores, years, notes = modelskill.score_stations(stations, windows, models, yesterday.year,
                                                     include_deep=include_deep, log=log, on_obs=keep)
    covered = [m for m in models
               if any(modelskill.covers(m, s["lat"], s["lon"]) for s in stations)]
    for m in covered:
        if not scores.get(m):
            notes.append(f"{m.upper()}: no forecasts matched")
    derived = modelskill.derive_weights({m: s for m, s in scores.items()
                                         if s and m not in modelskill.REFERENCE_ONLY})
    table = []
    for m, s in scores.items():
        if not s:
            continue
        ys = years.get(m) or []
        ref = m in modelskill.REFERENCE_ONLY
        table.append({"model": m, "reference": ref, "n": s["n"], "vector_rmse_kn": s["vector_rmse_kn"],
                      "speed_bias_kn": s["speed_bias_kn"], "dir_bias_deg": s["dir_bias_deg"],
                      "years": ys, "deep": s.get("deep", False),
                      "weight": None if ref else (derived.get(m) or {}).get("weight", 1.0),
                      "weighted": m in derived})
    table.sort(key=lambda r: r["vector_rmse_kn"])
    climates = []
    for st in stations:
        c = climate(obs_by_station.get(st["id"]) or {}, st.get("tz"), st["lon"])
        if c:
            climates.append(dict(c, station=st["id"], name=st.get("name") or st["id"],
                                 kind=st["kind"], line=climate_line(c)))
    if not table and not climates:
        notes.append("No measured wind at these stations in this window.")
    span = sorted({y for ys in years.values() for y in ys})
    label = "+".join(s["id"] for s in stations)
    src = "+".join(sorted({s["kind"] for s in stations}))
    modelskill.save_scores(key, label, src, [str(span[0]) if span else "", str(span[-1]) if span else ""],
                           {m: s for m, s in scores.items() if s}, study=True)
    result = {"verdict": verdict(table), "table": table, "notes": notes, "climate": climates,
              "deep": include_deep, "years": span, "window_label": spec.get("label"),
              "min_n": modelskill.MIN_N}
    st_keep = [{k: s[k] for k in ("id", "kind", "name", "lat", "lon", "tz") if k in s} for s in stations]
    save(key, name, lat, lon, radius_nm, spec, st_keep, result)
    log("Done.")
    return load(key)


# ---------------------------------------------------------------------------------- store
def save(key, name, lat, lon, radius_nm, spec, stations, result):
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO venue_study VALUES (?,?,?,?,?,?,?,?,?)",
                  (key, name, lat, lon, radius_nm, json.dumps(spec), json.dumps(stations),
                   json.dumps(result), now))


def _row(r):
    return {"key": r[0], "name": r[1], "lat": r[2], "lon": r[3], "radius_nm": r[4],
            "spec": json.loads(r[5] or "{}"), "stations": json.loads(r[6] or "[]"),
            "result": json.loads(r[7] or "null"), "updated_at": r[8]}


def load(key):
    c = _conn(create=False)
    if c is None:
        return None
    with c:
        r = c.execute("SELECT * FROM venue_study WHERE key=?", (key,)).fetchone()
    return _row(r) if r else None


def list_all():
    c = _conn(create=False)
    if c is None:
        return []
    with c:
        rows = c.execute("SELECT * FROM venue_study ORDER BY updated_at DESC").fetchall()
    out = []
    for r in rows:
        s = _row(r)
        out.append({"key": s["key"], "name": s["name"], "lat": s["lat"], "lon": s["lon"],
                    "window_label": (s["result"] or {}).get("window_label") or s["spec"].get("label"),
                    "verdict": (s["result"] or {}).get("verdict"), "updated_at": s["updated_at"]})
    return out


def delete(key):
    c = _conn(create=False)
    if c is None:
        return False
    with c:
        n = c.execute("DELETE FROM venue_study WHERE key=?", (key,)).rowcount
        try:
            c.execute("DELETE FROM model_skill WHERE venue_key=? AND study=1", (key,))
        except sqlite3.OperationalError:      # no model_skill table yet
            pass
    return bool(n)


def match(lat, lon, tag=None):
    """The study a race at (lat, lon) should use: the one keyed `tag` when the race names one, else
    the nearest study within MATCH_NM. None when there is none (or no study store yet)."""
    try:
        c = _conn(create=False)
        if c is None:
            return None
        with c:
            if tag:
                r = c.execute("SELECT * FROM venue_study WHERE key=?", (tag,)).fetchone()
                return _row(r) if r else None
            rows = c.execute("SELECT * FROM venue_study").fetchall()
    except sqlite3.Error:
        return None
    from . import venue
    best, bd = None, MATCH_NM
    for r in rows:
        d = venue._haversine_nm(lat, lon, r[2], r[3])
        if d <= bd:
            best, bd = r, d
    return _row(best) if best else None


if __name__ == "__main__":                       # quick manual check: python3 -m app.venuestudy
    print(json.dumps(list_all(), indent=1))
