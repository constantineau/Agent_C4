"""Observation stations for a race's area (#309): METAR airports and NDBC buoys near the course.

The live buoy ladder aboard, the playbook's station promises and venue model skill all need the
observed-wind stations near the race. They used to read only venue.STATIONS, a hand-picked Great
Lakes list, so a race anywhere else got nothing. Now each kind of station has a source that
declares its area, like the wind models in wind/models.py:

  - curated: venue.STATIONS. The verified set where it has stations. For each kind, a curated
    station near the course wins and the lookup stops there, so Great Lakes races keep exactly
    the stations (and order) they had.
  - METAR: aviationweather.gov's station list by bounding box. Worldwide.
  - NDBC: NDBC's active station table (stations with weather sensors), by box. US and Caribbean
    only: elsewhere the lookup says so plainly.

A kind with no source for the area gets a plain note, never another area's stations. Lookups are
cached in memory for a day. Network reads go through `_get` (stubbed in tests).
"""
from __future__ import annotations

import json
import math
import time
import urllib.request
import xml.etree.ElementTree as ET

from . import venue

_AWC_STATIONS = "https://aviationweather.gov/api/data/stationinfo?format=json&bbox={s},{w},{n},{e}"
_NDBC_ACTIVE = "https://www.ndbc.noaa.gov/activestations.xml"
_UA = {"User-Agent": "Agent_C4-C4PerformanceLab/1.0"}

CACHE_S = 24 * 3600
PAD_DEG = 0.6             # around the course box: catches the stations just off the course
MAX_PER_KIND = 10         # found (not curated) stations kept per kind, nearest the course first
_cache: dict = {}


def _get(url, timeout=20):          # seam for tests
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _cached(key, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def clear_cache():
    _cache.clear()


def _in_box(st, box):
    n, s, w, e = box
    return s <= st["lat"] <= n and w <= st["lon"] <= e


class StationSource:
    name, kind, label = "base", None, "stations"
    coverage = None               # (south, north, west, east), or None = worldwide
    coverage_label = None

    def covers_bbox(self, bbox) -> bool:
        if self.coverage is None or not bbox:
            return True
        n, s, w, e = bbox
        cs, cn, cw, ce = self.coverage
        return not (n < cs or s > cn or e < cw or w > ce)

    def not_here_note(self) -> str:
        where = f" ({self.coverage_label})" if self.coverage_label else ""
        return f"{self.label}: not here{where}."

    def find(self, box):
        """[{id, kind, lat, lon, name}] inside box=(n, s, w, e)."""
        raise NotImplementedError


class CuratedSource(StationSource):
    name, label = "curated", "Curated stations"

    def find(self, box):
        return [dict(st, curated=True) for st in venue.STATIONS if _in_box(st, box)]


class AwcMetarSource(StationSource):
    name, kind, label = "metar", "metar", "METAR airports"

    def find(self, box):
        n, s, w, e = (round(x, 2) for x in box)

        def load():
            out = []
            for r in json.loads(_get(_AWC_STATIONS.format(s=s, w=w, n=n, e=e))) or []:
                sid = r.get("icaoId") or r.get("id")
                if not sid or "METAR" not in (r.get("siteType") or []) \
                        or r.get("lat") is None or r.get("lon") is None:
                    continue
                out.append({"id": str(sid).upper(), "kind": "metar", "lat": float(r["lat"]),
                            "lon": float(r["lon"]), "name": r.get("site") or sid})
            return out
        return _cached(f"awc:{n},{s},{w},{e}", load)


def ndbc_active():
    """Every active NDBC station that reports weather (met="y"): [{id, kind, lat, lon, name}]."""
    def load():
        out = []
        for el in ET.fromstring(_get(_NDBC_ACTIVE)).iter("station"):
            if el.get("met") != "y":
                continue
            try:
                out.append({"id": el.get("id").upper(), "kind": "ndbc", "lat": float(el.get("lat")),
                            "lon": float(el.get("lon")), "name": el.get("name") or el.get("id")})
            except (TypeError, ValueError, AttributeError):
                continue
        return out
    return _cached("ndbc_active", load)


class NdbcSource(StationSource):
    name, kind, label = "ndbc", "ndbc", "NDBC buoys"
    coverage = (5.0, 72.0, -180.0, -52.0)     # US waters, the Caribbean, the Great Lakes
    coverage_label = "US and Caribbean only"

    def find(self, box):
        return [st for st in ndbc_active() if _in_box(st, box)]


CURATED = CuratedSource()
SOURCES = {"metar": AwcMetarSource(), "ndbc": NdbcSource()}


def _pad(bbox, pad_deg):
    n, s, w, e = bbox
    return (n + pad_deg, s - pad_deg, w - pad_deg, e + pad_deg)


def find(bbox, pad_deg=PAD_DEG, max_per_kind=MAX_PER_KIND):
    """The observation stations for a course box bbox=(n, s, w, e), padded by `pad_deg`.

    Returns {stations: [{id, kind, lat, lon, name, ...}], notes: [plain sentences]}. Per kind:
    curated stations in the box if there are any (in their list order), else the area source's
    stations nearest the course centre (at most `max_per_kind`), else a note saying why none."""
    if not bbox:
        return {"stations": [], "notes": []}
    box = _pad(bbox, pad_deg)
    curated = CURATED.find(box)
    n, s, w, e = bbox
    clat, clon = (n + s) / 2.0, (w + e) / 2.0
    out, notes = [], []
    for kind in ("metar", "ndbc"):
        if any(st["kind"] == kind for st in curated):
            continue
        src = SOURCES[kind]
        if not src.covers_bbox(bbox):
            notes.append(src.not_here_note())
            continue
        try:
            got = src.find(box)
        except Exception:          # noqa: BLE001 — one list down must not hide the other
            notes.append(f"{src.label}: could not load the station list.")
            continue
        got = sorted(got, key=lambda st: venue._haversine_nm(clat, clon, st["lat"], st["lon"]))
        if not got:
            notes.append(f"{src.label}: none near the course.")
        out += [dict(st, source=src.name) for st in got[:max_per_kind]]
    return {"stations": curated + out, "notes": notes}


def nearest(lat, lon, kind, max_nm):
    """The closest station of `kind` within `max_nm` of a point from the area sources (not the
    curated list), or None. Best-effort: a list that can't load is just None."""
    src = SOURCES[kind]
    dlat = max_nm / 60.0
    dlon = dlat / max(0.1, math.cos(math.radians(lat)))
    box = (lat + dlat, lat - dlat, lon - dlon, lon + dlon)
    if not src.covers_bbox(box):
        return None
    try:
        got = src.find(box)
    except Exception:          # noqa: BLE001
        return None
    best, bd = None, None
    for st in got:
        d = venue._haversine_nm(lat, lon, st["lat"], st["lon"])
        if d <= max_nm and (bd is None or d < bd):      # within the promised radius (Codex #343)
            best, bd = st, d
    return dict(best, dist_nm=round(bd, 1), source=src.name) if best else None
