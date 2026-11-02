"""Exact segment-vs-grid test: every cell a straight segment touches (a supercover).

ONE implementation for every land/obstacle mask in the system: the Lab's router (ObstacleField,
also on the Orin) and the Pi's GPS export (agent/app/landchart.py). Never sampled, so a segment
that clips a cell's corner can't slip past it (#145).

Coordinates are in GRID UNITS: x = (lon - west) / res, y = (lat - south) / res, so cell (c, r)
covers x in [c, c+1) and y in [r, r+1). A segment that clips a corner, or runs exactly along a
cell edge, touches the cells on both sides. Pure stdlib.
"""
import math


def _span(c, x0, x1, y0, slope):
    """The rows the segment meets inside column c: (first, last)."""
    xa = x0 if x0 > c else c
    xb = x1 if x1 < c + 1 else c + 1
    ya = y0 + slope * (xa - x0)
    yb = y0 + slope * (xb - x0)
    lo, hi = (ya, yb) if ya <= yb else (yb, ya)
    f = math.floor(lo)
    return (f - 1 if lo == f else f), math.floor(hi)   # on a row edge: the row below too


def cells_touched(x0, y0, x1, y1):
    """Yield (col, row) for every cell the segment touches, unbounded (the caller clips)."""
    if x1 < x0:
        x0, y0, x1, y1 = x1, y1, x0, y0
    if x1 == x0:                                       # vertical: one column (two on an edge)
        lo, hi = (y0, y1) if y0 <= y1 else (y1, y0)
        f = math.floor(lo)
        r0, r1 = (f - 1 if lo == f else f), math.floor(hi)
        fx = math.floor(x0)
        for c in ((fx - 1, fx) if x0 == fx else (fx,)):
            for r in range(r0, r1 + 1):
                yield c, r
        return
    slope = (y1 - y0) / (x1 - x0)
    fx = math.floor(x0)
    for c in range((fx - 1 if x0 == fx else fx), math.floor(x1) + 1):
        if max(x0, c) > min(x1, c + 1):
            continue
        r0, r1 = _span(c, x0, x1, y0, slope)
        for r in range(r0, r1 + 1):
            yield c, r


def any_set(mask, nx, ny, x0, y0, x1, y1):
    """True if the segment touches a cell whose mask byte is 1. `mask` is row-major (row * nx +
    col), nx by ny; cells outside the grid are never set (unknown = not blocked, as the masks'
    point tests say). The router's hot path: no generator, early exit."""
    if x1 < x0:
        x0, y0, x1, y1 = x1, y1, x0, y0
    if x1 == x0:
        lo, hi = (y0, y1) if y0 <= y1 else (y1, y0)
        f = math.floor(lo)
        r0 = max(0, f - 1 if lo == f else f)
        r1 = min(ny - 1, math.floor(hi))
        fx = math.floor(x0)
        for c in ((fx - 1, fx) if x0 == fx else (fx,)):
            if 0 <= c < nx:
                for r in range(r0, r1 + 1):
                    if mask[r * nx + c] == 1:
                        return True
        return False
    slope = (y1 - y0) / (x1 - x0)
    fx = math.floor(x0)
    c0 = max(0, fx - 1 if x0 == fx else fx)
    c1 = min(nx - 1, math.floor(x1))
    for c in range(c0, c1 + 1):
        if max(x0, c) > min(x1, c + 1):
            continue
        r0, r1 = _span(c, x0, x1, y0, slope)
        if r0 < 0:
            r0 = 0
        if r1 > ny - 1:
            r1 = ny - 1
        for r in range(r0, r1 + 1):
            if mask[r * nx + c] == 1:
                return True
    return False
