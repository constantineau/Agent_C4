"""Global coastline source for obstacle avoidance — pluggable, auto-clipped to a course bbox.

The land/lake geometry is GLOBAL, so the SAME code serves any race anywhere: the optimizer hands us
the course bounding box and we return just the polygons that touch it. Land is `land ∧ ¬lake` with
islands re-added inside lakes (a point in a lake is water; an island in that lake is land), so
Great-Lakes racing and offshore racing both fall out of one rule.

TWO pluggable global datasets, chosen by `COASTLINE_GLOBAL` (default `gshhg`):

  - **gshhg** — GSHHG (Global Self-consistent Hierarchical High-resolution Geography), the
    full-resolution shoreline. Its hierarchy maps EXACTLY onto our three roles: **L1 = land**
    (continents + ocean islands), **L2 = lakes**, **L3 = islands in lakes** — and L3 is precisely
    where the small Great-Lakes islands live (e.g. Cove Island, the Bayview Mackinac gate island in
    Ontario, which Natural Earth omits entirely). Shipped as shapefiles → clipped to the bbox with
    `ogr2ogr -clipsrc` (a prep-time GDAL step, like `enc.py`) into cached GeoJSON, then parsed
    pure-python on the hot path. This is the higher-res backstop that fixes the coarse-NE small-island
    gap globally (Canada included, where US-only ENC has nothing).
  - **natural_earth** — Natural Earth 1:10m (`land` / `lakes` / `minor_islands` as ready-made GeoJSON).
    Coarse: accurate for open water vs. mainland but it MISSES sub-nm islands and is imprecise right at
    the shoreline. Kept as a dependency-light fallback (no GDAL needed) and used automatically if GSHHG
    is unavailable (no download / no ogr2ogr).

Both are fetched + cached once to `COASTLINE_CACHE` (coastlines don't change) like the GRIB subsets.
The race-supplied island marks + zones layer (see `obstacles.py`) still backs the race-critical
obstacles regardless; this module is the global backstop under them.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
import zipfile

CACHE = os.environ.get("COASTLINE_CACHE", "/srv/coastline")

# Which global dataset to use: gshhg (full-res, GDAL clip) | natural_earth (coarse, ready-made GeoJSON).
GLOBAL_SOURCE = os.environ.get("COASTLINE_GLOBAL", "gshhg").strip().lower()

_HDRS = {"User-Agent": "Agent_C4-lab/1.0"}

# --- Natural Earth (coarse, ready-made GeoJSON, no GDAL) ----------------------
NE_SOURCES = {                            # role -> filename (martynafford NE GeoJSON mirror)
    "land":    "ne_10m_land.json",
    "lakes":   "ne_10m_lakes.json",
    "islands": "ne_10m_minor_islands.json",
}
NE_BASE_URL = "https://raw.githubusercontent.com/martynafford/natural-earth-geojson/master/10m/physical"
NE_DATA_VERSION = "ne_10m_v5.1.1"

# --- GSHHG (full-resolution shoreline, shapefiles → GDAL clip) ----------------
GSHHG_URL = "https://www.soest.hawaii.edu/pwessel/gshhg/gshhg-shp-2.3.7.zip"
GSHHG_RES = os.environ.get("GSHHG_RES", "f").strip().lower()   # f(ull)|h(igh)|i|l|c — full = all islands
GSHHG_DIR = "gshhg"                       # subdir under CACHE
GSHHG_ROLES = {"land": "L1", "lakes": "L2", "islands": "L3"}   # role -> GSHHG hierarchy level
GSHHG_VERSION = "gshhg_2.3.7"

# Provenance + cache-busting version reflect the CONFIGURED global source (see active_source()).
DATA_VERSION = (f"{GSHHG_VERSION}_{GSHHG_RES}" if GLOBAL_SOURCE == "gshhg" else NE_DATA_VERSION)

_LAYERS: dict = {}                        # NE: (role, cache_dir) -> [polygon]; polygon=[ring]; ring=[(lon,lat)]


def active_source() -> str:
    """The configured global dataset name ('gshhg' or 'natural_earth'), for field provenance."""
    return "gshhg" if GLOBAL_SOURCE == "gshhg" else "natural_earth"


# --- shared GeoJSON parser ----------------------------------------------------
def _parse_geojson(path: str) -> list:
    """Parse a GeoJSON file into [polygon] where polygon = [ring], ring = [(lon, lat)]."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            gj = json.load(f)
    except Exception:
        return []
    polys = []
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


# --- public API (dispatches on the active global source) ---------------------
def ensure_global(cache_dir: str = None) -> dict:
    """Fetch the active global dataset into the cache once. Returns {role/key: path}. Best-effort."""
    cache_dir = cache_dir or CACHE
    os.makedirs(cache_dir, exist_ok=True)
    if active_source() == "gshhg" and _ensure_gshhg(cache_dir):
        return {"gshhg": _gshhg_res_dir(cache_dir)}
    return _ensure_ne(cache_dir)          # NE (also the fallback when GSHHG can't be fetched)


def layers_in_bbox(bbox, cache_dir: str = None) -> dict:
    """Polygons of each role whose extent intersects bbox=(north, south, west, east).

    Returns {"land": [...], "lakes": [...], "islands": [...]}, each a list of polygons
    (polygon = [ring]; ring = [(lon, lat)], outer ring first then holes). The obstacle mask fills
    land, carves lakes, then re-adds islands. Falls back to Natural Earth automatically if GSHHG is
    the configured source but unavailable (missing files / ogr2ogr error). Empty roles are valid
    (e.g. an open-ocean bbox has no land)."""
    cache_dir = cache_dir or CACHE
    if active_source() == "gshhg":
        out = _gshhg_layers_in_bbox(bbox, cache_dir)
        if out is not None:
            return out                    # gshhg usable (even if some roles are legitimately empty)
    return _ne_layers_in_bbox(bbox, cache_dir)


# --- Natural Earth implementation --------------------------------------------
def _ensure_ne(cache_dir: str) -> dict:
    """Download the NE source files once into the cache. Returns {role: path}. Best-effort per file."""
    out = {}
    for role, fn in NE_SOURCES.items():
        p = os.path.join(cache_dir, fn)
        if not os.path.exists(p) or os.path.getsize(p) < 1000:
            try:
                urllib.request.urlretrieve(f"{NE_BASE_URL}/{fn}", p)
            except Exception:
                continue
        if os.path.exists(p):
            out[role] = p
    return out


def _ne_layer(role: str, cache_dir: str) -> list:
    key = (role, cache_dir)
    if key not in _LAYERS:
        _LAYERS[key] = _parse_geojson(os.path.join(cache_dir, NE_SOURCES[role]))
    return _LAYERS[key]


def _ne_layers_in_bbox(bbox, cache_dir: str) -> dict:
    n, s, w, e = bbox
    out = {}
    for role in NE_SOURCES:
        out[role] = [poly for poly in _ne_layer(role, cache_dir) if _poly_touches(poly, w, s, e, n)]
    return out


# --- GSHHG implementation -----------------------------------------------------
def _gshhg_res_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, GSHHG_DIR, GSHHG_RES)


def _gshhg_needed(cache_dir: str) -> list:
    rd = _gshhg_res_dir(cache_dir)
    return [os.path.join(rd, f"GSHHS_{GSHHG_RES}_{L}.shp") for L in ("L1", "L2", "L3")]


def _ensure_gshhg(cache_dir: str) -> bool:
    """Download + unzip the GSHHG bundle once, keeping only the chosen-res L1–L3 shapefiles. Idempotent.

    Returns True if the L1–L3 shapefiles for `GSHHG_RES` are present (so the caller can clip them)."""
    if all(os.path.exists(p) for p in _gshhg_needed(cache_dir)):
        return True
    rd = _gshhg_res_dir(cache_dir)
    os.makedirs(rd, exist_ok=True)
    zpath = os.path.join(cache_dir, GSHHG_DIR, "gshhg-shp.zip")
    if not (os.path.exists(zpath) and os.path.getsize(zpath) > 1_000_000):
        try:
            req = urllib.request.Request(GSHHG_URL, headers=_HDRS)
            with urllib.request.urlopen(req, timeout=600) as r, open(zpath, "wb") as f:
                shutil.copyfileobj(r, f)
        except Exception:
            return False
    # Extract only GSHHS_shp/<res>/GSHHS_<res>_L{1,2,3}.* (skip WDBII rivers/borders + other res).
    prefix = f"GSHHS_shp/{GSHHG_RES}/GSHHS_{GSHHG_RES}_L"
    try:
        with zipfile.ZipFile(zpath) as z:
            for m in z.namelist():
                if not m.startswith(prefix):
                    continue
                bn = os.path.basename(m)
                lvl = bn[len(f"GSHHS_{GSHHG_RES}_L"):len(f"GSHHS_{GSHHG_RES}_L") + 1]
                if lvl in ("1", "2", "3"):
                    with z.open(m) as src, open(os.path.join(rd, bn), "wb") as dst:
                        shutil.copyfileobj(src, dst)
    except Exception:
        return False
    try:
        os.remove(zpath)                  # ~150 MB — drop it once the shapefiles are extracted
    except OSError:
        pass
    return all(os.path.exists(p) for p in _gshhg_needed(cache_dir))


def _clip_shp(shp: str, out_path: str, bbox) -> bool:
    """ogr2ogr -clipsrc one GSHHG level to bbox=(north,south,west,east) → GeoJSON. True if it wrote.

    -clipsrc (not -spat) CLIPS the geometry to the window, so the giant continent L1 polygon collapses
    to the bbox rectangle (a handful of vertices) instead of being returned whole. An empty result
    (open ocean) is a valid success: ogr2ogr writes an empty FeatureCollection with returncode 0."""
    n, s, w, e = bbox
    try:
        r = subprocess.run(["ogr2ogr", "-f", "GeoJSON", "-clipsrc", str(w), str(s), str(e), str(n),
                            out_path, shp], capture_output=True, timeout=300)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0:
        if os.path.exists(out_path) and os.path.getsize(out_path) < 2:
            try:
                os.remove(out_path)
            except OSError:
                pass
        return False
    return os.path.exists(out_path)


def _gshhg_layers_in_bbox(bbox, cache_dir: str):
    """{land,lakes,islands} from GSHHG full-res, clipped to bbox. None if GSHHG is unusable (→ NE)."""
    if not _ensure_gshhg(cache_dir):
        return None
    clip_dir = os.path.join(cache_dir, GSHHG_DIR, "clip")
    os.makedirs(clip_dir, exist_ok=True)
    key = hashlib.sha1(repr((GSHHG_RES, tuple(round(x, 4) for x in bbox))).encode()).hexdigest()[:12]
    rd = _gshhg_res_dir(cache_dir)
    out = {}
    for role, L in GSHHG_ROLES.items():
        shp = os.path.join(rd, f"GSHHS_{GSHHG_RES}_{L}.shp")
        gj = os.path.join(clip_dir, f"{key}_{L}.json")
        if not os.path.exists(gj):
            if not os.path.exists(shp) or not _clip_shp(shp, gj, bbox):
                return None               # a missing level / ogr2ogr failure → fall back to NE
        out[role] = _parse_geojson(gj)
    return out


# --- bbox clip helpers (NE) ---------------------------------------------------
def _ring_bbox(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def _poly_touches(poly, w, s, e, n) -> bool:
    minx, miny, maxx, maxy = _ring_bbox(poly[0])      # outer ring's extent
    return not (maxx < w or minx > e or maxy < s or miny > n)


if __name__ == "__main__":                # manual prep: python -m app.geo.coastline [n s w e]
    import sys
    print(f"global source = {active_source()} (data_version {DATA_VERSION})")
    got = ensure_global()
    for k, p in got.items():
        print(f"  {k}: {p}")
    if len(sys.argv) == 5:
        bb = tuple(float(x) for x in sys.argv[1:5])
        lay = layers_in_bbox(bb)
        for role, polys in lay.items():
            print(f"  {role}: {len(polys)} polys in bbox {bb}")
