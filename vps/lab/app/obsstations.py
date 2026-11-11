"""Find observed-wind stations near any point on Earth (venue study, #309 step).

Model skill needs measured wind to score forecasts against. venue.STATIONS is a short hand-picked
Great Lakes list; this module finds stations anywhere from two public catalogues:

  - METAR (airports): the Iowa State Mesonet network list. Each network (a US state, a Canadian
    province, a country) carries a bounding box; we open only the networks whose box is in range,
    then measure the distance to each station. The same archive serves the observations
    (modelskill.fetch_metar), so anything found here can be scored.
  - NDBC (buoys, C-MAN, coastal stations): NDBC's station table. Observations come from NDBC's
    historical + realtime files (modelskill.fetch_ndbc_window).

The curated list stays as the preference: a curated station in range is marked `curated` and
ticked by default. Each source is a small object with `find(lat, lon, radius_nm)` so a regional
network (another country's buoys) can be added as one more source later: a first step toward the
per-area data-source registry in #309, not the whole registry.

`probe()` checks a station really has wind in the study window (catalogues list many stations that
are closed, or carry no wind sensor). Network reads are cached in memory for a day.
"""
from __future__ import annotations

import json
import math
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from . import modelskill, venue

_IEM_NETWORKS = "https://mesonet.agron.iastate.edu/api/1/networks.json"
_IEM_NETWORK = "https://mesonet.agron.iastate.edu/geojson/network/{net}.geojson"
_NDBC_TABLE = "https://www.ndbc.noaa.gov/data/stations/station_table.txt"

CACHE_S = 24 * 3600
MAX_PER_KIND = 8          # probe at most this many of each kind (nearest first)
DEADLINE_S = 150          # Find stations answers by then (gateway cap ~300 s): station lists and
                          # data checks share it; a list that is late is a note, a late check is
                          # "not checked"
_cache: dict = {}


def _cached(key, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _get_text(url, timeout=30):
    return modelskill._get(url, timeout, retries=2).decode("utf-8", "replace")


# ------------------------------------------------------------------------------------- METAR (IEM)
def _ewkb_box(hexstr):
    """(s, n, w, e) of a hex EWKB polygon (the IEM network `extent`), or None."""
    try:
        b = bytes.fromhex(hexstr)
        le = "<" if b[0] == 1 else ">"
        gtype = struct.unpack(le + "I", b[1:5])[0]
        off = 9 if gtype & 0x20000000 else 5            # skip the SRID when present
        rings = struct.unpack(le + "I", b[off:off + 4])[0]
        off += 4
        lats, lons = [], []
        for _ in range(rings):
            n = struct.unpack(le + "I", b[off:off + 4])[0]
            off += 4
            for _ in range(n):
                x, y = struct.unpack(le + "dd", b[off:off + 16])
                off += 16
                lons.append(x)
                lats.append(y)
        return (min(lats), max(lats), min(lons), max(lons)) if lats else None
    except (ValueError, struct.error, IndexError):
        return None


def iem_networks():
    """[{id, name, tz, box}] for every IEM *ASOS network (airport weather)."""
    def load():
        rows = json.loads(_get_text(_IEM_NETWORKS)).get("data") or []
        out = []
        for r in rows:
            if not str(r.get("id", "")).endswith("ASOS"):
                continue
            box = _ewkb_box(r.get("extent") or "")
            if box:
                out.append({"id": r["id"], "name": r.get("name"), "tz": r.get("tzname"), "box": box})
        return out
    return _cached("iem_networks", load)


def iem_network_stations(net):
    """[{id, name, lat, lon, tz, begin, end}] for one IEM network."""
    def load():
        feats = json.loads(_get_text(_IEM_NETWORK.format(net=net))).get("features") or []
        out = []
        for f in feats:
            p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
            if len(g) < 2:
                continue
            out.append({"id": f.get("id") or p.get("sid"), "name": p.get("sname") or f.get("id"),
                        "lat": float(g[1]), "lon": float(g[0]), "tz": p.get("tzname"),
                        "begin": p.get("archive_begin"), "end": p.get("archive_end")})
        return out
    return _cached("iem_net:" + net, load)


def _box_near(box, lat, lon, radius_nm):
    s, n, w, e = box
    pad = radius_nm / 60.0                              # degrees of latitude
    padlon = pad / max(0.1, abs(math.cos(math.radians(lat))))
    return (s - pad) <= lat <= (n + pad) and (w - padlon) <= lon <= (e + padlon)


class IemMetarSource:
    name, kind = "Iowa State METAR", "metar"

    def find(self, lat, lon, radius_nm):
        out, seen = [], set()
        for net in iem_networks():
            if not _box_near(net["box"], lat, lon, radius_nm):
                continue
            for s in iem_network_stations(net["id"]):
                if s["id"] in seen:
                    continue
                d = venue._haversine_nm(lat, lon, s["lat"], s["lon"])
                if d <= radius_nm:
                    seen.add(s["id"])
                    out.append(dict(s, kind="metar", dist_nm=round(d, 1), source=self.name))
        return out


# --------------------------------------------------------------------------------------- NDBC
_LOC = re.compile(r"([\d.]+)\s*([NS])\s+([\d.]+)\s*([EW])")


def ndbc_table():
    """[{id, name, lat, lon, type}] from NDBC's station table (active and closed stations)."""
    def load():
        out = []
        for line in _get_text(_NDBC_TABLE).splitlines():
            if line.startswith("#") or "|" not in line:
                continue
            c = line.split("|")
            if len(c) < 7:
                continue
            m = _LOC.search(c[6])
            if not m:
                continue
            la = float(m.group(1)) * (1 if m.group(2) == "N" else -1)
            lo = float(m.group(3)) * (1 if m.group(4) == "E" else -1)
            out.append({"id": c[0].strip().upper(), "name": c[4].strip() or c[0].strip(),
                        "lat": la, "lon": lo, "type": c[2].strip()})
        return out
    return _cached("ndbc_table", load)


class NdbcSource:
    name, kind = "NDBC", "ndbc"

    def find(self, lat, lon, radius_nm):
        out = []
        for s in ndbc_table():
            d = venue._haversine_nm(lat, lon, s["lat"], s["lon"])
            if d <= radius_nm:
                out.append(dict(s, kind="ndbc", dist_nm=round(d, 1), source=self.name))
        return out


class CuratedSource:
    """venue.STATIONS: hand-picked stations, preferred where they exist."""
    name, kind = "curated", None

    def find(self, lat, lon, radius_nm):
        out = []
        for s in venue.STATIONS:
            d = venue._haversine_nm(lat, lon, s["lat"], s["lon"])
            if d <= radius_nm:
                out.append(dict(s, dist_nm=round(d, 1), source=self.name, curated=True))
        return out


SOURCES = [CuratedSource(), IemMetarSource(), NdbcSource()]


# ------------------------------------------------------------------------------------- probing
# Iowa State answers parallel requests from one address with errors (seen live: 4 of 7 airports
# came back empty when probed 6 at a time, and all had data one at a time), so METAR checks go
# one at a time; NDBC serves static files and takes them in parallel.
_SERIAL = {"metar": threading.Semaphore(1)}


def probe(station, window):
    """How many observations a station has in `window` (year, sd, ed). 0 = none (closed, no wind
    sensor, or the archive is down)."""
    _, sd, ed = window
    gate = _SERIAL.get(station.get("kind"))
    try:
        if gate:
            with gate:
                return len(modelskill.obs_series(station, sd, ed))
        return len(modelskill.obs_series(station, sd, ed))
    except Exception:          # noqa: BLE001 — a dead station is just "no data"
        return 0


def _archive_misses(s, window):
    """True when the catalogue's own archive dates rule the window out (no fetch needed)."""
    _, sd, ed = window
    return bool((s.get("end") and s["end"] < sd) or (s.get("begin") and s["begin"] > ed))


def discover(lat, lon, radius_nm, window=None, sources=None, probe_fn=None, max_per_kind=MAX_PER_KIND,
             workers=6, deadline_s=None):
    """Stations within `radius_nm` of (lat, lon), nearest first, each with `obs` (count in `window`,
    None when no window). Curated stations win a duplicate id. A source that fails is noted, not
    fatal. Returns {stations: [...], notes: [...]}. `ticked` marks the default choice: curated
    stations with data, plus the nearest of each kind with data."""
    sources = SOURCES if sources is None else sources
    probe_fn = probe_fn or probe
    end = time.monotonic() + (DEADLINE_S if deadline_s is None else deadline_s)
    found, notes = {}, []
    ex = ThreadPoolExecutor(max_workers=max(workers, len(sources)))
    srcs = {ex.submit(src.find, lat, lon, radius_nm): src for src in sources}
    wait(srcs, timeout=max(0.0, end - time.monotonic()))
    for f, src in srcs.items():             # in source order: curated first
        if not f.done():
            notes.append(f"{src.name}: no answer in time")
            continue
        try:
            got = f.result()
        except Exception:      # noqa: BLE001 — one catalogue down must not hide the others
            notes.append(f"{src.name}: could not load the station list")
            continue
        for s in got:
            sid = str(s["id"]).upper()
            if s["kind"] == "metar" and len(sid) == 4 and sid[0] == "K":
                dk = ("metar", sid[1:])          # curated KAPN and Iowa State APN are one station
            else:
                dk = (s["kind"], sid)
            if dk not in found or s.get("curated"):
                found[dk] = dict(s, id=sid, tz=s.get("tz") or (found.get(dk) or {}).get("tz"))
    picks = []
    for kind in ("metar", "ndbc"):
        same = sorted((s for s in found.values() if s["kind"] == kind),
                      key=lambda s: (not s.get("curated"), s["dist_nm"]))
        if window:
            same = [s for s in same if s.get("curated") or not _archive_misses(s, window)]
        picks += same[:max_per_kind]
    if window:
        # a stalled archive must not hold the request past the gateway cap: whatever hasn't
        # answered by the deadline is left "not checked" (obs None) and the rest come back
        futs = {ex.submit(probe_fn, s, window): s for s in picks}
        done, late = wait(futs, timeout=max(0.0, end - time.monotonic()))
        for f in done:
            futs[f]["obs"] = f.result()
        for f in late:
            futs[f]["obs"] = None
        if late:
            notes.append(f"{len(late)} station(s) not checked in time. You can still tick them.")
    ex.shutdown(wait=False, cancel_futures=True)
    out = sorted(picks, key=lambda s: s["dist_nm"])
    for kind in ("metar", "ndbc"):
        with_data = [s for s in out if s["kind"] == kind and (not window or s.get("obs"))]
        for s in with_data:
            if s.get("curated"):
                s["ticked"] = True
        if with_data and not any(s.get("ticked") for s in with_data):
            with_data[0]["ticked"] = True
    if not out:
        notes.append("No stations in range. Try a bigger radius.")
    return {"stations": [_public(s) for s in out], "notes": notes}


def _public(s):
    keep = ("id", "kind", "name", "lat", "lon", "dist_nm", "source", "curated", "obs", "ticked", "tz")
    return {k: s[k] for k in keep if k in s}
