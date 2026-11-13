"""ObstacleField — the thing the isochrone router asks "can I sail from A to B?".

Race-agnostic. Three layers rasterize into ONE boolean land/blocked mask over the course bbox:
  1. global coastline (`coastline.py`)  — land ∧ ¬lake, auto-clipped to the bbox (any race);
  2. the race's `zones[]`               — exclusion / hazard / tss polygons (per race);
  3. the race's geocoded `island` marks — buffered to a disk (per race).

The optimizer routes each leg on its own mask (`build_leg_fields`, #141): these layers on every
leg, plus only the rounding barriers of the islands that leg rounds. Masks are cached and
queried O(1) in the hot loop:
  - `blocked(lat, lon)`          — is this point on an obstacle?
  - `crosses(lat1,lon1,lat2,lon2)` — does this leg step touch one? (exact grid walk, #145)

Coordinates everywhere here are decimal degrees, WGS84. Ring/polygon vertices are (lon, lat) to
match GeoJSON; the public point/segment API takes (lat, lon) to match the rest of the app.
"""
from __future__ import annotations

import collections
import hashlib
import math
import os
import threading

from shared import gridwalk

from . import coastline
from . import depthsrc
from . import enc

RES_DEG = float(os.environ.get("GEO_RES_DEG", "0.005"))      # mask cell size (~0.005° ≈ 550 m)
ISLAND_DEFAULT_NM = float(os.environ.get("GEO_ISLAND_NM", "1.5"))   # default island buffer radius
MARK_CARVE_NM = float(os.environ.get("GEO_MARK_CARVE_NM", "0.5"))   # pocket around an overridden mark
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
        # the shoreline: "off" (not asked for), "ok" (loaded; an empty box is real open water) or
        # "failed" (couldn't load: the mask has NO land in it, and must never pass as checked, #51)
        self.land = "off"
        self.land_error = None
        # the depth no-go (#242): "ok" (ENC loaded: depths checked wherever it charts them, see
        # `nodepth`), "failed" (ENC asked for, didn't load) or "none" (no depth chart asked for:
        # GSHHG / Natural Earth)
        self.depth = "none"
        self.depth_error = None
        # with depth "ok": 1 = a cell no ENC depth area covers (Canadian water: NOAA ENC is
        # US-only). A route through one isn't refused; the verdict marks it (#242)
        self.nodepth = None
        # depths a non-navigational grid gave (#292): [(label, mask)], 1 = that grid gave this
        # cell's depth. A route through one says so, by the grid's label
        self.nonnav = []
        # grid sources that cover the box but didn't load: their water stays "no depth data",
        # and the field is never cached (the next build tries again)
        self.depth_gaps = []

    # --- cell <-> coord ------------------------------------------------------
    def _ix(self, lon):
        return int((lon - self.w) / self.res)

    def _iy(self, lat):
        return int((lat - self.s) / self.res)

    def inside(self, lat, lon) -> bool:
        """Is (lat, lon) inside the box this field was built over?"""
        n, s, w, e = self.bbox
        return s <= lat <= n and w <= lon <= e

    def blocked(self, lat, lon) -> bool:
        if not self.active:
            return False
        if not self.inside(lat, lon):
            return True                # outside the box nothing was checked: never open water (#258)
        ix, iy = min(self._ix(lon), self.nx - 1), min(self._iy(lat), self.ny - 1)
        return self.mask[iy * self.nx + ix] == 1

    def crosses(self, lat1, lon1, lat2, lon2) -> bool:
        """True if the straight segment touches any blocked cell. Exact (shared/gridwalk.py, a
        supercover): a step that clips a cell's corner is caught, which half-cell sampling missed
        (#145). A step with an end outside the box counts as blocked, as in `blocked`: land was
        never checked out there (#258). The box is convex, so two ends inside keep the step in."""
        if not self.active:
            return False
        if not (self.inside(lat1, lon1) and self.inside(lat2, lon2)):
            return True
        return gridwalk.any_set(self.mask, self.nx, self.ny,
                                (lon1 - self.w) / self.res, (lat1 - self.s) / self.res,
                                (lon2 - self.w) / self.res, (lat2 - self.s) / self.res)

    def no_depth(self, lat1, lon1, lat2, lon2) -> bool:
        """Does this step touch water whose depth no chart gave (#242)? Every cell it touches,
        as in `crosses`; a step leaving the box counts. Without ENC depths at all, every step."""
        if self.depth != "ok" or self.nodepth is None:
            return True
        if not (self.inside(lat1, lon1) and self.inside(lat2, lon2)):
            return True
        return gridwalk.any_set(self.nodepth, self.nx, self.ny,
                                (lon1 - self.w) / self.res, (lat1 - self.s) / self.res,
                                (lon2 - self.w) / self.res, (lat2 - self.s) / self.res)

    def nonnav_labels(self, lat1, lon1, lat2, lon2) -> list:
        """The labels of the non-navigational grids that gave a depth this step touches (#292)."""
        if self.depth != "ok" or not self.nonnav:
            return []
        args = (self.nx, self.ny, (lon1 - self.w) / self.res, (lat1 - self.s) / self.res,
                (lon2 - self.w) / self.res, (lat2 - self.s) / self.res)
        return [label for label, m in self.nonnav if gridwalk.any_set(m, *args)]

    # --- rasterization -------------------------------------------------------
    def _fill_polygon(self, rings, value: int, layer: str, mask=None):
        """Scanline-fill a polygon (even-odd over ALL its rings → holes work) into the mask.

        rings = [ring], ring = [(lon, lat)]. value 1 sets (blocks), 0 clears (carves water).
        `mask` fills another grid of the same shape instead (no layer counts, no `active`)."""
        own = mask is None
        mask = self.mask if own else mask
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
        # only the rows the polygon spans: a row outside it has no crossings (a box full of small
        # ENC depth areas would otherwise scan every row once per area)
        ymin = min(e[0] for e in edges)
        ymax = max(e[1] for e in edges)
        iy0 = max(0, int(math.floor((ymin - self.s) / self.res - 0.5)))
        iy1 = min(self.ny - 1, int(math.ceil((ymax - self.s) / self.res - 0.5)))
        for iy in range(iy0, iy1 + 1):
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
                    if mask[row + ix] != value:
                        mask[row + ix] = value
                        touched += 1
        if value == 1 and own:
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
        real waypoint. This clears EVERY cell in the disk, so call it on a mask that holds only the
        shoreline layers, before zones, island disks or barriers are drawn (#148)."""
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
            "land": self.land,
            "depth": self.depth,
            "bbox": self.bbox,
            "res_deg": self.res,
            "grid": [self.nx, self.ny],
            "cells_blocked": sum(self.layers.values()),
            "layers": self.layers,
            "source": self.source,
            "data_version": enc.DATA_VERSION if self.source == "enc" else coastline.DATA_VERSION,
            "depth_grids": [label for label, _m in self.nonnav],
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
    pinned = {}       # seq index → a transit bearing carried on the island mark (see below)
    mark_of = {}      # seq index → the island's index in course["marks"] (one per occurrence)
    start = course.get("start") or {}
    if start.get("lat") is not None:
        seq.append(("nav", start["lat"], start["lon"], None, None))
    for mi, m in enumerate(course.get("marks", [])):
        t = m.get("type")
        if t == "island" and m.get("lat") is not None:
            mark_of[len(seq)] = mi
            if m.get("transit_brg") is not None:
                # the boat's onboard re-route trims sailed legs and starts the course at the boat;
                # it pins the deployed leg's bearing here so the legal side doesn't rotate
                pinned[len(seq)] = float(m["transit_brg"])
            seq.append(("island", m["lat"], m["lon"], m.get("rounding", "none"),
                        float(m.get("radius_nm") or ISLAND_DEFAULT_NM)))
        elif t == "gate" and m.get("lat") is not None and m.get("lat2") is not None:
            seq.append(("nav", (m["lat"] + m["lat2"]) / 2.0, (m["lon"] + m["lon2"]) / 2.0, None, None))
        elif m.get("lat") is not None:
            seq.append(("nav", m["lat"], m["lon"], None, None))
    fpts = [p for p in ((course.get("finish") or {}).get("points") or []) if isinstance(p, dict) and p.get("lat") is not None and p.get("lon") is not None]
    if len(fpts) >= 2:
        seq.append(("nav", (fpts[0]["lat"] + fpts[1]["lat"]) / 2.0,
                    (fpts[0]["lon"] + fpts[1]["lon"]) / 2.0, None, None))
    elif len(fpts) == 1:
        seq.append(("nav", fpts[0]["lat"], fpts[0]["lon"], None, None))
    out = []
    for i, (kind, la, lo, side, rad) in enumerate(seq):
        if kind != "island" or side not in ("port", "starboard"):
            continue
        if i in pinned:
            out.append({"lat": la, "lon": lo, "radius_nm": rad, "side": side,
                        "transit_brg": pinned[i], "mark_idx": mark_of.get(i)})
            continue
        prev = next((p for p in reversed(seq[:i]) if p[0] == "nav"), None)
        nxt = next((p for p in seq[i + 1:] if p[0] == "nav"), None)
        a = prev or (kind, la, lo, None, None)          # fall back to the island itself if unbracketed
        b = nxt or (kind, la, lo, None, None)
        if a[1:3] == b[1:3]:
            continue                                    # can't define a transit axis → skip enforcement
        out.append({"lat": la, "lon": lo, "radius_nm": rad, "side": side,
                    "transit_brg": _bearing(a[1], a[2], b[1], b[2]), "mark_idx": mark_of.get(i)})
    return out


# --- build --------------------------------------------------------------------
# key -> ObstacleField, reused across Lab-2's scenario fan-out (every scenario routes the same
# course). Least-recently-used entries go past FIELD_CACHE_MAX: a Bayview-sized mask is ~0.6 MB,
# and a course adds one entry for its land/zones/islands plus one per leg that rounds an island.
_FIELD_CACHE: "collections.OrderedDict" = collections.OrderedDict()
FIELD_CACHE_MAX = int(os.environ.get("GEO_FIELD_CACHE_MAX", "24"))
_CACHE_LOCK = threading.Lock()            # Lab jobs run in threads; get+touch / put+evict as one


def _cache_get(key):
    with _CACHE_LOCK:
        f = _FIELD_CACHE.get(key)
        if f is not None:
            _FIELD_CACHE.move_to_end(key)
        return f


def _cache_put(key, field):
    with _CACHE_LOCK:
        _FIELD_CACHE[key] = field
        _FIELD_CACHE.move_to_end(key)
        while len(_FIELD_CACHE) > max(1, FIELD_CACHE_MAX):
            _FIELD_CACHE.popitem(last=False)


def _barrier_sig(rounds):
    """What a set of rounding barriers draws: position, size, side and the leg's bearing."""
    return tuple((round(r["lat"], 6), round(r["lon"], 6), round(r["radius_nm"], 4), r["side"],
                  round(r["transit_brg"], 6)) for r in rounds)


def build_for_course(definition: dict, course_id, bbox, *, coastline_on=True, zones_on=True,
                     islands_on=True, res_deg=RES_DEG, cache_dir=None, use_cache=True,
                     source=None, safety_depth=None, carve_waypoints=True,
                     barriers=True) -> ObstacleField:
    """Assemble the ObstacleField for a course over `bbox` = (north, south, west, east).

    Modular: the coastline is global (any race); the zones + island marks come from THIS race's
    RaceDefinition. Any layer can be toggled off. Returns an (inactive) field if nothing applies.
    Built fields are cached by `cache_key` so Lab-2's many same-course scenarios reuse one mask.

    `source` (None → the COASTLINE_SOURCE env default) picks Natural Earth vs NOAA ENC; `safety_depth`
    (None → the env draft+margin default) is the ENC depth no-go = active boat draft + under-keel
    margin. Both are folded into `cache_key` so a different boat/draft or source builds a fresh mask.

    `barriers=False` leaves out every island's rounding-side barrier: the shared base that
    `build_leg_fields` adds each leg's own barriers to. The default draws ALL of them at once (one
    course-wide mask); the optimizer no longer routes on that (#141)."""
    src = (source or COASTLINE_SOURCE).strip().lower()
    depth = safety_depth if safety_depth is not None else safety_depth_m()
    ck = None
    # only the Lab's default (pockets carved) build is cached; a no-pocket build is never served from it
    if use_cache and coastline_on and zones_on and islands_on and carve_waypoints:
        ck = cache_key(definition, course_id, bbox, res_deg, source=src, safety_depth=depth)
        # the key has the islands but not their rounding sides: a barrier build is keyed by them
        ck += ("|nb" if not (barriers and ROUNDSIDE_ISLANDS) else
               "|b" + hashlib.sha1(repr(_barrier_sig(
                   _island_rounding_marks(definition, course_id))).encode()).hexdigest()[:12])
        hit = _cache_get(ck)
        if hit is not None:
            return hit
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
        field.land = "ok"                                    # _fill_global_backstop says if not
        if field.source == "enc":
            # the depth sources whose coverage touches this race's box (#292): the chart (NOAA ENC)
            # first, then any depth grid for the water it doesn't chart
            chart, *grids = depthsrc.sources_for(bbox)
            grid_used = False
            try:
                layers = chart.layers(bbox, cache_dir, depth)
                if layers.get("missing"):
                    raise OSError("ENC cells not downloaded: " + ", ".join(layers["missing"][:5]))
            except Exception as exc:  # noqa: BLE001 — reported: depths were not checked (#242)
                layers = {}
                field.depth = "failed"
                field.depth_error = f"{type(exc).__name__}: {exc}"[:200]
                print(f"[obstacles] ENC did not load: {field.depth_error}", flush=True)
            _fill_global_backstop(field, cache_dir)          # global land union backstop (incl. Canada)
            if field.depth != "failed":
                # depths are checked wherever an ENC depth area (DEPARE / DRGARE, deep or shallow)
                # covers the water. Everywhere else (Canada: NOAA ENC is US-only) a route isn't
                # refused but marked "no depth data" (Cole's decision on #242). ENC land counts as
                # covered: it is charted, and a route never crosses it (bar an override pocket).
                field.depth = "ok"
                field.nodepth = bytearray(b"\x01") * (field.nx * field.ny)
                for poly in layers.get("depth_cover", []) + layers.get("land", []):
                    field._fill_polygon(poly, 0, "depth", mask=field.nodepth)
                grid_used = _apply_grids(field, grids, depth, cache_dir,
                                         layers.get("depth_cover", []) + layers.get("land", []))
                if grid_used:
                    field.safety_depth = depth
            if layers.get("land") or layers.get("shoal") or layers.get("depth_cover"):
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
            elif not grid_used:
                # nothing from ENC or a grid → the global backstop alone. With grid depths the
                # source stays "enc" (charts + depth sources), so the Deploy chart for the boat
                # is rebuilt the same way, grid shoals included (Codex on #300)
                field.source = coastline.active_source()
        else:                                                # global coastline only (no ENC requested)
            _fill_global_backstop(field, cache_dir)
            field.source = coastline.active_source()         # honest provenance (gshhg | natural_earth)

    # 1b) land is never opened for a route (#242): a mark the chart puts on land or in shallow
    #    water is unreachable, and the route is refused. The one exception is a mark with a
    #    reviewed chart override (the course's `chart_overrides`: who, when, why), for a chart
    #    artefact: it gets a pocket of open water. The pocket is cut in the shoreline layers ONLY
    #    (land, shoal, obstruction), BEFORE the race's zones, island disks and barriers go on, so
    #    none of those is ever cleared (#148). Before, every waypoint got a pocket, which opened
    #    land at the Mackinac finish (#52). `carve_waypoints=False` skips this step: the Orin's
    #    re-route builds its race layer that way and opens its pockets in its own land mask (#124).
    if field.active and carve_waypoints:
        for _name, mlat, mlon in override_marks(definition, course_id):
            field._carve_disk(mlat, mlon, MARK_CARVE_NM)

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

    # 4) island ROUNDING-SIDE enforcement (after the carve, so it can't be re-opened): for an island
    #    that is a MARK OF THE RACE (rounding port/starboard) block the illegal side so the route passes
    #    on the legal hand. Source-independent (ENC or backstop) — it's a race rule, not an obstacle —
    #    and scoped to marked islands only (plain hazards stay avoided either side).
    if islands_on and ROUNDSIDE_ISLANDS and barriers:
        _add_barriers(field, _island_rounding_marks(definition, course_id))

    # a failed load (shoreline or ENC depths) is retried next run, never served from the cache
    if ck is not None and field.land != "failed" and field.depth != "failed" and not field.depth_gaps:
        _cache_put(ck, field)
    return field


def chart_overrides(definition, course_id):
    """The course's reviewed chart overrides: {nav mark name: {"by", "at", "why"}} (#242)."""
    course = _course(definition, course_id) or {}
    ov = course.get("chart_overrides")
    return {str(k): v for k, v in ov.items() if isinstance(v, dict)} if isinstance(ov, dict) else {}


def override_marks(definition, course_id):
    """[(name, lat, lon)] for every nav mark (start, marks, gate/finish midpoints) that has a chart
    override: the only marks that get a pocket of open water."""
    ov = chart_overrides(definition, course_id)
    if not ov:
        return []
    from shared import race_def
    marks, _skip, _cid = race_def.course_to_marks(definition, course_id)
    return [(n, la, lo) for _s, n, la, lo in marks
            if n in ov and la is not None and lo is not None]


def _add_barriers(field, rounds):
    """Draw each island's wrong-side barrier into `field` (in place)."""
    for r in rounds:
        n = field._fill_wrong_side_barrier(r["lat"], r["lon"], r["radius_nm"], r["transit_brg"],
                                           r["side"], ROUNDSIDE_BAND_NM)
        if n:
            field.geometry["rounding_barriers"].append(
                {"lat": round(r["lat"], 5), "lon": round(r["lon"], 5), "side": r["side"],
                 "transit_brg": round(r["transit_brg"], 1), "radius_nm": r["radius_nm"]})


# --- one mask per leg (#141) -----------------------------------------------------------------------
def nav_index_map(course):
    """For each course mark, the navigator index it gets (course_to_marks order), or None for an
    island / a mark with no position. Start is index 0 when it has a position. Returns
    (indices, k) with k = the finish's index (when the course has one)."""
    k = 1 if (course.get("start") or {}).get("lat") is not None else 0
    out = []
    for m in course.get("marks", []) or []:
        if m.get("type") == "island" or m.get("lat") is None:
            out.append(None)
        else:
            out.append(k)
            k += 1
    return out, k


def _course(definition, course_id):
    courses = (definition or {}).get("courses") or []
    return next((c for c in courses if c.get("id") == course_id), None) or \
        (courses[0] if courses else None)


def island_legs(definition, course_id):
    """{index in course["marks"]: leg} for every island. Leg k runs navigator point k → k + 1
    (0 = start → first mark); an island sits on the leg of the nav point before it. An island
    before the first nav point (no positioned start) is on no leg: -1."""
    course = _course(definition, course_id)
    if not course:
        return {}
    idx, _fin = nav_index_map(course)
    cur = 0 if (course.get("start") or {}).get("lat") is not None else -1
    out = {}
    for mi, (m, k) in enumerate(zip(course.get("marks") or [], idx)):
        if k is not None:
            cur = k
        elif m.get("type") == "island":
            out[mi] = cur
    return out


def only_leg_barriers(definition, course_id, leg):
    """`definition` with rounding sides kept only on the islands of leg `leg`; every other island
    keeps its disk but no side. A course that rounds the same island twice, once each way, then
    never has both barriers at once (together they'd wall off the island's whole meridian)."""
    import copy
    d = copy.deepcopy(definition)
    course = _course(d, course_id)
    if not course:
        return d
    marks = course.get("marks") or []
    for mi, lg in island_legs(d, course_id).items():
        if lg != leg:
            marks[mi]["rounding"] = "none"
    return d


def build_leg_fields(definition, course_id, bbox, *, source=None, safety_depth=None,
                     res_deg=RES_DEG, cache_dir=None):
    """One ObstacleField per leg of the course → (base, [field per leg]).

    `base` is land, zones and island disks, plus any overridden mark's pocket (`build_for_course` without
    barriers, cached). A leg that rounds no island routes on `base` itself; a leg that rounds one
    gets a copy of `base` plus only that leg's barriers, cached by the base and those barriers. So
    a course that rounds the same island twice, once each way, never has both walls at once, and a
    normal course's leg sees exactly the barriers that leg's island would have drawn on the old
    course-wide mask."""
    src = (source or COASTLINE_SOURCE).strip().lower()
    depth = safety_depth if safety_depth is not None else safety_depth_m()
    base = build_for_course(definition, course_id, bbox, source=src, safety_depth=depth,
                            res_deg=res_deg, cache_dir=cache_dir, barriers=False)
    from shared import race_def
    n_legs = max(0, len(race_def.course_to_marks(definition, course_id)[0]) - 1)
    rounds = _island_rounding_marks(definition, course_id) if ROUNDSIDE_ISLANDS else []
    leg_of = island_legs(definition, course_id)
    bkey = cache_key(definition, course_id, bbox, res_deg, source=src, safety_depth=depth) + "|nb"
    out = []
    for leg in range(n_legs):
        mine = [r for r in rounds if leg_of.get(r.get("mark_idx")) == leg]
        if not mine:
            out.append(base)
            continue
        key = bkey + "|leg" + hashlib.sha1(repr(_barrier_sig(mine)).encode()).hexdigest()[:12]
        failed = base.land == "failed" or base.depth == "failed" or bool(base.depth_gaps)
        f = _cache_get(key) if not failed else None
        if f is None:
            f = _copy_field(base)
            _add_barriers(f, mine)
            if not failed:
                _cache_put(key, f)
        out.append(f)
    return base, out


def _copy_field(field):
    """A copy whose mask, layers and barrier list can change; the (read-only) land/zone/island
    overlay geometry is shared, not copied. Built through __init__, not copy.copy: copy.copy reads
    `field.__dict__`, and on CPython 3.12 that alone makes every attribute read on `field` slower
    for good (~30% on the routing hot loop, measured on Bayview)."""
    f = ObstacleField(field.bbox, res_deg=field.res)
    f.mask = bytearray(field.mask)
    f.layers = dict(field.layers)
    f.geometry = dict(field.geometry, rounding_barriers=list(field.geometry["rounding_barriers"]))
    f.source, f.safety_depth, f.active = field.source, field.safety_depth, field.active
    f.land, f.land_error = field.land, field.land_error
    f.depth, f.depth_error = field.depth, field.depth_error
    f.nodepth = field.nodepth                  # read-only once built: shared, not copied
    f.nonnav, f.depth_gaps = field.nonnav, field.depth_gaps
    return f


def legs_summary(base, legs):
    """`summary()` for a course routed on per-leg fields: `base`'s, with every leg's barriers in
    the overlay geometry and the barrier cell count (so the map still draws all of them)."""
    fields = [f for f in ([base] if base is not None else []) + list(legs or []) if f is not None]
    if not fields:
        return {"active": False}
    first = fields[0]
    s = first.summary()
    drawn = list(first.geometry["rounding_barriers"])
    cells = first.layers.get("rounding_barrier", 0)
    seen = {id(first)}
    for f in fields[1:]:
        if id(f) in seen:
            continue
        seen.add(id(f))
        cells += max(0, f.layers.get("rounding_barrier", 0) - first.layers.get("rounding_barrier", 0))
        drawn += [g for g in f.geometry["rounding_barriers"] if g not in drawn]
    s["layers"] = dict(s["layers"], rounding_barrier=cells)
    s["cells_blocked"] = sum(s["layers"].values())
    s["geometry"] = dict(s["geometry"], rounding_barriers=drawn)
    s["active"] = any(f.active for f in fields)
    if any(getattr(f, "land", None) == "failed" for f in fields):
        s["land"] = "failed"
    if any(getattr(f, "depth", None) == "failed" for f in fields):
        s["depth"] = "failed"
    return s


def _fully_charted(field, chart_polys):
    """Cells the chart covers whole. A cell counts as charted (`nodepth` 0) when the chart covers
    its centre, so it may still hold uncharted water: at the chart's edge, or in a sliver between
    two charted areas. A cell is covered whole only if
      - it and its in-box neighbours are charted (the out-of-box ones are never routed), and
      - no edge of the chart's outline crosses it. The outline is every polygon edge that only
        one chart polygon has: an edge two depth areas share is inside the chart (S-57 areas
        share their boundaries), one only one polygon has bounds uncharted water.
    (Codex on #300.)"""
    nx, ny, nd = field.nx, field.ny, field.nodepth
    out = bytearray(nx * ny)
    for iy in range(ny):
        for ix in range(nx):
            i = iy * nx + ix
            if nd[i]:
                continue
            if all(not nd[jy * nx + jx]
                   for jy in range(max(0, iy - 1), min(ny, iy + 2))
                   for jx in range(max(0, ix - 1), min(nx, ix + 2))):
                out[i] = 1
    count = {}
    seen = set()
    for poly in chart_polys:
        rings = tuple(tuple((round(x, 7), round(y, 7)) for x, y in ring) for ring in poly)
        if rings in seen:            # the same area from two overlapping ENC cells: once
            continue
        seen.add(rings)
        for ring in rings:
            for k in range(len(ring)):
                a, b = ring[k], ring[(k + 1) % len(ring)]
                if a != b:
                    key = (a, b) if a < b else (b, a)
                    count[key] = count.get(key, 0) + 1
    w, sth, res = field.w, field.s, field.res
    for (a, b), c in count.items():
        if c != 1:
            continue
        for cx, cy in gridwalk.cells_touched((a[0] - w) / res, (a[1] - sth) / res,
                                             (b[0] - w) / res, (b[1] - sth) / res):
            if 0 <= cx < nx and 0 <= cy < ny:
                out[cy * nx + cx] = 0
    return out


def _apply_grids(field, grids, safety_depth, cache_dir, chart_polys=()) -> bool:
    """Fill the water no chart covers from the depth grids (#292). Per cell, everywhere but the
    cells the chart covers whole (`_fully_charted`: at the chart's edge both apply, so a shoal in
    a cell's uncharted half is still seen; Codex on #300):
      - the shallowest depth any grid has in the cell, shallower than `safety_depth`, blocks it
        (a shoal), even if the grid covers only part of the cell;
      - the cell's depth counts as checked (`nodepth` cleared, the grid named on the leg) only
        where a grid covers the WHOLE cell; a part-covered cell keeps "no depth data".
    A grid that fails to load is listed in `field.depth_gaps` and its water stays "no depth
    data". True if any grid gave a depth."""
    got = []
    for g in grids:
        try:
            got.append((g, *g.cells(field.bbox, field.res, field.nx, field.ny, cache_dir)))
        except Exception as exc:  # noqa: BLE001 — reported; that water stays "no depth data"
            why = f"{g.name}: {type(exc).__name__}: {exc}"[:200]
            field.depth_gaps.append(why)
            print(f"[obstacles] depth grid did not load: {why}", flush=True)
    if not got:
        return False
    marks = {g.name: bytearray(field.nx * field.ny) for g, _d, _f in got if not g.navigational}
    nd, mask = field.nodepth, field.mask
    charted = _fully_charted(field, chart_polys)
    shoal = 0
    any_depth = False
    primary = [t for t in got if not getattr(t[0], "fallback", False)]
    lake = getattr(field, "lake", None)
    for i in range(field.nx * field.ny):
        if charted[i]:
            continue
        best, who = None, None                  # shallowest anywhere / its grid, if it is full
        # a fallback grid (a world grid, heights from sea level) is ignored where another grid
        # covers the WHOLE cell, and on lake water (the shoreline's lakes): a lake's surface isn't
        # at sea level, so its bed height is not a depth. Where other grids cover only part of
        # the cell its shoals still count; on the coast its rocks and drying banks still block
        # (Codex on #350)
        whole = any(depths[i] is not None and full[i] for _g, depths, full in primary)
        on_lake = lake is not None and lake[i]
        for g, depths, full in (primary if whole or on_lake else got):
            d = depths[i]
            if d is None:
                continue
            any_depth = True
            if best is None or d < best:
                best = d
            if full[i] and (who is None or d < who[1]):
                who = (g, d)
        if best is None:
            continue
        if who is not None:
            nd[i] = 0
            if who[0].name in marks:
                marks[who[0].name][i] = 1
        if best < safety_depth and mask[i] != 1:
            mask[i] = 1
            shoal += 1
    if shoal:
        field.layers["shoal"] = field.layers.get("shoal", 0) + shoal
        field.active = True
    field.nonnav = [(g.label, marks[g.name]) for g, _d, _f in got
                    if g.name in marks and any(marks[g.name])]
    return any_depth


def _fill_global_backstop(field, cache_dir):
    """Rasterize the GLOBAL coastline (land ∧ ¬lake, islands re-added inside lakes) into the mask.

    The dataset is `coastline.active_source()` — GSHHG full-res by default (catches the sub-nm
    Great-Lakes islands Natural Earth omits, e.g. Cove Island), Natural Earth as the fallback; both
    expose the same land/lakes/islands roles so this code is source-agnostic. Used both as the primary
    source in non-ENC mode and as an always-on UNION BACKSTOP under ENC (which is US-only) so
    Canadian/non-US land is never missed. Only ADDS land + carves lakes; safe to run before ENC
    overlays it. Returns the loaded layers.

    When no shoreline can be loaded the field is marked `land = "failed"` (with the reason) and
    nothing is drawn: the caller must say the route's land was not checked, never treat the
    empty mask as open water (#51)."""
    try:
        coastline.ensure_global(cache_dir)
        layers = coastline.layers_in_bbox(field.bbox, cache_dir)
    except Exception as exc:  # noqa: BLE001 — any failure is "not loaded", reported, never water
        field.land = "failed"
        field.land_error = f"{type(exc).__name__}: {exc}"[:200]
        print(f"[obstacles] shoreline did not load: {field.land_error}", flush=True)
        return {}
    for poly in layers.get("land", []):
        field._fill_polygon(poly, 1, "coastline")
    for poly in layers.get("lakes", []):
        field._fill_polygon(poly, 0, "coastline")        # carve lakes back to water
    for poly in layers.get("islands", []):
        field._fill_polygon(poly, 1, "coastline")        # re-add islands inside lakes
    # which cells are lake water: a world grid's heights are from sea level, wrong on a lake
    # whose surface isn't (_apply_grids, Codex on #350)
    field.lake = bytearray(field.nx * field.ny)
    for poly in layers.get("lakes", []):
        field._fill_polygon(poly, 1, "lake", mask=field.lake)
    for poly in layers.get("islands", []):
        field._fill_polygon(poly, 0, "lake", mask=field.lake)
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
    switching coastline source or the boat draft (ENC depth no-go) yields a different mask. The
    pockets (where and how big) are in it too: a moved overridden mark, or a new override, must
    never get the old mask from the cache (#44)."""
    z = [(z.get("name"), z.get("type"), z.get("geometry")) for z in definition.get("zones", []) or []]
    isl = _course_islands(definition, course_id)
    pockets = [(n, round(la, 6), round(lo, 6)) for n, la, lo in override_marks(definition, course_id)]
    src = (source or COASTLINE_SOURCE).strip().lower()
    dv = depthsrc.data_version(bbox) if src == "enc" else coastline.DATA_VERSION
    depth = safety_depth if safety_depth is not None else safety_depth_m()
    depth = round(depth, 3) if src == "enc" else None
    payload = repr((src, dv, depth, course_id, tuple(round(b, 4) for b in bbox),
                    round(res_deg, 5), z, isl, pockets, round(MARK_CARVE_NM, 4)))
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


# --- carry a built field to another computer (the boat's Orin, issue #124) ------------------------
FIELD_SCHEMA = "c4.obstacles/v1"
MAX_FIELD_CELLS = 20_000_000     # a carried field bigger than this is refused (a course box is ~0.5 M)


def field_to_dict(field) -> dict:
    """A built ObstacleField as plain JSON: the mask zlib+base64 (a course box is ~0.5 M cells and
    packs to tens of KB), plus its provenance. The web-overlay geometry is left out. The Orin can't
    build the GSHHG/ENC land mask itself (no GDAL aboard), so the Lab builds it and the boat loads
    it with `field_from_dict` — the same mask the Lab routed on."""
    import base64
    import zlib
    return {"schema": FIELD_SCHEMA, "bbox": list(field.bbox), "res_deg": field.res,
            "nx": field.nx, "ny": field.ny, "layers": dict(field.layers), "source": field.source,
            "safety_depth": field.safety_depth, "active": field.active,
            "mask_zb64": base64.b64encode(zlib.compress(bytes(field.mask), 9)).decode()}


def field_from_dict(d: dict):
    """Rebuild an ObstacleField from `field_to_dict` output. Raises ValueError on a bad payload."""
    import base64
    import zlib
    if not isinstance(d, dict) or d.get("schema") != FIELD_SCHEMA:
        raise ValueError("not a c4.obstacles/v1 field")
    try:
        bbox, res = tuple(float(x) for x in d["bbox"]), float(d["res_deg"])
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ValueError(f"bad grid: {type(exc).__name__}") from None
    if len(bbox) != 4 or not all(math.isfinite(x) for x in bbox + (res,)) or res <= 0 \
            or bbox[0] <= bbox[1] or bbox[3] <= bbox[2]:   # (n, s, w, e)
        raise ValueError("bad grid")
    # the grid the box and cell size make, checked BEFORE anything is allocated: a tiny res_deg
    # with a small stated nx/ny would otherwise allocate terabytes
    qx, qy = (bbox[3] - bbox[2]) / res, (bbox[0] - bbox[1]) / res
    # a vanishing cell size divides to inf (math.ceil would raise OverflowError): refuse it here
    if not (math.isfinite(qx) and math.isfinite(qy)) or qx > MAX_FIELD_CELLS or qy > MAX_FIELD_CELLS:
        raise ValueError("grid too large")
    nx, ny = max(1, int(math.ceil(qx))), max(1, int(math.ceil(qy)))
    if nx * ny > MAX_FIELD_CELLS:
        raise ValueError("grid too large")
    try:
        if (nx, ny) != (int(d["nx"]), int(d["ny"])):
            raise ValueError("grid does not match its box")
    except (KeyError, TypeError, OverflowError) as exc:      # nx = 1e309 → OverflowError
        raise ValueError(f"bad grid: {type(exc).__name__}") from None
    field = ObstacleField(bbox, res_deg=res)
    try:
        dec = zlib.decompressobj()
        mask = dec.decompress(base64.b64decode(d["mask_zb64"], validate=True), nx * ny + 1)
    except (ValueError, TypeError, KeyError, zlib.error) as exc:   # binascii.Error is a ValueError
        raise ValueError(f"bad mask: {type(exc).__name__}") from None
    if (field.nx, field.ny) != (int(d["nx"]), int(d["ny"])) or len(mask) != field.nx * field.ny:
        raise ValueError("mask size does not match its box")
    # cells are 0 (water) or 1 (blocked): `blocked` tests == 1, so any other byte would read as
    # open water while the chart claims land was checked
    if mask.translate(None, b"\x00\x01"):
        raise ValueError("mask has values other than 0 and 1")
    field.mask = bytearray(mask)
    layers = d.get("layers") or {}
    if not isinstance(layers, dict):
        raise ValueError("bad layers")
    try:
        field.layers.update({str(k): int(v) for k, v in layers.items()})
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"bad layers: {type(exc).__name__}") from None
    # provenance only (reported, never routed on): the depth the chart was built for is bound by
    # the fingerprint, which the Orin checks against the boat it routes with
    field.source = str(d.get("source") or field.source)[:40]
    sd = d.get("safety_depth")
    try:
        field.safety_depth = float(sd) if isinstance(sd, (int, float)) and not isinstance(sd, bool) \
            and math.isfinite(sd) else None
    except OverflowError:                       # an int too large for a float
        field.safety_depth = None
    # derived from the mask, never taken from the payload: an inactive field ignores every cell
    field.active = any(field.mask)
    return field


def course_fingerprint(definition: dict, course_id, safety_depth) -> str:
    """What a carried land mask was built from: the course, the race's zones and the no-go depth.
    ONE implementation (shared/chartid.py) for the Lab, the Pi's GPS export and the Orin."""
    from shared import chartid
    return chartid.course_fingerprint(definition, course_id, safety_depth)
