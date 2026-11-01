"""The deployed LAND chart, read on the Pi for the GPS export (#125 review).

The Lab's Deploy push (#142) carries the course's land-only mask ("land-v2") to the Pi
(orinassets.py); the Orin routes on it. The GPS export must check the same land when it thins a
route for the plotter and when it picks the waypoint to steer to: a boat off the track beside a
narrow peninsula must never be sent a bearing across it.

Used only when it is the chart for the course aboard, exactly as the Orin decides
(pi/orin/reroute/job.load_obstacles): the deployed definition matches the navigator's marks and
load (orinroute._definition_for), the chart is for that race and course, and its fingerprint (the
course, the zones, the no-go depth it was built for) matches the definition. Like the Orin, the
pocket round each mark is opened (MARK_CARVE_NM): those are waters the route must reach. Round
the boat only its own cell (and one neighbour when the coarse raster puts the boat on land): the
steer-to check must still see a headland close aboard. Outside the chart's box nothing is known: `covers` says so, and the caller must not
claim the shore was checked.

Pure Python (zlib + base64), no GDAL: the Pi holds no shoreline, only this mask.
"""
import base64
import math
import zlib

from shared import chartid

from . import orinassets, orinroute

CHART_KIND = "land-v2"
MARK_CARVE_NM = 0.5        # = the Lab's geo.MARK_CARVE_NM: the pocket the Orin opens at each mark


def course_fingerprint(definition, course_id, safety_depth):
    """The shared one (shared/chartid.py): the Lab stamps it, the Orin checks it, so does this."""
    return chartid.course_fingerprint(definition, course_id, safety_depth)


class LandMask:
    def __init__(self, chart):
        nx, ny = orinassets.grid_dims(chart)
        if nx is None:
            raise ValueError("bad chart grid")
        self.n, self.s, self.w, self.e = (float(x) for x in chart["bbox"])
        self.res = float(chart["res_deg"])
        self.nx, self.ny = nx, ny
        d = zlib.decompressobj()
        mask = d.decompress(base64.b64decode(chart["mask_zb64"], validate=True), nx * ny + 1)
        if len(mask) != nx * ny or mask.translate(None, b"\x00\x01"):
            raise ValueError("bad chart mask")
        self.mask = bytearray(mask)

    def covers(self, lat, lon):
        return self.s <= lat <= self.n and self.w <= lon <= self.e

    def _cell(self, lat, lon):
        ix, iy = int((lon - self.w) / self.res), int((lat - self.s) / self.res)
        if ix < 0 or iy < 0 or ix >= self.nx or iy >= self.ny:
            return None
        return iy * self.nx + ix

    def blocked(self, lat, lon):
        c = self._cell(lat, lon)
        return c is not None and self.mask[c] == 1

    def carve(self, lat, lon, radius_nm=MARK_CARVE_NM):
        """Open a disk of water (a mark, the boat) like the Lab's ObstacleField._carve_disk."""
        rlat = radius_nm / 60.0
        for iy in range(max(0, int((lat - rlat - self.s) / self.res)),
                        min(self.ny - 1, int((lat + rlat - self.s) / self.res)) + 1):
            clat = self.s + (iy + 0.5) * self.res
            f = 1.0 - ((clat - lat) / rlat) ** 2
            if f < 0:
                continue
            half = rlat / max(0.05, math.cos(math.radians(clat))) * math.sqrt(f)
            ia = max(0, int((lon - half - self.w) / self.res))
            ib = min(self.nx - 1, int((lon + half - self.w) / self.res))
            row = iy * self.nx
            for ix in range(ia, ib + 1):
                self.mask[row + ix] = 0

    def open_boat(self, lat, lon):
        """The boat is afloat: open its own cell, and when that cell reads as land (the raster is
        coarser than the shore) the ONE neighbour nearest the boat as well. Never a disk: a
        headland a few hundred metres from the boat is exactly what the steer-to check is for."""
        c = self._cell(lat, lon)
        if c is None or self.mask[c] == 0:
            return
        self.mask[c] = 0
        ix, iy = c % self.nx, c // self.nx
        fx = (lon - self.w) / self.res - ix            # where in its cell the boat sits, 0..1
        fy = (lat - self.s) / self.res - iy
        dx = -1 if fx < 0.5 else 1
        dy = -1 if fy < 0.5 else 1
        # the nearest cell edge is the neighbour across it
        nx_, ny_ = (ix + dx, iy) if min(fx, 1 - fx) <= min(fy, 1 - fy) else (ix, iy + dy)
        if 0 <= nx_ < self.nx and 0 <= ny_ < self.ny:
            self.mask[ny_ * self.nx + nx_] = 0

    def cells_touched(self, lat1, lon1, lat2, lon2):
        """Every grid cell the straight segment touches — exact (a supercover, no sampling): a
        segment that clips a cell's corner, or runs along a cell edge, touches the cells on both
        sides. Swept column by column: in each column the segment spans one y-interval, and every
        row that interval meets is touched (a boundary exactly on a row edge counts the row on the
        other side too). Cells outside the grid are left out (unknown water, as `blocked` says)."""
        x0, y0 = (lon1 - self.w) / self.res, (lat1 - self.s) / self.res
        x1, y1 = (lon2 - self.w) / self.res, (lat2 - self.s) / self.res
        if x1 < x0:
            x0, y0, x1, y1 = x1, y1, x0, y0
        dx = x1 - x0

        def y_at(x):
            return y0 if dx == 0 else y0 + (y1 - y0) * (x - x0) / dx

        def rows(ya, yb):
            lo, hi = min(ya, yb), max(ya, yb)
            r0 = math.floor(lo) - (1 if lo == math.floor(lo) else 0)   # on an edge: the row below
            return range(r0, math.floor(hi) + 1)

        c0 = math.floor(x0) - (1 if x0 == math.floor(x0) else 0)       # on an edge: the column left
        c1 = math.floor(x1)
        for c in range(c0, c1 + 1):
            if c < 0 or c >= self.nx:
                continue
            xa, xb = max(x0, c), min(x1, c + 1)
            if xa > xb:
                continue
            for r in rows(y_at(xa), y_at(xb)) if dx else rows(y0, y1):
                if 0 <= r < self.ny:
                    yield c, r

    def crosses(self, lat1, lon1, lat2, lon2):
        """Does the straight segment touch any land cell? Exact grid traversal (cells_touched),
        corners and edges included — never sampled, so a clipped corner can't slip through."""
        return any(self.mask[r * self.nx + c] == 1
                   for c, r in self.cells_touched(lat1, lon1, lat2, lon2))


def for_course(nav_route, marks, boat=None):
    """The land mask for the course aboard, pockets opened at its marks and the boat, or None
    (no chart pushed, or not this course's chart: the caller says the shore isn't checked).
    Caller holds orinroute.COURSE_LOCK."""
    if not nav_route or not marks:
        return None
    defn, cid = orinroute._definition_for(nav_route, marks)
    if not defn:
        return None
    rec = orinassets._read()
    a = (rec or {}).get("assets") or {}
    chart = a.get("chart")
    if not isinstance(chart, dict) or chart.get("kind") != CHART_KIND:
        return None
    if a.get("race_id") != defn.get("race_id") or a.get("course_id") != cid:
        return None
    # the depth resolved exactly as the Lab (orinassets) and the Orin (job.load_obstacles) do:
    # the pushed boat profile's draft + margin, else the shared default
    boat_p = a.get("boat") if isinstance(a.get("boat"), dict) else None
    try:
        depth = chartid.safety_depth_m(boat_p)
    except (TypeError, ValueError):
        return None
    if chart.get("fingerprint") != course_fingerprint(defn, cid, depth):
        return None                                       # a re-deployed course: an old chart
    try:
        land = LandMask(chart)
    except (KeyError, TypeError, ValueError, zlib.error):
        return None
    for m in marks:
        if m.get("lat") is not None and m.get("lon") is not None:
            land.carve(m["lat"], m["lon"])
    if boat and boat[0] is not None:
        land.open_boat(boat[0], boat[1])
    return land
