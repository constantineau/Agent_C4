"""Exact segment-vs-grid test: every cell a straight segment touches (a supercover).

ONE implementation for every land/obstacle mask in the system: the Lab's router (ObstacleField,
also on the Orin) and the Pi's GPS export (agent/app/landchart.py). Never sampled, so a segment
that clips a cell's corner can't slip past it (#145).

Coordinates are in GRID UNITS: x = (lon - west) / res, y = (lat - south) / res, so cell (c, r)
covers x in [c, c+1) and y in [r, r+1). A segment that clips a corner, or runs exactly along a
cell edge, touches the cells on both sides. Pure stdlib.
"""
import math


# Boundaries are conservative: a value within EPS of a grid line counts as ON it (both sides
# touched), and a segment's own endpoints are used exactly, never re-derived through the slope.
# Float error must only ever add a cell, never drop one (Codex on #200: an endpoint exactly on a
# blocked cell's corner came out 4e-16 past it and the cell was missed). EPS is in grid units:
# 1e-9 of a ~550 m cell is far below a millimetre.
EPS = 1e-9


def _lo(v):
    """First row/column touched by an interval starting at v (on an edge: the one below too)."""
    return math.floor(v - EPS)


def _hi(v):
    return math.floor(v + EPS)


def _span(c, x0, y0, x1, y1, slope):
    """The rows the segment meets inside column c: (first, last)."""
    xa = x0 if x0 > c else c
    xb = x1 if x1 < c + 1 else c + 1
    ya = y0 if xa == x0 else (y1 if xa == x1 else y0 + slope * (xa - x0))
    yb = y1 if xb == x1 else (y0 if xb == x0 else y0 + slope * (xb - x0))
    lo, hi = (ya, yb) if ya <= yb else (yb, ya)
    ymin, ymax = (y0, y1) if y0 <= y1 else (y1, y0)
    lo, hi = max(lo, ymin), min(hi, ymax)       # never past the segment's own ends
    return _lo(lo), _hi(hi)


def cells_touched(x0, y0, x1, y1):
    """Yield (col, row) for every cell the segment touches, unbounded (the caller clips)."""
    if x1 < x0:
        x0, y0, x1, y1 = x1, y1, x0, y0
    if x1 - x0 <= EPS:                                 # vertical: one column (two on an edge)
        lo, hi = (y0, y1) if y0 <= y1 else (y1, y0)
        for c in range(_lo(x0), _hi(x1) + 1):
            for r in range(_lo(lo), _hi(hi) + 1):
                yield c, r
        return
    slope = (y1 - y0) / (x1 - x0)
    for c in range(_lo(x0), _hi(x1) + 1):
        r0, r1 = _span(c, x0, y0, x1, y1, slope)
        for r in range(r0, r1 + 1):
            yield c, r


def any_set(mask, nx, ny, x0, y0, x1, y1):
    """True if the segment touches a cell whose mask byte is 1. `mask` is row-major (row * nx +
    col), nx by ny; cells outside the grid are never set (unknown = not blocked, as the masks'
    point tests say). The router's hot path: no generator, early exit."""
    if x1 < x0:
        x0, y0, x1, y1 = x1, y1, x0, y0
    if x1 - x0 <= EPS:
        lo, hi = (y0, y1) if y0 <= y1 else (y1, y0)
        r0, r1 = max(0, _lo(lo)), min(ny - 1, _hi(hi))
        for c in range(max(0, _lo(x0)), min(nx - 1, _hi(x1)) + 1):
            for r in range(r0, r1 + 1):
                if mask[r * nx + c] == 1:
                    return True
        return False
    slope = (y1 - y0) / (x1 - x0)
    for c in range(max(0, _lo(x0)), min(nx - 1, _hi(x1)) + 1):
        r0, r1 = _span(c, x0, y0, x1, y1, slope)
        if r0 < 0:
            r0 = 0
        if r1 > ny - 1:
            r1 = ny - 1
        for r in range(r0, r1 + 1):
            if mask[r * nx + c] == 1:
                return True
    return False
