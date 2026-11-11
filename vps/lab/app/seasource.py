"""Per-area sources for the sea layers: water currents and waves (#308, the start of #309).

Each source says WHERE it has data as a water mask (the cells its model treats as water, made from
the model's own grid by tools/seamasks/build.py), not a box, and fetches a field for a bbox and a
time window. The course area picks the source: of the sources whose water holds most of the
area's water, the finest one wins, and a coarser one is tried when it has no data. Outside every
source's water the field is a plain "none", and every route says so in its `data` block.

    current.SOURCES  NOAA LMHOFS (Lake Michigan-Huron), then HYCOM ESPC (global ocean)
    wave.SOURCES     NOAA GLWU (Great Lakes), then NOAA GFS-Wave (global ocean)

"The area's water" = lattice points over the bbox that lie in ANY source's mask (currents and
waves). So a Lake Erie course, which GLWU models but LMHOFS does not, has water that is not
LMHOFS water: no current source covers it, even though Erie is inside LMHOFS's old box.
"""
import base64
import json
import math
import os
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
MASK_DIR = os.environ.get("SEA_MASK_DIR", os.path.join(HERE, "seamasks"))
# a source covers the area when its water holds at least this share of the area's water
COVER_MIN = float(os.environ.get("SEA_COVER_MIN", "0.5"))
LATTICE_MAX = 25           # lattice points per side over the bbox

# why a field is empty → the data block's words. The notes are what the Lab shows.
OUTSIDE, NO_DATA, OFF, OFFLINE = "outside coverage", "no data for the race time", "turned off", \
    "offline"
_NOTE = {
    OUTSIDE: "No {k} data for this area.",
    NO_DATA: "No {k} data for the race time.",
    OFFLINE: "No {k} data: no internet.",
    OFF: "No {k}: turned off.",
}
TIDAL_WARNING = "Tidal water, but no current data. The route ignores the tide. Check the tide tables."


class Coverage:
    """A water mask on a regular lat/lon raster. Bit (r, c) = 1: the source's model has water in
    the cell whose south-west corner is (s + r*step, w + c*step). A raster 360° wide wraps."""

    def __init__(self, s, w, step, rows, cols, bits, name=""):
        self.s, self.w, self.step = float(s), float(w), float(step)
        self.rows, self.cols = int(rows), int(cols)
        self.bits = bytes(bits)
        self.name = name
        self.wraps = self.cols * self.step >= 359.999

    @classmethod
    def from_cells(cls, s, w, step, rows, cols, cells, name=""):
        """Mask from an iterable of water cells (r, c): tests and the mask builder."""
        buf = bytearray((rows * cols + 7) // 8)
        for r, c in cells:
            if 0 <= r < rows and 0 <= c < cols:
                k = r * cols + c
                buf[k >> 3] |= 0x80 >> (k & 7)
        return cls(s, w, step, rows, cols, bytes(buf), name)

    @classmethod
    def load(cls, path):
        with open(path) as f:
            d = json.load(f)
        bits = zlib.decompress(base64.b64decode(d["bits"]))
        return cls(d["s"], d["w"], d["step"], d["rows"], d["cols"], bits, d.get("name", ""))

    def to_json(self, **meta):
        return dict(meta, name=self.name, s=self.s, w=self.w, step=self.step, rows=self.rows,
                    cols=self.cols, bits=base64.b64encode(zlib.compress(self.bits, 9)).decode())

    def contains(self, lat, lon):
        r = math.floor((lat - self.s) / self.step)
        x = lon - self.w
        if self.wraps:
            x %= 360.0
        c = math.floor(x / self.step)
        if not (0 <= r < self.rows and 0 <= c < self.cols):
            return False
        k = r * self.cols + c
        return bool(self.bits[k >> 3] & (0x80 >> (k & 7)))


def lattice(bbox, n=None):
    """Points over a (north, south, west, east) bbox: about one per 0.02°, 5..LATTICE_MAX a side."""
    north, south, west, east = bbox
    if north < south:
        north, south = south, north
    def count(span):
        return n or max(5, min(LATTICE_MAX, int(span / 0.02) + 1))
    ny, nx = count(north - south), count(east - west)
    return [(south + (north - south) * j / (ny - 1), west + (east - west) * i / (nx - 1))
            for j in range(ny) for i in range(nx)]


class SeaSource:
    """One data source for one sea layer. Subclasses set the facts and `fetch`."""
    kind = "current"           # "current" | "waves"
    id = ""                    # the field's `source` (what results and the Lab show)
    name = ""                  # for people: "NOAA LMHOFS"
    resolution_km = 10.0       # finest wins
    sea = False                # open sea, where there are tides (drives the tidal flag)
    mask = ""                  # seamasks/<mask>.json
    _coverage = None

    def coverage(self):
        if self._coverage is None:
            self._coverage = Coverage.load(os.path.join(MASK_DIR, self.mask + ".json"))
        return self._coverage

    def fetch(self, bbox, t_start, t_end, log):
        """The field over bbox (north, south, west, east) and the window, or None (no data)."""
        raise NotImplementedError


def _all_sources():
    from . import current, wave
    return list(current.SOURCES) + list(wave.SOURCES)


def _water(bbox, sources):
    pts = lattice(bbox)
    covs = [s.coverage() for s in sources]
    return [p for p in pts if any(c.contains(*p) for c in covs)]


def share(source, bbox, water=None):
    """Share of the area's water (points in any source's mask) that is in `source`'s water."""
    water = _water(bbox, _all_sources()) if water is None else water
    if not water:
        return 0.0
    cov = source.coverage()
    return sum(1 for p in water if cov.contains(*p)) / len(water)


def pick(sources, bbox):
    """The sources that cover the area, finest first ([] = none does)."""
    water = _water(bbox, _all_sources())
    ok = [s for s in sources if water and share(s, bbox, water) >= COVER_MIN]
    return sorted(ok, key=lambda s: s.resolution_km)


def build(sources, bbox, t_start, t_end, log, zero, word):
    """The field from the finest covering source that has data, else `zero(why)`. `word` names
    the layer in the log ("currents", "waves")."""
    try:
        cands = pick(sources, bbox)
    except Exception as ex:         # a broken mask file must not break the route: say none
        log(f"{word}: coverage check failed ({type(ex).__name__}) — none")
        return zero(NO_DATA)
    if not cands:
        log(f"{word}: no source covers this area — none")
        return zero(OUTSIDE)
    for src in cands:
        try:
            field = src.fetch(bbox, t_start, t_end, log)
        except Exception as ex:     # never let a sea layer break an optimize
            log(f"{word}: {src.name} failed ({type(ex).__name__})")
            field = None
        if field is not None and getattr(field, "loaded", False):
            return field
        log(f"{word}: {src.name} has no data for the window")
    return zero(NO_DATA)


def is_tidal(definition=None, bbox=None):
    """Is the course on tidal water? The race says so (`tidal`: true/false); else the finest
    source (current or waves) that covers the area is an open-sea model."""
    t = (definition or {}).get("tidal")
    if isinstance(t, bool):
        return t
    if not bbox:
        return False
    try:
        srcs = _all_sources()
        water = _water(bbox, srcs)
        cover = sorted((s for s in srcs if water and share(s, bbox, water) >= COVER_MIN),
                       key=lambda s: s.resolution_km)
        return bool(cover) and cover[0].sea
    except Exception:
        return False


def describe(field):
    """The data block's words for one layer: the source's name, or "none: <why>"."""
    if field is not None and getattr(field, "loaded", False):
        return getattr(field, "label", None) or getattr(field, "source", None) or "loaded"
    return "none: " + (getattr(field, "why", None) or OFF)


def data_block(cur, waves, definition=None, bbox=None):
    """What sea data a result was built on, in words the Lab shows as is (#308)."""
    out = {"current": describe(cur), "waves": describe(waves),
           "current_source": getattr(cur, "source", None) if getattr(cur, "loaded", False) else None,
           "waves_source": (getattr(waves, "source", None)
                            if getattr(waves, "loaded", False) else None),
           "tidal": None, "notes": [], "warning": None}
    for field, k in ((cur, "current"), (waves, "wave")):
        if not getattr(field, "loaded", False):
            out["notes"].append(_NOTE.get(getattr(field, "why", None) or OFF,
                                          _NOTE[OFF]).format(k=k))
    cur_why = getattr(cur, "why", None) if not getattr(cur, "loaded", False) else None
    if cur_why in (OUTSIDE, NO_DATA, OFFLINE) and is_tidal(definition, bbox):
        out["tidal"] = True
        out["warning"] = TIDAL_WARNING
    return out
