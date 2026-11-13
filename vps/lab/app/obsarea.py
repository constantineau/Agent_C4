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
  - buoy networks on ERDDAP servers (obserddap.py), e.g. Ireland's Marine Institute buoys. Each
    covers its own water and says nothing elsewhere.

A kind with no source for the area gets a plain note, never another area's stations. Lookups are
cached in memory for a day. Network reads go through `_get` (stubbed in tests).

A race can pick its own stations in the Lab (Races > Weather stations). `finder()` lists every
station the sources know around the course, with `auto` on the ones `find()` would pick; `lookup()`
resolves one id. The picks are saved on the race definition as

    stations: {"picked": [{id, kind, lat, lon, name, source?}, ...]}

(absent or null = automatic; `picked: []` = no stations). `race_stations()` is what the playbook
uses: the picks when the race has them, else `find()`.

Adding a source (e.g. a UK buoy network): subclass StationSource with `kind`, `label`, `coverage`,
`find(box)` and `lookup(sid)`, then `register()` it. The finder, the automatic list and the Lab card
pick it up with no other change. The boat reads only the kinds in READABLE_KINDS live, and only
those are frozen. A buoy network on an ERDDAP server is readable once its `shared.erddap.Network`
is registered there (the boat's reader and the freeze's live probe go by kind); any other new kind
needs a reader aboard (vps/agent/app/buoys.py) and a live probe (synthesis) first.
"""
from __future__ import annotations

import json
import math
import time
import urllib.request
import xml.etree.ElementTree as ET

from shared import erddap

from . import venue

_AWC_STATIONS = "https://aviationweather.gov/api/data/stationinfo?format=json&bbox={s},{w},{n},{e}"
_AWC_STATION_ID = "https://aviationweather.gov/api/data/stationinfo?format=json&ids={sid}"
_NDBC_ACTIVE = "https://www.ndbc.noaa.gov/activestations.xml"
_UA = {"User-Agent": "Agent_C4-C4PerformanceLab/1.0"}

CACHE_S = 24 * 3600
PAD_DEG = 0.6             # around the course box: catches the stations just off the course
MAX_PER_KIND = 10         # found (not curated) stations kept per kind, nearest the course first
FINDER_PER_SOURCE = 15    # the Lab's finder lists a few more per source than the automatic pick
MAX_PICKS = 20            # stations a race can pick
# kinds the boat reads live (and the freeze can probe): METAR, NDBC, and every ERDDAP buoy network
# registered in shared.erddap (the boat's buoys.py reads those by kind)
READABLE_KINDS = ("metar", "ndbc") + tuple(erddap.NETWORKS)
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
    quiet_outside = False         # True: no "not here" note outside its coverage (a regional network)

    def covers_bbox(self, bbox) -> bool:
        if self.coverage is None or not bbox:
            return True
        n, s, w, e = bbox
        cs, cn, cw, ce = self.coverage
        return not (n < cs or s > cn or e < cw or w > ce)

    def not_here_note(self):
        if self.quiet_outside:
            return None
        where = f" ({self.coverage_label})" if self.coverage_label else ""
        return f"{self.label}: not here{where}."

    def find(self, box):
        """[{id, kind, lat, lon, name}] inside box=(n, s, w, e)."""
        raise NotImplementedError

    def lookup(self, sid):
        """{id, kind, lat, lon, name} for one station id, or None (default: search the whole
        coverage box with find)."""
        if self.coverage is None:
            return None
        cs, cn, cw, ce = self.coverage
        sid = str(sid).upper()
        return next((st for st in self.find((cn, cs, cw, ce)) if st["id"] == sid), None)


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
            body = _get(_AWC_STATIONS.format(s=s, w=w, n=n, e=e))
            # an area with no airports answers 204 with an empty body: no stations, not an error
            for r in (json.loads(body) if (body or "").strip() else []) or []:
                sid = r.get("icaoId") or r.get("id")
                if not sid or "METAR" not in (r.get("siteType") or []) \
                        or r.get("lat") is None or r.get("lon") is None:
                    continue
                out.append({"id": str(sid).upper(), "kind": "metar", "lat": float(r["lat"]),
                            "lon": float(r["lon"]), "name": r.get("site") or sid})
            return out
        return _cached(f"awc:{n},{s},{w},{e}", load)

    def lookup(self, sid):
        sid = str(sid).upper()

        def load():
            body = _get(_AWC_STATION_ID.format(sid=sid))
            for r in (json.loads(body) if (body or "").strip() else []) or []:
                rid = str(r.get("icaoId") or r.get("id") or "").upper()
                if rid == sid and r.get("lat") is not None and r.get("lon") is not None:
                    return {"id": sid, "kind": "metar", "lat": float(r["lat"]),
                            "lon": float(r["lon"]), "name": r.get("site") or sid}
            return None
        return _cached(f"awc_id:{sid}", load)


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
# The area sources, by name. `find` (automatic picks), `finder` (the Lab list) and `lookup` (add by
# id) all walk this registry in order, so a new source shows up everywhere once registered.
SOURCES = {"metar": AwcMetarSource(), "ndbc": NdbcSource()}


def register(source):
    """Add an area source (a StationSource with a unique `name`)."""
    SOURCES[source.name] = source
    return source


def source_for_kind(kind):
    """The area source whose stations are of `kind`, or None."""
    return next((src for src in SOURCES.values() if src.kind == kind), None)


def kinds():
    """Every station kind a source can return."""
    return {"metar", "ndbc"} | {src.kind for src in SOURCES.values() if src.kind}


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
    for src in SOURCES.values():
        if any(st["kind"] == src.kind for st in curated):
            continue
        if not src.covers_bbox(bbox):
            if src.not_here_note():
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
    src = source_for_kind(kind)
    if src is None:
        return None
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


# ------------------------------------------------------------------- the Lab's finder + race picks
def _key(st):
    return (st.get("kind"), str(st.get("id", "")).upper())


def finder(bbox, pad_deg=PAD_DEG, per_source=FINDER_PER_SOURCE):
    """Every station the sources know around a course box, for the Lab to pick from: the curated
    ones in range and up to `per_source` from each area source, nearest the course centre first.
    Each row: {id, kind, name, lat, lon, dist_nm, source, auto}; `auto` marks what `find()` picks
    (what the race uses with no picks). Returns {stations, notes}."""
    if not bbox:
        return {"stations": [], "notes": ["No marks with a position yet."]}
    auto = find(bbox, pad_deg)
    auto_keys = {_key(st) for st in auto["stations"]}
    box = _pad(bbox, pad_deg)
    n, s, w, e = bbox
    clat, clon = (n + s) / 2.0, (w + e) / 2.0
    dist = lambda st: venue._haversine_nm(clat, clon, st["lat"], st["lon"])   # noqa: E731
    rows = {}
    for st in CURATED.find(box):
        rows.setdefault(_key(st), dict(st, source="curated"))
    notes = []
    for src in SOURCES.values():
        if not src.covers_bbox(bbox):
            if src.not_here_note():
                notes.append(src.not_here_note())
            continue
        try:
            got = src.find(box)
        except Exception:          # noqa: BLE001 — one list down must not hide the others
            notes.append(f"{src.label}: could not load the station list.")
            continue
        for st in sorted(got, key=dist)[:per_source]:
            rows.setdefault(_key(st), dict(st, source=src.name))
    out = []
    for k, st in rows.items():
        out.append({"id": str(st["id"]).upper(), "kind": st["kind"], "name": st.get("name") or st["id"],
                    "lat": st["lat"], "lon": st["lon"], "dist_nm": round(dist(st), 1),
                    "source": st.get("source"), "auto": k in auto_keys})
    out.sort(key=lambda st: st["dist_nm"])
    return {"stations": out, "notes": notes}


def lookup(sid, near=None):
    """One station by id from the curated list, then each area source, or None. `near` = (lat, lon)
    adds `dist_nm`. Best-effort: a source that can't load is skipped."""
    sid = str(sid or "").strip().upper()
    if not sid or len(sid) > 16:
        return None
    hit = next((dict(st, source="curated") for st in venue.STATIONS if st["id"].upper() == sid), None)
    if not hit:
        for src in SOURCES.values():
            try:
                st = src.lookup(sid)
            except Exception:      # noqa: BLE001
                st = None
            if st:
                hit = dict(st, source=src.name)
                break
    if not hit:
        return None
    out = {"id": sid, "kind": hit["kind"], "name": hit.get("name") or sid, "lat": hit["lat"],
           "lon": hit["lon"], "source": hit.get("source")}
    if near:
        out["dist_nm"] = round(venue._haversine_nm(near[0], near[1], hit["lat"], hit["lon"]), 1)
    return out


def clean_pick(row):
    """A picked station as stored on the race ({id, kind, lat, lon, name, source?}), or None when
    it is not a usable station."""
    try:
        sid = str(row["id"]).strip().upper()
        lat, lon = float(row["lat"]), float(row["lon"])
        kind = str(row["kind"])
    except (KeyError, TypeError, ValueError):
        return None
    if not sid or len(sid) > 16 or not all(c.isalnum() or c in "-_" for c in sid):
        return None
    if kind not in kinds() or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    out = {"id": sid, "kind": kind, "lat": lat, "lon": lon,
           "name": str(row.get("name") or sid)[:80]}
    if row.get("source"):
        out["source"] = str(row["source"])[:40]
    return out


def clean_picks(rows, limit=MAX_PICKS):
    """The race's `stations` block from a list of picked rows (bad rows dropped, one per id, at
    most MAX_PICKS). None = automatic."""
    if rows is None:
        return None
    out, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        c = clean_pick(r) if isinstance(r, dict) else None
        if c and _key(c) not in seen:
            seen.add(_key(c))
            out.append(c)
    return {"picked": out if limit is None else out[:limit]}


def picks(definition):
    """The race's picked stations (a list, maybe empty), or None when the race uses automatic."""
    block = (definition or {}).get("stations")
    if not isinstance(block, dict) or not isinstance(block.get("picked"), list):
        return None
    return [c for c in (clean_pick(r) for r in block["picked"] if isinstance(r, dict)) if c]


def race_stations(definition, bbox, pad_deg=PAD_DEG):
    """The stations a race's playbook uses: its picks when it has them (no network), else the
    automatic `find()`. Returns {stations, notes, picked}."""
    p = picks(definition)
    if p is not None:
        return {"stations": p, "notes": [], "picked": True}
    return dict(find(bbox, pad_deg), picked=False)


# buoy networks on ERDDAP servers (Ireland's Marine Institute, ...): they register themselves
from . import obserddap  # noqa: E402,F401
