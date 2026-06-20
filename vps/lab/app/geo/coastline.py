"""Global coastline source for obstacle avoidance — pluggable, auto-clipped to a course bbox.

The land/lake geometry is GLOBAL (Natural Earth 1:10m), so the SAME code serves any race anywhere:
the optimizer hands us the course bounding box and we return just the polygons that touch it. Land
is `land ∧ ¬lake` evaluated per layer (a point in a lake is water; an island re-added by the
minor-islands layer is land), so Great-Lakes racing and offshore racing both fall out of one rule.

Fetched + cached like the GRIB subsets (`grib.py`): the three Natural Earth files are downloaded once
to `COASTLINE_CACHE` (coastlines don't change) and parsed once per process. Swapping in a
higher-resolution dataset (OSM land polygons / GSHHG) is a drop-in: change `SOURCES` + `_load_layer`.

Caveat (documented for the human-review posture this project keeps): Natural Earth 1:10m is coarse —
accurate for open water vs. mainland, but it misses small islands (e.g. the Mackinac-straits islands)
and is imprecise right at the shoreline. The race-supplied island marks + zones layer (see
`obstacles.py`) is what guarantees the race-critical small obstacles; this layer is the global
backstop. Upgrade the dataset to tighten shoreline fidelity.
"""
from __future__ import annotations

import json
import os
import urllib.request

CACHE = os.environ.get("COASTLINE_CACHE", "/srv/coastline")

# Pluggable source: Natural Earth 1:10m physical, as GeoJSON (martynafford mirror). role -> filename.
SOURCES = {
    "land":    "ne_10m_land.json",
    "lakes":   "ne_10m_lakes.json",
    "islands": "ne_10m_minor_islands.json",
}
BASE_URL = "https://raw.githubusercontent.com/martynafford/natural-earth-geojson/master/10m/physical"
DATA_VERSION = "ne_10m_v5.1.1"          # bump when the dataset/source changes (cache + mask keys)

_LAYERS: dict = {}                       # role -> [polygon]; polygon = [ring]; ring = [(lon,lat)]


# --- fetch + parse -----------------------------------------------------------
def _path(role: str) -> str:
    return os.path.join(CACHE, SOURCES[role])


def ensure_global(cache_dir: str = None) -> dict:
    """Download the source files once into the cache. Returns {role: path}. Best-effort per file."""
    cache_dir = cache_dir or CACHE
    os.makedirs(cache_dir, exist_ok=True)
    out = {}
    for role, fn in SOURCES.items():
        p = os.path.join(cache_dir, fn)
        if not os.path.exists(p) or os.path.getsize(p) < 1000:
            try:
                urllib.request.urlretrieve(f"{BASE_URL}/{fn}", p)
            except Exception:
                continue
        if os.path.exists(p):
            out[role] = p
    return out


def _load_layer(role: str, cache_dir: str) -> list:
    """Parse one GeoJSON file into [polygon] where polygon = [ring], ring = [(lon, lat)]."""
    p = os.path.join(cache_dir, SOURCES[role])
    if not os.path.exists(p):
        return []
    polys = []
    with open(p) as f:
        gj = json.load(f)
    for ft in gj.get("features", []):
        geom = ft.get("geometry") or {}
        t = geom.get("type")
        coords = geom.get("coordinates") or []
        groups = coords if t == "MultiPolygon" else ([coords] if t == "Polygon" else [])
        for poly in groups:
            rings = [[(float(x), float(y)) for x, y in ring] for ring in poly if ring]
            if rings:
                polys.append(rings)
    return polys


def _layer(role: str, cache_dir: str) -> list:
    key = (role, cache_dir)
    if key not in _LAYERS:
        _LAYERS[key] = _load_layer(role, cache_dir)
    return _LAYERS[key]


# --- bbox clip ---------------------------------------------------------------
def _ring_bbox(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def _poly_touches(poly, w, s, e, n) -> bool:
    minx, miny, maxx, maxy = _ring_bbox(poly[0])      # outer ring's extent
    return not (maxx < w or minx > e or maxy < s or miny > n)


def layers_in_bbox(bbox, cache_dir: str = None) -> dict:
    """Polygons of each role whose extent intersects bbox=(north, south, west, east).

    Returns {"land": [...], "lakes": [...], "islands": [...]}, each a list of polygons
    (polygon = [ring]; ring = [(lon, lat)], outer ring first then holes). Empty if the dataset
    isn't present (caller treats that as 'no coastline layer' and leans on zones/islands)."""
    cache_dir = cache_dir or CACHE
    n, s, w, e = bbox
    out = {}
    for role in SOURCES:
        out[role] = [poly for poly in _layer(role, cache_dir) if _poly_touches(poly, w, s, e, n)]
    return out


if __name__ == "__main__":                # manual prep: python -m app.geo.coastline
    got = ensure_global()
    for role, p in got.items():
        sz = os.path.getsize(p) if os.path.exists(p) else 0
        print(f"{role:8s} {p}  {sz} bytes")
    print("OK" if len(got) == len(SOURCES) else "INCOMPLETE (some files failed to fetch)")
