"""Depth sources for the route check (#292): which charts or depth grids cover a race's water.

Region-free. A race's box is checked against every source whose coverage touches it, in order:

  1. **chart sources** (navigational vector charts, `EncSource` = NOAA ENC, `enc.py`): real land,
     depth areas and rocks. They come first and win wherever they chart the water.
  2. **grid sources** (`GridSource`, a gridded bathymetry raster such as the NOAA Great Lakes
     depth grids, which cover both sides of the US/Canada border): they fill only the water no
     chart covers. Usually NOT for navigation: every leg whose depth came from one says so, by
     the source's own label, and keeps the paper-chart caution.
  3. **nothing**: the water stays "No depth data: check the paper chart." (#242). Never refused,
     never assumed deep.

The join is per mask cell, so two sets meet at a border with no gap and no double rule. Where
two grids cover the same cell the shallowest depth wins.

Grid sources come from a catalog: the built-in `GRIDS` (public-domain NOAA Great Lakes grids), or
a JSON list at `DEPTH_GRIDS` (a path; "none" turns grids off). A new region is a catalog entry:
{"name", "label", "navigational", "bbox": [n, s, w, e], "url", "member" (the GeoTIFF inside a
.tar.gz/.zip, or omitted for a bare .tif), "z": "elevation" (up is +) | "depth" (down is +),
"version"}. GDAL (`gdalwarp`) is a prep-time tool here as for ENC; the router only reads the
cached per-cell result.
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import os
import subprocess
import sys
import tarfile
import threading
import urllib.request
import zipfile

from .. import atomicio
from . import enc

CACHE = os.environ.get("ENC_CACHE", "/srv/enc")
GRID_DATA_VERSION = "grid_v1"           # bump when the grid reduction changes (cache + mask keys)
_HDRS = {"User-Agent": "Agent_C4-lab/1.0"}

# NOAA NCEI (ex-NGDC) Great Lakes bathymetry, 3 arc-second (~90 m), compiled 1999 with CHS: each
# grid covers its whole lake, both sides of the border. US Government work, public domain, "not to
# be used for navigation". Elevation in metres from the lake's chart datum (negative = depth); land
# is its height, so it reads as too shallow. Footprints are the GeoTIFFs' own extents.
_NGDC = "https://www.ngdc.noaa.gov/mgg/greatlakes/{lake}/data/geotiff/{lake}_lld.geotiff.tar.gz"
GRIDS = [
    {"name": f"noaa_gl_{lake}", "label": f"NOAA {title} depth grid",
     "navigational": False, "bbox": bbox, "url": _NGDC.format(lake=lake),
     "member": f"{lake}_lld/{lake}_lld.tif", "z": "elevation", "version": "1999"}
    for lake, title, bbox in (
        ("huron", "Lake Huron", [46.5004, 42.9996, -84.5004, -79.6796]),
        ("superior", "Lake Superior", [49.5004, 45.9996, -92.2004, -83.9996]),
        ("michigan", "Lake Michigan", [46.0904, 41.6196, -88.0004, -84.4996]),
        ("erie", "Lake Erie", [43.0004, 40.9996, -84.0004, -77.9996]),
        ("ontario", "Lake Ontario", [44.2504, 43.1496, -79.9004, -76.0496]),
    )]


def _touches(bbox, other) -> bool:
    n, s, w, e = bbox
    on, os_, ow, oe = other
    return not (oe < w or ow > e or on < s or os_ > n)


class GridError(RuntimeError):
    """A grid source covers the box but couldn't be loaded."""


class EncSource:
    """NOAA ENC (S-57 vector charts) through `enc.py`: the chart source."""
    name = "noaa_enc"
    label = "NOAA charts"
    navigational = True
    kind = "chart"

    @property
    def data_version(self):
        return enc.DATA_VERSION

    def covers(self, bbox) -> bool:
        return True              # US cells are found from NOAA's catalog; none = nothing charted

    def layers(self, bbox, cache_dir, safety_depth):
        """{land, shoal, obstruction, depth_cover, missing} polygons (see `enc.layers_in_bbox`)."""
        enc.ensure_bbox(bbox, cache_dir)
        return enc.layers_in_bbox(bbox, cache_dir, safety_depth)


class GridSource:
    """A gridded bathymetry raster (GeoTIFF), reduced at prep to the shallowest depth per mask cell."""
    kind = "grid"

    def __init__(self, spec: dict):
        self.spec = spec
        self.name = str(spec["name"])
        self.label = str(spec.get("label") or self.name)
        self.navigational = bool(spec.get("navigational", False))
        self.bbox = tuple(float(v) for v in spec["bbox"])
        self.file_id = hashlib.sha1(repr((spec.get("url"), spec.get("member"),
                                          spec.get("version"))).encode()).hexdigest()[:10]
        # everything that changes what the grid says or how a leg is labelled: in the cell and
        # mask cache keys, so a corrected catalog entry never reuses an old result (Codex on #300)
        meaning = hashlib.sha1(repr((self.file_id, (spec.get("z") or "elevation").lower(),
                                     self.navigational, self.label)).encode()).hexdigest()[:10]
        self.data_version = f"{GRID_DATA_VERSION}:{self.name}:{spec.get('version', '')}:{meaning}"

    def covers(self, bbox) -> bool:
        return _touches(bbox, self.bbox)

    # --- prep: download once -------------------------------------------------
    def _dir(self, cache_dir):
        return os.path.join(cache_dir or CACHE, "grids", self.name)

    def raster(self, cache_dir) -> str:
        """Path to the source GeoTIFF on disk, downloading it the first time. The file is named
        by the source's url, member and version: a new version of the grid is a new download,
        never the old file under a new key (Codex on #300)."""
        d = self._dir(cache_dir)
        out = os.path.join(d, f"{self.name}-{self.file_id}.tif")
        if os.path.exists(out):
            return out
        member = self.spec.get("member")
        # each caller its own archive: two first builds at once never share (or delete) one
        blob = atomicio.tmp_name(os.path.join(d, f"{self.name}-{self.file_id}.download"))
        try:
            req = urllib.request.Request(self.spec["url"], headers=_HDRS)
            with urllib.request.urlopen(req, timeout=300) as r, \
                    atomicio.atomic_open(out if not member else blob, "wb") as f:
                _copy(r, f)
            if member:
                with atomicio.atomic_open(out, "wb") as f:
                    _unpack_member(blob, member, f)
        except Exception as exc:  # noqa: BLE001 — reported by the caller
            raise GridError(f"{self.name}: {type(exc).__name__}: {exc}"[:200]) from None
        finally:
            if member:
                try:
                    os.remove(blob)
                except OSError:
                    pass
        return out

    def _valid_mask(self, src) -> str:
        """The raster's validity mask as a Byte GeoTIFF (255 = data, 0 = none), made once. Whatever
        the nodata value (a sentinel of either sign, 0, NaN), GDAL's mask band says which pixels
        have data (Codex on #300)."""
        out = src[:-4] + "-valid.tif"
        if os.path.exists(out):
            return out
        tmp = atomicio.tmp_name(out)
        try:
            r = subprocess.run(["gdal_translate", "-q", "-of", "GTiff", "-b", "mask", "-ot", "Byte",
                                "-co", "COMPRESS=DEFLATE", src, tmp],
                               capture_output=True, timeout=600)
            if r.returncode != 0 or not os.path.exists(tmp):
                raise GridError("gdal_translate: " + r.stderr.decode(errors="replace")[:150])
        except (OSError, subprocess.TimeoutExpired, GridError) as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            if isinstance(exc, GridError):
                raise
            raise GridError(f"gdal_translate: {type(exc).__name__}") from None
        atomicio.commit(tmp, out)
        return out

    # --- prep: reduce to the mask grid ---------------------------------------
    def cells(self, bbox, res, nx, ny, cache_dir) -> list:
        """Shallowest depth (m, down is +) in each mask cell, row-major from the SOUTH-west cell
        (the ObstacleField's order), or None where the grid has no data for the whole cell.

        Cached per box and cell size: a different boat re-thresholds this, it never re-warps."""
        n, s, w, e = bbox
        key = f"{self.data_version}|{s:.6f},{w:.6f}|{res:.8f}|{nx}x{ny}"
        cpath = os.path.join(self._dir(cache_dir),
                             "cells-" + hashlib.sha1(key.encode()).hexdigest()[:16] + ".json")
        try:
            with open(cpath) as f:
                got = json.load(f)
            if got.get("key") == key and len(got.get("d", [])) == nx * ny:
                return got["d"]
        except (OSError, ValueError, AttributeError):
            pass
        src = self.raster(cache_dir)
        te = [str(w), str(s), str(w + nx * res), str(s + ny * res)]
        z = (self.spec.get("z") or "elevation").lower()
        nodata, ext = self._info(src)
        # the shallowest valid value per cell: max elevation (up is +), or min depth (down is +)
        wd = self._dir(cache_dir)
        shallow = self._warp(wd, src, te, nx, ny, "max" if z == "elevation" else "min", nodata)
        # a cell is covered only if EVERY source pixel in it has data: the validity mask's minimum
        # over the cell is 255; outside the raster the cell is 0
        cover = self._warp(wd, self._valid_mask(src), te, nx, ny, "min", None, dst_nodata=0.0)
        # GDAL reduces only the pixels a cell has: a cell the raster's edge cuts through would read
        # as covered from its inside half. Only cells wholly inside the raster count (Codex on #300)
        eps = 1e-9                                   # float noise on an exactly shared edge
        rw, rs, re_, rn = ext[0] - eps, ext[1] - eps, ext[2] + eps, ext[3] + eps
        inside_col = [rw <= w + c * res and w + (c + 1) * res <= re_ for c in range(nx)]
        inside_row = [rs <= s + (ny - 1 - r) * res and s + (ny - r) * res <= rn for r in range(ny)]
        out = []
        for i in range(nx * ny):
            v = shallow[i]
            if v is None or v != v or cover[i] != 255.0 or not (inside_row[i // nx]
                                                                and inside_col[i % nx]):
                out.append(None)
                continue
            # to the cm, rounded DOWN (shallower): rounding never opens a cell (Codex on #300)
            out.append(math.floor((-v if z == "elevation" else v) * 100) / 100)
        # rows came north-first from GDAL; the field counts rows from the south
        rows = [out[r * nx:(r + 1) * nx] for r in range(ny)]
        flat = [v for row in reversed(rows) for v in row]
        os.makedirs(os.path.dirname(cpath), exist_ok=True)
        atomicio.write_json(cpath, {"key": key, "d": flat})
        return flat

    @staticmethod
    def _info(src):
        """(nodata value or None, (west, south, east, north) of the raster in WGS84)."""
        try:
            r = subprocess.run(["gdalinfo", "-json", src], capture_output=True, timeout=120)
            if r.returncode != 0:
                raise GridError("gdalinfo: " + r.stderr.decode(errors="replace")[:150])
            info = json.loads(r.stdout)
            nd = (info.get("bands") or [{}])[0].get("noDataValue")
            ring = info["wgs84Extent"]["coordinates"][0]
            xs, ys = [float(p[0]) for p in ring], [float(p[1]) for p in ring]
            return (float(nd) if nd is not None else None), (min(xs), min(ys), max(xs), max(ys))
        except (OSError, ValueError, subprocess.TimeoutExpired, TypeError, KeyError,
                IndexError) as exc:
            raise GridError(f"gdalinfo: {type(exc).__name__}") from None

    @staticmethod
    def _warp(wd, src, te, nx, ny, how, src_nodata, dst_nodata=-3.0e38):
        """gdalwarp the raster onto the mask grid (EPSG:4326, `how` = max|min over the source
        pixels in each cell) → [float|None] north-first. Raises GridError on any failure."""
        out = os.path.join(wd, f"warp-{os.getpid()}-{threading.get_ident()}-{how}.bil")
        cmd = ["gdalwarp", "-q", "-overwrite", "-of", "EHdr", "-ot", "Float32",
               "-t_srs", "EPSG:4326", "-te", *te, "-ts", str(nx), str(ny), "-r", how,
               "-srcnodata", "None" if src_nodata is None else repr(src_nodata),
               "-dstnodata", repr(float(dst_nodata)), "-wo", "INIT_DEST=NO_DATA", src, out]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=600)
            if r.returncode != 0:
                raise GridError(f"gdalwarp: {r.stderr.decode(errors='replace')[:150]}")
            order = "I"
            with open(out[:-4] + ".hdr") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 2 and parts[0].upper() == "BYTEORDER":
                        order = parts[1].upper()
            vals = array.array("f")
            with open(out, "rb") as f:
                vals.frombytes(f.read())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GridError(f"gdalwarp: {type(exc).__name__}") from None
        finally:
            for ext in (".bil", ".hdr", ".prj", ".bil.aux.xml", ".stx"):
                try:
                    os.remove(out[:-4] + ext)
                except OSError:
                    pass
        if (order == "M") != (sys.byteorder == "big"):
            vals.byteswap()
        if len(vals) != nx * ny:
            raise GridError("gdalwarp: wrong size")
        dn = array.array("f", [dst_nodata])[0]          # the sentinel as float32 rounds it
        return [None if v == dn else float(v) for v in vals]


def _copy(src, dst):
    while True:
        chunk = src.read(1 << 20)
        if not chunk:
            break
        dst.write(chunk)


def _unpack_member(archive, member, dst):
    """Copy one named file out of a .tar.gz or .zip into the open file `dst`. Only that member is
    read, so a path in the archive never lands anywhere on disk."""
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z, z.open(member) as src:
            _copy(src, dst)
        return
    with tarfile.open(archive) as t:
        m = t.getmember(member)
        if not m.isfile():
            raise GridError(f"{member} is not a file")
        with t.extractfile(m) as src:
            _copy(src, dst)


def grid_catalog() -> list:
    """The grid sources' specs: `DEPTH_GRIDS` (a JSON file, or "none"), else the built-in list."""
    path = os.environ.get("DEPTH_GRIDS", "").strip()
    if not path:
        return list(GRIDS)
    if path.lower() in ("none", "off", "0"):
        return []
    with open(path) as f:
        got = json.load(f)
    return [g for g in got if isinstance(g, dict)]


class _BadCatalog:
    """Stands in for every grid when the catalog won't read: covers everything, never loads, so
    the water stays "no depth data" and the reason is reported (never a silent "no grids")."""
    kind = "grid"
    navigational = False
    name = "depth_grids"
    label = "depth grids"

    def __init__(self, why):
        self.why = why
        self.data_version = "grid-catalog-error"

    def covers(self, bbox):
        return True

    def cells(self, *_a, **_k):
        raise GridError(f"catalog: {self.why}")


def sources_for(bbox) -> list:
    """The depth sources whose coverage touches bbox=(n,s,w,e): the chart source first, then the
    grids, in catalog order."""
    out = [EncSource()]
    try:
        grids = [GridSource(spec) for spec in grid_catalog()]
    except Exception as exc:  # noqa: BLE001 — a broken catalog is reported, not fatal
        return out + [_BadCatalog(f"{type(exc).__name__}: {exc}"[:150])]
    return out + [g for g in grids if g.covers(bbox)]


def data_version(bbox) -> str:
    """What the depth sources for this box are: part of the obstacle cache key."""
    return "+".join(s.data_version for s in sources_for(bbox))
