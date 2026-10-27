"""Polar lookup: target boatspeed at any (TWS, TWA), interpolated on the polar grid.

A polar table is a flat list of (tws_kn, twa_deg, stw_kn) points — one row of angles per charted
wind speed. Picking the "nearest" point by |dTWS| + |dTWA| adds knots to degrees, so a 2 kn breeze
at 30 deg used to read the 10 kn row's beat speed. This module does it properly:

- **Within a row** (one TWS): linear in TWA between the charted angles; above the last angle the
  row's last speed holds; BELOW the row's first angle (the beat angle on the envelope, the lowest
  rated angle on a per-sail curve) the boat can't sail that close → 0.
- **Between rows**: bracket the TWS and interpolate linearly. The lowest angle is interpolated too,
  so the no-go edge slides smoothly from one row's beat angle to the next; at a TWA between the two
  rows' edges, the row whose edge is higher reads its edge speed.
- **Below the lowest row** (e.g. under 4 kn on the ORC cert): the lowest row scaled by tws/tws_min,
  reaching 0 in no wind. Never the lowest row's full speed.
- **Above the highest row**: the highest row (the cert stops there; no extrapolation).

`lo_margin` (deg) lets a per-sail curve be held a few degrees under its lowest rated angle at its
edge speed (the router's domain slack) before it reads 0.

Shared by the Lab optimizer and the onboard engine so both read the polar the same way.
"""
from __future__ import annotations

import bisect
import math

_ROWS_CACHE: dict = {}          # id(table) -> (table, rows); the table ref keeps the id from reuse
_ROWS_CACHE_MAX = 64


def build_rows(table):
    """Flat [(tws, twa, stw)] → sorted [(tws, [twa...], [stw...])], skipping null speeds and
    duplicate angles (first one kept)."""
    by = {}
    for p in table or ():
        tws, twa, stw = p[0], p[1], p[2]
        if tws is None or twa is None or stw is None:
            continue
        row = by.setdefault(float(tws), {})
        row.setdefault(float(twa), float(stw))
    rows = []
    for tws in sorted(by):
        pts = sorted(by[tws].items())
        rows.append((tws, [a for a, _ in pts], [s for _, s in pts]))
    return rows


def rows_for(table):
    """Cached `build_rows` keyed on the table object (the optimizer passes the same list for a
    whole route, so this is built once per table, not once per lookup)."""
    if not table:
        return []
    hit = _ROWS_CACHE.get(id(table))
    if hit is not None and hit[0] is table:
        return hit[1]
    rows = build_rows(table)
    if len(_ROWS_CACHE) >= _ROWS_CACHE_MAX:
        _ROWS_CACHE.clear()
    _ROWS_CACHE[id(table)] = (table, rows)
    return rows


def _on_row(twas, stws, twa):
    """Speed on one row at `twa` (caller has already handled the below-edge case)."""
    if twa <= twas[0]:
        return stws[0]
    if twa >= twas[-1]:
        return stws[-1]
    i = bisect.bisect_right(twas, twa)
    a0, a1 = twas[i - 1], twas[i]
    s0, s1 = stws[i - 1], stws[i]
    return s0 + (s1 - s0) * (twa - a0) / (a1 - a0) if a1 > a0 else s0


def _bracket(rows, tws):
    """(row_lo, row_hi, f, scale): interpolate f of the way from row_lo to row_hi, then × scale."""
    if tws <= rows[0][0]:
        r = rows[0]
        return r, r, 0.0, (max(0.0, tws) / r[0] if r[0] > 0 else 1.0)
    if tws >= rows[-1][0]:
        r = rows[-1]
        return r, r, 0.0, 1.0
    i = bisect.bisect_right([r[0] for r in rows], tws)
    r0, r1 = rows[i - 1], rows[i]
    return r0, r1, (tws - r0[0]) / (r1[0] - r0[0]), 1.0


def min_twa(rows, tws):
    """The lowest sailable TWA at this TWS (the interpolated beat angle on an envelope)."""
    if not rows:
        return None
    r0, r1, f, _ = _bracket(rows, tws)
    return r0[1][0] + (r1[1][0] - r0[1][0]) * f


def speed(rows, tws, twa, lo_margin=0.0):
    """Target boatspeed (kn) at (tws, twa) from prepared `rows`; 0 in the no-go zone, in no wind, or
    with no table."""
    if not rows or tws is None or twa is None:
        return 0.0
    twa = abs(((float(twa) + 180.0) % 360.0) - 180.0)
    r0, r1, f, scale = _bracket(rows, float(tws))
    if scale <= 0.0:
        return 0.0
    edge = r0[1][0] + (r1[1][0] - r0[1][0]) * f
    if twa < edge - lo_margin - 1e-9:
        return 0.0
    v0 = _on_row(r0[1], r0[2], twa)
    v = v0 if r1 is r0 else v0 + (_on_row(r1[1], r1[2], twa) - v0) * f
    return v * scale


def lookup(table, tws, twa, lo_margin=0.0):
    """`speed` straight off a flat [(tws, twa, stw)] table (rows cached per table object)."""
    return speed(rows_for(table), tws, twa, lo_margin)


def vmg_twa(rows, tws, pos):
    """The VMG-optimal TWA at this TWS on the interpolated polar: 'beat' (upwind, < 90°) or 'run'
    (downwind, > 90°). Scans every charted angle of the bracketing rows plus a 0.5° grid, so the
    answer is exact wherever the optimum sits on a charted angle (it normally does). None if the
    table has no speed on that side."""
    if not rows or tws is None or tws <= 0:
        return None
    r0, r1, _f, _s = _bracket(rows, float(tws))
    edge = min_twa(rows, tws)
    cands = set(r0[1]) | set(r1[1]) | {edge} | {x * 0.5 for x in range(0, 361)}
    best, best_a = 0.0, None
    for a in cands:
        if a < edge - 1e-9:
            continue
        if pos == "beat":
            if a >= 90:
                continue
            v = speed(rows, tws, a) * math.cos(math.radians(a))
        elif pos == "run":
            if a <= 90:
                continue
            v = -speed(rows, tws, a) * math.cos(math.radians(a))
        else:
            return None
        if v > best + 1e-12 or (best_a is not None and abs(v - best) <= 1e-12 and a < best_a):
            best, best_a = v, a
    return round(best_a, 1) if best_a is not None else None


def target(table, tws, twa, no_go=30.0):
    """The polar TARGET to grade a sailed speed against (polar %), not a routing speed: like `lookup`,
    except a boat pointing above the beat angle (but not inside `no_go`) is graded against the beat
    speed instead of 0, so pinching samples still count. 0 inside `no_go` or with no table."""
    rows = rows_for(table)
    if not rows or tws is None or twa is None:
        return 0.0
    twa = abs(((float(twa) + 180.0) % 360.0) - 180.0)
    if twa < no_go:
        return 0.0
    return speed(rows, tws, max(twa, min_twa(rows, tws)))
