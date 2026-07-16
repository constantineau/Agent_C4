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
import threading
import time
import urllib.parse

from . import grib

CACHE = os.environ.get("GRIB_CACHE", "/srv/gribcache")
NOMADS = "https://nomads.ncep.noaa.gov/cgi-bin"

# ECMWF open-data hardening. The ecmwf-opendata client downloads via multiurl.robust, whose default
# retry is maximum_tries=500 / retry_after=120 s — so a single 429 (rate limit) blocks the request
# for minutes (it was hanging /api/optimize past the gateway timeout → an HTML 504 the UI can't parse
# as JSON). We cap those retries and, once ECMWF rate-limits, trip a cooldown so the rest of the
# frames skip instantly and the route just proceeds on the NOMADS models (best-effort, like them).
ECMWF_MAX_TRIES = int(os.environ.get("ECMWF_MAX_TRIES", "2"))        # vs multiurl's 500
ECMWF_RETRY_AFTER = int(os.environ.get("ECMWF_RETRY_AFTER", "5"))    # seconds, vs multiurl's 120
ECMWF_COOLDOWN = float(os.environ.get("ECMWF_COOLDOWN", "600"))      # back off this long after a rate-limit
# HARD wall-clock per ECMWF frame. The ecmwf-opendata Client has no socket timeout we control, so a
# SLOW (not erroring) server hangs the retrieve indefinitely — the 429 cooldown only trips on an
# EXCEPTION, so a hang blew /api/optimize past the gateway. This bounds each fetch: on a hang we trip
# the cooldown so every remaining frame skips instantly and the route proceeds on the NOMADS models.
ECMWF_FETCH_TIMEOUT = float(os.environ.get("ECMWF_FETCH_TIMEOUT", "75"))


def _cap_multiurl_retries():
    """Shrink multiurl.robust's default retry budget (500×120 s) so an ECMWF 429 can't hang the
    request. robust is a shared function object, so patching its defaults caps every caller (the
    index fetch AND the data download). Idempotent + defensive across multiurl versions."""
    try:
        import multiurl
        d = list(getattr(multiurl.robust, "__defaults__", ()) or ())
        if len(d) >= 2 and d[0] != ECMWF_MAX_TRIES:
            d[0], d[1] = ECMWF_MAX_TRIES, ECMWF_RETRY_AFTER
            multiurl.robust.__defaults__ = tuple(d)
    except Exception:
        pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _freshest_cycle(now: dt.datetime, cycles, lag_h: float) -> dt.datetime:
    """Most recent valid cycle hour (in `cycles`) whose data should be posted by `now` given `lag_h`."""
    probe = now - dt.timedelta(hours=lag_h)
    for back in range(0, 30):
        c = (probe - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        if c.hour in cycles:
            return c
    return probe.replace(minute=0, second=0, microsecond=0)


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

    def horizon_for(self, cycle: dt.datetime) -> int:
        """Forecast horizon (h) this model reaches from `cycle`. Cycle-independent by default; HRRR
        overrides (only its synoptic cycles run long)."""
        return self.horizon_h

    def pick_cycle(self, now: dt.datetime | None = None, min_horizon_h: int = 0) -> dt.datetime:
        """Freshest cycle whose data should be posted by `now` (accounting for lag). `min_horizon_h`
        lets a model prefer a longer-reaching cycle for a long race (used by HRRR)."""
        return _freshest_cycle(now or _utcnow(), self.cycles, self.lag_h)

    def prev_cycle(self, cycle: dt.datetime) -> dt.datetime:
        """The valid cycle immediately before `cycle` — for the not-yet-posted cycle-fallback retry."""
        for back in range(1, 30):
            c = (cycle - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
            if c.hour in self.cycles:
                return c
        return cycle - dt.timedelta(hours=6)

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
    horizon_h = 384       # GFS 0.25° posts 1-hourly to f120 + 3-hourly f123–f384; our 3-hourly grid
    fhr_step = 3          # spans the whole range (the old 120 cap made a race >5 days out route on
    dense_to = 0          # ZERO frames — the "0/0 frames" no-route failure)
    priority = 1.0

    def _url(self, cycle, fhr, member, bbox):
        file = f"gfs.t{cycle.hour:02d}z.pgrb2.0p25.f{fhr:03d}"
        dir_ = f"/gfs.{cycle:%Y%m%d}/{cycle.hour:02d}/atmos"
        return _nomads_qs("filter_gfs_0p25.pl", file, dir_, bbox)


class NAM(ModelSource):
    name = "nam"
    cycles = (0, 6, 12, 18)
    lag_h = 1.8
    horizon_h = 84         # awphys files run 3-hourly past f36 out to f84
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

    def pick_cycle(self, now=None, min_horizon_h: int = 0):
        """HRRR runs hourly but only its SYNOPTIC cycles (00/06/12/18) reach 48 h — the off-synoptic
        cycles stop at 18 h. For a race needing more than that, pick the freshest synoptic cycle so the
        back half of the course isn't left on fallback wind (the HRRR per-cycle-horizon fix)."""
        cycles = (0, 6, 12, 18) if min_horizon_h > self.horizon_h else self.cycles
        return _freshest_cycle(now or _utcnow(), cycles, self.lag_h)

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
    horizon_h = 240                # IFS open data: 3-hourly to f144, 6-hourly f150–f240
    fhr_step = 3
    priority = 1.15
    members = ("det",)
    _cooldown_until = 0.0          # circuit breaker: skip ECMWF until this epoch after a rate-limit

    def fhrs(self, horizon_h: int):
        """IFS open data posts 3-hourly steps to f144 and 6-hourly f150–f240 — generate the REAL
        grid so the f147/f153-style steps that don't exist are never requested."""
        h = min(horizon_h, self.horizon_h)
        out = list(range(0, min(h, 144) + 1, 3))
        out += list(range(150, h + 1, 6))
        return out

    def _stream_type(self, cycle, member):
        if member == "det":
            return ("oper" if cycle.hour in (0, 12) else "scda"), "fc", None
        return "enfo", "pf", int(member)

    def fetch(self, cycle, fhr, member, bbox, timeout=None):
        timeout = timeout or ECMWF_FETCH_TIMEOUT
        path = self._cache_path(cycle, fhr, member, bbox)
        if os.path.exists(path) and os.path.getsize(path) > 100:
            return path
        if time.time() < ECMWF._cooldown_until:
            return None            # ECMWF rate-limited us recently — skip, don't hang the request
        try:
            from ecmwf.opendata import Client
        except Exception:
            return None
        _cap_multiurl_retries()    # bound the 429 retry storm before any ECMWF HTTP call
        stream, typ, number = self._stream_type(cycle, member)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        req = dict(date=cycle.strftime("%Y%m%d"), time=cycle.hour, stream=stream, type=typ,
                   step=int(fhr), param=["10u", "10v"], target=path)
        if number is not None:
            req["number"] = number

        # Run the retrieve in a worker thread with a HARD wall-clock join — the Client itself has no
        # timeout we can set, so a slow (non-erroring) server would otherwise hang here forever.
        result = {}

        def _do():
            try:
                Client(source="ecmwf").retrieve(**req)
                result["path"] = path if os.path.exists(path) and os.path.getsize(path) > 100 else None
            except Exception:      # noqa: BLE001 — 429/network; treated same as a hang below
                result["err"] = True

        th = threading.Thread(target=_do, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive() or result.get("err"):
            # hung past our wall clock, or errored (likely a 429) — back off so the remaining frames
            # skip instantly and the route proceeds on the NOMADS models instead of stalling on ECMWF.
            ECMWF._cooldown_until = time.time() + ECMWF_COOLDOWN
            return None
        return result.get("path")


class ECMWF_ENS(ECMWF):
    """ECMWF ENS (ensemble) open data — the control run + 50 perturbed members.

    A SEPARATE source from the HRES `ecmwf` deterministic run (which stays `kind="deterministic"` so
    it still loads when ensembles are off): `_members_for` returns `["det"]` for a deterministic
    source regardless of its `members`, so an ensemble had to be its own source to be member-driven.
    Opt-in + member-capped by the request's `ensemble_members` (50 members × frames = many downloads;
    pairs with the sparse/degraded-GRIB hardening + the 429 cap/cooldown inherited from ECMWF)."""
    name = "ecmwf-ens"
    kind = "ensemble"
    priority = 1.1
    # control ("c" → enfo/cf) first so a small member cap still includes the central estimate, then
    # the 50 perturbed members ("1".."50" → enfo/pf/N).
    members = ("c",) + tuple(str(i) for i in range(1, 51))

    def _stream_type(self, cycle, member):
        if member == "c":
            return "enfo", "cf", None
        return "enfo", "pf", int(member)


class ICON(ModelSource):
    """DWD ICON-global, routed via the Open-Meteo forecast API (regular lat-lon regrid).

    DWD's own open data publishes ICON-global only on its native ICOSAHEDRAL mesh — the GRIB
    embeds no lat/lon (an external grid-definition file is required), so it doesn't fit the
    cfgrib path the other sources share. Open-Meteo serves the SAME regridded `icon_global`
    data the venue model-skill backtest measured (tied-top at Mackinac 2026), so routing on it
    is consistent with how it was scored. Opt-in for A/B — deliberately NOT in DEFAULT_MODELS.

    This is the first API-grid source: instead of `_url`/`fetch` per frame it implements
    `load_series` (the windfield loader's API-source seam) — one batched multi-point call
    covering the whole bbox × window, reshaped into regular-grid GribFrames.
    """
    name = "icon"
    cycles = (0, 6, 12, 18)
    lag_h = 4.0
    horizon_h = 180
    fhr_step = 3
    priority = 1.15               # venue backtest: tied-top with HRRR/ECMWF at Mackinac

    GRID_STEP = 0.25              # ≈ the 13 km icon-global cell — honest, no invented resolution
    CHUNK = 90                    # locations per API call (URL length + courtesy)
    MAX_FRAMES = 64               # mirrors the loader's per-member frame cap
    API = os.environ.get("ICON_OM_URL", "https://api.open-meteo.com/v1/forecast")
    TIMEOUT = float(os.environ.get("ICON_OM_TIMEOUT", "45"))

    def _grid(self, bbox):
        n, s, w, e = bbox
        lats = [round(s + i * self.GRID_STEP, 4) for i in range(int((n - s) / self.GRID_STEP) + 2)]
        lons = [round(w + i * self.GRID_STEP, 4) for i in range(int((e - w) / self.GRID_STEP) + 2)]
        return lats, lons

    def _fetch_points(self, points, t_start, t_end):
        """One batched Open-Meteo call for `points` [(lat,lon)…] → list of per-point hourly dicts."""
        import json as _json
        import urllib.request
        iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:00")
        qs = urllib.parse.urlencode({
            "latitude": ",".join(str(p[0]) for p in points),
            "longitude": ",".join(str(p[1]) for p in points),
            "hourly": "wind_speed_10m,wind_direction_10m",
            "models": "icon_global", "wind_speed_unit": "ms",
            "timeformat": "unixtime", "timezone": "UTC",
            "start_hour": iso(t_start), "end_hour": iso(t_end),
        })
        # cache by (points, window, current UTC hour) — re-runs within the hour are free and a new
        # hour naturally picks up the next Open-Meteo refresh
        key = hashlib.md5((qs + _utcnow().strftime("%Y%m%d%H")).encode()).hexdigest()[:16]
        cpath = os.path.join(CACHE, self.name, f"om_{key}.json")
        if os.path.exists(cpath) and os.path.getsize(cpath) > 100:
            with open(cpath) as f:
                return _json.load(f)
        req = urllib.request.Request(self.API + "?" + qs,
                                     headers={"User-Agent": "Agent_C4-C4PerformanceLab/1.0"})
        with urllib.request.urlopen(req, timeout=self.TIMEOUT) as r:
            data = _json.loads(r.read().decode())
        if isinstance(data, dict):                     # single-location responses aren't wrapped
            data = [data]
        os.makedirs(os.path.dirname(cpath), exist_ok=True)
        with open(cpath, "w") as f:
            _json.dump(data, f)
        return data

    def load_series(self, bbox, t_start, t_end, on_progress=None):
        """Whole-series ingest: bbox grid × [t_start, t_end] → {(icon, det): [GribFrame]}, meta."""
        import math as _math

        import numpy as np

        from . import grib as g
        lats, lons = self._grid(bbox)
        points = [(la, lo) for la in lats for lo in lons]
        pad = 3 * 3600
        try:
            per_point = []
            for i in range(0, len(points), self.CHUNK):
                per_point.extend(self._fetch_points(points[i:i + self.CHUNK], t_start - pad, t_end + pad))
            if len(per_point) != len(points):
                raise OSError(f"expected {len(points)} points, got {len(per_point)}")
            times = (per_point[0].get("hourly") or {}).get("time") or []
            # 3-hourly sampling (matches the GRIB sources' fhr grid), capped like the loader
            keep = [k for k, t in enumerate(times) if t % (self.fhr_step * 3600) == 0][:self.MAX_FRAMES]
            frames = []
            la_arr = np.asarray(lats, dtype="float64")
            lo_arr = np.asarray(lons, dtype="float64")
            for k in keep:
                u = np.full((len(lats), len(lons)), np.nan)
                v = np.full((len(lats), len(lons)), np.nan)
                for idx, pp in enumerate(per_point):
                    hh = pp.get("hourly") or {}
                    spd = (hh.get("wind_speed_10m") or [])
                    drn = (hh.get("wind_direction_10m") or [])
                    if k >= len(spd) or spd[k] is None or k >= len(drn) or drn[k] is None:
                        continue
                    r = _math.radians(float(drn[k]))                  # direction FROM
                    u[idx // len(lons), idx % len(lons)] = -float(spd[k]) * _math.sin(r)
                    v[idx // len(lons), idx % len(lons)] = -float(spd[k]) * _math.cos(r)
                if np.isnan(u).all():
                    continue
                # the odd null point (API gap) must not poison the bilinear sample — fill with the
                # frame mean rather than let NaN propagate into the blend
                u = np.where(np.isnan(u), np.nanmean(u), u)
                v = np.where(np.isnan(v), np.nanmean(v), v)
                frames.append(g.GribFrame(self.name, "det", float(times[k]), la_arr, lo_arr, u, v, True))
            frames.sort(key=lambda fr: fr.valid_time)
            series = {(self.name, "det"): frames} if frames else {}
            meta = {"model": self.name, "cycle": "open-meteo icon_global (latest run)",
                    "members": 1, "frames": len(frames), "expected_frames": len(keep),
                    "cycle_fallbacks": 0, "priority": self.priority, "kind": self.kind}
            if on_progress:
                on_progress(f"{self.name}: {len(frames)}/{len(keep)} frames via Open-Meteo "
                            f"({len(points)} grid points @ {self.GRID_STEP}°)")
            return series, meta
        except Exception as exc:  # noqa: BLE001 — best-effort like every other source
            if on_progress:
                on_progress(f"{self.name}: unavailable ({type(exc).__name__}) — skipped")
            return {}, {"model": self.name, "cycle": "open-meteo icon_global", "members": 1,
                        "frames": 0, "expected_frames": 0, "cycle_fallbacks": 0,
                        "priority": self.priority, "kind": self.kind}


MODELS = {m.name: m for m in (GFS(), NAM(), HRRR(), GEFS(), ECMWF(), ECMWF_ENS(), ICON())}
# Default blend: the fast, reliable deterministic models (model spread = confidence). Ensembles
# (gefs, ecmwf-ens) are opt-in via the request because they multiply the download count.
DEFAULT_MODELS = ("gfs", "nam", "hrrr")


def available_models():
    return {name: {"kind": m.kind, "horizon_h": m.horizon_h, "members": len(m.members),
                   "priority": m.priority} for name, m in MODELS.items()}
