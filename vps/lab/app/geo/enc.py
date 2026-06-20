"""NOAA ENC source — authoritative S-57 vector charts for obstacle avoidance (the [A] upgrade).

Natural Earth 1:10m (`coastline.py`) is a global backstop but coarse: it misses the Mackinac-straits
islands entirely and is imprecise right at the shoreline. NOAA's Electronic Navigational Charts (ENC)
are the authoritative US S-57 vector product and cover the whole Great Lakes — real land polygons,
real depth areas, real rocks/obstructions. This module plugs ENC into the same pluggable `coastline`
seam, adding two capabilities Natural Earth can't give:

  - **LNDARE** (land areas)            → real coastline + every island as a true polygon (role `land`);
  - **DEPARE** (depth areas)           → the boat's **draft-aware shoal no-go** (role `shoal`): any
                                         depth area whose shallow bound is < the boat's safety depth
                                         (draft + under-keel margin) is blocked water;
  - **OBSTRN / UWTROC** (obstructions, rocks) → point/area hazards shallower than the safety depth
                                         (role `obstruction`).

GDAL is a **prep-time** dependency only: at prep we download the covering ENC cells and run `ogr2ogr`
(S-57 driver) once per cell/layer → cached GeoJSON on the `lab_enc` volume. The routing hot loop stays
pure-python — it just loads the cached GeoJSON, exactly like the Natural Earth path. Re-deriving for a
different draft re-filters the cached DEPARE/OBSTRN GeoJSON; it does **not** re-run GDAL.

Discovery uses NOAA's ENC Product Catalog (cell footprints + zip URLs). We pick cells by usage band:
Great-Lakes ENC coverage is band 2 (overview) → band 4 (approach, 1:45k–1:90k, where the straits
islands live) → band 5 (harbor). Default to band 4 — full land/depth detail without thousands of tiny
berthing cells.

CAVEAT (kept consistent with this project's verify-against-official-chart posture): NOAA GIS/derived
exports are "non-navigational use" — fine as strategy/planning data; the boat still navigates off its
own certified gear.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

CACHE = os.environ.get("ENC_CACHE", "/srv/enc")
CATALOG_URL = "https://charts.noaa.gov/ENCs/ENCProdCat.xml"
DOWNLOAD_BASE = "https://www.charts.noaa.gov/ENCs"
DATA_VERSION = "noaa_enc_v1"                 # bump when the extraction logic changes (cache + mask keys)

# Usage bands to pull, in preference order. Great Lakes: 4 = approach (islands + shoals), 5 = harbor.
ENC_BANDS = [b.strip() for b in os.environ.get("ENC_BANDS", "4").split(",") if b.strip()]
MAX_CELLS = int(os.environ.get("ENC_MAX_CELLS", "80"))      # safety cap on a single bbox's cell set
CATALOG_TTL_DAYS = float(os.environ.get("ENC_CATALOG_TTL_DAYS", "14"))

# Layers we extract from each S-57 cell, and the geometry-bearing object classes.
LAYERS = ("LNDARE", "DEPARE", "OBSTRN", "UWTROC")
OBSTRUCTION_NM = float(os.environ.get("GEO_OBSTRUCTION_NM", "0.1"))   # buffer for point rocks/obstrs

_HDRS = {"User-Agent": "Agent_C4-lab/1.0"}


# --- discovery: the ENC product catalog --------------------------------------
def _index_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "cell_index.json")


def cell_index(cache_dir: str = None, refresh: bool = False) -> dict:
    """{cell_name: {"bbox":[n,s,w,e], "url":..., "band":"4"}} for every Active ENC cell.

    Parsed once from the 10 MB product catalog and cached to disk (refreshed past CATALOG_TTL_DAYS)."""
    cache_dir = cache_dir or CACHE
    os.makedirs(cache_dir, exist_ok=True)
    p = _index_path(cache_dir)
    if not refresh and os.path.exists(p):
        if (time.time() - os.path.getmtime(p)) < CATALOG_TTL_DAYS * 86400:
            try:
                with open(p) as f:
                    return json.load(f)
            except Exception:
                pass
    req = urllib.request.Request(CATALOG_URL, headers=_HDRS)
    root = ET.fromstring(urllib.request.urlopen(req, timeout=60).read())
    idx = {}
    for c in root.findall(".//cell"):
        if (c.findtext("status") or "") != "Active":
            continue
        name = c.findtext("name")
        if not name:
            continue
        xs, ys = [], []
        for v in c.findall(".//vertex"):
            la, lo = v.find("lat"), v.find("long")
            if la is not None and lo is not None:
                try:
                    ys.append(float(la.text))
                    xs.append(float(lo.text))
                except (TypeError, ValueError):
                    continue
        if not xs:
            continue
        url = c.findtext("zipfile_location") or f"{DOWNLOAD_BASE}/{name}.zip"
        if not url.endswith(".zip"):
            url += ".zip"
        idx[name] = {"bbox": [max(ys), min(ys), min(xs), max(xs)], "url": url, "band": name[2]}
    with open(p, "w") as f:
        json.dump(idx, f)
    return idx


def _touches(bbox, cell_bbox) -> bool:
    n, s, w, e = bbox
    cn, cs, cw, ce = cell_bbox
    return not (ce < w or cw > e or cn < s or cs > n)


def cells_for_bbox(bbox, cache_dir: str = None, bands=None, max_cells: int = MAX_CELLS) -> list:
    """Active ENC cells whose footprint intersects bbox=(n,s,w,e), filtered to `bands`.

    Falls back to the next band(s) only if the preferred band yields nothing for the bbox."""
    bands = bands or ENC_BANDS
    idx = cell_index(cache_dir)
    for band in bands:                       # prefer the first band that actually covers the bbox
        hits = [{"name": nm, **meta} for nm, meta in idx.items()
                if meta["band"] == band and _touches(bbox, meta["bbox"])]
        if hits:
            hits.sort(key=lambda h: h["name"])
            return hits[:max_cells]
    # nothing in the preferred bands — try ANY band (coarse overview better than nothing)
    hits = [{"name": nm, **meta} for nm, meta in idx.items() if _touches(bbox, meta["bbox"])]
    hits.sort(key=lambda h: h["band"])       # coarsest (lowest band number) first
    return hits[:max_cells]


# --- download + S-57 extraction (GDAL, prep-time) ----------------------------
def _cell_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, "cells")


def _extract_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, "extract")


def _ensure_cell_s57(cell: dict, cache_dir: str) -> str | None:
    """Download + unzip one cell. Returns the path to its `.000` base file (or None on failure)."""
    name = cell["name"]
    root = _cell_dir(cache_dir)
    s57 = os.path.join(root, "ENC_ROOT", name, f"{name}.000")
    if os.path.exists(s57):
        return s57
    os.makedirs(root, exist_ok=True)
    try:
        req = urllib.request.Request(cell["url"], headers=_HDRS)
        blob = urllib.request.urlopen(req, timeout=120).read()
        with zipfile.ZipFile(__import__("io").BytesIO(blob)) as z:
            z.extractall(root)
    except Exception:
        return None
    return s57 if os.path.exists(s57) else None


def _extract_layer(s57_path: str, layer: str, out_path: str) -> bool:
    """Run ogr2ogr (S-57 driver) to dump one object class to GeoJSON. True if `out_path` now exists.

    Missing layers are normal (not every cell has every class) → returns False without raising."""
    if os.path.exists(out_path):
        return True
    env = dict(os.environ)
    # keep only feature geometry; drop the S-57 graph primitives/linkages we don't need
    env["OGR_S57_OPTIONS"] = "RETURN_PRIMITIVES=OFF,RETURN_LINKAGES=OFF,LNAM_REFS=OFF"
    try:
        r = subprocess.run(
            ["ogr2ogr", "-f", "GeoJSON", "-skipfailures", out_path, s57_path, layer],
            env=env, capture_output=True, timeout=180)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0:
        # ogr2ogr may still have written an empty file or none; clean a partial empty
        if os.path.exists(out_path) and os.path.getsize(out_path) < 2:
            try:
                os.remove(out_path)
            except OSError:
                pass
        return os.path.exists(out_path)
    return os.path.exists(out_path)


def ensure_bbox(bbox, cache_dir: str = None, on_progress=None) -> dict:
    """PREP STEP: download + GDAL-extract every layer of every cell covering bbox.

    Returns a manifest {cell: {layer: geojson_path|None}}. Best-effort per cell/layer (a failed
    download or missing layer is just skipped). Idempotent + cached, so re-runs are cheap."""
    cache_dir = cache_dir or CACHE
    os.makedirs(_extract_dir(cache_dir), exist_ok=True)
    cells = cells_for_bbox(bbox, cache_dir)
    manifest = {}
    for cell in cells:
        name = cell["name"]
        s57 = _ensure_cell_s57(cell, cache_dir)
        if not s57:
            if on_progress:
                on_progress(f"enc: {name} download failed")
            continue
        per = {}
        for layer in LAYERS:
            out = os.path.join(_extract_dir(cache_dir), f"{name}_{layer}.json")
            per[layer] = out if _extract_layer(s57, layer, out) else None
        manifest[name] = per
        if on_progress:
            got = [l for l, v in per.items() if v]
            on_progress(f"enc: {name} extracted {','.join(got) or 'none'}")
    return manifest


# --- GeoJSON → polygons (pure-python, hot-path) ------------------------------
def _feature_polys(feat) -> list:
    """A GeoJSON feature → [polygon] of (lon,lat) rings; points/lines buffered to a small disk."""
    geom = feat.get("geometry") or {}
    t = geom.get("type")
    coords = geom.get("coordinates") or []
    if t == "Polygon":
        return [[[(float(x), float(y)) for x, y in ring] for ring in coords if ring]]
    if t == "MultiPolygon":
        return [[[(float(x), float(y)) for x, y in ring] for ring in poly if ring] for poly in coords]
    if t == "Point":
        return [_disk_ring(float(coords[0]), float(coords[1]), OBSTRUCTION_NM)]
    if t in ("LineString", "MultiLineString"):
        pts = coords if t == "LineString" else [p for line in coords for p in line]
        return [_disk_ring(float(x), float(y), OBSTRUCTION_NM) for x, y in pts]
    return []


def _disk_ring(lon, lat, radius_nm, sides=8) -> list:
    """A small polygon ring approximating a disk of `radius_nm` around (lon,lat)."""
    import math
    rlat = radius_nm / 60.0
    rlon = radius_nm / (60.0 * max(0.15, math.cos(math.radians(lat))))
    return [[(lon + rlon * math.cos(2 * math.pi * k / sides),
              lat + rlat * math.sin(2 * math.pi * k / sides)) for k in range(sides)]]


def _load_polys(path: str, keep=None) -> list:
    """Load a cached GeoJSON into [polygon]; `keep(props)->bool` filters features (e.g. by depth)."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            gj = json.load(f)
    except Exception:
        return []
    out = []
    for ft in gj.get("features", []):
        if keep and not keep(ft.get("properties") or {}):
            continue
        for poly in _feature_polys(ft):
            if poly and poly[0]:
                out.append(poly)
    return out


# --- depth / hazard predicates -----------------------------------------------
def _shoal_keep(safety_depth_m):
    """A DEPARE is no-go water if its SHALLOW bound (DRVAL1) is under the safety depth."""
    def keep(props):
        d1 = props.get("DRVAL1")
        try:
            d1 = float(d1) if d1 is not None else 0.0     # null shallow-bound → treat as 0 (block)
        except (TypeError, ValueError):
            d1 = 0.0
        return d1 < safety_depth_m
    return keep


def _hazard_keep(safety_depth_m):
    """An OBSTRN/UWTROC is a hazard if its sounding is unknown or shallower than the safety depth."""
    def keep(props):
        v = props.get("VALSOU")
        if v is None:
            return True                                   # unknown depth → treat as a hazard
        try:
            return float(v) < safety_depth_m
        except (TypeError, ValueError):
            return True
    return keep


# --- public: role layers for the obstacle mask -------------------------------
def layers_in_bbox(bbox, cache_dir: str = None, safety_depth_m: float = 3.0) -> dict:
    """ENC obstacle polygons by role, clipped to bbox=(n,s,w,e). Mirrors `coastline.layers_in_bbox`.

    Returns {"land":[poly], "shoal":[poly], "obstruction":[poly]} — every polygon is BLOCKED water
    (no `lakes`/`islands` carve: ENC LNDARE is land-only, water is everything else). `shoal` and
    `obstruction` are filtered by `safety_depth_m` (= boat draft + under-keel margin), so a different
    boat/draft re-filters the same cached GeoJSON without re-running GDAL. Empty if ENC isn't prepped
    (caller falls back to the global coastline)."""
    cache_dir = cache_dir or CACHE
    cells = cells_for_bbox(bbox, cache_dir)
    ed = _extract_dir(cache_dir)
    land, shoal, obstr = [], [], []
    shoal_keep = _shoal_keep(safety_depth_m)
    hazard_keep = _hazard_keep(safety_depth_m)
    for cell in cells:
        name = cell["name"]
        land += _load_polys(os.path.join(ed, f"{name}_LNDARE.json"))
        shoal += _load_polys(os.path.join(ed, f"{name}_DEPARE.json"), keep=shoal_keep)
        for lyr in ("OBSTRN", "UWTROC"):
            obstr += _load_polys(os.path.join(ed, f"{name}_{lyr}.json"), keep=hazard_keep)
    # clip to polygons whose extent actually touches the bbox (cells overlap their neighbours)
    n, s, w, e = bbox

    def _in(poly):
        xs = [p[0] for p in poly[0]]
        ys = [p[1] for p in poly[0]]
        return not (max(xs) < w or min(xs) > e or max(ys) < s or min(ys) > n)

    return {"land": [p for p in land if _in(p)],
            "shoal": [p for p in shoal if _in(p)],
            "obstruction": [p for p in obstr if _in(p)]}


def available(cache_dir: str = None) -> bool:
    """True if any ENC extract exists in the cache (i.e. a prep has run)."""
    ed = _extract_dir(cache_dir or CACHE)
    return os.path.isdir(ed) and any(f.endswith(".json") for f in os.listdir(ed))


if __name__ == "__main__":            # manual prep: python -m app.geo.enc <n> <s> <w> <e>
    import sys
    if len(sys.argv) == 5:
        bb = tuple(float(x) for x in sys.argv[1:5])
    else:
        bb = (46.0, 42.9, -84.7, -82.3)      # default: Bayview Mackinac course bbox
    print("cells:", [c["name"] for c in cells_for_bbox(bb)])
    man = ensure_bbox(bb, on_progress=print)
    L = layers_in_bbox(bb, safety_depth_m=2.63)   # SR33 7 ft draft + 0.5 m margin
    print(f"land={len(L['land'])} shoal={len(L['shoal'])} obstruction={len(L['obstruction'])} polys")
