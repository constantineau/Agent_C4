"""ObstacleField — the thing the isochrone router asks "can I sail from A to B?".

Race-agnostic. Three layers rasterize into ONE boolean land/blocked mask over the course bbox:
  1. global coastline (`coastline.py`)  — land ∧ ¬lake, auto-clipped to the bbox (any race);
  2. the race's `zones[]`               — exclusion / hazard / tss polygons (per race);
  3. the race's geocoded `island` marks — buffered to a disk (per race).

The mask is built once per optimize() (cheap; optionally cached) and queried O(1) in the hot loop:
  - `blocked(lat, lon)`          — is this point on an obstacle?
  - `crosses(lat1,lon1,lat2,lon2)` — does this leg step cut through one? (sampled along the segment)

Coordinates everywhere here are decimal degrees, WGS84. Ring/polygon vertices are (lon, lat) to
match GeoJSON; the public point/segment API takes (lat, lon) to match the rest of the app.
"""
from __future__ import annotations

import hashlib
import math
import os

from . import coastline
from . import enc

RES_DEG = float(os.environ.get("GEO_RES_DEG", "0.005"))      # mask cell size (~0.005° ≈ 550 m)
ISLAND_DEFAULT_NM = float(os.environ.get("GEO_ISLAND_NM", "1.5"))   # default island buffer radius
MARK_CARVE_NM = float(os.environ.get("GEO_MARK_CARVE_NM", "0.5"))   # navigable pocket around each mark
NM_PER_DEG_LAT = 60.0

# Coastline/obstacle data source: "natural_earth" (global backstop) or "enc" (NOAA S-57, draft-aware).
COASTLINE_SOURCE = os.environ.get("COASTLINE_SOURCE", "natural_earth").strip().lower()
# Active boat draft + under-keel safety margin → the ENC depth no-go contour (DEPARE shallower = block).
# Draft is canonical METERS (raw-SI convention); SR33 = 7 ft = 2.1336 m. BoatProfile ([B]) will set this.
BOAT_DRAFT_M = float(os.environ.get("BOAT_DRAFT_M", "2.1336"))
DRAFT_MARGIN_M = float(os.environ.get("GEO_DRAFT_MARGIN_M", "0.5"))


def safety_depth_m() -> float:
    """No-go depth: draft + under-keel margin. Water shallower than this is blocked (ENC source)."""
    return BOAT_DRAFT_M + DRAFT_MARGIN_M


def _nm_to_dlat(nm):
    return nm / NM_PER_DEG_LAT


def _nm_to_dlon(nm, lat):
    return nm / (NM_PER_DEG_LAT * max(0.15, math.cos(math.radians(lat))))


class ObstacleField:
    """A rasterized blocked/water mask over a bbox, plus provenance + drawable geometry."""

    def __init__(self, bbox, res_deg=RES_DEG):
        n, s, w, e = bbox
        self.bbox = (n, s, w, e)
        self.res = res_deg
        self.w, self.s = w, s
        self.nx = max(1, int(math.ceil((e - w) / res_deg)))
        self.ny = max(1, int(math.ceil((n - s) / res_deg)))
        self.mask = bytearray(self.nx * self.ny)          # 1 = blocked (land/zone/island/shoal/obstr)
        self.layers = {"coastline": 0, "zones": 0, "islands": 0, "shoal": 0, "obstruction": 0}
        self.geometry = {"land_rings": [], "zones": [], "islands": [],   # for the web overlay
                         "shoal_rings": [], "obstruction_rings": []}
        self.source = COASTLINE_SOURCE                     # which coastline/obstacle dataset built this
        self.safety_depth = None                           # ENC no-go depth used (draft + margin), m
        self.active = False                                # any obstacle present at all

    # --- cell <-> coord ------------------------------------------------------
    def _ix(self, lon):
        return int((lon - self.w) / self.res)

    def _iy(self, lat):
        return int((lat - self.s) / self.res)

    def blocked(self, lat, lon) -> bool:
        if not self.active:
            return False
        ix, iy = self._ix(lon), self._iy(lat)
        if ix < 0 or iy < 0 or ix >= self.nx or iy >= self.ny:
            return False                                   # outside the (padded) bbox = open water
        return self.mask[iy * self.nx + ix] == 1

    def crosses(self, lat1, lon1, lat2, lon2) -> bool:
        """True if the great-ish-circle segment passes over any obstacle (sampled < 1 cell apart)."""
        if not self.active:
            return False
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        span = math.hypot(dlat, dlon)
        steps = max(2, int(span / (self.res * 0.5)) + 1)
        for k in range(steps + 1):
            f = k / steps
            if self.blocked(lat1 + dlat * f, lon1 + dlon * f):
                return True
        return False

    # --- rasterization -------------------------------------------------------
    def _fill_polygon(self, rings, value: int, layer: str):
        """Scanline-fill a polygon (even-odd over ALL its rings → holes work) into the mask.

        rings = [ring], ring = [(lon, lat)]. value 1 sets (blocks), 0 clears (carves water)."""
        if not rings:
            return
        edges = []                       # (ylo, yhi, x_at_ylo, slope)
        for ring in rings:
            m = len(ring)
            for i in range(m):
                x1, y1 = ring[i]
                x2, y2 = ring[(i + 1) % m]
                if y1 == y2:
                    continue
                if y1 < y2:
                    edges.append((y1, y2, x1, (x2 - x1) / (y2 - y1)))
                else:
                    edges.append((y2, y1, x2, (x1 - x2) / (y1 - y2)))
        if not edges:
            return
        touched = 0
        for iy in range(self.ny):
            lat = self.s + (iy + 0.5) * self.res
            xs = [x0 + slope * (lat - ylo) for ylo, yhi, x0, slope in edges if ylo <= lat < yhi]
            if not xs:
                continue
            xs.sort()
            row = iy * self.nx
            for k in range(0, len(xs) - 1, 2):
                ixa = max(0, self._ix(xs[k]))
                ixb = min(self.nx - 1, self._ix(xs[k + 1]))
                for ix in range(ixa, ixb + 1):
                    if self.mask[row + ix] != value:
                        self.mask[row + ix] = value
                        touched += 1
        if value == 1:
            self.layers[layer] = self.layers.get(layer, 0) + touched
            self.active = True

    def _fill_disk(self, lat, lon, radius_nm, layer: str):
        rlat = _nm_to_dlat(radius_nm)
        iy0 = max(0, self._iy(lat - rlat))
        iy1 = min(self.ny - 1, self._iy(lat + rlat))
        touched = 0
        for iy in range(iy0, iy1 + 1):
            clat = self.s + (iy + 0.5) * self.res
            rlon = _nm_to_dlon(radius_nm, clat)
            half = rlon * math.sqrt(max(0.0, 1.0 - ((clat - lat) / rlat) ** 2)) if rlat else 0.0
            ixa = max(0, self._ix(lon - half))
            ixb = min(self.nx - 1, self._ix(lon + half))
            row = iy * self.nx
            for ix in range(ixa, ixb + 1):
                if self.mask[row + ix] != 1:
                    self.mask[row + ix] = 1
                    touched += 1
        self.layers[layer] = self.layers.get(layer, 0) + touched
        if touched:
            self.active = True

    def _carve_disk(self, lat, lon, radius_nm):
        """Clear (un-block) a small disk — a course waypoint must be reachable even in shoal/land.

        Race finishes and marks are deliberately set near shore/islands; ENC will flag that water as
        shallow/land. You can't route AROUND your own destination, so open a navigable pocket at each
        real waypoint while leaving every other obstacle intact."""
        rlat = _nm_to_dlat(radius_nm)
        iy0 = max(0, self._iy(lat - rlat))
        iy1 = min(self.ny - 1, self._iy(lat + rlat))
        for iy in range(iy0, iy1 + 1):
            clat = self.s + (iy + 0.5) * self.res
            rlon = _nm_to_dlon(radius_nm, clat)
            half = rlon * math.sqrt(max(0.0, 1.0 - ((clat - lat) / rlat) ** 2)) if rlat else 0.0
            ixa = max(0, self._ix(lon - half))
            ixb = min(self.nx - 1, self._ix(lon + half))
            row = iy * self.nx
            for ix in range(ixa, ixb + 1):
                self.mask[row + ix] = 0

    # --- summary -------------------------------------------------------------
    def summary(self) -> dict:
        return {
            "active": self.active,
            "bbox": self.bbox,
            "res_deg": self.res,
            "grid": [self.nx, self.ny],
            "cells_blocked": sum(self.layers.values()),
            "layers": self.layers,
            "source": self.source,
            "data_version": enc.DATA_VERSION if self.source == "enc" else coastline.DATA_VERSION,
            "safety_depth_m": (round(self.safety_depth, 2)
                               if self.source == "enc" and self.safety_depth is not None else None),
            "geometry": self.geometry,
        }


# --- zone geometry parsing ----------------------------------------------------
def _zone_rings(geometry):
    """Normalize a RaceDefinition zone geometry into [ring] of (lon, lat), or [] if unusable.

    Accepts GeoJSON-ish polygons ({"coordinates": [[[lon,lat],...]]} or [[lon,lat],...]),
    a bbox ({"north","south","west","east"}), or a circle ({"center":[lat,lon],"radius_nm"})."""
    if not geometry:
        return []
    g = geometry
    if "coordinates" in g:
        coords = g["coordinates"]
        # polygon: [ring, hole, ...]; or a bare ring
        if coords and isinstance(coords[0][0], (int, float)):
            coords = [coords]
        return [[(float(x), float(y)) for x, y in ring] for ring in coords if ring]
    if all(k in g for k in ("north", "south", "west", "east")):
        n, s, w, e = g["north"], g["south"], g["west"], g["east"]
        return [[(w, s), (e, s), (e, n), (w, n)]]
    return []


def _course_islands(definition, course_id):
    """Geocoded island marks of the course → [{name, lat, lon, radius_nm}] (per-race obstacles)."""
    courses = definition.get("courses", []) or []
    course = next((c for c in courses if c.get("id") == course_id), None) or (courses[0] if courses else None)
    out = []
    for m in (course or {}).get("marks", []):
        if m.get("type") == "island" and m.get("lat") is not None and m.get("lon") is not None:
            out.append({"name": m.get("name", "island"), "lat": m["lat"], "lon": m["lon"],
                        "radius_nm": float(m.get("radius_nm") or ISLAND_DEFAULT_NM)})
    return out


# --- build --------------------------------------------------------------------
_FIELD_CACHE: dict = {}                   # cache_key -> ObstacleField (reused across scenario fan-out)


def build_for_course(definition: dict, course_id, bbox, *, coastline_on=True, zones_on=True,
                     islands_on=True, res_deg=RES_DEG, cache_dir=None, use_cache=True,
                     source=None, safety_depth=None) -> ObstacleField:
    """Assemble the ObstacleField for a course over `bbox` = (north, south, west, east).

    Modular: the coastline is global (any race); the zones + island marks come from THIS race's
    RaceDefinition. Any layer can be toggled off. Returns an (inactive) field if nothing applies.
    Built fields are cached by `cache_key` so Lab-2's many same-course scenarios reuse one mask.

    `source` (None → the COASTLINE_SOURCE env default) picks Natural Earth vs NOAA ENC; `safety_depth`
    (None → the env draft+margin default) is the ENC depth no-go = active boat draft + under-keel
    margin. Both are folded into `cache_key` so a different boat/draft or source builds a fresh mask."""
    src = (source or COASTLINE_SOURCE).strip().lower()
    depth = safety_depth if safety_depth is not None else safety_depth_m()
    ck = None
    if use_cache and coastline_on and zones_on and islands_on:
        ck = cache_key(definition, course_id, bbox, res_deg, source=src, safety_depth=depth)
        if ck in _FIELD_CACHE:
            return _FIELD_CACHE[ck]
    field = ObstacleField(bbox, res_deg=res_deg)
    field.source = src

    # 1) coastline / depth obstacles. ENC (NOAA S-57) gives real land + draft-aware shoals, but it is
    #    US-ONLY — so the global Natural-Earth land is ALWAYS rasterized first as a UNION BACKSTOP
    #    (covers Canada + everywhere), and in ENC mode the draft-aware US shoals/rocks + sharper US land
    #    are layered ON TOP. (Before this was either/or: a cross-border bbox — e.g. Bayview Mackinac
    #    rounding Cove Island / Manitoulin in Ontario — had US land from ENC, so the NE backstop never
    #    fired and Canadian land was never blocked → routes cut straight through it.)
    if coastline_on:
        if field.source == "enc":
            try:
                enc.ensure_bbox(bbox, cache_dir)
                layers = enc.layers_in_bbox(bbox, cache_dir, depth)
            except Exception:
                layers = {}
            _fill_natural_earth(field, cache_dir)            # global land union backstop (incl. Canada)
            if layers.get("land") or layers.get("shoal"):
                field.safety_depth = depth
                for poly in layers.get("land", []):          # ENC's sharper US land on top
                    field._fill_polygon(poly, 1, "coastline")
                for poly in layers.get("shoal", []):         # DEPARE shallower than the safety depth
                    field._fill_polygon(poly, 1, "shoal")
                for poly in layers.get("obstruction", []):   # rocks / obstructions
                    field._fill_polygon(poly, 1, "obstruction")
                _overlay(field, "land_rings", layers.get("land", []))
                _overlay(field, "shoal_rings", layers.get("shoal", []))
                _overlay(field, "obstruction_rings", layers.get("obstruction", []))
            else:
                field.source = "natural_earth"               # ENC unavailable → NE backstop alone
        else:                                                # natural_earth (no ENC requested)
            _fill_natural_earth(field, cache_dir)

    # 2) the race's exclusion / hazard / tss zones (per race)
    if zones_on:
        for z in definition.get("zones", []) or []:
            if z.get("type") not in ("exclusion", "hazard", "tss"):
                continue
            rings = _zone_rings(z.get("geometry"))
            if not rings:
                continue
            field._fill_polygon(rings, 1, "zones")
            field.geometry["zones"].append(
                {"name": z.get("name", "zone"), "type": z.get("type"),
                 "ring": [[round(y, 5), round(x, 5)] for x, y in rings[0]]})

    # 3) the race's geocoded island marks, buffered to a disk (per race).
    #    Under ENC the real LNDARE polygons already cover them — the crude disks are exactly the
    #    inaccuracy ENC replaces — so only buffer disks on the Natural-Earth backstop.
    if islands_on and field.source != "enc":
        for isl in _course_islands(definition, course_id):
            field._fill_disk(isl["lat"], isl["lon"], isl["radius_nm"], "islands")
            field.geometry["islands"].append(isl)

    # 4) open a navigable pocket at every real course waypoint (start/gate/finish) so the route can
    #    always REACH its marks — finishes/marks are set near shore/islands and ENC blocks that water.
    if field.active:
        from shared import race_def
        marks, _skip, _cid = race_def.course_to_marks(definition, course_id)
        for _seq, _name, mlat, mlon in marks:
            if mlat is not None and mlon is not None:
                field._carve_disk(mlat, mlon, MARK_CARVE_NM)

    if ck is not None:
        _FIELD_CACHE[ck] = field
    return field


def _fill_natural_earth(field, cache_dir):
    """Rasterize the GLOBAL Natural-Earth coastline (land ∧ ¬lake, islands re-added inside lakes) into
    the mask. Used both as the primary source in NE mode and as an always-on UNION BACKSTOP under ENC
    (which is US-only) so Canadian/non-US land is never missed. Only ADDS land + carves lakes; safe to
    run before ENC overlays it. Returns the loaded layers."""
    try:
        coastline.ensure_global(cache_dir)
        layers = coastline.layers_in_bbox(field.bbox, cache_dir)
    except Exception:
        layers = {}
    for poly in layers.get("land", []):
        field._fill_polygon(poly, 1, "coastline")
    for poly in layers.get("lakes", []):
        field._fill_polygon(poly, 0, "coastline")        # carve lakes back to water
    for poly in layers.get("islands", []):
        field._fill_polygon(poly, 1, "coastline")        # re-add islands inside lakes
    _overlay(field, "land_rings", layers.get("land", []) + layers.get("islands", []))
    return layers


def _overlay(field, key, polys, cap=300):
    """Append downsampled outer rings to a web-overlay geometry list ([[lat,lon],...] per polygon)."""
    for poly in polys:
        ring = poly[0]
        step = max(1, len(ring) // cap)
        field.geometry[key].append([[round(y, 5), round(x, 5)] for x, y in ring[::step]])


def cache_key(definition, course_id, bbox, res_deg=RES_DEG, *, source=None, safety_depth=None) -> str:
    """Stable hash so an identical obstacle build can be cached/reused. Source- and draft-aware:
    switching coastline source or the boat draft (ENC depth no-go) yields a different mask."""
    z = [(z.get("name"), z.get("type"), z.get("geometry")) for z in definition.get("zones", []) or []]
    isl = _course_islands(definition, course_id)
    src = (source or COASTLINE_SOURCE).strip().lower()
    dv = enc.DATA_VERSION if src == "enc" else coastline.DATA_VERSION
    depth = safety_depth if safety_depth is not None else safety_depth_m()
    depth = round(depth, 3) if src == "enc" else None
    payload = repr((src, dv, depth, course_id, tuple(round(b, 4) for b in bbox),
                    round(res_deg, 5), z, isl))
    return hashlib.sha1(payload.encode()).hexdigest()[:16]
