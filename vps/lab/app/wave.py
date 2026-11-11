"""Sea state (significant wave height) for the optimizer — a WaveField parallel to the WindField and
CurrentField.

The ORC polar is a FLAT-WATER speed. Waves slow the boat below it — most upwind (slamming into a head
sea), least downwind (a following sea barely hurts, can even help) — so a route that looks fast on the
polar can be slower in a seaway, and the upwind/downwind legs degrade differently. Feeding sea state
in lets the optimizer route on ACHIEVABLE speed (with the boat's helm-skill factor), and the gap to the
theoretical polar becomes an honest coaching number. `wave_at(lat, lon, epoch) -> hs_m` = significant
wave height in metres; 0.0 = flat water / no data / out of domain.

Phase 1 shipped the SEAM — `ZeroWave` (default, no behaviour change) + `ConstantWave` for tests and a
uniform what-if (`WAVES_CONST_HS`). **Phase 2 (this file) wires the real Great-Lakes wave provider:**
`GLWUWave` reads NOAA **GLWU** (Great Lakes Wave model, an unstructured WAVEWATCH III run) significant
wave height (`HTSGW` → cfgrib `swh`) from the **gridded 2.5 km product** distributed via the NOMADS
**GRIB-filter** — the SAME machinery the wind models use (`wind/models.py`), not the native
unstructured mesh. One bbox-subset download carries ALL forecast hours (anl + hourly to ~149 h) in a
single multi-message GRIB, so a build is one HTTP fetch + one parse (vs the per-slice OPeNDAP reads the
GLOFS current provider does). The grid is CURVILINEAR (2-D lat/lon), so sampling is nearest-neighbour
in space (like the NAM/HRRR wind frames) and linear in time. Best-effort like the GRIB/current layers:
outside the Great-Lakes domain, no posted cycle, or any fetch/parse error → `ZeroWave` (route
unchanged). The cfgrib parse runs in an ISOLATED subprocess so a native eccodes segfault degrades to a
skipped field instead of killing the optimize worker (mirrors `wind/grib.IsolatedGribParser`). The
degradation MODEL (how Hs slows the boat) lives in `optimizer._wave_factor` so it's shared by any
source and stays deliberately conservative.

Sources (#308): GLWU is one `seasource.SeaSource`, chosen by its water mask; NOAA GFS-Wave
(`GFSWaveSource`, the global WAVEWATCH III run, 1/6°) covers the open sea through the same NOMADS
GRIB filter and the same isolated parse. The course area picks the source (seasource.pick); an
empty field says why in `why`.
"""
import bisect
import datetime
import hashlib
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

import numpy as np

from . import atomicio, seasource

ENABLED = os.environ.get("WAVES_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
# A uniform sea state (m) for what-ifs / demos / tests when no real provider is wired — 0 = off.
CONST_HS = float(os.environ.get("WAVES_CONST_HS", "0"))

# --- GLWU provider config ---------------------------------------------------
STEP_H = float(os.environ.get("WAVES_STEP_H", "3"))            # sub-sample cadence (waves evolve slowly)
MAX_SLICES = int(os.environ.get("WAVES_MAX_SLICES", "60"))    # cap frames kept per build
FETCH_TIMEOUT = float(os.environ.get("WAVES_FETCH_TIMEOUT", "60"))     # GRIB-filter HTTP download (s)
PARSE_TIMEOUT = float(os.environ.get("WAVES_PARSE_TIMEOUT", "60"))     # isolated cfgrib parse (s)
CYCLE_LAG_H = float(os.environ.get("WAVES_CYCLE_LAG_H", "5"))          # availability lag after a cycle
CYCLE_FALLBACKS = int(os.environ.get("WAVES_CYCLE_FALLBACKS", "2"))    # step back if a cycle isn't posted
GLWU_FILTER = os.environ.get("WAVES_GLWU_FILTER",
                             "https://nomads.ncep.noaa.gov/cgi-bin/filter_glwu.pl")
# The long-range gridded product (anl + hourly to ~149 h), run at the 01/07/13/19Z cycles.
GLWU_PRODUCT = os.environ.get("WAVES_GLWU_PRODUCT", "grlc_2p5km")
GLWU_CYCLES = (1, 7, 13, 19)
WAVE_CACHE = os.environ.get("GRIB_CACHE", "/srv/gribcache")
# --- GFS-Wave provider config (global WAVEWATCH III, one GRIB per forecast hour) -----------------
GFSWAVE_FILTER = os.environ.get("WAVES_GFSWAVE_FILTER",
                                "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfswave.pl")
GFSWAVE_STEP_H = float(os.environ.get("WAVES_GFSWAVE_STEP_H", "3"))
GFSWAVE_MAX_FILES = int(os.environ.get("WAVES_GFSWAVE_MAX_FILES", "40"))   # downloads per build
GFSWAVE_LAG_H = float(os.environ.get("WAVES_GFSWAVE_LAG_H", "6"))          # posted ~5-6 h after
GFSWAVE_MAX_FHR = 384          # hourly to f120, then 3-hourly

# Built fields, reused only for the same dataset cycle, area, full time span and processing (#44.2).
# key -> (field, built_at, exact); `exact` = built from the cycle it was keyed on, not a fallback.
CACHE_VERSION = 1      # bump when the processing changes: a field built by older code is not reused
CACHE_MAX_H = float(os.environ.get("WAVES_CACHE_MAX_H", "6"))   # secondary time limit
FALLBACK_RETRY_S = 3600   # a field from an older cycle (the newest wasn't posted yet) is retried after this
_cache = {}

# Child that parses the GLWU GRIB in isolation (a native eccodes crash kills only this process).
# argv: <grib_path> <npz_out> <t_lo_epoch> <t_hi_epoch> <step_h> <max_slices>
_CHILD = r"""
import sys, numpy as np, xarray as xr
path, out, t_lo, t_hi, step_h, max_slices = (sys.argv[1], sys.argv[2], float(sys.argv[3]),
                                             float(sys.argv[4]), float(sys.argv[5]), int(sys.argv[6]))
ds = xr.open_dataset(path, engine="cfgrib",
                     backend_kwargs={"indexpath": "", "filter_by_keys": {"typeOfLevel": "surface"}})
name = "swh" if "swh" in ds.data_vars else list(ds.data_vars)[0]
v = ds[name]
lat = np.asarray(ds["latitude"].values, dtype="float64")
lon = np.asarray(ds["longitude"].values, dtype="float64")
if lat.ndim == 1 and lon.ndim == 1:                    # a regular grid (GFS-Wave): 2-D like GLWU
    lon, lat = np.meshgrid(lon, lat)
lon = np.where(lon > 180.0, lon - 360.0, lon)          # 0..360 -> -180..180
vt = np.asarray(ds["valid_time"].values).astype("datetime64[s]").astype("int64")
arr = np.asarray(v.values, dtype="float32")
ds.close()
if arr.ndim == 2:                                      # single-step file (defensive)
    arr = arr[None]; vt = np.atleast_1d(vt)
# keep frames in [t_lo, t_hi] (+1 h margin), sub-sampled to step_h, capped
keep, last = [], None
margin = 3600.0
for k in range(len(vt)):
    e = float(vt[k])
    if e < t_lo - margin or e > t_hi + margin:
        continue
    if last is not None and (e - last) < step_h * 3600.0 - 1.0:
        continue
    keep.append(k); last = e
# no frame in the window: keep none (the caller steps back a cycle), never the earliest frames (#44.2b)
keep = keep[:max_slices]
np.savez(out, lat=lat, lon=lon, ep=vt[list(keep)], hs=arr[list(keep)])
print("OK")
"""


class WaveField:
    loaded = False
    source = None
    label = None           # the source's name for people (the data block)
    why = None             # an empty field: why (seasource.OUTSIDE, NO_DATA, OFF, OFFLINE)

    def wave_at(self, lat, lon, epoch):
        return 0.0

    def sample_grid(self, epoch, step_deg, bbox):
        """Significant wave height on a lat/lon grid at one time → [{lat,lon,hs}]. Mirrors
        CurrentField.sample_grid (SAME lattice as the wind/current frames, so the sea-state heatmap
        overlays the wind arrows + one slider scrubs all three). ZeroWave → all-zero (caller drops it)."""
        n, s, w, e = bbox
        step = max(0.02, float(step_deg))
        pts = []
        lat = s
        while lat <= n + 1e-9:
            lon = w
            while lon <= e + 1e-9:
                hs = self.wave_at(lat, lon, epoch)
                pts.append({"lat": round(lat, 4), "lon": round(lon, 4), "hs": round(hs, 2)})
                lon += step
            lat += step
        return pts

    def status(self):
        return {"loaded": self.loaded, "source": self.source}


class ZeroWave(WaveField):
    """Flat water everywhere — outside every source / on any miss (route unchanged)."""

    def __init__(self, why=None):
        self.why = why


class ConstantWave(WaveField):
    """Uniform sea state — for tests + manual what-ifs (`WAVES_CONST_HS`)."""
    loaded = True
    source = "constant"

    def __init__(self, hs_m):
        self.hs_m = float(hs_m)

    def wave_at(self, lat, lon, epoch):
        return self.hs_m

    def status(self):
        return {"loaded": True, "source": "constant", "hs_m": self.hs_m}


class GLWUWave(WaveField):
    """Significant wave height (Hs, m) slices over the course bbox from NOAA GLWU. The GLWU grid is
    curvilinear (2-D lat/lon) → nearest-neighbour in space, linear in time. Each slice = (epoch, hs2d);
    NaN cells are land → 0 (flat). Mirrors `current.GLOFSCurrent`."""
    loaded = True
    source = "glwu"
    label = "NOAA GLWU"

    def __init__(self, lat2d, lon2d, slices, meta, source=None, label=None, nearest_water=False):
        if source:
            self.source = source
        if label:
            self.label = label
        self.lat2d = lat2d                  # 2-D latitude grid
        self.lon2d = lon2d                  # 2-D longitude grid (normalised -180..180)
        # a coarse grid (GFS-Wave, 1/6°): a point by the shore samples the nearest SEA cell, not
        # the land cell it falls in (which would read as flat water)
        self._land = None
        if nearest_water and slices:
            self._land = ~np.isfinite(slices[0][1])
        self.slices = slices                # [(epoch, hs2d)] sorted by epoch
        self.epochs = [s[0] for s in slices]
        self.meta = meta
        self._coslat = math.cos(math.radians(float(np.nanmean(lat2d)))) if lat2d.size else 1.0
        self._ji = {}                       # memoise nearest cell by rounded (lat, lon)

    def peak_hs(self):
        m = 0.0
        for _e, h in self.slices:
            if np.isfinite(h).any():
                m = max(m, float(np.nanmax(h)))
        return m

    def _nearest(self, lat, lon):
        kk = (round(lat, 2), round(lon, 2))
        if kk in self._ji:
            return self._ji[kk]
        d2 = (self.lat2d - lat) ** 2 + ((self.lon2d - lon) * self._coslat) ** 2
        if self._land is not None:
            d2 = np.where(self._land, np.inf, d2)
        j, i = np.unravel_index(int(np.argmin(d2)), d2.shape)
        # reject if the nearest cell is absurdly far (position outside the GLWU grid)
        ji = None if float(d2[j, i]) > 0.25 else (int(j), int(i))   # ~0.5° → outside
        self._ji[kk] = ji
        return ji

    def wave_at(self, lat, lon, epoch):
        if not self.slices:
            return 0.0
        ji = self._nearest(lat, lon)
        if ji is None:
            return 0.0
        j, i = ji
        k = bisect.bisect_left(self.epochs, epoch)
        if k <= 0:
            a, b, f = self.slices[0], self.slices[0], 0.0
        elif k >= len(self.slices):
            a, b, f = self.slices[-1], self.slices[-1], 0.0
        else:
            a, b = self.slices[k - 1], self.slices[k]
            span = b[0] - a[0]
            f = (epoch - a[0]) / span if span else 0.0
        ha, hb = float(a[1][j, i]), float(b[1][j, i])
        if not math.isfinite(ha):
            ha = hb
        if not math.isfinite(hb):
            hb = ha
        if not math.isfinite(ha):           # both land → flat
            return 0.0
        hs = ha * (1 - f) + hb * f
        return hs if hs > 0 else 0.0

    def status(self):
        return {"loaded": True, "source": self.source, "label": self.label,
                "frames": len(self.slices), **self.meta}


def _cycle_for(epoch, back=0):
    """Freshest posted GLWU base cycle (01/07/13/19Z) at or before min(race-start, now) − lag, stepped
    back `back` cycles. Clamping to `now` means a future race still picks the latest posted cycle (the
    149 h forecast reaches forward to the race), not an unposted cycle near the start."""
    now = datetime.datetime.utcnow()
    probe = min(datetime.datetime.utcfromtimestamp(epoch), now) - datetime.timedelta(hours=CYCLE_LAG_H)
    base = probe.replace(minute=0, second=0, microsecond=0)
    for d in range(0, 30):
        c = (probe - datetime.timedelta(hours=d)).replace(minute=0, second=0, microsecond=0)
        if c.hour in GLWU_CYCLES:
            base = c
            break
    return base - datetime.timedelta(hours=6 * back)    # the cycles are 6 h apart


def _grib_filter_url(cyc, sub):
    """NOMADS GRIB-filter URL for HTSGW (surface) over `sub` = (s, n, w, e). One file holds every
    forecast hour, so no per-fhr loop (unlike the wind models)."""
    s, n, w, e = sub
    qs = urllib.parse.urlencode({
        "file": f"glwu.{GLWU_PRODUCT}.t{cyc.hour:02d}z.grib2",
        "var_HTSGW": "on", "subregion": "",
        "leftlon": w, "rightlon": e, "toplat": n, "bottomlat": s,
        "dir": f"/glwu.{cyc:%Y%m%d}",
    })
    return f"{GLWU_FILTER}?lev_surface=on&{qs}"


def _cache_path(cyc, key):
    tag = f"glwu_{GLWU_PRODUCT}_{cyc:%Y%m%d%H}_" + hashlib.md5(repr(key).encode()).hexdigest()[:6]
    return os.path.join(WAVE_CACHE, "glwu", tag + ".grib2")


def _parse_isolated(path, t_lo, t_hi, log):
    """Parse the GLWU GRIB in a child process → (lat2d, lon2d, epochs, hs3d), or None on
    crash/timeout/error. Isolates a native eccodes segfault from the optimize worker."""
    # A private temp file, NOT a fixed `path + ".npz"` beside the GRIB (#202): two workers
    # parsing the same cycle collided on that name (one read the other's half-written file), and
    # a killed parent left it in the cache dir for good. It is read once and deleted, so it needs
    # no fsync. Same handoff as the wind parser (wind/_grib_parser.py).
    fd, out = tempfile.mkstemp(suffix=".npz", prefix="glwu_")
    os.close(fd)
    try:
        try:
            r = subprocess.run([sys.executable, "-c", _CHILD, path, out, str(float(t_lo)),
                                str(float(t_hi)), str(STEP_H), str(MAX_SLICES)],
                               capture_output=True, text=True, timeout=PARSE_TIMEOUT)
        except subprocess.TimeoutExpired:
            log("waves: GLWU parse timed out")
            return None
        if r.returncode != 0:
            return None
        try:
            with np.load(out) as d:
                return (d["lat"].copy(), d["lon"].copy(), d["ep"].copy(), d["hs"].copy())
        except Exception:
            return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def _cache_get(key, now):
    """The cached field for `key`, or None when absent or past its limit: CACHE_MAX_H, or
    FALLBACK_RETRY_S for a field built from an older cycle than the key's."""
    hit = _cache.get(key)
    if hit is None:
        return None
    field, built, exact = hit
    if now - built < (CACHE_MAX_H * 3600 if exact else min(FALLBACK_RETRY_S, CACHE_MAX_H * 3600)):
        return field
    _cache.pop(key, None)
    return None


def _cache_put(key, field, exact, now):
    for k, (_f, built, _e) in list(_cache.items()):    # drop expired entries: keys change by cycle
        if now - built >= CACHE_MAX_H * 3600:
            _cache.pop(k, None)
    _cache[key] = (field, now, exact)


def _build_glwu(bbox, t_start, t_end, log):
    """Best-effort GLWU significant-wave-height field over the course bbox + window. Returns a
    `GLWUWave` or None (→ caller falls back to `ZeroWave`)."""
    from .wind import grib    # reuse the wind layer's atomic-download-with-miss-detection helper

    n, s, w, e = bbox[0], bbox[1], bbox[2], bbox[3]
    if n < s:                                   # course_bbox is (north, south, west, east)
        n, s = s, n
    sub = (s - 0.1, n + 0.1, w - 0.1, e + 0.1)  # (s, n, w, e) with a small margin

    area = (round(s, 2), round(n, 2), round(w, 2), round(e, 2))
    want = _cycle_for(t_start)              # the newest posted cycle: a new one is a new key
    key = (CACHE_VERSION, want.strftime("%Y%m%d%H"), *area, int(t_start), int(t_end), STEP_H,
           MAX_SLICES)
    hit = _cache_get(key, time.time())
    if hit is not None:
        log("waves: GLWU field (cached)")
        return hit

    for back in range(CYCLE_FALLBACKS + 1):
        cyc = want - datetime.timedelta(hours=6 * back)     # the cycles are 6 h apart
        url = _grib_filter_url(cyc, sub)
        path = _cache_path(cyc, area)       # the download holds every hour: one file per cycle + area
        try:
            grib.http_download(url, path, timeout=int(FETCH_TIMEOUT))
        except Exception as ex:                 # not yet posted / no egress → step back a cycle
            log(f"waves: GLWU cycle {cyc:%Y-%m-%d %HZ} not available ({type(ex).__name__}) — stepping back")
            continue
        parsed = _parse_isolated(path, t_start, t_end, log)
        if parsed is None:
            log("waves: GLWU parse failed — flat water")
            return None
        lat2d, lon2d, epochs, hs = parsed
        if len(epochs) == 0 or lat2d.size == 0:
            log(f"waves: GLWU cycle {cyc:%Y-%m-%d %HZ} has no frames in the window — stepping back")
            continue
        slices = [(int(epochs[i]), hs[i]) for i in range(len(epochs))]
        meta = {"cycle": cyc.strftime("%Y-%m-%d %HZ"), "product": GLWU_PRODUCT, "domain": "great_lakes"}
        field = GLWUWave(lat2d, lon2d, slices, meta)
        _cache_put(key, field, back == 0, time.time())
        log(f"waves: GLWU {len(slices)} frames from {cyc:%Y-%m-%d %HZ} (peak {field.peak_hs():.1f} m)")
        return field
    log("waves: no GLWU data for the window — flat water")
    return None


def _gfswave_cycle(epoch, back=0, now=None):
    """Freshest posted GFS-Wave cycle (00/06/12/18Z) at or before min(race start, now) − lag."""
    def utc(x):
        return datetime.datetime.fromtimestamp(x, datetime.timezone.utc).replace(tzinfo=None)
    now_dt = utc(time.time() if now is None else now)
    probe = min(utc(epoch), now_dt) - \
        datetime.timedelta(hours=GFSWAVE_LAG_H)
    base = probe.replace(hour=(probe.hour // 6) * 6, minute=0, second=0, microsecond=0)
    return base - datetime.timedelta(hours=6 * back)


def _gfswave_url(cyc, fhr, sub, grid):
    s, n, w, e = sub
    qs = urllib.parse.urlencode({
        "dir": f"/gfs.{cyc:%Y%m%d}/{cyc.hour:02d}/wave/gridded",
        "file": f"gfswave.t{cyc.hour:02d}z.{grid}.f{fhr:03d}.grib2",
        "var_HTSGW": "on", "lev_surface": "on", "subregion": "",
        "leftlon": w, "rightlon": e, "toplat": n, "bottomlat": s,
    })
    return f"{GFSWAVE_FILTER}?{qs}"


def _gfswave_hours(cyc_ep, t_start, t_end):
    """Forecast hours over the window: every GFSWAVE_STEP_H on the files that exist (hourly to
    f120, 3-hourly after), one before and one after, capped."""
    def exists(f):
        return 0 <= f <= GFSWAVE_MAX_FHR and (f <= 120 or f % 3 == 0)
    lo = int(math.floor((t_start - cyc_ep) / 3600.0)) - int(GFSWAVE_STEP_H)
    hi = int(math.ceil((t_end - cyc_ep) / 3600.0)) + int(GFSWAVE_STEP_H)
    out, f, step = [], max(lo, 0), max(1, int(GFSWAVE_STEP_H))
    while f <= min(hi, GFSWAVE_MAX_FHR) and len(out) < GFSWAVE_MAX_FILES:
        g = f
        while not exists(g) and g <= GFSWAVE_MAX_FHR:
            g += 1
        if g > GFSWAVE_MAX_FHR or (out and g == out[-1]):
            f += step
            continue
        out.append(g)
        f = g + step
    return out


def _build_gfswave(bbox, t_start, t_end, log, grid, label, now=None):
    """GFS-Wave significant-wave-height field over the course bbox + window, or None. One small
    GRIB-filter download per forecast hour, joined into one file and parsed once in isolation."""
    from .wind import grib
    now = time.time() if now is None else now
    n, s, w, e = bbox[0], bbox[1], bbox[2], bbox[3]
    if n < s:
        n, s = s, n
    sub = (s - 0.25, n + 0.25, w - 0.25, e + 0.25)    # a few 1/6° cells beyond the course
    area = (round(s, 2), round(n, 2), round(w, 2), round(e, 2))
    want = _gfswave_cycle(t_start, now=now)
    key = ("gfswave", grid, CACHE_VERSION, f"{want:%Y%m%d%H}", *area, int(t_start), int(t_end),
           GFSWAVE_STEP_H)
    hit = _cache_get(key, now)
    if hit is not None:
        log("waves: GFS-Wave field (cached)")
        return hit
    for back in range(CYCLE_FALLBACKS + 1):
        cyc = want - datetime.timedelta(hours=6 * back)
        cyc_ep = cyc.replace(tzinfo=datetime.timezone.utc).timestamp()
        hours = _gfswave_hours(cyc_ep, t_start, t_end)
        if not hours:
            log("waves: GFS-Wave's forecast doesn't reach the race yet")
            return None
        # a private folder per build (Codex on #331): two jobs on the same area and cycle must
        # not read or delete each other's files
        os.makedirs(os.path.join(WAVE_CACHE, "gfswave"), exist_ok=True)
        work = tempfile.mkdtemp(prefix=f"{grid}_{cyc:%Y%m%d%H}_",
                                dir=os.path.join(WAVE_CACHE, "gfswave"))
        try:
            blob = b""
            for f in hours:                # a few hundred bytes each: joined in memory
                part = os.path.join(work, f"f{f:03d}.grib2")
                grib.http_download(_gfswave_url(cyc, f, sub, grid), part,
                                   timeout=int(FETCH_TIMEOUT))
                with open(part, "rb") as f_in:
                    blob += f_in.read()
            joined = os.path.join(work, "joined.grib2")
            atomicio.write_bytes(joined, blob)
            parsed = _parse_isolated(joined, t_start, t_end, log)
        except Exception as ex:           # not posted yet / no egress → step back a cycle
            log(f"waves: GFS-Wave cycle {cyc:%Y-%m-%d %HZ} not available ({type(ex).__name__})"
                " — stepping back")
            continue
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if parsed is None:
            log("waves: GFS-Wave parse failed")
            return None
        lat2d, lon2d, epochs, hs = parsed
        if len(epochs) == 0 or lat2d.size == 0 or not np.isfinite(hs).any():
            log(f"waves: GFS-Wave cycle {cyc:%Y-%m-%d %HZ} has no sea in the window")
            return None
        slices = [(int(epochs[i]), hs[i]) for i in range(len(epochs))]
        meta = {"cycle": f"{cyc:%Y-%m-%d %HZ}", "product": grid, "domain": "global"}
        field = GLWUWave(lat2d, lon2d, slices, meta, source="gfs-wave", label=label,
                         nearest_water=True)
        _cache_put(key, field, back == 0, now)
        log(f"waves: GFS-Wave {len(slices)} frames from {cyc:%Y-%m-%d %HZ} "
            f"(peak {field.peak_hs():.1f} m)")
        return field
    log("waves: no GFS-Wave data for the window")
    return None


class GLWUSource(seasource.SeaSource):
    """NOAA GLWU: all five Great Lakes (and Lake St Clair), 2.5 km."""
    kind, id, name, resolution_km, sea, mask = "waves", "glwu", "NOAA GLWU", 2.5, False, "glwu"

    def fetch(self, bbox, t_start, t_end, log):
        return _build_glwu(bbox, t_start, t_end, log)


class GFSWaveSource(seasource.SeaSource):
    """NOAA GFS-Wave (WAVEWATCH III), one of its gridded products: `global.0p16` (1/6°, about
    15°S to 52°N) and `global.0p25` (1/4°, the whole globe) for the rest."""
    kind, id, sea = "waves", "gfs-wave", True

    def __init__(self, grid, resolution_km, name):
        self.grid, self.resolution_km, self.name = grid, resolution_km, name
        self.mask = "gfs-wave-" + grid.replace(".", "-")

    def fetch(self, bbox, t_start, t_end, log):
        return _build_gfswave(bbox, t_start, t_end, log, self.grid, self.name)


# Every wave source; the course area picks one (finest covering first).
SOURCES = [GLWUSource(),
           GFSWaveSource("global.0p16", 18.0, "NOAA GFS-Wave (global 1/6°)"),
           GFSWaveSource("global.0p25", 28.0, "NOAA GFS-Wave (global 1/4°)")]


def build_wavefield(bbox, t_start, t_end, on_progress=None):
    """Best-effort sea-state field over the course bbox + window. A uniform `ConstantWave` what-if
    (`WAVES_CONST_HS`) wins if set; otherwise the source that covers the area (seasource.pick);
    any miss / outside every source → `ZeroWave(why)` (route unchanged), saying why."""
    log = on_progress or (lambda *_: None)
    if not ENABLED:
        return ZeroWave(seasource.OFF)
    if CONST_HS > 0:
        log(f"waves: uniform {CONST_HS} m sea state (what-if)")
        return ConstantWave(CONST_HS)
    return seasource.build(SOURCES, bbox, t_start, t_end, log, ZeroWave, "waves")
