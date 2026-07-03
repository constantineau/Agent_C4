"""Deep (pre-2021) archived-forecast providers for model-skill weighting — Phase 2b.

Byte-range `.idx` subsetting from public AWS GRIB archives, so we pull only the 10 m UGRD/VGRD
message(s) we need (~1 MB) instead of whole global/CONUS files:

  - HRRR archive     `noaa-hrrr-bdp-pds`        2014-08 → now  (CONUS, operational, our best model here)
  - GEFS Reforecast  `noaa-gefs-retrospective`  ~2005 → 2019   (ensemble ctrl, fixed 2020 model)

Each `.idx` line is `num:byteoffset:date:VAR:LEVEL:FCST:...`; the message spans [offset, next_offset).
We range-GET the U and V messages, concat (GRIB2 messages are self-contained), and parse with the
existing eccodes via `wind.grib.open_uv`. Returns {valid_epoch: (tws_kn, twd_deg)}.

Lead time is held at a consistent short-range same-day band (the 00Z run, +6..18 h) — deliberately
chosen because it exists across the FULL archive depth (HRRR extended f24+ only begins ~2019, but
f06..f18 go back to 2014; GEFS reforecast reaches 2005). All models in a comparison are scored at the
same lead, so the relative ranking stays fair. Heavy / offline: driven by an explicit deep-backfill,
never inline on optimize.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import tempfile
import urllib.request

import numpy as np

from .wind import grib

KN_PER_MS = grib.KN_PER_MS
HRRR_BASE = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
GEFS_BASE = "https://noaa-gefs-retrospective.s3.amazonaws.com/GEFSv12/reforecast"
LEAD_FHRS = (6, 12, 18)               # same-day 00Z run, +6..18 h — exists across full archive depth


def _get(url, timeout=90, rng=None):
    req = urllib.request.Request(url, headers={"User-Agent": "agent-c4-deepfc/1"})
    if rng:
        req.add_header("Range", f"bytes={rng[0]}-{rng[1]}" if rng[1] != "" else f"bytes={rng[0]}-")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _idx(url, timeout=45):
    """Parse a wgrib2-style .idx → [(offset, var, level, fcst), ...] in file order."""
    recs = []
    try:
        lines = _get(url, timeout).decode("utf-8", "replace").splitlines()
    except Exception:
        return recs
    for ln in lines:
        p = ln.split(":")
        if len(p) < 6:
            continue
        try:
            recs.append((int(p[1]), p[3], p[4], p[5]))
        except ValueError:
            continue
    return recs


def _msg_range(recs, i):
    return recs[i][0], (recs[i + 1][0] - 1 if i + 1 < len(recs) else "")


def _find(recs, var, level, fcst=None):
    for i, r in enumerate(recs):
        if r[1] == var and r[2] == level and (fcst is None or r[3] == fcst):
            return i
    return None


def _point_uv(blob, lat, lon):
    """Nearest-cell (u, v) m/s from a small GRIB blob (one U + one V 10 m message)."""
    fd, path = tempfile.mkstemp(suffix=".grib2")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        la, lo, u, v, _regular = grib.open_uv(path)
        if la.ndim == 1:
            i = int(np.argmin(np.abs(la - lat)))
            j = int(np.argmin(np.abs(lo - lon)))
            return float(u[i, j]), float(v[i, j])
        d = (la - lat) ** 2 + (lo - lon) ** 2
        idx = np.unravel_index(int(np.argmin(d)), d.shape)
        return float(u[idx]), float(v[idx])
    finally:
        os.remove(path)


def _uv_kn_twd(u, v):
    return (math.hypot(u, v) * KN_PER_MS, (270.0 - math.degrees(math.atan2(v, u))) % 360.0)


# ------------------------------------------------------------------------------------------- HRRR
def hrrr_uv(cycle, fhr, lat, lon):
    """One HRRR 10 m wind at (lat, lon): cycle='YYYYMMDDHH', forecast hour `fhr`. (tws_kn, twd_deg)|None."""
    d, hh = cycle[:8], cycle[8:10]
    base = f"{HRRR_BASE}/hrrr.{d}/conus/hrrr.t{hh}z.wrfsfcf{fhr:02d}.grib2"
    recs = _idx(base + ".idx")
    iu, iv = _find(recs, "UGRD", "10 m above ground"), _find(recs, "VGRD", "10 m above ground")
    if iu is None or iv is None:
        return None
    try:
        blob = _get(base, rng=_msg_range(recs, iu)) + _get(base, rng=_msg_range(recs, iv))
        return _uv_kn_twd(*_point_uv(blob, lat, lon))
    except Exception:
        return None


# ------------------------------------------------------------------------------------------- GEFS
def gefs_uv(cycle, fhr, lat, lon):
    """One GEFS Reforecast v12 control 10 m wind: U and V live in separate ugrd_hgt/vgrd_hgt files."""
    y, ymd = cycle[:4], cycle[:8]
    fcst = f"{fhr} hour fcst"
    day_dir = "Days:1-10" if fhr <= 240 else "Days:10-16"
    blobs = []
    for var, fn in (("UGRD", "ugrd_hgt"), ("VGRD", "vgrd_hgt")):
        base = f"{GEFS_BASE}/{y}/{cycle}/c00/{day_dir}/{fn}_{cycle}_c00.grib2"
        recs = _idx(base + ".idx")
        i = _find(recs, var, "10 m above ground", fcst)
        if i is None:
            return None
        try:
            blobs.append(_get(base, rng=_msg_range(recs, i)))
        except Exception:
            return None
    return _uv_kn_twd(*_point_uv(blobs[0] + blobs[1], lat, lon))


# --------------------------------------------------------------------- window fetch (day-ahead band)
def _series(fetch, lat, lon, sd, ed):
    """Build {valid_epoch: (tws,twd)} over [sd, ed] by sampling LEAD_FHRS from each day's 00Z run
    (valid the same day, ~6-18 h lead). `fetch(cycle, fhr, lat, lon)` is the model point provider."""
    out = {}
    day = dt.date.fromisoformat(sd)
    end = dt.date.fromisoformat(ed)
    while day <= end:
        cycle = f"{day:%Y%m%d}00"
        for fhr in LEAD_FHRS:
            r = fetch(cycle, fhr, lat, lon)
            if r is None:
                continue
            valid = dt.datetime(day.year, day.month, day.day, 0, tzinfo=dt.timezone.utc) \
                + dt.timedelta(hours=fhr)
            out[valid.timestamp()] = r
        day += dt.timedelta(days=1)
    return out


def hrrr_series(lat, lon, sd, ed):
    return _series(hrrr_uv, lat, lon, sd, ed)


def gefs_series(lat, lon, sd, ed):
    return _series(gefs_uv, lat, lon, sd, ed)


if __name__ == "__main__":
    # de-risk: one real 2018 HRRR-archive 10 m wind at Alpena KAPN
    r = hrrr_uv("2018070100", 24, 45.07, -83.56)
    print("HRRR 2018-07-01 00Z f24 @ KAPN:", None if r is None else f"{r[0]:.1f} kt / {r[1]:.0f}°")
    g = gefs_uv("2018070100", 24, 45.07, -83.56)
    print("GEFS reforecast 2018-07-01 00Z f24 @ KAPN:", None if g is None else f"{g[0]:.1f} kt / {g[1]:.0f}°")
