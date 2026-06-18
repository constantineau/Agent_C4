"""Weather model sources for the wind field.

Each `ModelSource` knows one model's public layout (NOMADS GRIB-filter URL or the ECMWF open-data
client), run cadence, forecast-hour grid, availability lag and ensemble membership. The WindField
asks a source to FETCH the 10 m-wind GRIB subset for a (cycle, forecast-hour, member) over the
race bbox; the source returns a local file path (downloading + caching as needed) or None if that
field is not posted yet.

All sources are key-free. Deterministic models (GFS / NAM / HRRR / ECMWF-HRES) give MODEL spread as
a confidence signal even without ensembles; GEFS / ECMWF-ENS add ENSEMBLE spread and are opt-in
(many members → many downloads). bbox is (north, south, west, east) in decimal degrees.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import urllib.parse

from . import grib

CACHE = os.environ.get("GRIB_CACHE", "/srv/gribcache")
NOMADS = "https://nomads.ncep.noaa.gov/cgi-bin"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _frange(stop, step, dense_to=None, dense_step=1):
    """Forecast-hour list: dense hourly up to `dense_to`, then `step` to `stop` (inclusive)."""
    hrs, h = [], 0
    while h <= stop:
        hrs.append(h)
        h += dense_step if (dense_to is not None and h < dense_to) else step
    return hrs


class ModelSource:
    name = "base"
    kind = "deterministic"            # or "ensemble"
    cycles = (0, 6, 12, 18)
    lag_h = 4.0                       # typical hours from cycle time to data availability
    horizon_h = 120
    fhr_step = 3                      # forecast-hour spacing we sample for routing
    dense_to = None                   # hours that are available hourly before fhr_step kicks in
    members = ("det",)
    priority = 1.0                    # blend weight (higher = trusted more)

    def fhrs(self, horizon_h: int):
        h = min(horizon_h, self.horizon_h)
        return _frange(h, self.fhr_step, self.dense_to, 1)

    def pick_cycle(self, now: dt.datetime | None = None) -> dt.datetime:
        """Freshest cycle whose data should be posted by `now` (accounting for lag)."""
        now = now or _utcnow()
        probe = now - dt.timedelta(hours=self.lag_h)
        # walk back hour by hour to the most recent valid cycle hour on/before probe
        for back in range(0, 30):
            c = (probe - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
            if c.hour in self.cycles:
                return c
        return probe.replace(minute=0, second=0, microsecond=0)

    # --- per-model implementations override these ---
    def _url(self, cycle, fhr, member, bbox):
        raise NotImplementedError

    def _cache_path(self, cycle, fhr, member, bbox):
        tag = f"{self.name}_{cycle:%Y%m%d%H}_{member}_f{fhr:03d}_" + \
              hashlib.md5(repr(bbox).encode()).hexdigest()[:6]
        return os.path.join(CACHE, self.name, tag + ".grib2")

    def fetch(self, cycle, fhr, member, bbox, timeout=60):
        """Return a local GRIB path for the field, or None if it isn't available."""
        path = self._cache_path(cycle, fhr, member, bbox)
        if os.path.exists(path) and os.path.getsize(path) > 100:
            return path
        url = self._url(cycle, fhr, member, bbox)
        try:
            return grib.http_download(url, path, timeout=timeout)
        except Exception:
            return None

    def valid_time(self, cycle: dt.datetime, fhr: int) -> float:
        return (cycle + dt.timedelta(hours=fhr)).timestamp()


def _nomads_qs(base_cgi, file, dir_, bbox, var_lev="lev_10_m_above_ground=on"):
    n, s, w, e = bbox
    qs = urllib.parse.urlencode({
        "file": file, "var_UGRD": "on", "var_VGRD": "on",
        "subregion": "", "leftlon": w, "rightlon": e, "toplat": n, "bottomlat": s,
        "dir": dir_,
    })
    # var_lev is a bare flag (no value vocabulary in urlencode), prepend it
    return f"{NOMADS}/{base_cgi}?{var_lev}&{qs}"


class GFS(ModelSource):
    name = "gfs"
    cycles = (0, 6, 12, 18)
    lag_h = 3.8
    horizon_h = 120
    fhr_step = 3
    dense_to = 0
    priority = 1.0

    def _url(self, cycle, fhr, member, bbox):
        file = f"gfs.t{cycle.hour:02d}z.pgrb2.0p25.f{fhr:03d}"
        dir_ = f"/gfs.{cycle:%Y%m%d}/{cycle.hour:02d}/atmos"
        return _nomads_qs("filter_gfs_0p25.pl", file, dir_, bbox)


class NAM(ModelSource):
    name = "nam"
    cycles = (0, 6, 12, 18)
    lag_h = 1.8
    horizon_h = 60
    fhr_step = 3
    dense_to = 36          # NAM is hourly to f36
    priority = 1.1         # higher-res regional → trust a touch more near shore

    def _url(self, cycle, fhr, member, bbox):
        file = f"nam.t{cycle.hour:02d}z.awphys{fhr:02d}.tm00.grib2"
        dir_ = f"/nam.{cycle:%Y%m%d}"
        return _nomads_qs("filter_nam.pl", file, dir_, bbox)


class HRRR(ModelSource):
    name = "hrrr"
    cycles = tuple(range(24))         # hourly cycles
    lag_h = 1.2
    horizon_h = 18                    # standard cycles; 00/06/12/18 run to 48
    fhr_step = 1
    dense_to = 48
    priority = 1.2                    # 3 km, freshest → trust most for near-term

    def horizon_for(self, cycle):
        return 48 if cycle.hour in (0, 6, 12, 18) else 18

    def fhrs(self, horizon_h: int):
        return _frange(min(horizon_h, 48), 1, 0, 1)

    def _url(self, cycle, fhr, member, bbox):
        file = f"hrrr.t{cycle.hour:02d}z.wrfsfcf{fhr:02d}.grib2"
        dir_ = f"/hrrr.{cycle:%Y%m%d}/conus"
        return _nomads_qs("filter_hrrr_2d.pl", file, dir_, bbox)


class GEFS(ModelSource):
    name = "gefs"
    kind = "ensemble"
    cycles = (0, 6, 12, 18)
    lag_h = 5.0
    horizon_h = 240
    fhr_step = 3
    priority = 0.9
    # control + perturbed members; default-capped by ENSEMBLE_MEMBERS env at ingest time
    members = ("gec00",) + tuple(f"gep{i:02d}" for i in range(1, 31))

    def _url(self, cycle, fhr, member, bbox):
        file = f"{member}.t{cycle.hour:02d}z.pgrb2a.0p50.f{fhr:03d}"
        dir_ = f"/gefs.{cycle:%Y%m%d}/{cycle.hour:02d}/atmos/pgrb2ap5"
        return _nomads_qs("filter_gefs_atmos_0p50a.pl", file, dir_, bbox)


class ECMWF(ModelSource):
    """ECMWF IFS open data via the ecmwf-opendata client (HRES; ENS opt-in).

    Unlike NOMADS this isn't a single URL — the client retrieves to a target file — so it overrides
    `fetch`. HRES uses stream `oper` at 00/12 and `scda` at 06/18. Slowest to publish (~7-8 h).
    """
    name = "ecmwf"
    cycles = (0, 6, 12, 18)
    lag_h = 8.0
    horizon_h = 144
    fhr_step = 3
    priority = 1.15
    members = ("det",)

    def _stream_type(self, cycle, member):
        if member == "det":
            return ("oper" if cycle.hour in (0, 12) else "scda"), "fc", None
        return "enfo", "pf", int(member)

    def fetch(self, cycle, fhr, member, bbox, timeout=120):
        path = self._cache_path(cycle, fhr, member, bbox)
        if os.path.exists(path) and os.path.getsize(path) > 100:
            return path
        try:
            from ecmwf.opendata import Client
        except Exception:
            return None
        stream, typ, number = self._stream_type(cycle, member)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        req = dict(date=cycle.strftime("%Y%m%d"), time=cycle.hour, stream=stream, type=typ,
                   step=int(fhr), param=["10u", "10v"], target=path)
        if number is not None:
            req["number"] = number
        try:
            Client(source="ecmwf").retrieve(**req)
            return path if os.path.exists(path) and os.path.getsize(path) > 100 else None
        except Exception:
            return None


MODELS = {m.name: m for m in (GFS(), NAM(), HRRR(), GEFS(), ECMWF())}
# Default blend: the fast, reliable deterministic models (model spread = confidence). Ensembles
# (gefs, ecmwf-ens) are opt-in via the request because they multiply the download count.
DEFAULT_MODELS = ("gfs", "nam", "hrrr")


def available_models():
    return {name: {"kind": m.kind, "horizon_h": m.horizon_h, "members": len(m.members),
                   "priority": m.priority} for name, m in MODELS.items()}
