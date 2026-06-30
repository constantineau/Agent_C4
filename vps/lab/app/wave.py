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
"""
import bisect
import datetime
import hashlib
import math
import os
import subprocess
import sys
import urllib.parse

import numpy as np

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
# Great-Lakes bounding box (s, n, w, e) — quick reject for non-Great-Lakes races.
_DOMAIN = (41.0, 49.5, -93.0, -76.0)

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
if not keep:
    keep = list(range(min(len(vt), max_slices)))
keep = keep[:max_slices]
np.savez(out, lat=lat, lon=lon, ep=vt[list(keep)], hs=arr[list(keep)])
print("OK")
"""


class WaveField:
    loaded = False
    source = None

    def wave_at(self, lat, lon, epoch):
        return 0.0

    def status(self):
        return {"loaded": self.loaded, "source": self.source}


class ZeroWave(WaveField):
    """Flat water everywhere — the default outside the Great Lakes / on any miss (route unchanged)."""
    pass


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

    def __init__(self, lat2d, lon2d, slices, meta):
        self.lat2d = lat2d                  # 2-D latitude grid
        self.lon2d = lon2d                  # 2-D longitude grid (normalised -180..180)
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
        return {"loaded": True, "source": self.source, "frames": len(self.slices), **self.meta}


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
    out = path + ".npz"
    try:
        r = subprocess.run([sys.executable, "-c", _CHILD, path, out, str(float(t_lo)),
                            str(float(t_hi)), str(STEP_H), str(MAX_SLICES)],
                           capture_output=True, text=True, timeout=PARSE_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("waves: GLWU parse timed out")
        return None
    if r.returncode != 0 or not os.path.exists(out):
        return None
    try:
        d = np.load(out)
        res = (d["lat"].copy(), d["lon"].copy(), d["ep"].copy(), d["hs"].copy())
        d.close()
        return res
    except Exception:
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def _build_glwu(bbox, t_start, t_end, log):
    """Best-effort GLWU significant-wave-height field over the course bbox + window. Returns a
    `GLWUWave` or None (→ caller falls back to `ZeroWave`)."""
    from .wind import grib    # reuse the wind layer's atomic-download-with-miss-detection helper

    n, s, w, e = bbox[0], bbox[1], bbox[2], bbox[3]
    if n < s:                                   # course_bbox is (north, south, west, east)
        n, s = s, n
    dom_s, dom_n, dom_w, dom_e = _DOMAIN
    if n < dom_s or s > dom_n or e < dom_w or w > dom_e:
        log("waves: course outside the Great Lakes (GLWU) domain — flat water")
        return None
    sub = (s - 0.1, n + 0.1, w - 0.1, e + 0.1)  # (s, n, w, e) with a small margin

    key = (round(s, 2), round(n, 2), round(w, 2), round(e, 2), int(t_start // 3600), int(STEP_H))
    if key in _cache:
        log("waves: GLWU field (cached)")
        return _cache[key]

    for back in range(CYCLE_FALLBACKS + 1):
        cyc = _cycle_for(t_start, back)
        url = _grib_filter_url(cyc, sub)
        path = _cache_path(cyc, key)
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
        _cache[key] = field
        log(f"waves: GLWU {len(slices)} frames from {cyc:%Y-%m-%d %HZ} (peak {field.peak_hs():.1f} m)")
        return field
    log("waves: no GLWU data for the window — flat water")
    return None


def build_wavefield(bbox, t_start, t_end, on_progress=None):
    """Best-effort sea-state field over the course bbox + window. A uniform `ConstantWave` what-if
    (`WAVES_CONST_HS`) wins if set; otherwise the real `GLWUWave` (Great Lakes); any miss / outside the
    domain → `ZeroWave` (route unchanged), like the GRIB/current layers."""
    log = on_progress or (lambda *_: None)
    if not ENABLED:
        return ZeroWave()
    if CONST_HS > 0:
        log(f"waves: uniform {CONST_HS} m sea state (what-if)")
        return ConstantWave(CONST_HS)
    try:
        field = _build_glwu(bbox, t_start, t_end, log)
        if field is not None:
            return field
    except Exception as ex:                     # never let the wave layer break an optimize
        log(f"waves: GLWU build failed ({type(ex).__name__}) — flat water")
    return ZeroWave()
