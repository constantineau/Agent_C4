"""Global ocean surface currents from HYCOM ESPC-D-V02 (#308): the second current source, for races
on the open sea (the Florida Current off Fort Lauderdale, the Gulf Stream, any coast).

ESPC-D-V02 is the US Navy's global coupled forecast (HYCOM ocean at 1/12°: 0.08° lon x 0.04° lat,
about 8 km), served free with no account by HYCOM.org's THREDDS. We read the HOURLY surface
velocity (`ssu`/`ssv`, m/s) from the FMRC "best" series, which runs about 9 days back to 8 days
ahead and already falls back to the previous run when one is missing, so there is no cycle
stepping here. Its ocean includes 8 tidal constituents, so open-water tidal streams are in the
field; narrow tidal channels are not resolved at 8 km.

Why HYCOM and not NOAA RTOFS: RTOFS is the same model family, but NOMADS retired OPeNDAP in 2025
(SCN 25-81) and its files are 155 MB a forecast hour with no server-side subset. HYCOM's THREDDS
slices the course box on the server, so a build is a few small HTTP reads.

The read is OPeNDAP ASCII (`.ascii?ssu[t][lat][lon],ssv[...]`): plain text over urllib with a
socket timeout, parsed in pure Python. No netCDF library, so no native crash to isolate and it
runs the same on the Orin, whose venv has no netCDF4. The box and the slices are capped
(`OCEAN_CURRENT_MAX_CELLS`, `..._MAX_SLICES`, `..._MAX_BYTES`) so a big box costs a coarser
stride, not memory.
"""
import bisect
import datetime
import math
import os
import re
import time
import urllib.parse
import urllib.request

URL = os.environ.get("OCEAN_CURRENT_URL",
                     "https://tds.hycom.org/thredds/dodsC/FMRC_ESPC-D-V02_ice/"
                     "FMRC_ESPC-D-V02_ice_best.ncd")
VAR_U, VAR_V = "ssu", "ssv"
STEP_H = float(os.environ.get("OCEAN_CURRENT_STEP_H", "3"))          # slice cadence
MAX_SLICES = int(os.environ.get("OCEAN_CURRENT_MAX_SLICES", "40"))
MAX_CELLS = int(os.environ.get("OCEAN_CURRENT_MAX_CELLS", "4000"))   # per slice, before striding
MAX_BYTES = int(os.environ.get("OCEAN_CURRENT_MAX_BYTES", str(30 * 1024 * 1024)))
TIMEOUT = float(os.environ.get("OCEAN_CURRENT_TIMEOUT", "60"))
HORIZON_H = float(os.environ.get("OCEAN_CURRENT_HORIZON_H", "192"))  # the forecast's reach
CACHE_MAX_H = float(os.environ.get("OCEAN_CURRENT_CACHE_MAX_H", "3"))
CACHE_VERSION = 1
_UA = "AgentC4-lab/1.0"
_axes = {}          # url -> (lats, lons): the grid never changes
_cache = {}         # key -> (field, built_at)

_HEAD = re.compile(r"^([A-Za-z_][\w.]*)((?:\[\d+\])+)$")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise OSError(f"answer over {MAX_BYTES} bytes")
    return data.decode("utf-8", "replace")


def _q(expr):
    """OPeNDAP constraint, percent-encoded (brackets are not legal raw in a URL)."""
    return urllib.parse.quote(expr, safe=",:")


def parse_ascii(text):
    """OPeNDAP .ascii answer → {name: [values]} in row-major order. Grid arrays come as
    `ssu.ssu[9][21][10]` then `[i][j], v, v, ...` rows; maps and plain arrays as one line."""
    body = text.split("\n---", 1)[-1]
    body = body.split("\n", 1)[-1] if body.startswith("-") else body
    out, cur = {}, None
    for line in body.splitlines():
        line = line.strip()
        if not line:
            cur = None
            continue
        m = _HEAD.match(line)
        if m:
            cur = out.setdefault(m.group(1), [])
            continue
        if cur is None:
            continue
        if line.startswith("["):
            line = line.split(",", 1)[1] if "," in line else ""
        cur.extend(float(x) for x in line.split(",") if x.strip())
    return out


def _time_axis():
    """(epochs) of the series' time axis, from its values and its `units` in the DAS."""
    das = _get(URL + ".das")
    m = re.search(r"time\s*\{[^}]*?units\s+\"hours since ([0-9-]+)[ T]([0-9:]+)", das, re.S)
    if not m:
        raise ValueError("time units not found")
    ref = datetime.datetime.strptime(m.group(1) + " " + m.group(2)[:8], "%Y-%m-%d %H:%M:%S")
    ref_ep = ref.replace(tzinfo=datetime.timezone.utc).timestamp()
    hours = parse_ascii(_get(URL + ".ascii?" + _q("time")))["time"]
    return [ref_ep + h * 3600.0 for h in hours]


def _grid_axes():
    hit = _axes.get(URL)
    if hit is None:
        got = parse_ascii(_get(URL + ".ascii?" + _q("lat,lon")))
        hit = _axes[URL] = (got["lat"], got["lon"])
    return hit


def lon_ranges(lons, w, e):
    """Index ranges (i0, i1, offset) of a 0..360 longitude axis covering w..e (-180..180), each
    with the offset that puts its longitudes in the caller's frame, ascending. A box across the
    axis seam (0°) is two ranges."""
    if e - w >= 359.0:
        return [(0, len(lons) - 1, 0.0)]
    w360, e360 = w % 360.0, e % 360.0
    off = w - w360
    def lo(x):
        return max(bisect.bisect_left(lons, x) - 1, 0)
    def hi(x):
        return min(bisect.bisect_right(lons, x), len(lons) - 1)
    if w360 <= e360:
        return [(lo(w360), hi(e360), off)]
    return [(lo(w360), len(lons) - 1, off), (0, hi(e360), off + 360.0)]


def _pick_times(epochs, t_start, t_end):
    """(k0, stride, k1) over the time axis, or None unless the series covers ALL of the window
    (Codex on #331): the frames picked start at or before the gun and reach the window's end, so
    no frame is held past the forecast as if it were one. k1 - k0 is a whole number of strides,
    so the last frame read is the one at or after the end."""
    if not epochs or epochs[0] > t_start or epochs[-1] < t_end:
        return None
    k_lo = max(k for k, e in enumerate(epochs) if e <= t_start)
    k1 = min(k for k, e in enumerate(epochs) if e >= t_end)
    dt = (epochs[1] - epochs[0]) if len(epochs) > 1 else 3600.0
    stride = max(1, int(round(STEP_H * 3600.0 / dt)) if dt > 0 else 1)
    while -(-(k1 - k_lo) // stride) + 1 > MAX_SLICES:
        stride += 1
    k0 = k1 - (-(-(k1 - k_lo) // stride)) * stride      # at or before k_lo, on the stride
    if k0 < 0:                                          # the gun is at the series' very start
        k0, stride = 0, 1
        if k1 + 1 > MAX_SLICES:
            return None
    return k0, stride, k1


def fetch(bbox, t_start, t_end, log, now=None):
    """Surface-current field over bbox (north, south, west, east) and the window → a
    `current.GLOFSCurrent` (source "hycom-espc"), or None when there is no data for it."""
    from .current import FILL, GLOFSCurrent
    now = time.time() if now is None else now
    if t_start > now + HORIZON_H * 3600:
        log("currents: HYCOM's forecast doesn't reach the race yet")
        return None
    n, s, w, e = bbox
    if n < s:
        n, s = s, n
    s, n, w, e = s - 0.1, n + 0.1, w - 0.1, e + 0.1
    key = (CACHE_VERSION, URL, round(s, 2), round(n, 2), round(w, 2), round(e, 2), int(t_start),
           int(t_end), STEP_H, MAX_SLICES, MAX_CELLS)
    hit = _cache.get(key)
    if hit and now - hit[1] < CACHE_MAX_H * 3600:
        log("currents: HYCOM field (cached)")
        return hit[0]

    epochs = _time_axis()
    pick = _pick_times(epochs, t_start, t_end)
    if pick is None:
        log("currents: HYCOM has no forecast for the race time")
        return None
    k0, ks, k1 = pick
    lats, lons = _grid_axes()
    j0 = max(bisect.bisect_left(lats, s) - 1, 0)
    j1 = min(bisect.bisect_right(lats, n), len(lats) - 1)
    ranges = lon_ranges(lons, w, e)
    cells = (j1 - j0 + 1) * sum(i1 - i0 + 1 for i0, i1, _o in ranges)
    hs = max(1, math.ceil(math.sqrt(cells / MAX_CELLS)))     # a big box reads every hs-th cell

    def reach(a, b, last):
        """End index from `a` on the stride, at or past `b` (Codex on #331: a stride must not
        stop short of the box's far edge), within the axis."""
        return min(a + -(-(b - a) // hs) * hs, last)

    j1 = reach(j0, j1, len(lats) - 1)
    ranges = [(i0, reach(i0, i1, len(lons) - 1), off) for i0, i1, off in ranges]

    out_lons, u_parts, v_parts, times, out_lats = [], [], [], None, None
    for i0, i1, off in ranges:
        sl = f"[{k0}:{ks}:{k1}][{j0}:{hs}:{j1}][{i0}:{hs}:{i1}]"
        got = parse_ascii(_get(URL + ".ascii?" + _q(f"{VAR_U}{sl},{VAR_V}{sl}")))
        la, lo = got[f"{VAR_U}.lat"], got[f"{VAR_U}.lon"]
        tt = got[f"{VAR_U}.time"]
        u, v = got[f"{VAR_U}.{VAR_U}"], got[f"{VAR_V}.{VAR_V}"]
        if len(u) != len(tt) * len(la) * len(lo) or len(v) != len(u):
            raise ValueError("short answer")
        out_lats, times = la, tt
        out_lons.extend(x + off for x in lo)
        u_parts.append((u, len(lo)))
        v_parts.append((v, len(lo)))
    ny = len(out_lats)

    def frame(parts, k):
        rows = []
        for j in range(ny):
            row = []
            for vals, nx in parts:
                base = (k * ny + j) * nx
                row.extend(x if math.isfinite(x) else FILL - 1 for x in vals[base:base + nx])
            rows.append(row)
        return rows

    slices = [(epochs[k0] + (times[k] - times[0]) * 3600.0, frame(u_parts, k), frame(v_parts, k))
              for k in range(len(times))]
    if not any(math.isfinite(x) and x > FILL for vals, _n in u_parts for x in vals):
        log("currents: HYCOM has no water in this box")
        return None
    def utc(x):
        return datetime.datetime.fromtimestamp(x, datetime.timezone.utc).strftime("%Y-%m-%d %HZ")
    meta = {"from": utc(slices[0][0]), "to": utc(slices[-1][0]),
            "stride": hs, "domain": "global"}
    field = GLOFSCurrent(out_lons, out_lats, slices, meta, source="hycom-espc",
                         label="HYCOM ESPC (global)")
    _cache[key] = (field, now)
    for k_, (_f, built) in list(_cache.items()):
        if now - built >= CACHE_MAX_H * 3600:
            _cache.pop(k_, None)
    log(f"currents: HYCOM {len(slices)} slices, {ny}x{len(out_lons)} cells")
    return field
