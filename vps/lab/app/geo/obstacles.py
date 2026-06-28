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
# Island rounding-SIDE enforcement: only for an island that is a MARK OF THE RACE (its `rounding` is
# port/starboard) — a plain hazard island (rounding 'none') is still avoided either side. We rasterize a
# WRONG-SIDE BARRIER: a wall on the illegal side of the island, perpendicular to the leg's transit axis,
# so the only gap left is the legal side. `BAND` is the wall's half-thickness along the transit axis
# (added to the island radius) — wide enough the route can't sneak between the disk and the wall ends.
ROUNDSIDE_ISLANDS = os.environ.get("ROUTE_ROUNDSIDE_ISLANDS", "1").strip().lower() in ("1", "true", "yes", "on")
ROUNDSIDE_BAND_NM = float(os.environ.get("ROUTE_ROUNDSIDE_BAND_NM", "1.5"))   # wall half-thickness added to radius

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
        self.layers = {"coastline": 0, "zones": 0, "islands": 0, "shoal": 0, "obstruction": 0,
                       "rounding_barrier": 0}
        self.geometry = {"land_rings": [], "zones": [], "islands": [],   # for the web overlay
                         "shoal_rings": [], "obstruction_rings": [], "rounding_barriers": []}
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

    def _fill_wrong_side_barrier(self, lat, lon, radius_nm, transit_brg, side, band_nm):
        """Block the ILLEGAL side of an island that is a race mark, so the route can only pass on the
        legal hand. The wall is the half-plane on the wrong side of the island center, perpendicular to
        the leg's transit axis (`transit_brg`), within a band of |along-axis| <= radius+band_nm (so it's
        a thick wall the route can't slip past the ends of). The legal side stays open; the island disk /
        land polygon still blocks the centre. `side` = which hand to LEAVE the island on ('port' keeps
        the island to the boat's port → the boat passes on the island's starboard side → block the port
        half). Returns cells blocked."""
        if side not in ("port", "starboard"):
            return 0
        b = math.radians(transit_brg)
        de_u, dn_u = math.sin(b), math.cos(b)            # transit direction unit (east, north)
        le_u, ln_u = -math.cos(b), math.sin(b)           # PORT (left) of transit, unit (east, north)
        band_half = radius_nm + band_nm
        coslat = max(0.15, math.cos(math.radians(lat)))
        # bound the scan to a square enclosing radius+band+full perpendicular reach (the wall spans the
        # bbox perpendicular, so just scan the whole grid rows within the along-band's latitude reach).
        touched = 0
        for iy in range(self.ny):
            clat = self.s + (iy + 0.5) * self.res
            dn = (clat - lat) * NM_PER_DEG_LAT
            row = iy * self.nx
            for ix in range(self.nx):
                clon = self.w + (ix + 0.5) * self.res
                de = (clon - lon) * NM_PER_DEG_LAT * coslat
                along = de * de_u + dn * dn_u
                if abs(along) > band_half:
                    continue
                perp_port = de * le_u + dn * ln_u        # >0 = port side of the island vs transit
                wrong = perp_port > 0 if side == "port" else perp_port < 0
                if wrong and self.mask[row + ix] != 1:
                    self.mask[row + ix] = 1
                    touched += 1
        self.layers["rounding_barrier"] = self.layers.get("rounding_barrier", 0) + touched
        if touched:
            self.active = True
        return touched

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


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _island_rounding_marks(definition, course_id):
    """Island marks that are MARKS OF THE RACE (rounding port/starboard) → the wrong-side-barrier inputs.

    For each, the `transit_brg` is the bearing of the LEG the island sits on = from the nearest preceding
    nav point to the nearest following nav point in course order (islands skipped, gate/finish collapsed
    to midpoints), so the legal side is defined relative to the direction the boat actually passes it.
    Plain hazard islands (rounding 'none') are excluded — they stay side-agnostic obstacles."""
    courses = definition.get("courses", []) or []
    course = next((c for c in courses if c.get("id") == course_id), None) or (courses[0] if courses else None)
    if not course:
        return []
    # ordered course points: ('nav'|'island', lat, lon, side, radius)
    seq = []
    start = course.get("start") or {}
    if start.get("lat") is not None:
        seq.append(("nav", start["lat"], start["lon"], None, None))
    for m in course.get("marks", []):
        t = m.get("type")
        if t == "island" and m.get("lat") is not None:
            seq.append(("island", m["lat"], m["lon"], m.get("rounding", "none"),
                        float(m.get("radius_nm") or ISLAND_DEFAULT_NM)))
        elif t == "gate" and m.get("lat") is not None and m.get("lat2") is not None:
            seq.append(("nav", (m["lat"] + m["lat2"]) / 2.0, (m["lon"] + m["lon2"]) / 2.0, None, None))
        elif m.get("lat") is not None:
            seq.append(("nav", m["lat"], m["lon"], None, None))
    fpts = [p for p in ((course.get("finish") or {}).get("points") or []) if p and p.get("lat") is not None]
    if len(fpts) >= 2:
        seq.append(("nav", (fpts[0]["lat"] + fpts[1]["lat"]) / 2.0,
                    (fpts[0]["lon"] + fpts[1]["lon"]) / 2.0, None, None))
    elif len(fpts) == 1:
        seq.append(("nav", fpts[0]["lat"], fpts[0]["lon"], None, None))
    out = []
    for i, (kind, la, lo, side, rad) in enumerate(seq):
        if kind != "island" or side not in ("port", "starboard"):
            continue
        prev = next((p for p in reversed(seq[:i]) if p[0] == "nav"), None)
        nxt = next((p for p in seq[i + 1:] if p[0] == "nav"), None)
        a = prev or (kind, la, lo, None, None)          # fall back to the island itself if unbracketed
        b = nxt or (kind, la, lo, None, None)
        if a[1:3] == b[1:3]:
            continue                                    # can't define a transit axis → skip enforcement
        out.append({"lat": la, "lon": lo, "radius_nm": rad, "side": side,
                    "transit_brg": _bearing(a[1], a[2], b[1], b[2])})
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
    #    US-ONLY — so the global coastline is ALWAYS rasterized first as a UNION BACKSTOP (covers Canada
    #    + everywhere), and in ENC mode the draft-aware US shoals/rocks + sharper US land are layered ON
    #    TOP. (Before this was either/or: a cross-border bbox — e.g. Bayview Mackinac rounding Cove
    #    Island / Manitoulin in Ontario — had US land from ENC, so the backstop never fired and Canadian
    #    land was never blocked → routes cut straight through it.) The backstop defaults to GSHHG
    #    full-res (catches the sub-nm Great-Lakes islands NE omits, e.g. Cove Island), NE as fallback.
    if coastline_on:
        if field.source == "enc":
            try:
                enc.ensure_bbox(bbox, cache_dir)
                layers = enc.layers_in_bbox(bbox, cache_dir, depth)
            except Exception:
                layers = {}
            _fill_global_backstop(field, cache_dir)          # global land union backstop (incl. Canada)
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
                field.source = coastline.active_source()     # ENC unavailable → global backstop alone
        else:                                                # global coastline only (no ENC requested)
            _fill_global_backstop(field, cache_dir)
            field.source = coastline.active_source()         # honest provenance (gshhg | natural_earth)

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
    #    inaccuracy ENC (and now GSHHG full-res) replaces — so skip disks in ENC mode. On the GSHHG/NE
    #    backstop the disks stay as belt-and-braces for any island whose real polygon the dataset lacks.
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

    # 5) island ROUNDING-SIDE enforcement (after the carve, so it can't be re-opened): for an island
    #    that is a MARK OF THE RACE (rounding port/starboard) block the illegal side so the route passes
    #    on the legal hand. Source-independent (ENC or backstop) — it's a race rule, not an obstacle —
    #    and scoped to marked islands only (plain hazards stay avoided either side).
    if islands_on and ROUNDSIDE_ISLANDS:
        for r in _island_rounding_marks(definition, course_id):
            n = field._fill_wrong_side_barrier(r["lat"], r["lon"], r["radius_nm"], r["transit_brg"],
                                               r["side"], ROUNDSIDE_BAND_NM)
            if n:
                field.geometry["rounding_barriers"].append(
                    {"lat": round(r["lat"], 5), "lon": round(r["lon"], 5), "side": r["side"],
                     "transit_brg": round(r["transit_brg"], 1), "radius_nm": r["radius_nm"]})

    if ck is not None:
        _FIELD_CACHE[ck] = field
    return field


def _fill_global_backstop(field, cache_dir):
    """Rasterize the GLOBAL coastline (land ∧ ¬lake, islands re-added inside lakes) into the mask.

    The dataset is `coastline.active_source()` — GSHHG full-res by default (catches the sub-nm
    Great-Lakes islands Natural Earth omits, e.g. Cove Island), Natural Earth as the fallback; both
    expose the same land/lakes/islands roles so this code is source-agnostic. Used both as the primary
    source in non-ENC mode and as an always-on UNION BACKSTOP under ENC (which is US-only) so
    Canadian/non-US land is never missed. Only ADDS land + carves lakes; safe to run before ENC
    overlays it. Returns the loaded layers."""
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
