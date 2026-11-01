"""The deployed LAND chart, read on the Pi for the GPS export (#125 review).

The Lab's Deploy push (#142) carries the course's land-only mask ("land-v2") to the Pi
(orinassets.py); the Orin routes on it. The GPS export must check the same land when it thins a
route for the plotter and when it picks the waypoint to steer to: a boat off the track beside a
narrow peninsula must never be sent a bearing across it.

Used only when it is the chart for the course aboard, exactly as the Orin decides
(pi/orin/reroute/job.load_obstacles): the deployed definition matches the navigator's marks and
load (orinroute._definition_for), the chart is for that race and course, and its fingerprint (the
course, the zones, the no-go depth it was built for) matches the definition. Like the Orin, the
pocket round each mark and round the boat is opened (MARK_CARVE_NM): those are waters the route
must reach. Outside the chart's box nothing is known: `covers` says so, and the caller must not
claim the shore was checked.

Pure Python (zlib + base64), no GDAL: the Pi holds no shoreline, only this mask.
"""
import base64
import hashlib
import json
import math
import zlib

from shared import boat_profile

from . import orinassets, orinroute

CHART_KIND = "land-v2"
MARK_CARVE_NM = 0.5        # = the Lab's geo.MARK_CARVE_NM: the pocket the Orin opens at each mark


def course_fingerprint(definition, course_id, safety_depth):
    """= vps/lab/app/geo/obstacles.course_fingerprint (the Lab writes it, the Orin checks it)."""
    courses = (definition or {}).get("courses") or []
    course = next((c for c in courses if c.get("id") == course_id), None) or \
        (courses[0] if courses else None)
    payload = json.dumps({"course": course, "zones": (definition or {}).get("zones") or [],
                          "depth": None if safety_depth is None else round(float(safety_depth), 3)},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


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

    def crosses(self, lat1, lon1, lat2, lon2):
        """Does the straight segment pass over land (sampled under half a cell apart)?"""
        dlat, dlon = lat2 - lat1, lon2 - lon1
        steps = max(2, int(math.hypot(dlat, dlon) / (self.res * 0.5)) + 1)
        return any(self.blocked(lat1 + dlat * k / steps, lon1 + dlon * k / steps)
                   for k in range(steps + 1))


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
    # the depth it was built for: the chart's own record of it, else the pushed boat profile's
    # (draft + margin, as the Lab and the Orin compute it), else the Lab's default
    boat_p = a.get("boat") if isinstance(a.get("boat"), dict) else None
    depths = [chart.get("safety_depth"), 2.63]
    if boat_p and boat_p.get("draft_m") is not None:
        try:
            depths.insert(1, boat_profile.safety_depth_m(boat_p))
        except (TypeError, ValueError):
            pass
    fp = chart.get("fingerprint")
    if not any(fp == course_fingerprint(defn, cid, d) for d in depths
               if d is None or isinstance(d, (int, float))):
        return None                                       # a re-deployed course: an old chart
    try:
        land = LandMask(chart)
    except (KeyError, TypeError, ValueError, zlib.error):
        return None
    for m in marks:
        if m.get("lat") is not None and m.get("lon") is not None:
            land.carve(m["lat"], m["lon"])
    if boat and boat[0] is not None:
        land.carve(boat[0], boat[1])
    return land
