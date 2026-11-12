"""Water currents (set & drift) for the optimizer — a CurrentField parallel to the WindField.

The boat sails at its polar speed THROUGH THE WATER; the current carries it over the GROUND. So the
isochrone advances each step by the boat's water-velocity PLUS the current's drift — the track bows
with a cross stream, the boat crabs to hold its course made good, and ETAs reflect a fair vs foul
current. `current_at(lat, lon, epoch)` returns **(set_deg, drift_kn)** — the compass direction the
water is GOING and its speed in knots; (0, 0) = no current / land / out of domain.

Data source = NOAA GLOFS, the Lake Michigan-Huron Operational Forecast System (**LMHOFS**, an FVCOM
model NOAA also publishes on a regular 0.01° lat/lon grid). We read surface `u_eastward`/`v_northward`
from the CO-OPS THREDDS OPeNDAP server, slicing only the course bbox + surface layer (the whole file
is ~140 MB; the bbox-surface slice is tiny). Best-effort like the GRIB layer: out of the Great-Lakes
domain, no forecast yet (future race), or any fetch error → ZeroCurrent (route unchanged). Validated
deterministically with ConstantCurrent (test_routing_currents.py).

Sources (#308): LMHOFS is one `seasource.SeaSource`, chosen by its water mask (Lake Erie and Lake
St Clair are inside its old box but not its water); HYCOM ESPC (hycom.py) covers the open sea.
The course area picks the source (seasource.pick); an empty field says why in `why`.
"""
import bisect
import math
import os
import threading
from concurrent.futures import TimeoutError as FTimeout

from . import seasource

ENABLED = os.environ.get("CURRENTS_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
STEP_H = float(os.environ.get("CURRENTS_STEP_H", "6"))         # sample cadence across the race window
MAX_SLICES = int(os.environ.get("CURRENTS_MAX_SLICES", "8"))   # cap OPeNDAP reads per build
FETCH_TIMEOUT = float(os.environ.get("CURRENTS_FETCH_TIMEOUT", "20"))   # per-slice OPeNDAP open+read (s)
CYCLE_LAG_H = float(os.environ.get("CURRENTS_CYCLE_LAG_H", "5"))        # availability lag after a cycle
CYCLE_FALLBACKS = int(os.environ.get("CURRENTS_CYCLE_FALLBACKS", "2"))  # step back if a cycle isn't posted
THREDDS = os.environ.get("CURRENTS_THREDDS",
                         "https://opendap.co-ops.nos.noaa.gov/thredds/dodsC/NOAA/LMHOFS/MODELS")
FILL = -9000.0                            # GLOFS land/missing sentinel is -99999

# Built fields, reused only for the same dataset cycle, area, full time span and processing (#44.2).
# key -> (field, built_at, exact); `exact` = built from the cycle it was keyed on, not a fallback.
CACHE_VERSION = 1      # bump when the processing changes: a field built by older code is not reused
CACHE_MAX_H = float(os.environ.get("CURRENTS_CACHE_MAX_H", "6"))   # secondary time limit
FALLBACK_RETRY_S = 3600   # a field from an older cycle (the newest wasn't posted yet) is retried after this
_cache = {}
_stuck = None          # the last read that timed out and may still be hung (#203)
_fetch_lock = threading.Lock()   # check/start/join/update of `_stuck` as one step (Codex on #220)


class CurrentField:
    loaded = False
    source = None
    label = None           # the source's name for people (the data block)
    why = None             # an empty field: why (seasource.OUTSIDE, NO_DATA, OFF, OFFLINE)

    def current_at(self, lat, lon, epoch):
        return (0.0, 0.0)

    def sample_grid(self, epoch, step_deg, bbox):
        """Set/drift on a lat/lon grid at one time → [{lat,lon,set,drift}]. Mirrors
        WindField.sample_grid (SAME lattice, so current arrows overlay the wind arrows) — for the
        map's current overlay + forecast time slider. ZeroCurrent → all-zero (the caller drops it)."""
        n, s, w, e = bbox
        step = max(0.02, float(step_deg))
        pts = []
        lat = s
        while lat <= n + 1e-9:
            lon = w
            while lon <= e + 1e-9:
                cset, cdrift = self.current_at(lat, lon, epoch)
                pts.append({"lat": round(lat, 4), "lon": round(lon, 4),
                            "set": round(cset, 1), "drift": round(cdrift, 2)})
                lon += step
            lat += step
        return pts

    def status(self):
        return {"loaded": self.loaded, "source": self.source}


class ZeroCurrent(CurrentField):
    def __init__(self, why=None):
        self.why = why


class ConstantCurrent(CurrentField):
    """Uniform current — for tests + manual what-ifs. set_deg = where the water flows TO."""
    loaded = True
    source = "constant"

    def __init__(self, set_deg, drift_kn):
        self.set_deg = float(set_deg)
        self.drift_kn = float(drift_kn)

    def current_at(self, lat, lon, epoch):
        return (self.set_deg, self.drift_kn)

    def status(self):
        return {"loaded": True, "source": "constant", "set_deg": self.set_deg, "drift_kn": self.drift_kn}


class GLOFSCurrent(CurrentField):
    """Time-stamped surface-current slices over the course bbox, sampled bilinearly in space and
    linearly in time. Each slice = (epoch, u2d, v2d) on shared 1-D axes (lons, lats), m/s."""
    loaded = True
    source = "glofs-lmhofs"
    label = "NOAA LMHOFS"

    def __init__(self, lons, lats, slices, meta, source=None, label=None):
        if source:
            self.source = source
        if label:
            self.label = label
        self.lons = lons          # ascending 1-D longitude axis (bbox subset)
        self.lats = lats          # ascending 1-D latitude axis
        self.slices = slices      # [(epoch, u2d, v2d)] sorted by epoch
        self.epochs = [s[0] for s in slices]
        self.meta = meta

    def _bilin(self, u2d, v2d, lat, lon):
        lons, lats = self.lons, self.lats
        if lon < lons[0] and lon + 360.0 <= lons[-1]:      # a grid in 0..360 (or across 180°)
            lon += 360.0
        elif lon > lons[-1] and lon - 360.0 >= lons[0]:
            lon -= 360.0
        if lon < lons[0] or lon > lons[-1] or lat < lats[0] or lat > lats[-1]:
            return None
        i = min(max(bisect.bisect_right(lons, lon) - 1, 0), len(lons) - 2)
        j = min(max(bisect.bisect_right(lats, lat) - 1, 0), len(lats) - 2)
        fx = (lon - lons[i]) / (lons[i + 1] - lons[i]) if lons[i + 1] != lons[i] else 0.0
        fy = (lat - lats[j]) / (lats[j + 1] - lats[j]) if lats[j + 1] != lats[j] else 0.0
        out = []
        for g in (u2d, v2d):
            c00, c10, c01, c11 = g[j][i], g[j][i + 1], g[j + 1][i], g[j + 1][i + 1]
            if any(c <= FILL for c in (c00, c10, c01, c11)):   # any land corner → treat as no current
                return None
            out.append((c00 * (1 - fx) + c10 * fx) * (1 - fy) + (c01 * (1 - fx) + c11 * fx) * fy)
        return out[0], out[1]

    def current_at(self, lat, lon, epoch):
        sl = self.slices
        if not sl:
            return (0.0, 0.0)
        # bracket in time
        k = bisect.bisect_left(self.epochs, epoch)
        if k <= 0:
            pair, f = (sl[0], sl[0]), 0.0
        elif k >= len(sl):
            pair, f = (sl[-1], sl[-1]), 0.0
        else:
            pair = (sl[k - 1], sl[k])
            span = sl[k][0] - sl[k - 1][0]
            f = (epoch - sl[k - 1][0]) / span if span else 0.0
        a = self._bilin(pair[0][1], pair[0][2], lat, lon)
        b = self._bilin(pair[1][1], pair[1][2], lat, lon)
        if a is None and b is None:
            return (0.0, 0.0)
        if a is None: a = b
        if b is None: b = a
        u = a[0] * (1 - f) + b[0] * f
        v = a[1] * (1 - f) + b[1] * f
        drift = math.hypot(u, v) * 1.94384                     # m/s → kn
        if drift < 0.01:
            return (0.0, 0.0)
        return ((math.degrees(math.atan2(u, v)) % 360.0), drift)

    def status(self):
        return {"loaded": True, "source": self.source, "label": self.label,
                "slices": len(self.slices), **self.meta}


def _cycle_for(epoch, now=None):
    """Freshest LMHOFS cycle (00/06/12/18 UTC) at or before min(race start, now) - lag. Clamped to
    `now` like wave.py: a race days out still gets the latest POSTED cycle (its forecast reaches
    forward to the race), not a cycle near the start that doesn't exist yet (#51)."""
    import datetime
    import time
    epoch = min(float(epoch), time.time() if now is None else float(now))
    t = datetime.datetime.utcfromtimestamp(epoch - CYCLE_LAG_H * 3600)
    cyc = (t.hour // 6) * 6
    return t.replace(hour=cyc, minute=0, second=0, microsecond=0)


def _dods_url(cycle_dt, fhr):
    d = cycle_dt
    return (f"{THREDDS}/{d.year:04d}/{d.month:02d}/{d.day:02d}/"
            f"lmhofs.t{d.hour:02d}z.{d.year:04d}{d.month:02d}{d.day:02d}.regulargrid.f{fhr:03d}.nc")


def _open_slice(url, bbox, want_axes):
    """Open one OPeNDAP regulargrid file, slice the surface u/v over bbox. Returns (lons,lats,u,v) when
    want_axes else (u,v); raises on any failure (caller treats as a skipped slice)."""
    import netCDF4 as nc
    import numpy as np
    s, n, w, e = bbox
    d = nc.Dataset(url)
    try:
        lon = d.variables["Longitude"][0, :]
        lat = d.variables["Latitude"][:, 0]
        lon = np.asarray(lon); lat = np.asarray(lat)
        i0, i1 = int(np.searchsorted(lon, w) - 1), int(np.searchsorted(lon, e) + 1)
        j0, j1 = int(np.searchsorted(lat, s) - 1), int(np.searchsorted(lat, n) + 1)
        i0 = max(i0, 0); j0 = max(j0, 0); i1 = min(i1, len(lon)); j1 = min(j1, len(lat))
        u = np.asarray(d.variables["u_eastward"][0, 0, j0:j1, i0:i1], dtype=float)
        v = np.asarray(d.variables["v_northward"][0, 0, j0:j1, i0:i1], dtype=float)
        u = np.nan_to_num(u, nan=FILL - 1); v = np.nan_to_num(v, nan=FILL - 1)
        if want_axes:
            return (lon[i0:i1].tolist(), lat[j0:j1].tolist(), u.tolist(), v.tolist())
        return (u.tolist(), v.tolist())
    finally:
        d.close()


def _fetch(url, bbox, want_axes):
    """Run one OPeNDAP slice with a hard timeout so a slow THREDDS can't stall the optimize (#51).

    The read is in netCDF4's C code (libcurl), which no Python socket timeout reaches and which
    can't be interrupted, so it runs on a DAEMON thread: on a timeout we stop waiting, and a still
    hung read never blocks the Lab's shutdown. The build stops launching slices after the first
    timeout, so a dead THREDDS costs at most one stuck thread per build (Codex on #200). A
    subprocess could be killed, but would have to ship every slice's arrays back.

    Across builds (#203): while an earlier timed-out read is still hung, no new read starts, so
    repeated Optimize/Playbook runs against a hung THREDDS never pile up more stuck threads. Reads
    run one at a time, so two jobs racing can't both slip past that check."""
    global _stuck
    # one at a time (Codex on #220): concurrent Optimize and Playbook jobs both passed the check
    # before either timed out, each started a read, and only the last stuck one stayed tracked
    with _fetch_lock:
        if _stuck is not None and _stuck.is_alive():
            raise FTimeout("an earlier read is still hung")
        box = {}

        def work():
            try:
                box["v"] = _open_slice(url, bbox, want_axes)
            except BaseException as exc:  # noqa: BLE001 — handed to the caller below
                box["e"] = exc

        t = threading.Thread(target=work, daemon=True, name="lmhofs-slice")
        t.start()
        t.join(FETCH_TIMEOUT)
        if t.is_alive():
            _stuck = t
            raise FTimeout(f"no answer in {FETCH_TIMEOUT:.0f} s")
        if "e" in box:
            raise box["e"]
        return box["v"]


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


def _build_lmhofs(bbox, t_start, t_end, log):
    """LMHOFS surface-current field over the course bbox + window, or None on any miss."""
    n, s, w, e = bbox[0], bbox[1], bbox[2], bbox[3]
    if n < s:                                   # course_bbox is (north, south, west, east)
        n, s = s, n
    sub_bbox = (s - 0.05, n + 0.05, w - 0.05, e + 0.05)

    # build the list of target times across the window (capped)
    step = max(1.0, STEP_H) * 3600.0
    times = []
    t = t_start
    while t <= t_end + 1 and len(times) < MAX_SLICES:
        times.append(t); t += step
    if times[-1] < t_end:
        times[-1] = t_end

    import time
    want = _cycle_for(t_start)              # the newest posted cycle: a new one is a new key
    key = (CACHE_VERSION, want.strftime("%Y%m%d%H"), round(s, 2), round(n, 2), round(w, 2),
           round(e, 2), int(t_start), int(t_end), STEP_H, MAX_SLICES)
    hit = _cache_get(key, time.time())
    if hit is not None:
        log("currents: LMHOFS field (cached)")
        return hit

    # pick the freshest posted cycle (step back a few if not yet available)
    hung = False
    for back in range(CYCLE_FALLBACKS + 1):
        import datetime
        cyc = want - datetime.timedelta(hours=6 * back)
        cyc_ep = cyc.replace(tzinfo=datetime.timezone.utc).timestamp()
        lons = lats = None
        slices = []
        for tt in times:
            fhr = int(round((tt - cyc_ep) / 3600.0))
            if fhr < 0 or fhr > 120:
                continue
            url = _dods_url(cyc, fhr)
            try:
                if lons is None:
                    lon, lat, u, v = _fetch(url, sub_bbox, True)
                    lons, lats = lon, lat
                else:
                    u, v = _fetch(url, sub_bbox, False)
                slices.append((tt, u, v))
            except FTimeout:
                # THREDDS isn't answering: stop here, don't stack up another stuck read per slice
                log(f"currents: slice f{fhr:03d} timed out — THREDDS not answering, no more tries")
                hung = True
                break
            except Exception as ex:   # any other failure → skip this slice
                log(f"currents: slice f{fhr:03d} skipped ({type(ex).__name__})")
                continue
        if slices and lons:
            meta = {"cycle": cyc.strftime("%Y-%m-%d %HZ"), "fhr_span": [int(round((times[0]-cyc_ep)/3600)),
                    int(round((times[-1]-cyc_ep)/3600))], "domain": "lmhofs"}
            field = GLOFSCurrent(lons, lats, slices, meta)
            if not hung:                      # a cut-short field is used once, not kept
                _cache_put(key, field, back == 0, time.time())
            log(f"currents: LMHOFS {len(slices)} slices from {cyc.strftime('%Y-%m-%d %HZ')}")
            return field
        if hung:
            break
        log(f"currents: cycle {cyc.strftime('%Y-%m-%d %HZ')} not available — stepping back")
    log("currents: no LMHOFS data for the window")
    return None


class LMHOFSSource(seasource.SeaSource):
    """NOAA LMHOFS: Lakes Michigan and Huron (with Georgian Bay and the North Channel)."""
    kind, id, name, resolution_km, sea, mask = \
        "current", "glofs-lmhofs", "NOAA LMHOFS", 1.0, False, "lmhofs"

    def fetch(self, bbox, t_start, t_end, log):
        return _build_lmhofs(bbox, t_start, t_end, log)


class HYCOMSource(seasource.SeaSource):
    """HYCOM ESPC-D-V02: the global ocean (hycom.py). No Great Lakes."""
    kind, id, name, resolution_km, sea, mask = \
        "current", "hycom-espc", "HYCOM ESPC (global)", 8.0, True, "hycom-espc"

    def fetch(self, bbox, t_start, t_end, log):
        from . import hycom
        return hycom.fetch(bbox, t_start, t_end, log)


# Every current source; the course area picks one (finest covering first).
SOURCES = [LMHOFSSource(), HYCOMSource()]


def build_currentfield(bbox, t_start, t_end, on_progress=None):
    """Best-effort surface-current field over the course bbox + window, from the source that
    covers the area (seasource.pick). ZeroCurrent(why) on any miss, saying why."""
    log = on_progress or (lambda *_: None)
    if not ENABLED:
        return ZeroCurrent(seasource.OFF)
    return seasource.build(SOURCES, bbox, t_start, t_end, log, ZeroCurrent, "currents")
