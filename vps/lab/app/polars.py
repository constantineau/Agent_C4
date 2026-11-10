"""Boat polar helpers for the Lab optimizer.

The polar itself is the BOAT's (#303): `boats.polar_for(boat)` resolves the profile's own files or
its inline cert polar (shared/boat_polar.py) and every route, play and debrief is handed that block.
There is no Lab-wide default polar: a boat with none gets "No polar for this boat.", never sr33's.
This module keeps the pieces that work on whatever polar a caller passes: the envelope / per-sail
tables of a block, and the human-approved refined-polar overlay (Lab-4).
"""
from shared import boat_polar as BP

_TABLES: dict = {}          # (kind, block version) -> table: one object per polar, so lookups cache


def _memo(kind, block, build):
    key = (kind, block.get("version"))
    if key[1] is None:
        return build(block)
    if key not in _TABLES:
        if len(_TABLES) > 32:
            _TABLES.clear()
        _TABLES[key] = build(block)
    return _TABLES[key]


def polars_stw(block):
    """[(tws, twa, target_stw)] of a boat polar block ([] for None). Never mutate the result."""
    return _memo("env", block, BP.envelope_stw) if block else []


def sail_polars(block):
    """Per-sail polar curves `{sail_short: [(tws, twa, stw)]}` of a boat polar block (routing
    fidelity 2g — sail-aware routing). The envelope is the max-over-sails speed; THIS is the speed
    of EACH sail across its rated TWA domain, so the optimizer can model the cost of HOLDING a
    sub-optimal sail vs PEELING. Empty dict when the boat has no per-sail curves (a cert-only boat)
    — the optimizer then routes on the envelope only (sail-aware off)."""
    return _memo("sails", block, BP.sail_curves) if block else {}


CELL_MULT_MIN, CELL_MULT_MAX = 0.85, 1.15     # same band as learning._CELL_MULT_* / apply_proposal


def _cell_key(tws, twa):
    return (round(float(tws), 1), round(float(twa), 1))


def _mults(adjustments):
    """{(tws, twa): clamped multiplier} from the approved overlay; bad rows skipped."""
    out = {}
    for a in adjustments or ():
        try:
            m = float(a.get("mult"))
            k = _cell_key(a.get("tws"), a.get("twa"))
        except (TypeError, ValueError, AttributeError):
            continue
        out[k] = max(CELL_MULT_MIN, min(CELL_MULT_MAX, m))
    return out


def apply_adjustments(P, adjustments):
    """Apply the Lab-4 human-APPROVED refined-polar overlay to a polar table `P` = [(tws,twa,stw)].

    `adjustments` = [{tws, twa, mult}] — a multiplicative tweak per ORC grid cell (the cert stays the
    canonical source; this is the explicit, reviewed overlay the learning loop produces). Returns a NEW
    list (P is the module cache — never mutate it). Multipliers are clamped to 0.85–1.15 (the band the
    proposal and the apply use) so an approved tweak can't wildly distort the polar. Empty/none → P
    unchanged."""
    if not adjustments or not P:
        return P
    mults = _mults(adjustments)
    if not mults:
        return P
    return [(tws, twa, stw * mults.get(_cell_key(tws, twa), 1.0)) for (tws, twa, stw) in P]


def adjust_sail_polars(SP, adjustments, grid):
    """The same approved overlay applied to the PER-SAIL curves `{sail: [(tws,twa,stw)]}`, so holding a
    non-optimal sail is priced on the boat's refined polar, not the raw cert (and a gear-loss envelope
    rebuilt from these curves keeps the overlay).

    The overlay is a multiplier per envelope cell (`grid` = the cert envelope [(tws,twa,stw)]). A sail
    point on a cell takes that cell's multiplier; a sail point between the envelope's angles in the
    same TWS row (a kite's own run angle, say) takes the multiplier interpolated along that row,
    unadjusted cells counting as 1.0. A row the envelope doesn't chart → 1.0. Returns a NEW dict."""
    if not adjustments or not SP:
        return SP
    mults = _mults(adjustments)
    if not mults:
        return SP
    rows = {}
    for tws, twa, _stw in grid or ():
        rows.setdefault(round(float(tws), 1), set()).add(round(float(twa), 1))
    rows = {t: sorted(a) for t, a in rows.items()}

    def mult(tws, twa):
        k = _cell_key(tws, twa)
        if k in mults:
            return mults[k]
        angles = rows.get(k[0])
        if not angles:
            return 1.0
        a = k[1]
        if a <= angles[0]:
            return mults.get((k[0], angles[0]), 1.0)
        if a >= angles[-1]:
            return mults.get((k[0], angles[-1]), 1.0)
        for a0, a1 in zip(angles, angles[1:]):
            if a0 <= a <= a1:
                m0, m1 = mults.get((k[0], a0), 1.0), mults.get((k[0], a1), 1.0)
                return m0 + (m1 - m0) * ((a - a0) / (a1 - a0) if a1 > a0 else 0.0)
        return 1.0

    return {s: [(tws, twa, stw * mult(tws, twa)) for (tws, twa, stw) in pts] for s, pts in SP.items()}
