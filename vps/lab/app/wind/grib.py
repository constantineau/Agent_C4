"""GRIB2 download + parse → a samplable wind grid frame.

One `GribFrame` holds a single (model, cycle, forecast-hour, member) field of 10 m wind over the
race bbox: the U/V components as numpy arrays plus the lat/lon grid and the field's valid time.
Regular lat/lon grids (GFS / GEFS / ECMWF) get proper bilinear interpolation; curvilinear Lambert
grids (NAM / HRRR), after a small bbox subset, are sampled nearest-neighbour (≤ grid spacing
error, fine for routing).

Parsing uses cfgrib/eccodes; the eccodes pip wheel bundles the binary so the slim image needs no
apt packages. Downloads are cached to `GRIB_CACHE` keyed by the request, so re-runs and the many
ensemble members are cheap.
"""
from __future__ import annotations

import math
import os
import tempfile
import urllib.request
from dataclasses import dataclass

import numpy as np

KN_PER_MS = 1.9438445
_UA = "Agent_C4-C4PerformanceLab/1.0 (+sailing race optimizer)"


# --- download ----------------------------------------------------------------
def http_download(url: str, dest: str, timeout: int = 60) -> str:
    """Download `url` to `dest` atomically; return `dest`. Caller decides caching."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if len(data) < 100:                       # NOMADS returns a tiny HTML error page on a miss
        raise OSError(f"grib download too small ({len(data)} B) — likely not yet posted: {url}")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest) or ".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest


# --- parse -------------------------------------------------------------------
def _norm_lon(lon):
    """GRIB longitudes come 0..360 or -180..180; normalise to -180..180."""
    return np.where(lon > 180.0, lon - 360.0, lon)


def open_uv(path: str):
    """Parse a 10 m-wind GRIB2 file → (lat, lon, u, v, regular).

    lat/lon are 1-D (regular grid) or 2-D (curvilinear); u/v are m/s, shaped like the grid.
    `regular` says whether bilinear (True) or nearest (False) sampling applies.
    """
    import xarray as xr

    ds = xr.open_dataset(
        path, engine="cfgrib",
        backend_kwargs={
            "indexpath": "",                  # don't write a .idx sidecar
            "filter_by_keys": {"typeOfLevel": "heightAboveGround", "level": 10},
        },
    )
    try:
        u = _pick(ds, ("u10", "10u", "u")).astype("float64")
        v = _pick(ds, ("v10", "10v", "v")).astype("float64")
        lat = ds["latitude"].values.astype("float64")
        lon = _norm_lon(ds["longitude"].values.astype("float64"))
    finally:
        ds.close()
    regular = lat.ndim == 1 and lon.ndim == 1
    return lat, lon, np.asarray(u), np.asarray(v), regular


def _pick(ds, names):
    for n in names:
        if n in ds:
            return ds[n].values
    raise KeyError(f"none of {names} in GRIB ({list(ds.data_vars)})")


# --- frame + sampling --------------------------------------------------------
@dataclass
class GribFrame:
    model: str
    member: str
    valid_time: float          # epoch seconds (UTC)
    lat: np.ndarray
    lon: np.ndarray
    u: np.ndarray
    v: np.ndarray
    regular: bool

    @classmethod
    def from_file(cls, path, model, member, valid_time):
        lat, lon, u, v, regular = open_uv(path)
        return cls(model, member, float(valid_time), lat, lon, u, v, regular)

    def sample_uv(self, lat: float, lon: float):
        """(u, v) m/s at a position, or None if the position is outside the grid."""
        if self.regular:
            return _bilinear(self.lat, self.lon, self.u, self.v, lat, lon)
        return _nearest(self.lat, self.lon, self.u, self.v, lat, lon)


def _bilinear(lats, lons, u, v, lat, lon):
    """Bilinear sample on a regular (1-D lats, 1-D lons) grid. Handles either axis order."""
    iy = _axis_frac(lats, lat)
    ix = _axis_frac(lons, lon)
    if iy is None or ix is None:
        return None
    y0, fy = iy
    x0, fx = ix
    y1 = min(y0 + 1, len(lats) - 1)
    x1 = min(x0 + 1, len(lons) - 1)

    def interp(a):
        return (a[y0, x0] * (1 - fy) * (1 - fx) + a[y0, x1] * (1 - fy) * fx +
                a[y1, x0] * fy * (1 - fx) + a[y1, x1] * fy * fx)

    return float(interp(u)), float(interp(v))


def _axis_frac(axis, val):
    """Return (i0, frac) so val sits between axis[i0] and axis[i0+1], or None if out of range.
    Works for ascending or descending axes."""
    asc = axis[-1] >= axis[0]
    a = axis if asc else axis[::-1]
    if val < a[0] - 1e-9 or val > a[-1] + 1e-9:
        return None
    i = int(np.searchsorted(a, val))
    i0 = max(0, min(i - 1, len(a) - 2))
    span = a[i0 + 1] - a[i0]
    frac = 0.0 if span == 0 else (val - a[i0]) / span
    frac = min(1.0, max(0.0, frac))
    if asc:
        return i0, frac
    j0 = len(axis) - 2 - i0           # map back to original (descending) indices
    return j0, 1.0 - frac


def _nearest(lat2d, lon2d, u, v, lat, lon):
    """Nearest-neighbour sample on a curvilinear (2-D) grid via squared-distance argmin."""
    coslat = math.cos(math.radians(lat))
    d2 = (lat2d - lat) ** 2 + ((lon2d - lon) * coslat) ** 2
    j, i = np.unravel_index(int(np.argmin(d2)), d2.shape)
    # reject if the nearest cell is absurdly far (position outside this model's domain)
    if d2[j, i] > 1.0:                # ~1° → outside; skip
        return None
    return float(u[j, i]), float(v[j, i])


def uv_to_tws_twd(u: float, v: float):
    """(u_east, v_north) m/s → (tws_kn, twd_deg) — TWD is the meteorological FROM bearing."""
    tws = math.hypot(u, v) * KN_PER_MS
    twd = (270.0 - math.degrees(math.atan2(v, u))) % 360.0
    return tws, twd
