"""Live BUOY OBSERVATIONS — the up-course leading indicator (locked 2026-07-07).

NDBC/GLOS realtime observations are COMMON PUBLIC DATA available to all boats (RRS 41 clean, same
class as GRIB), pulled onboard over Starlink. A buoy up-course is the fleet's oldest leading
indicator: it reports the breeze the boat will be IN before the boat gets there — real observation,
not forecast. This module gives the matcher + the Tier-2 copilot that signal:

  - per station: the latest obs (TWS/TWD), its age, a short TREND (change over the last ~2 h), the
    range/bearing from the boat, and whether it is UP-COURSE (within a bearing cone of the course
    to the next mark — falling back to COG when no route is active);
  - the headline `upcourse` read: nearest up-course station's TWS delta + signed TWD shift vs the
    boat's OWN instruments ("45003 up-course 18 nm: 4 kn MORE pressure, 15° right of here").

Stations come from the frozen bundle's `buoys` block (synthesis picks stations near the course —
the homework pattern), else the BUOY_STATIONS env ("id:lat:lon:name;..."), else a curated
Great-Lakes fallback. Fetches are cached (~10 min — the obs cadence) and best-effort: offline or
out of season (lakes buoys are pulled in winter) → stations report stale/absent, never an error.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import time
import urllib.request

from . import deviation
from . import navigator

_RT_URL = "https://www.ndbc.noaa.gov/data/realtime2/{station}.txt"
REFRESH_S = float(os.environ.get("BUOY_REFRESH_S", "600"))       # NDBC obs cadence ~10 min
MAX_NM = float(os.environ.get("BUOY_MAX_NM", "60"))              # ignore stations beyond this
UPCOURSE_CONE = float(os.environ.get("BUOY_UPCOURSE_CONE_DEG", "75"))
STALE_MIN = float(os.environ.get("BUOY_STALE_MIN", "90"))        # obs older than this = stale
FETCH_TIMEOUT = float(os.environ.get("BUOY_FETCH_TIMEOUT", "10"))

# curated Great-Lakes fallback (mirrors the Lab's venue list) — the bundle/env override this
_GL_FALLBACK = [
    {"id": "45003", "lat": 45.35, "lon": -82.84, "name": "N Lake Huron buoy"},
    {"id": "45008", "lat": 44.28, "lon": -82.42, "name": "S Lake Huron buoy"},
    {"id": "45002", "lat": 45.34, "lon": -86.41, "name": "N Lake Michigan buoy"},
    {"id": "45007", "lat": 42.67, "lon": -87.02, "name": "S Lake Michigan buoy"},
]

_CACHE: dict = {}          # station -> (fetched_at, [(epoch, tws_kn, twd_deg), ...] newest-first)


def _wrap180(a):
    return ((a + 180.0) % 360.0) - 180.0


def _hav_nm(lat1, lon1, lat2, lon2):
    r = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def stations():
    """The station list, by precedence: frozen bundle `buoys` block → BUOY_STATIONS env → the
    curated Great-Lakes fallback."""
    try:
        blk = (deviation._load_playbook() or {}).get("buoys") or []
        if blk:
            return [s for s in blk if s.get("id") and s.get("lat") is not None]
    except Exception:
        pass
    env = os.environ.get("BUOY_STATIONS", "").strip()
    if env:
        out = []
        for part in env.split(";"):
            bits = part.split(":")
            if len(bits) >= 3:
                out.append({"id": bits[0].strip(), "lat": float(bits[1]), "lon": float(bits[2]),
                            "name": bits[3].strip() if len(bits) > 3 else bits[0].strip()})
        if out:
            return out
    return list(_GL_FALLBACK)


def _fetch(url, timeout):          # seam for tests
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _parse_realtime2(text, keep_h=3.0):
    """NDBC realtime2 → [(epoch, tws_kn, twd_deg)] newest-first, last `keep_h` hours only.
    Same column layout the Lab's model-skill verifier parses (WDIR deg true, WSPD m/s)."""
    now = time.time()
    out = []
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
            if now - ep > keep_h * 3600:
                break                     # the feed is newest-first — past the window, stop
            out.append((ep, round(float(wspd) * 1.943844, 1), float(wdir)))
        except (ValueError, IndexError):
            continue
    return out


def _obs(station_id):
    """Cached recent obs for one station (newest-first). [] offline / no data / out of season."""
    now = time.time()
    hit = _CACHE.get(station_id)
    if hit and now - hit[0] < REFRESH_S:
        return hit[1]
    try:
        rows = _parse_realtime2(_fetch(_RT_URL.format(station=station_id), FETCH_TIMEOUT))
    except Exception:
        rows = hit[1] if hit else []      # keep the last good read on a transient miss
    _CACHE[station_id] = (now, rows)
    return rows


def _trend(rows):
    """TWS/TWD change per hour over the recent obs (newest-first; needs ≥2 spanning ≥20 min)."""
    if len(rows) < 2:
        return None
    (t1, s1, d1), (t0, s0, d0) = rows[0], rows[-1]
    span_h = (t1 - t0) / 3600.0
    if span_h < 0.33:
        return None
    return {"tws_kn_per_h": round((s1 - s0) / span_h, 1),
            "twd_deg_per_h": round(_wrap180(d1 - d0) / span_h)}


def get_buoys(route=None):
    """The buoy picture: every configured station with its latest obs, trend, range/bearing and
    up-course flag, plus the headline up-course deltas vs the boat's own wind. Common public data
    read by the boat's own computer — legal in-race; `na` with no fix or no reachable station."""
    live = navigator._latest()
    lat, lon = live.get("lat"), live.get("lon")
    if lat is None or lon is None:
        return {"available": False, "note": "no position fix"}
    # course-to-next-mark from the active route (falls back to COG — practice / no course loaded)
    ref_brg, ref_src = live.get("cog"), "cog"
    try:
        nav = navigator.get_navigator(route)
        nm_brg = ((nav.get("next_mark") or {}).get("bearing_deg")
                  if nav.get("available") else None)
        if nm_brg is not None:
            ref_brg, ref_src = nm_brg, "course"
    except Exception:
        pass
    own_tws, own_twd = live.get("tws"), live.get("twd")
    now = time.time()
    rows, upcourse = [], None
    for st in stations():
        rng = _hav_nm(lat, lon, st["lat"], st["lon"])
        if rng > MAX_NM:
            continue
        brg = _bearing(lat, lon, st["lat"], st["lon"])
        up = (ref_brg is not None and abs(_wrap180(brg - ref_brg)) <= UPCOURSE_CONE)
        obs = _obs(st["id"])
        row = {"id": st["id"], "name": st.get("name") or st["id"],
               "range_nm": round(rng, 1), "bearing": round(brg), "up_course": up}
        if obs:
            ep, tws, twd = obs[0]
            age_min = (now - ep) / 60.0
            row.update({"tws_kn": tws, "twd_deg": round(twd), "age_min": round(age_min),
                        "stale": age_min > STALE_MIN, "trend": _trend(obs)})
            if own_tws is not None and own_twd is not None and age_min <= STALE_MIN:
                row["delta"] = {"tws_kn": round(tws - own_tws, 1),
                                "twd_deg": round(_wrap180(twd - own_twd))}
            if up and not row.get("stale") and row.get("delta") and (
                    upcourse is None or rng < upcourse["range_nm"]):
                upcourse = {"station": st["id"], "name": row["name"], "range_nm": row["range_nm"],
                            "tws_delta_kn": row["delta"]["tws_kn"],
                            "twd_shift_deg": row["delta"]["twd_deg"],
                            "tws_kn": tws, "twd_deg": round(twd), "age_min": round(age_min)}
        else:
            row.update({"tws_kn": None, "stale": True,
                        "note": "no recent obs (offline or out of season)"})
        rows.append(row)
    if not rows:
        return {"available": False, "note": f"no configured buoy within {MAX_NM:g} nm"}
    rows.sort(key=lambda r: (not r["up_course"], r["range_nm"]))
    return {"available": True, "stations": rows, "upcourse": upcourse,
            "own": {"tws_kn": round(own_tws, 1) if own_tws is not None else None,
                    "twd_deg": round(own_twd) if own_twd is not None else None},
            "ref_bearing": round(ref_brg) if ref_brg is not None else None, "ref_src": ref_src,
            "based": ["ndbc_realtime", "own_instruments"], "conf": "engine",
            "disclaimer": ("Public NDBC observations (available to all boats) read by the boat's "
                           "own computer — a leading indicator, aged and best-effort; lake buoys "
                           "are seasonal.")}


def clear_cache():
    _CACHE.clear()
