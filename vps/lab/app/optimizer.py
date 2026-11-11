"""Lab-1 optimizer core — isochrone routing over a RaceDefinition course through a WindField.

Self-contained (no agent package): given a course's ordered marks, the boat polars and a multi-model
`WindField`, it routes leg-by-leg with the classic isochrone method — fan every heading over a short
time step, advance each by the polar boatspeed at the local TWA, prune to the outer envelope, repeat
until the envelope lays the mark, then backtrack the optimal path (which naturally tacks upwind /
gybes downwind). It samples the wind field's per-point confidence along the route so the briefing can
honestly flag where the models disagree.

Output = ONE optimal route + per-leg summary + a route-wide confidence + an Opus-written briefing.
Lab-2 will fan this across ensemble members/scenarios into a branching playbook; Lab-1 is the core.
RRS 41: this is pre-race cloud homework, frozen at the gun.
"""
from __future__ import annotations

import math
import os
import time

from shared import race_def
from shared import polar as SPOL
from shared.boat_polar import NO_POLAR
from . import polars as POL
from . import sailplan

HSTEP = 12          # heading fan resolution (deg)
SECTOR = 3.0        # isochrone pruning bucket (deg of bearing from leg start)
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
COVERAGE_MIN = float(os.environ.get("GRIB_COVERAGE_MIN", "0.6"))   # below this → degraded route
# #203 (the Lab side of #156): a point no forecast reaches borrows the nearest covered point's wind
# within this many nm (the boat's limit, same env); past it there is no wind and no route there
FILL_NM = float(os.environ.get("C4_FORECAST_FILL_NM", "10"))
GAPS_FILLED = "Forecast gaps filled from nearby."
ROUTE_CONE_DEG = float(os.environ.get("ROUTE_CONE_DEG", "120"))    # prune headings >this° off the mark
TACK_COST_S = float(os.environ.get("ROUTE_TACK_COST_S", "30"))     # time a tack/gybe costs (anti-over-tack)
ISO_CURVES = int(os.environ.get("ROUTE_ISO_CURVES", "10"))         # frontier polylines emitted per leg (viz)
ISO_PTS = int(os.environ.get("ROUTE_ISO_PTS", "60"))               # max points kept per frontier curve
ISO_MAX = int(os.environ.get("ROUTE_ISO_MAX", "80"))               # cap on total frontier curves in a result
LAYLINE_NM = float(os.environ.get("ROUTE_LAYLINE_NM", "10"))       # max layline draw length (nm)
# Mark-approach fidelity — stop the route sailing PAST a mark and doubling back (the "north of the
# mark then south then around" zig-zag), and leave port/starboard marks on the legal side:
LAYLINE_GATE = os.environ.get("ROUTE_LAYLINE_GATE", "1").strip().lower() in ("1", "true", "yes", "on")
OVERSTAND_NM = float(os.environ.get("ROUTE_OVERSTAND_NM", "0.5"))   # reject candidates this far PAST the mark on the up/down-wind axis
# ...but only this close to the mark (#256): the gate is for the final approach (no orbiting past
# the mark). Farther off, "past the mark" along the wind axis can be the only way in: Bayview Shore,
# wind 016, the Mackinac finish is reached up the channel from the east, and the gate shut it (no
# route at any wind speed). At 3 nm it still did; at 1 nm every Shore/016 case finishes.
OVERSTAND_RADIUS_NM = float(os.environ.get("ROUTE_OVERSTAND_RADIUS_NM", "1.0"))
ROUND_OFFSET_NM = float(os.environ.get("ROUTE_ROUND_OFFSET_NM", "0.10"))  # standoff to the correct side at a port/stbd rounding mark (nm)
# --- finish/mark over-tack ("scramble") fixes (routing fidelity 2e) — each env-flagged for A/B ---
def _flag(name, default="1"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")
# #1 LAYLINE COMMIT: once a node can lay the mark (bears more than the VMG angle off the wind axis),
# drop the opposite-tack headings so it sails the layline instead of free-tacking up to the mark.
LAYLINE_COMMIT = _flag("ROUTE_LAYLINE_COMMIT")
# WIND-OVER-WATER (2d lever b): index the polar by the wind the boat feels over the WATER (ground wind
# − current), not the ground wind. Default ON; a no-op wherever there's no current, so it's safe.
WIND_OVER_WATER = _flag("ROUTE_WIND_OVER_WATER")
LAYLINE_COMMIT_EPS = float(os.environ.get("ROUTE_LAYLINE_COMMIT_EPS", "2.0"))  # deg slack before committing
# LAYLINE TACK (#66): also offer the step that tacks exactly on the layline (see route_leg).
LAYLINE_TACK = _flag("ROUTE_LAYLINE_TACK")
# BEAT TO-GO (#66): on a beat leg the prune ranks by distance to go along the VMG boards, not the
# straight line to the mark (see _beat_dist_nm).
BEAT_TOGO = _flag("ROUTE_BEAT_TOGO")
LAYLINE_TACK_MARGIN = float(os.environ.get("ROUTE_LAYLINE_TACK_MARGIN_DEG", "0.5"))  # deg past the layline
LAYLINE_COMMIT_NM = float(os.environ.get("ROUTE_LAYLINE_COMMIT_NM", "10.0"))   # only commit within this of the mark (final approach; play shifts farther out)
# #2 CUMULATIVE TACK COST: the maneuver cost accrues along the whole path (not a one-step haircut), so
# repeated alternation genuinely loses ground in the prune + ETA — not just a ~5% per-step nudge.
TACK_CUMULATIVE = _flag("ROUTE_TACK_CUMULATIVE")
# The prune regularizer is DECOUPLED from the ETA cost: a real tack costs ~TACK_COST_S (30 s) of clock,
# but the isochrone needs a much larger penalty to stop the upwind STAIRCASE (with a 0.5 nm lane, 30 s
# ≈ 0.04 nm is far too small to make committing to a tack win). TACK_PRUNE_S only biases the prune
# ranking — so a beat converges to one tack to the layline — while the ETA still uses the real 30 s, so
# predicted times aren't inflated. Genuine shifts still pay enough to tack (verified on an oscillating
# beat), so this suppresses the weave without under-tacking.
TACK_PRUNE_S = float(os.environ.get("ROUTE_TACK_PRUNE_S", "300"))
# #3 POSITION PRUNE NEAR MARK: within MARK_PRUNE_NM of the mark, bucket the isochrone prune by POSITION
# (a small lat/lon cell) instead of bearing-from-start, so near-colocated opposite-tack nodes compete
# and the least-penalized one wins (kills the both-boards-survive weave on the final approach).
MARK_POS_PRUNE = _flag("ROUTE_MARK_POS_PRUNE")
MARK_PRUNE_NM = float(os.environ.get("ROUTE_MARK_PRUNE_NM", "6.0"))
MARK_PRUNE_CELL_NM = float(os.environ.get("ROUTE_MARK_PRUNE_CELL_NM", "0.25"))
# ROOT-CAUSE FIX for the mark-approach scramble: the legacy isochrone prunes by distance-FROM-START
# bucketed by bearing-from-start — which rewards sailing sideways (oversail) and lets BOTH tacks survive
# every generation (the weave). DMG_PRUNE instead ranks each candidate by distance MADE GOOD toward the
# mark and buckets by CROSS-TRACK LANE (lateral offset from the start→mark rhumb), so the frontier is one
# leading node per lane and a beat converges to a single tack to the layline. Supersedes MARK_POS_PRUNE.
DMG_PRUNE = _flag("ROUTE_DMG_PRUNE")
LANE_NM = float(os.environ.get("ROUTE_LANE_NM", "0.5"))   # cross-track lane width for the DMG prune (nm)
# --- progress monotonicity (the "circles/zig-zags at marks" fix) -------------------------------
# In near-drifting, ROTATING air the time-optimal isochrone path can genuinely LOOP (sail away to
# meet the new breeze), and with boatspeed ~1-3 kn broad regions of the frontier are near-equal so
# the winning node is arbitrary — the crew gets a self-crossing scribble on a low-confidence
# forecast. MONOTONE forbids giving back progress: a candidate is rejected once it is more than
# BACKTRACK_NM farther from the mark than the CLOSEST its own path has already been. Legitimate
# excursions survive (a beat/run gains ground on every board; layline overshoot ≪ the slack), and a
# node whose whole fan is monotone-blocked is re-expanded WITHOUT the gate (fail-open, mirroring the
# cone-gate reopen) so obstacle detours and being swept backward by a foul current still work.
MONOTONE = _flag("ROUTE_MONOTONE")
BACKTRACK_NM = float(os.environ.get("ROUTE_BACKTRACK_NM", "5.0"))   # max give-back toward the mark (nm)
# --- adaptive ENDGAME step (the other half of the loops fix) -----------------------------------
# The per-leg time step is sized to the LEG (a 120 nm leg steps ~1 h ≈ 4 nm in light air), so once
# the boat is a couple of nm from the mark EVERY step overshoots — the search can only orbit the
# mark in quantized hops until one lands close (the drawn "circles at the mark"). ENDGAME shrinks
# each generation's dt as the frontier nears the mark (step ≈ remaining/4 at light-air speeds), so
# the final approach is solved at fine resolution and the closing board fetches via the direct-lay.
ENDGAME_DT = _flag("ROUTE_ENDGAME_DT")
ENDGAME_DIV = float(os.environ.get("ROUTE_ENDGAME_DIV", "16"))   # gen dt = dist-to-mark / DIV (h)
ENDGAME_DT_MIN = float(os.environ.get("ROUTE_ENDGAME_DT_MIN", "0.15"))   # dt floor (h)
# --- sail-aware routing (routing fidelity 2g) --------------------------------
# The route's SPEED is already sail-optimal (the envelope IS the max-over-sails speed), but the boat
# can't peel for free: a sail change costs crew time + a momentary slow-down, and a smart crew HOLDS a
# sub-optimal sail through a marginal crossover rather than peel for 0.2 kn. SAIL_AWARE carries the
# current sail in the isochrone node state and, per step, weighs HOLDING the current sail (its OWN —
# slower-off-optimal — per-sail polar) vs PEELING to the envelope-optimal sail (full speed, but a peel
# cost). So the route peels (like it tacks) only when it genuinely pays, and the sail plan reflects the
# sails actually flown. Off (or no per-sail polars) → routes on the envelope exactly as before.
# --- realized (achievable) speed: helm skill + sea state (routing fidelity 2d lever d, fuzzy baseline)
# The ORC polar is a FLAT-WATER, perfectly-sailed target; the boat never quite makes it. realized_stw =
# polar_stw × HELM factor (boat-level, the crew's % of polar) × WAVE factor (sea state, by point of sail
# — a head sea hurts most, a following sea least). Route on this ACHIEVABLE speed; the gap to the polar
# is a coaching number. Default no-op (helm 1.0 + flat water), so disabled ⇒ behaviour unchanged. The
# wave MODEL lives here (source-agnostic — Zero/Constant now, a real Great-Lakes provider in phase 2).
# CONSERVATIVE first-cut coefficients — deliberately UNDER-correct (better than distorting the route on
# an uncalibrated guess). They're priors to be CALIBRATED from the boat's realized-polar archive (Lab-4
# loop), not trusted as-is. A low-Hs DEADBAND keeps small chop from perturbing the route at all, and the
# FLOOR caps the extreme. Per-meter slopes apply to Hs ABOVE the deadband.
WAVE_HS_DEADBAND = float(os.environ.get("ROUTE_WAVE_HS_DEADBAND", "0.5"))  # m of sea state with NO penalty
WAVE_K_UP = float(os.environ.get("ROUTE_WAVE_K_UP", "0.04"))        # frac speed lost per m Hs (above deadband), beating
WAVE_K_REACH = float(os.environ.get("ROUTE_WAVE_K_REACH", "0.025"))  # reaching
WAVE_K_DOWN = float(os.environ.get("ROUTE_WAVE_K_DOWN", "0.01"))     # running (a following sea barely hurts)
WAVE_FLOOR = float(os.environ.get("ROUTE_WAVE_FLOOR", "0.6"))       # never degrade below this fraction
# RESHAPE GATE — a spatially-varying sea state can RESHAPE the route (bend toward calmer water), not just
# tax the ETA. On a single-model, uncalibrated wave field that reshaping can be spurious, so we gate it:
# `auto` (default) reshapes ONLY when the sea is materially non-uniform over the course (a near-uniform
# field can't be routed around anyway → apply a UNIFORM mean-Hs penalty: honest ETA, geometry unchanged);
# `on` always reshapes (local Hs per node); `off` never does (uniform mean Hs → ETA-only). Conservative:
# under-correcting the geometry beats bending the route on a field we don't trust.
WAVE_RESHAPE_MODE = os.environ.get("ROUTE_WAVE_RESHAPE", "auto").strip().lower()
WAVE_RESHAPE_MIN_SPREAD = float(os.environ.get("ROUTE_WAVE_RESHAPE_MIN_SPREAD", "0.5"))  # m of P10–P90 Hs spread
SAIL_AWARE = _flag("ROUTE_SAIL_AWARE")
PEEL_COST_S = float(os.environ.get("ROUTE_PEEL_COST_S", "90"))      # clock a sail peel costs (honest ETA)
# Prune regularizer (like TACK_PRUNE_S): a one-off per-peel penalty into the path score so the isochrone
# disfavors a course that needs an extra peel for ~equal gain — decoupled from the ETA cost above.
PEEL_PRUNE_S = float(os.environ.get("ROUTE_PEEL_PRUNE_S", "180"))
# Hysteresis: hold a sub-optimal sail until it's THIS fraction off the optimal sail's speed, then peel.
# A dead-band across the crossover so the route doesn't thrash on noise / a wind clocking past a boundary
# (you peel A3→S1 only when the S1 is materially faster, not for the first 0.1 kn). > this → peel.
PEEL_HOLD_TOL = float(os.environ.get("ROUTE_PEEL_HOLD_TOL", "0.06"))
SAIL_DOMAIN_MARGIN = float(os.environ.get("ROUTE_SAIL_DOMAIN_MARGIN", "5"))  # deg slack on a sail's rated TWA domain


# --- geometry ----------------------------------------------------------------
def _wrap180(d):
    return ((d + 180) % 360) - 180


def _hav_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1, math.sqrt(a)))


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _advance(lat, lon, brg, dist_nm):
    b = math.radians(brg)
    return (lat + dist_nm * math.cos(b) / 60.0,
            lon + dist_nm * math.sin(b) / (60.0 * max(0.1, math.cos(math.radians(lat)))))


def _wind_over_water(tws, twd, cset, cdrift):
    """Wind-over-water 2nd-order correction (routing-fidelity 2d lever b). The boat sails relative to
    the WATER it floats on, not the ground; the forecast wind is ground-referenced. In a current the
    water (and boat) drift at (set, drift), so the wind the sails actually feel is the ground wind MINUS
    the current vector — `W_water = W_ground − C`. Returns the over-water (tws, twd) so the polar is
    indexed by the TWA/TWS the boat truly sails. (The displacement still adds the current's drift over
    the ground separately — that's the 1st-order crab already in route_leg.) No current → unchanged."""
    if not WIND_OVER_WATER or cdrift is None or cdrift <= 0.01 or tws is None:
        return tws, twd
    wdir = math.radians((twd + 180.0) % 360)          # wind velocity vector points TOWARD twd+180
    wx, wy = tws * math.sin(wdir), tws * math.cos(wdir)   # (east, north)
    cdir = math.radians(cset % 360)                    # current sets TOWARD `cset`
    ox, oy = wx - cdrift * math.sin(cdir), wy - cdrift * math.cos(cdir)   # W_ground − C
    tws_w = math.hypot(ox, oy)
    twd_w = (math.degrees(math.atan2(ox, oy)) + 180.0) % 360   # back to a FROM bearing
    return tws_w, twd_w


def _xtrack_nm(slat, slon, dlat, dlon, plat, plon):
    """Signed cross-track distance (nm) of point p from the start→dest great circle (sign = side)."""
    R = 3440.065
    d13 = _hav_nm(slat, slon, plat, plon) / R
    if d13 <= 0:
        return 0.0
    dth = math.radians(_bearing(slat, slon, plat, plon) - _bearing(slat, slon, dlat, dlon))
    return math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(dth)))) * R


# --- polars ------------------------------------------------------------------
def _polar_speed(P, tws, twa):
    """Polar boatspeed at (tws, twa), interpolated on the grid (`shared.polar`): linear in TWA within
    a row and in TWS between rows, 0 below the beat angle, scaled toward 0 under the lowest row."""
    return SPOL.lookup(P, tws, twa)


def _crab_onto(bmark, c_perp, speed_at, iters=6, tol_deg=0.05):
    """Heading through the water that holds a ground track of `bmark` against a cross current
    `c_perp` (+ = sets to the right). `speed_at(hdg)` returns (speed, extra) for that heading, or
    None when it can't sail it. Speed depends on the crab angle and vice versa, so iterate; returns
    (hdg, speed, crab, extra) once the heading settles AND its own speed was priced, else None — an
    unsettled last step can land inside the no-go zone with a speed nobody looked up."""
    hdg = bmark
    for _ in range(iters):
        got = speed_at(hdg)
        if got is None or got[0] < 0.3 or abs(c_perp) >= got[0]:
            return None
        crab = math.degrees(math.asin(-c_perp / got[0]))
        new = (bmark + crab) % 360
        if abs(_wrap180(new - hdg)) < tol_deg:
            fin = speed_at(new)
            if fin is None or fin[0] < 0.3 or abs(c_perp) >= fin[0]:
                return None
            crab = math.degrees(math.asin(-c_perp / fin[0]))
            return new, fin[0], crab, fin[1]
        hdg = new
    return None


_VMG_CACHE = {}


def _vmg_twa_cached(P, tws, pos):
    """`shared.polar.vmg_twa` on table `P`, memoized per (table, TWS to 0.1 kn, beat/run) — the
    router asks for it at every node of every generation."""
    rows = SPOL.rows_for(P)
    if not rows or tws is None:
        return None
    k = (id(rows), round(float(tws), 1), pos)
    hit = _VMG_CACHE.get(k)
    if hit is None or hit[0] is not rows:          # the rows ref guards against a recycled id
        if len(_VMG_CACHE) > 20000:
            _VMG_CACHE.clear()
        hit = _VMG_CACHE[k] = (rows, SPOL.vmg_twa(rows, k[1], pos))
    a = hit[1]
    if a is not None and pos == "beat":
        # the cache is keyed to 0.1 kn; never hand back an angle inside THIS wind's no-go edge
        a = max(a, SPOL.min_twa(rows, float(tws)))
    return a


def _wave_factor(hs, twa, coeffs=None):
    """Speed-retention fraction (≤1) for a sea state `hs` (m) at this TWA. CONSERVATIVE by design: a
    DEADBAND (small chop costs nothing), then a gentle linear slope on the excess Hs, scaled by point of
    sail (a head sea slows you most, a following sea least), with a FLOOR so it can never run away.
    hs at/below the deadband → 1.0 (no route distortion from ripples). `coeffs` (the active boat's
    Lab-4-calibrated overlay {hs_deadband,k_up,k_reach,k_down,floor}) overrides the ROUTE_WAVE_* env
    priors per-key; None → all priors (unchanged behaviour)."""
    c = coeffs or {}
    db = c.get("hs_deadband", WAVE_HS_DEADBAND)
    eff = hs - db
    if eff <= 0:
        return 1.0
    a = abs(((twa + 180) % 360) - 180)
    if a < 70:
        k = c.get("k_up", WAVE_K_UP)
    elif a < 120:
        k = c.get("k_reach", WAVE_K_REACH)
    else:
        k = c.get("k_down", WAVE_K_DOWN)
    return max(c.get("floor", WAVE_FLOOR), 1.0 - k * eff)


def _realized_factor(hs, twa, helm, coeffs=None):
    """The fraction of the FLAT-WATER polar the boat actually achieves here = helm skill × sea state."""
    return helm * _wave_factor(hs, twa, coeffs)


def _wave_field_stats(waves, marks):
    """Sample the sea state along the course (mark polyline × a few of the field's own times) → mean /
    P10–P90 spread / peak Hs. The spread is the RESHAPE signal: near-uniform → nothing to route around;
    materially variable → bending toward calmer water is meaningful. None when no real field."""
    if waves is None or not getattr(waves, "loaded", False):
        return None
    pts = [(m[2], m[3]) for m in marks if m[2] is not None and m[3] is not None]
    if len(pts) < 2:
        return None
    times = list(getattr(waves, "epochs", []) or [])
    if times:
        times = times[:: max(1, len(times) // 3)][:3]          # up to 3 representative frames
    else:
        times = [0]
    xs = []
    for i in range(len(pts) - 1):
        for f in range(6):
            g = f / 6.0
            lat = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * g
            lon = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * g
            for t in times:
                try:
                    hs = waves.wave_at(lat, lon, t)
                except Exception:
                    hs = None
                if hs is not None:
                    xs.append(hs)
    if not xs:
        return None
    xs.sort()
    n = len(xs)
    p = lambda q: xs[min(n - 1, max(0, int(q * (n - 1))))]
    return {"mean": round(sum(xs) / n, 3), "spread": round(p(0.9) - p(0.1), 3),
            "peak": round(xs[-1], 3), "n": n}


def _wave_reshape_decision(waves, stats):
    """Whether the sea state may RESHAPE the route (local Hs per node) or only tax the ETA (a uniform
    mean-Hs penalty, geometry unchanged). Returns (uniform_hs_or_None, reshape_bool, note). Gated by
    ROUTE_WAVE_RESHAPE (auto|on|off) + the course's Hs spread."""
    if stats is None:
        return (None, False, "no sea-state field")
    if WAVE_RESHAPE_MODE == "on":
        return (None, True, "reshape forced on (local Hs)")
    if WAVE_RESHAPE_MODE == "off":
        return (stats["mean"], False, f"ETA-only (forced) — uniform {stats['mean']:.1f} m Hs")
    if stats["spread"] >= WAVE_RESHAPE_MIN_SPREAD:            # auto
        return (None, True, f"Waves vary {stats['spread']:.1f} m across the course, so they shape the route.")
    return (stats["mean"], False,
            f"Waves are even across the course ({stats['spread']:.1f} m range), so they only slow the boat.")


def _sail_polar_key(sail):
    """The per-sail polar-curve key for a DISPLAY sail. The ORC cert rates ONE jib (J1); J2/J3 are the
    crew's change-downs sharing the upwind slot, so they map to the J1 curve for SPEED (the polar can't
    distinguish them — a jib change is a wind/depower call, not a speed one). The Code 0 is the same
    kind of overlay in the jib slot's light reaching band (the cert doesn't rate it) — it reads the J1
    curve too, so a J1↔C0 relabel is free, not a routing peel."""
    return "J1" if sail and (sail[0] == "J" or sail == "C0") else sail


def _sail_domains(SP):
    """{key: (twa_min, twa_max)} rated TWA domain per sail — precomputed once so the hot lookup is O(1)
    instead of scanning the curve. Outside this (± a margin) a sail can't be flown (a kite hard on the
    wind), so its speed is 0 → the router is forced to peel."""
    out = {}
    for k, pts in SP.items():
        twas = [p[1] for p in pts]
        if twas:
            out[k] = (min(twas), max(twas))
    return out


def _sail_speed(SP, dom, sail, tws, twa):
    """A specific sail's polar boatspeed at (tws, twa) — interpolated on its own curve like the
    envelope (`shared.polar`), 0 outside its rated TWA domain (± SAIL_DOMAIN_MARGIN, held at the edge
    speed inside the margin). This is the speed of HOLDING `sail` here."""
    key = _sail_polar_key(sail)
    pts = SP.get(key)
    if not pts:
        return 0.0
    lo, hi = dom.get(key, (0.0, 180.0))
    if twa < lo - SAIL_DOMAIN_MARGIN or twa > hi + SAIL_DOMAIN_MARGIN:
        return 0.0
    return SPOL.lookup(pts, tws, twa, lo_margin=SAIL_DOMAIN_MARGIN)


def _point_of_sail(twa):
    return "beat" if twa < 70 else ("reach" if twa < 130 else "run")


def _vmg_headings(P, tws, twd):
    """The VMG-optimal upwind (beat) and downwind (run) headings at this TWS, as compass headings
    relative to TWD. Injected into the heading fan so the router can sail the TRUE best-VMG tacking/
    gybing angle instead of being limited to the nearest coarse-grid heading — the routing-fidelity-2c
    'VMG gate'. Returns up to 4 headings (port+stbd × upwind+downwind)."""
    out = []
    beat = _vmg_twa_cached(P, tws, "beat")
    run = _vmg_twa_cached(P, tws, "run")
    if beat is not None:
        out += [(twd + beat) % 360, (twd - beat) % 360]
    if run is not None:
        out += [(twd + run) % 360, (twd - run) % 360]
    return out


def _vmg_twa(P, tws, pos):
    """The VMG-optimal TWA (deg off the wind) for a beat or run at this TWS — the half-angle of the
    layline cone. `pos` is 'beat' (upwind) or 'run' (downwind); None for reaches or no polar band."""
    if pos not in ("beat", "run"):
        return None
    return _vmg_twa_cached(P, tws, pos)


def _layline_pair(P, tws, twd, pos, mlat, mlon, length_nm):
    """The two laylines into a mark = the VMG-optimal approach corridor (the lines along which the
    boat, sailing its best beat/run angle on each board, just lays the mark). `pos` is 'beat' or
    'run'; returns up to 2 {tack, twa, pts:[[mlat,mlon],[endlat,endlon]]} extending the reciprocal of
    each VMG sailing heading back from the mark. Reaches have no meaningful laylines → []."""
    if pos not in ("beat", "run"):
        return []
    vmg_twa = _vmg_twa_cached(P, tws, pos)
    if vmg_twa is None:
        return []
    length_nm = max(0.5, min(length_nm, LAYLINE_NM))
    out = []
    for tack, h in (("stbd", (twd + vmg_twa) % 360), ("port", (twd - vmg_twa) % 360)):
        elat, elon = _advance(mlat, mlon, (h + 180) % 360, length_nm)
        out.append({"tack": tack, "twa": round(vmg_twa), "pos": pos,
                    "pts": [[round(mlat, 5), round(mlon, 5)], [round(elat, 5), round(elon, 5)]]})
    return out


def _layline_fan(P, tws, twd, pos, bmark, fan):
    """The layline commit (#1) for one node: when the mark (bearing `bmark`) is on or past the
    layline (more than the VMG angle off the wind axis: dead upwind on a beat, dead downwind on a
    run), keep only the headings of the board that lays it. Otherwise `fan` unchanged."""
    half = _vmg_twa(P, tws, pos)
    if half is None:
        return fan
    axis = twd if pos == "beat" else (twd + 180.0)
    if pos == "run":
        # the run VMG angle is a TWA (off the wind, ~150°); `off` below is measured from dead
        # DOWNWIND, so the run layline sits 180 - TWA off the axis. Compared as a TWA it never
        # fired (#65).
        half = 180.0 - half
    off = _wrap180(bmark - axis)
    if abs(off) < half - LAYLINE_COMMIT_EPS:
        return fan
    side = 1.0 if off > 0 else -1.0
    committed = [h for h in fan if (1.0 if _wrap180(h - axis) >= 0 else -1.0) == side]
    if pos == "beat" and committed:
        # …and don't FOOT below the mark: a heading lower than the bearing to the mark drops the
        # boat under the layline and costs a tack back (#30 — the honest no-go zone stopped the old
        # lookup "fetching" the mark at 30°). Only headings the boat can sail (#257): a mark just
        # inside the beat angle left only no-go headings, all at speed 0, and the leg stranded.
        above = [h for h in committed if abs(_wrap180(h - axis)) <= abs(off) + 1.0
                 and _polar_speed(P, tws, abs(_wrap180(h - twd))) >= 0.3]
        committed = above + [bmark] if above else committed
    return committed or [bmark]   # never strand the node — keep the fetch heading


def _beat_dist_nm(P, tws, twd, dmark, bmark):
    """Distance to go on a beat, measured the way the boat must sail it (#66): with the mark inside
    the boat's VMG cone it is the two VMG boards' length, d*cos(off)/cos(vmg angle); past the
    layline it is the straight line, scaled by how much faster that angle sails than the VMG angle
    (so it is time to go at VMG-board speed, continuous at the layline). Plain distance ranks a
    boat that foots toward the mark ahead of one sailing the layline, though footing leaves it
    under the layline and costs two more tacks. Angles and speeds come from the boat's polar."""
    half = _vmg_twa_cached(P, tws, "beat")
    if half is None or dmark <= 0:
        return dmark
    off = abs(_wrap180(bmark - twd))
    if off < half:
        return dmark * math.cos(math.radians(off)) / math.cos(math.radians(half))
    v_half, v_off = _polar_speed(P, tws, half), _polar_speed(P, tws, off)
    if v_half < 0.3 or v_off < 0.3:
        return dmark
    return dmark * v_half / v_off


def _beat_tack_owed(P, tws, twd, bmark, hdg):
    """Does a boat sailing `hdg` on a beat still owe a tack before it can fetch the mark (#66)? Yes
    when the mark is inside its VMG cone (it isn't on a layline yet), or past the layline on the
    other side of the wind. A board that tacked short of the layline and one sailing the layline
    have made the same ground to windward; this is what tells them apart."""
    half = _vmg_twa_cached(P, tws, "beat")
    if half is None or hdg is None:
        return False
    off = _wrap180(bmark - twd)
    if abs(off) < half:
        return True          # even a hair inside the VMG angle can't be fetched on this board
    return (off > 0) != (_wrap180(hdg - twd) > 0)


# --- one leg -----------------------------------------------------------------
def _nearby_wind(wf, lat, lon, epoch, max_nm=None):
    """The wind of the nearest point the forecast covers, within `max_nm` (default FILL_NM), or
    None. Searched on rings (a quarter of the limit apart, every 45°), nearest ring first."""
    p = _nearby_point(wf, lat, lon, epoch, max_nm)
    return wf.wind_at(*p, epoch) if p else None


def _nearby_point(wf, lat, lon, epoch, max_nm=None):
    """The (lat, lon) whose wind `_nearby_wind` borrows, or None: the nearest candidate with wind
    within `max_nm`. Candidates are rings (a quarter of the limit apart, every 45°) plus the
    nearest point on each forecast grid's edge, which the rays can pass either side of (Codex on
    #220: a corner 9.4 nm off sat between them)."""
    max_nm = FILL_NM if max_nm is None else max_nm
    if max_nm <= 0:
        return None
    cands = [_advance(lat, lon, brg, max_nm * k / 4.0)
             for k in range(1, 5) for brg in range(0, 360, 45)]
    cands += _grid_edge_points(wf, lat, lon)
    best = None
    for d, p in sorted((_hav_nm(lat, lon, *p), p) for p in cands):
        if d > max_nm:
            break
        if wf.wind_at(*p, epoch):
            best = p
            break
    return best


def _grid_edge_points(wf, lat, lon):
    """The point of each forecast series' grid box nearest (lat, lon), nudged just inside it."""
    out = []
    for frames in (getattr(wf, "series", None) or {}).values():
        if not frames:
            continue
        f = frames[0]
        try:
            la0, la1 = float(f.lat.min()), float(f.lat.max())
            lo0, lo1 = float(f.lon.min()), float(f.lon.max())
        except (AttributeError, TypeError, ValueError):
            continue
        eps = 1e-6
        out.append((min(max(lat, la0 + eps), la1 - eps), min(max(lon, lo0 + eps), lo1 - eps)))
    return out


def route_leg(wf, P, slat, slon, t0, dlat, dlon, deadline=None,
              obstacles=None, capture=False, hstep=HSTEP, dt_cap=1.0, cur=None,
              sail_polars=None, jib_crossovers=None, start_sail=None,
              waves=None, helm_factor=1.0, wave_coeffs=None, wave_uniform_hs=None,
              maneuver_prune_mult=1.0, sail_config=None, xo=None):
    """Isochrone-optimal path from (slat,slon)@t0 to (dlat,dlon). Returns dict with path/eta.

    `obstacles` (an ObstacleField) makes the fan reject any heading whose step would cut across land,
    an island, or a race exclusion zone — so the route sails AROUND obstacles instead of through them.
    `cur` (a CurrentField) carries the boat over the GROUND: each step advances by the boat's
    water-velocity (polar speed on its heading) PLUS the current's drift, so the route crabs into a
    cross stream and ETAs reflect a fair/foul current. None / ZeroCurrent = no current (unchanged).
    `capture` records each generation's frontier (the equal-time isochrone) and emits down-sampled
    `isochrones` polylines — the exploration the single route summarizes, drawn on the Gameplan map.
    `hstep` (heading-fan degrees) + `dt_cap` (per-leg step ceiling, h) come from the resolution
    selector — Fine = smaller both (sharper, slower); Fast = larger (quicker, coarser).
    `sail_polars` (+ `jib_crossovers`, `start_sail`) enable SAIL-AWARE routing (2g): the node carries
    the current sail, each step holds it (at its own per-sail speed) or PEELS to the envelope-optimal
    sail (full speed + a peel cost), so the route peels only when it pays and the sail plan is real.
    `start_sail` = the sail the boat is already carrying into this leg (continuity across marks).
    No invented wind (#203): where the forecast has nothing, the nearest covered point's wind within
    FILL_NM stands in (`wind_filled` counts the path nodes sailed from on it); past that the route doesn't sail from that node,
    so a leg that needs it comes back `reached: False`."""
    direct = _hav_nm(slat, slon, dlat, dlon)
    SP = sail_polars or {}
    sail_aware = SAIL_AWARE and bool(SP)
    dom = _sail_domains(SP) if sail_aware else {}
    # realized (achievable) speed = helm × sea state; only do the work when it actually bites. Helm may
    # be >1.0 (boat rated soft / sails above the cert), so trigger on any departure from 1.0, not just <1.
    realized_on = (abs(helm_factor - 1.0) > 1e-3) or (waves is not None and getattr(waves, "loaded", False))

    def _hs(lat, lon, t):
        """Sea state to degrade speed with. When `wave_uniform_hs` is set (the reshape gate chose
        ETA-only), every node sees the SAME mean Hs → the wave penalty taxes the ETA without bending
        the geometry; otherwise the LOCAL Hs (route may reshape toward calmer water)."""
        if wave_uniform_hs is not None:
            return wave_uniform_hs
        return waves.wave_at(lat, lon, t) if waves is not None else 0.0

    def sail_step(cur_sail, tws, twa, sp_env):
        """Decide the sail + speed for one step from a node currently flying `cur_sail`. Returns
        (sail, speed, is_peel). HOLD the current sail (its own, slower-off-optimal speed) until it's
        PEEL_HOLD_TOL off the optimal sail, then PEEL to the optimal sail at full (envelope) speed +
        a peel cost. A jib change-down (same aero family) is a free relabel, not a routing peel."""
        opt = sailplan.optimal_sail(tws, twa, jib_crossovers, sail_config, xo=xo)
        if sail_aware and opt is not None and _sail_polar_key(opt) not in dom:
            # gear-loss (exclude_sails): the cert-optimal sail has no curve in the FILTERED inventory
            # — the honest call is the fastest REMAINING sail (the rebuilt envelope already prices it)
            opt = max(SP, key=lambda s: _sail_speed(SP, dom, s, tws, twa), default=None)
        if not sail_aware or opt is None:
            return (opt or cur_sail, sp_env, False)          # sail-aware off → envelope speed, no peel
        if cur_sail is None or _sail_polar_key(cur_sail) == _sail_polar_key(opt):
            return (opt, sp_env, False)                      # initial set, or same family → relabel free
        hold = _sail_speed(SP, dom, cur_sail, tws, twa)
        if hold > 0.3 and hold >= sp_env * (1.0 - PEEL_HOLD_TOL):
            return (cur_sail, hold, False)                   # current sail still within tolerance → HOLD
        return (opt, sp_env, True)                           # too slow / infeasible here → PEEL
    dt_h = min(dt_cap, max(0.15, direct / 40.0))       # fixed per-leg step (equal-time isochrone)
    max_steps = 600
    headings = list(range(0, 360, max(1, int(hstep))))
    blocked_hits = 0

    def wind(lat, lon, epoch):
        return wf.wind_at(lat, lon, epoch) or _nearby_wind(wf, lat, lon, epoch)

    def wind_along(lat0, lon0, t_a, lat1, lon1, t_b):
        """Wind (forecast or borrowed) all along a board, its end included, sampled every ~2 nm.
        A board that leaves the fill limit isn't sailed (Codex on #220). A leg arrives only two
        ways: this closing board, or a fan step landing within 0.05 nm of the mark, which needs
        wind at the mark too. Fan steps aren't sampled (that made routing ~9x slower): a node
        with no wind just never sails on."""
        n = max(1, int(math.ceil(_hav_nm(lat0, lon0, lat1, lon1) / 2.0)))
        return all(wind(lat0 + (lat1 - lat0) * k / n, lon0 + (lon1 - lon0) * k / n,
                        t_a + (t_b - t_a) * k / n) for k in range(1, n + 1))

    def board_borrowed(lat0, lon0, t_a, lat1, lon1, t_b):
        """Did any of `wind_along`'s samples on this board borrow nearby wind (no forecast there)?
        The closing board isn't a path node, so `wind_filled` counts it from this (#221)."""
        n = max(1, int(math.ceil(_hav_nm(lat0, lon0, lat1, lon1) / 2.0)))
        return not all(wf.wind_at(lat0 + (lat1 - lat0) * k / n, lon0 + (lon1 - lon0) * k / n,
                                  t_a + (t_b - t_a) * k / n) for k in range(1, n + 1))

    # Overstand axis (#2 layline gate): on a beat the mark is to windward, on a run to leeward — in
    # both cases sailing PAST the mark along the wind axis is overstanding (the "go past then double
    # back" the user sees near a mark). Classify the leg from the wind AT THE MARK (which sets the
    # laylines) and build a unit (north,east) vector pointing the OVERSTAND way; reaches → no gate.
    _wm = wind(dlat, dlon, t0)
    _twd_m = _wm[1] if _wm else None
    _pos0 = (_point_of_sail(abs(_wrap180(_bearing(slat, slon, dlat, dlon) - _twd_m)))
             if _wm else None)                 # no wind at the mark: no layline gate
    leg_axis = None
    if LAYLINE_GATE and _pos0 in ("beat", "run"):
        wb = _twd_m if _pos0 == "beat" else (_twd_m + 180.0)        # windward (beat) / leeward (run) bearing
        leg_axis = (math.cos(math.radians(wb)), math.sin(math.radians(wb)))
    coslat = math.cos(math.radians(dlat))

    def expand(node, hdgs, tws, twd, dt_h, cand, monotone=True):
        """Fan `hdgs` from `node`, advancing each by its polar speed; keep the farthest-along candidate
        per bearing sector (the classic isochrone prune — it routes through shifts best and converges
        fast). Candidates that have sailed PAST the mark on the up/down-wind axis are rejected (the
        layline gate, #2) so the route lays the mark instead of overstanding and doubling back; and a
        candidate that gives back more than BACKTRACK_NM of its path's best progress toward the mark
        is rejected (the MONOTONE loop gate) unless `monotone` is off (the per-node fail-open).
        Returns (n_placed, n_blocked, n_backtracked) — 0-placed means this node is boxed in."""
        placed = blocked = back = 0
        for hdg in hdgs:
            twa = abs(_wrap180(hdg - twd))
            sp_env = _polar_speed(P, tws, twa)
            if sp_env < 0.3:
                continue
            # sail-aware: hold the current sail (its own per-sail speed) or peel to the optimal sail.
            sail, sp, is_peel = sail_step(node.get("sail"), tws, twa, sp_env)
            # realized: degrade to ACHIEVABLE speed (helm skill × sea state at this node/angle)
            if realized_on:
                hs = _hs(node["lat"], node["lon"], node["t"])
                sp *= _realized_factor(hs, twa, helm_factor, wave_coeffs)
            if sp < 0.3:
                continue
            step_nm = sp * dt_h
            is_tack = (node["hdg"] is not None and
                       (_wrap180(hdg - twd) > 0) != (_wrap180(node["hdg"] - twd) > 0))
            # maneuver cost. Legacy (#2c): a one-step distance haircut that only nudges this step's prune.
            # Cumulative (#2): the cost instead accrues into a per-path penalty `pen` (and the node ETA),
            # so a path that has tacked many times genuinely loses ground in the prune ranking + clock —
            # this is what actually suppresses the high-frequency weave, not a per-step ~5% nudge.
            pen = node.get("pen", 0.0)
            tk = node.get("tk", 0)
            pl = node.get("pl", 0)
            tack_s = 0.0
            # `maneuver_prune_mult` (Playbook v2 Phase C, low-maneuver variant): scales the PRUNE
            # penalty only — the search avoids maneuvers ×N harder, but the clock cost of a tack/peel
            # stays real, so the low-maneuver route's ETA is honestly comparable to the nominal's.
            if is_tack and TACK_COST_S > 0:
                if TACK_CUMULATIVE:
                    pen += sp * (TACK_PRUNE_S * maneuver_prune_mult / 3600.0)   # prune regularizer
                    tack_s = TACK_COST_S                  # realistic ETA cost — don't inflate predicted time
                    tk += 1
                else:
                    step_nm = max(0.0, step_nm - sp * (TACK_COST_S * maneuver_prune_mult / 3600.0))
            # peel cost (2g) — mirrors the cumulative tack cost: a one-off prune penalty + honest ETA per
            # sail change, so the isochrone disfavors a course needing an extra peel and predicted times
            # carry the change-down. The PEEL_HOLD_TOL hysteresis already stops boundary thrash.
            peel_s = 0.0
            if is_peel and PEEL_COST_S > 0:
                pen += sp * (PEEL_PRUNE_S * maneuver_prune_mult / 3600.0)
                peel_s = PEEL_COST_S
                pl += 1
            nlat, nlon = _advance(node["lat"], node["lon"], hdg, step_nm)   # displacement through the water
            if cur is not None:                                              # + drift over the ground (set/drift)
                cset, cdrift = cur.current_at(node["lat"], node["lon"], node["t"])
                if cdrift > 0.01:
                    nlat, nlon = _advance(nlat, nlon, cset, cdrift * dt_h)
            if obstacles and obstacles.crosses(node["lat"], node["lon"], nlat, nlon):
                blocked += 1
                continue
            dmark_new = _hav_nm(nlat, nlon, dlat, dlon)
            # layline gate: drop a candidate near the mark that has sailed PAST it along the wind
            # axis. Only near it (#256): farther off that can be the only way round an island.
            if leg_axis is not None and dmark_new <= OVERSTAND_RADIUS_NM:
                wp = (nlat - dlat) * 60.0 * leg_axis[0] + (nlon - dlon) * 60.0 * coslat * leg_axis[1]
                if wp > OVERSTAND_NM:
                    blocked += 1
                    continue
            # MONOTONE loop gate: never give back more than BACKTRACK_NM of this path's best
            # progress toward the mark — kills the light-air rotating-wind loops/scribbles.
            dmin = min(node.get("dmin", direct), dmark_new)
            if monotone and MONOTONE and dmark_new > node.get("dmin", direct) + BACKTRACK_NM:
                back += 1
                continue
            place(cand, node, nlat, nlon, node["t"] + dt_h * 3600 + tack_s + peel_s, hdg,
                  pen, tk, sail, pl, dmin, dmark_new, tws, twd, sp)
            placed += 1
        return placed, blocked, back

    def place(cand, parent, nlat, nlon, t, hdg, pen, tk, sail, pl, dmin, dmark_new, tws, twd, sp,
              calm=False):
        """Offer one candidate node to this generation's prune (keeps the best per bucket)."""
        rng = _hav_nm(slat, slon, nlat, nlon)
        # PRUNE KEY + SCORE. DMG_PRUNE (root-cause fix): rank by distance MADE GOOD toward the mark,
        # bucketed by CROSS-TRACK LANE off the start→mark rhumb — one leading node per lane, so a beat
        # lays the layline in one tack instead of ballooning sideways and weaving. Legacy paths:
        # #3 position-cell near the mark, else the classic bearing-from-start sector; both score by
        # distance-from-start. Score is net of the accumulated maneuver penalty (#2).
        if DMG_PRUNE:
            sec = ("dmg", round(_xtrack_nm(slat, slon, dlat, dlon, nlat, nlon) / LANE_NM))
            togo = dmark_new
            if BEAT_TOGO and _pos0 == "beat":
                bm = _bearing(nlat, nlon, dlat, dlon)
                togo = _beat_dist_nm(P, tws, twd, dmark_new, bm)
                if TACK_CUMULATIVE and TACK_COST_S > 0 and _beat_tack_owed(P, tws, twd, bm, hdg):
                    # the tack this node still owes, priced like one it made (#66)
                    togo += sp * (TACK_PRUNE_S * maneuver_prune_mult / 3600.0)
            score = (direct - togo) - pen
        elif MARK_POS_PRUNE and dmark_new <= MARK_PRUNE_NM:
            cell = MARK_PRUNE_CELL_NM / 60.0
            sec = ("p", round(nlat / cell), round(nlon / max(1e-6, cell * coslat)))
            score = rng - pen
        else:
            sec = round(_bearing(slat, slon, nlat, nlon) / SECTOR)
            score = rng - pen
        if sec not in cand or score > cand[sec]["rng_eff"]:
            cand[sec] = {"lat": nlat, "lon": nlon, "t": t,
                         "parent": parent, "hdg": hdg, "rng": rng, "rng_eff": score,
                         "pen": pen, "tk": tk, "sail": sail, "pl": pl, "dmin": dmin, "calm": calm}

    def price(sail_in, node, tws, twa):
        """(sail, realized speed, is_peel) for sailing `twa` from `node` carrying `sail_in`, priced
        like a step in `expand`; None when the boat can't sail it."""
        sp_env = _polar_speed(P, tws, twa)
        if sp_env < 0.3:
            return None
        sail, sp, is_peel = sail_step(sail_in, tws, twa, sp_env)
        if realized_on:
            sp *= _realized_factor(_hs(node["lat"], node["lon"], node["t"]), twa, helm_factor, wave_coeffs)
        return (sail, sp, is_peel) if sp >= 0.3 else None

    def layline_tack(node, tws, twd, dt_h, cand):
        """LAYLINE TACK (#66): on a beat, the fan's equal-time steps put the tack where a step ends,
        not on the layline. Tacking a little short leaves the boat under the layline, and the mark
        then drifts toward head-to-wind: two more tacks to fetch it. Overstanding costs distance.
        So when this step's VMG board crosses the other board's layline, also offer the step that
        tacks exactly there and spends the rest of the step on the other board's VMG heading. Same
        clock and prune price as a fan step that tacks (the tack sits inside the step). The angle is
        the boat's own VMG angle from its polar at this wind; nothing here is boat-specific. In a
        current the laylines over the ground move, so the candidate isn't offered there."""
        half = _vmg_twa(P, tws, "beat")
        if half is None:
            return
        if cur is not None and cur.current_at(node["lat"], node["lon"], node["t"])[1] > 0.01:
            return
        dx = (dlon - node["lon"]) * 60.0 * math.cos(math.radians((dlat + node["lat"]) / 2.0))
        dy = (dlat - node["lat"]) * 60.0
        if math.hypot(dx, dy) < 0.05:
            return
        if node["hdg"] is None:
            sides = (1.0, -1.0)
        else:
            sides = (1.0 if _wrap180(node["hdg"] - twd) > 0 else -1.0,)
        for side in sides:
            h1 = (twd + side * half) % 360
            # tack a hair past the layline: a node even 0.1° under it can't fetch at the VMG angle
            h2m = (twd - side * (half + LAYLINE_TACK_MARGIN)) % 360
            u1 = (math.sin(math.radians(h1)), math.cos(math.radians(h1)))
            u2 = (math.sin(math.radians(h2m)), math.cos(math.radians(h2m)))
            det = u1[0] * u2[1] - u1[1] * u2[0]
            if abs(det) < 1e-6:
                continue
            s = (dx * u2[1] - dy * u2[0]) / det          # nm on h1 to the other board's layline
            r = (u1[0] * dy - u1[1] * dx) / det          # nm from there to the mark on h2
            if s <= 1e-3 or r <= 1e-3:
                continue                                 # mark not between the laylines
            a = price(node.get("sail"), node, tws, half)
            if a is None:
                continue
            sail1, sp1, peel1 = a
            t1 = s / sp1
            if t1 >= dt_h:
                continue                                 # the layline is beyond this step
            mlat, mlon = _advance(node["lat"], node["lon"], h1, s)
            if obstacles and obstacles.crosses(node["lat"], node["lon"], mlat, mlon):
                continue
            t_mid = node["t"] + t1 * 3600 + (PEEL_COST_S if peel1 and PEEL_COST_S > 0 else 0.0)
            if cur is not None and cur.current_at(mlat, mlon, t_mid)[1] > 0.01:
                continue                                 # current at the tack point: not offered
            # the tack point is a node the boat sails on from: it needs wind of its own (#203),
            # and the second board is priced on that wind
            w2 = wind(mlat, mlon, t_mid)
            if not w2:
                continue
            tws2, twd2 = w2
            half2 = _vmg_twa(P, tws2, "beat")
            if half2 is None:
                continue
            h2 = (twd2 - side * half2) % 360
            mid = {"lat": mlat, "lon": mlon, "t": t_mid, "parent": node, "hdg": h1, "sail": sail1,
                   "pen": node.get("pen", 0.0), "tk": node.get("tk", 0), "pl": node.get("pl", 0)}
            b = price(sail1, mid, tws2, half2)
            if b is None:
                continue
            sail2, sp2, peel2 = b
            pen = node.get("pen", 0.0)
            tk, pl = node.get("tk", 0), node.get("pl", 0)
            extra_s = 0.0
            for peel, sp in ((peel1, sp1), (peel2, sp2)):
                if peel and PEEL_COST_S > 0:
                    pen += sp * (PEEL_PRUNE_S * maneuver_prune_mult / 3600.0)
                    extra_s += PEEL_COST_S
                    pl += 1
            tack_s = 0.0
            if TACK_COST_S > 0:                          # priced like a tacking fan step in expand
                pen += sp2 * (TACK_PRUNE_S * maneuver_prune_mult / 3600.0)
                tack_s = TACK_COST_S
                tk += 1
            d2 = min(r, (dt_h - t1) * sp2)
            nlat, nlon = _advance(mlat, mlon, h2, d2)
            if obstacles and obstacles.crosses(mlat, mlon, nlat, nlon):
                continue
            dmark_mid = _hav_nm(mlat, mlon, dlat, dlon)
            dmark_new = _hav_nm(nlat, nlon, dlat, dlon)
            dmin = min(node.get("dmin", direct), dmark_mid, dmark_new)
            mid["dmin"] = min(node.get("dmin", direct), dmark_mid)
            t_end = node["t"] + (t1 + d2 / sp2) * 3600 + tack_s + extra_s
            place(cand, mid, nlat, nlon, t_end, h2, pen, tk, sail2, pl, dmin, dmark_new, tws2, twd2, sp2)

    def becalmed(node, tws, twd):
        """True when no heading makes way here (every one is under 0.3 kn, priced like a step in
        `expand`: held sail, helm, sea state). Not the same as boxed in by land: nothing was tried."""
        return not any(price(node.get("sail"), node, tws, abs(_wrap180(h - twd))) for h in headings)

    def hold(node, tws, twd, dt_h, cand):
        """#326 BECALMED: the boat can't sail anywhere from `node`, so it waits out this step where
        it is, drifting with the current, instead of the node being dropped. A calm over the whole
        front used to kill the leg ("no clear heading") even when the forecast filled in a new
        breeze a few hours later. The wait is real clock time, so the ETA carries it. No tack, no
        sail change, no maneuver cost. A drift that would carry the boat across an obstacle drops
        the node, as before: the router doesn't invent an anchor."""
        nlat, nlon = node["lat"], node["lon"]
        if cur is not None:
            cset, cdrift = cur.current_at(nlat, nlon, node["t"])
            if cdrift > 0.01:
                dlat_, dlon_ = _advance(nlat, nlon, cset, cdrift * dt_h)
                if obstacles and obstacles.crosses(nlat, nlon, dlat_, dlon_):
                    return
                nlat, nlon = dlat_, dlon_
        dmark_new = _hav_nm(nlat, nlon, dlat, dlon)
        place(cand, node, nlat, nlon, node["t"] + dt_h * 3600, node["hdg"], node.get("pen", 0.0),
              node.get("tk", 0), node.get("sail"), node.get("pl", 0),
              min(node.get("dmin", direct), dmark_new), dmark_new, tws, twd, 0.0, calm=True)

    def direct_lay(node, tws, twd, dmark, bmark):
        """The closing board straight to the mark, priced like any other step in `expand`: the held or
        peeled sail, the realized factor (helm × sea state), and the current — the boat crabs so its
        track over the ground runs straight at the mark. Returns {gs (kn over the ground toward the
        mark), hdg (through the water), sail, peel} or None when the mark can't be fetched."""
        cset = cdrift = 0.0
        if cur is not None:
            cset, cdrift = cur.current_at(node["lat"], node["lon"], node["t"])
            if cdrift <= 0.01:
                cdrift = 0.0
        c_par = cdrift * math.cos(math.radians(cset - bmark))     # current along the track to the mark
        c_perp = cdrift * math.sin(math.radians(cset - bmark))    # …and across it (+ = sets to the right)

        def speed_at(hdg):
            twa = abs(_wrap180(hdg - twd))
            sp_env = _polar_speed(P, tws, twa)
            if sp_env < 0.3:
                return None
            sail, sp, is_peel = sail_step(node.get("sail"), tws, twa, sp_env)
            if realized_on:
                sp *= _realized_factor(_hs(node["lat"], node["lon"], node["t"]), twa, helm_factor, wave_coeffs)
            return sp, (sail, is_peel)

        lay = _crab_onto(bmark, c_perp, speed_at)
        if lay is None:
            return None
        hdg, sp, crab, (sail, is_peel) = lay
        gs = sp * math.cos(math.radians(crab)) + c_par
        return {"gs": gs, "hdg": hdg, "sail": sail, "peel": is_peel} if gs > 0.3 else None

    start = {"lat": slat, "lon": slon, "t": t0, "parent": None, "hdg": None, "sail": start_sail}
    frontier = [start]
    reached = None
    snaps = []
    stop = "steps"                 # why a leg that doesn't arrive stopped (#326); see the loop
    calm_end = False               # …and whether the whole front was waiting out a calm then
    for _ in range(max_steps):
        if deadline and time.time() > deadline:
            stop = "time_budget"
            break
        cand = {}
        # ADAPTIVE ENDGAME: shrink this generation's step as the frontier nears the mark, so the
        # final approach is solved at fine resolution instead of orbiting in leg-sized hops. The
        # direct-lay shortcut below keeps the FULL dt_h window (it is exact — one true-speed
        # segment), so a fetchable mark still closes early.
        gen_dt = dt_h
        if ENDGAME_DT and frontier:
            d_best = min(_hav_nm(n["lat"], n["lon"], dlat, dlon) for n in frontier)
            gen_dt = min(dt_h, max(ENDGAME_DT_MIN, d_best / ENDGAME_DIV))
        local = []
        for node in frontier:
            w = wind(node["lat"], node["lon"], node["t"])
            if not w:
                continue               # no forecast in reach: the route doesn't sail from here
            tws, twd = w
            if cur is not None:            # 2d lever b: the sails feel the wind over the WATER
                _cs, _cd = cur.current_at(node["lat"], node["lon"], node["t"])
                tws, twd = _wind_over_water(tws, twd, _cs, _cd)
            local.append((node, tws, twd, _hav_nm(node["lat"], node["lon"], dlat, dlon),
                          _bearing(node["lat"], node["lon"], dlat, dlon)))
        # DIRECT LAY: can any node fetch the mark within one full step? Take the EARLIEST arrival over
        # the whole frontier (not the first node that happens to fetch), priced like a normal step:
        # held/peeled sail, helm × sea state, current, and the clock cost of a tack or peel onto it.
        for node, tws, twd, dmark, bmark in local:
            lay = direct_lay(node, tws, twd, dmark, bmark)
            if lay is None or dmark > lay["gs"] * dt_h or (
                    obstacles and obstacles.crosses(node["lat"], node["lon"], dlat, dlon)):
                continue
            is_peel = lay["peel"] and PEEL_COST_S > 0
            is_tack = (node["hdg"] is not None and
                       (_wrap180(lay["hdg"] - twd) > 0) != (_wrap180(node["hdg"] - twd) > 0))
            t_arr = (node["t"] + (dmark / lay["gs"]) * 3600 + (PEEL_COST_S if is_peel else 0.0)
                     + (TACK_COST_S if is_tack and TACK_CUMULATIVE else 0.0))
            if (reached is None or t_arr < reached["t"]) and wind_along(
                    node["lat"], node["lon"], node["t"], dlat, dlon, t_arr):
                reached = {"lat": dlat, "lon": dlon, "t": t_arr,
                           "parent": node, "hdg": lay["hdg"], "sail": lay["sail"],
                           "pl": node.get("pl", 0) + (1 if is_peel else 0),
                           "board_filled": board_borrowed(node["lat"], node["lon"], node["t"],
                                                          dlat, dlon, t_arr)}
        if reached:
            break
        for node, tws, twd, dmark, bmark in local:
            # CONE GATE: only fan headings within a wide cone of the bearing-to-mark (drops the
            # truly-backward third), plus the VMG-optimal angles (always kept). If the whole cone is
            # obstacle-blocked here, reopen the FULL fan so avoidance can still detour around land.
            vmg = _vmg_headings(P, tws, twd)
            coned = [h for h in headings if abs(_wrap180(h - bmark)) <= ROUTE_CONE_DEG]
            fan = coned + vmg
            # LAYLINE COMMIT (#1): if this node can already lay the mark — the mark bears more than the
            # VMG angle off the LOCAL wind axis (dead-upwind on a beat / dead-downwind on a run) — there
            # is no reason to keep offering the opposite tack; drop it so the boat fetches the layline
            # instead of free-tacking up to the mark. Between the laylines both boards stay open (the
            # strategic side choice is preserved). Re-evaluated every generation against the node-local
            # wind, so a genuine shift re-opens the layline rather than locking a stale one.
            if LAYLINE_COMMIT and _pos0 in ("beat", "run") and dmark <= LAYLINE_COMMIT_NM:
                fan = _layline_fan(P, tws, twd, _pos0, bmark, fan)
            placed, blocked, back = expand(node, fan, tws, twd, gen_dt, cand)
            if LAYLINE_TACK and TACK_CUMULATIVE and _pos0 == "beat":
                layline_tack(node, tws, twd, gen_dt, cand)
            if placed == 0 and blocked > 0 and obstacles is not None:
                placed, blocked, back = expand(node, headings + vmg, tws, twd, gen_dt, cand)
            if placed == 0 and back > 0:
                # every heading gave back too much ground (an obstacle detour / a foul stream really
                # does need to retreat) — fail open for THIS node so the boat is never stranded
                _p, blocked, _b = expand(node, headings + vmg, tws, twd, gen_dt, cand, monotone=False)
            blocked_hits += blocked
            if placed == 0 and blocked == 0 and back == 0 and becalmed(node, tws, twd):
                hold(node, tws, twd, gen_dt, cand)
        if reached:
            break
        if not cand:
            # why the search died (#326): no forecast wind for any node, else every heading blocked
            # A front that had wind at the leg start but none now: the forecast ended ("forecast_end")
            stop = ("blocked" if local else
                    "forecast_end" if any(wind(n["lat"], n["lon"], t0) for n in frontier) else "no_wind")
            calm_end = bool(frontier) and all(n.get("calm") for n in frontier)
            break
        frontier = list(cand.values())
        if capture:
            snaps.append(frontier)
        best = min(frontier, key=lambda n: _hav_nm(n["lat"], n["lon"], dlat, dlon))
        if _hav_nm(best["lat"], best["lon"], dlat, dlon) < 0.05 and wind(dlat, dlon, best["t"]):
            reached = best
            break
    # #31: a leg that never closes on its target (time budget out, or every heading blocked) returns
    # its NEAREST node — flagged `reached: False` so the caller stops the course there instead of
    # chaining the next leg from the mark (a free teleport, possibly across land).
    arrived = reached is not None
    if not reached:
        reached = min(frontier, key=lambda n: _hav_nm(n["lat"], n["lon"], dlat, dlon))

    path, node, into, sails, peels = [], reached, [], [], 0
    calm_s = 0.0                   # time the path spends waiting out a calm (#326)
    while node is not None:
        if node.get("calm") and node["parent"] is not None:
            calm_s += node["t"] - node["parent"]["t"]
        path.append({"lat": round(node["lat"], 5), "lon": round(node["lon"], 5), "t": node["t"]})
        sails.append(node.get("sail"))
        peels = max(peels, node.get("pl", 0))     # modeled peels accrue monotonically along the path
        into.append(None if node.get("calm") else node["hdg"])   # heading sailed INTO this point
        node = node["parent"]
    path.reverse(); into.reverse(); sails.reverse()
    # the SAILED segments as (k, heading leaving path[k]); a calm wait (#326) isn't sailed, so it
    # isn't a heading, a tack or a realized-speed sample, and the headings stay on their own points
    segs = [(k, into[k + 1]) for k in range(len(path) - 1) if into[k + 1] is not None]
    hdgs = [h for _k, h in segs]
    # sail track (2g): collapse consecutive same-DISPLAY sails into the runs actually flown, recording
    # where each begins (sails[k] is the sail flown INTO path[k]). `peels` is the count of MODELED
    # aerodynamic peels (the `pl` the search accrued — jib change-downs J1→J2 share a curve and aren't
    # routing peels). The whole-route plan is built from these in optimize_course, so the sail plan
    # reflects the sailed sails, not a post-hoc per-leg guess.
    sail_track = []
    for k, s in enumerate(sails):
        if s is None:
            continue
        if not sail_track or sail_track[-1]["sail"] != s:
            sail_track.append({"sail": s, "lat": path[k]["lat"], "lon": path[k]["lon"],
                               "t": path[k]["t"]})
    sail_end = sail_track[-1]["sail"] if sail_track else start_sail
    sailed = sum(_hav_nm(path[i]["lat"], path[i]["lon"], path[i + 1]["lat"], path[i + 1]["lon"])
                 for i in range(len(path) - 1))
    # tacks/gybes = genuine port↔starboard crossings along the path. Each `segs` heading is the one
    # sailed on the segment LEAVING `path[k]` at `path[k]["t"]`, so classify the board against the wind LOCAL to
    # that point/time — NOT a single frozen leg-start wind. On a leg where the breeze clocks (e.g. a long
    # light beat) the stale-wind version miscounts every shift-following heading swing as a tack and badly
    # over-reports; sampling local wind makes the count the real maneuver tally (tacks upwind / gybes down).
    tacks = 0
    prev_side = None
    rf_sum = hs_sum = 0.0          # realized-factor + sea-state accumulators (the coaching number)
    for k, h in segs:
        p = path[k]
        w = wind(p["lat"], p["lon"], p["t"])
        if not w:
            continue
        side = "stbd" if _wrap180(w[1] - h) > 0 else "port"
        if prev_side and side != prev_side:
            tacks += 1
        prev_side = side
        if realized_on:
            hs = _hs(p["lat"], p["lon"], p["t"])
            rf_sum += _realized_factor(hs, abs(_wrap180(h - w[1])), helm_factor, wave_coeffs)
            hs_sum += hs
    realized_mean = round(rf_sum / len(hdgs), 3) if (realized_on and hdgs) else 1.0
    hs_mean = round(hs_sum / len(hdgs), 2) if (realized_on and hdgs) else 0.0
    # equal-time isochrone curves (down-sampled) — each generation's frontier sorted by bearing from
    # the leg start, so it draws as an arc fanning outward; ~ISO_CURVES per leg, ≤ISO_PTS pts each.
    isochrones = []
    if capture and snaps:
        stride = max(1, len(snaps) // max(1, ISO_CURVES))
        for i in range(0, len(snaps), stride):
            snap = sorted(snaps[i], key=lambda nd: _bearing(slat, slon, nd["lat"], nd["lon"]))
            if len(snap) > ISO_PTS:
                step = len(snap) / float(ISO_PTS)
                snap = [snap[int(k * step)] for k in range(ISO_PTS)]
            poly = [[round(nd["lat"], 4), round(nd["lon"], 4)] for nd in snap]
            if len(poly) >= 2:
                isochrones.append(poly)
    return {"path": path, "eta": reached["t"], "sailed_nm": round(sailed, 2),
            "reached": arrived,
            # why it stopped short (#326): "blocked" (every heading blocked), "no_wind" (no forecast
            # wind for any node, "forecast_end" when it ran out in time), "time_budget", "steps";
            # None if arrived. `calm_end`: the whole front was becalmed when the wind ran out.
            "stop": None if arrived else stop,
            "calm_end": (not arrived) and calm_end,
            "becalmed_h": round(calm_s / 3600.0, 1),
            "short_nm": 0.0 if arrived else round(_hav_nm(reached["lat"], reached["lon"], dlat, dlon), 2),
            "direct_nm": round(direct, 2), "tacks": tacks,
            "first_heading": round(hdgs[0]) if hdgs else None,
            "blocked_steps": blocked_hits, "isochrones": isochrones,
            "sail_track": sail_track, "peels": peels, "sail_end": sail_end,
            "realized_factor": realized_mean, "hs_mean": hs_mean,
            # path nodes sailed from on borrowed nearby wind (the forecast itself had none there),
            # plus the closing board when it borrowed on the way to the mark (#221)
            "wind_filled": (sum(1 for p in path[:-1] if not wf.wind_at(p["lat"], p["lon"], p["t"]))
                            + (1 if arrived and reached.get("board_filled") else 0))}


def _rounding_offset(plat, plon, mlat, mlon, side, nm=None):
    """#3 rounding side: a small standoff point to the correct side of a port/starboard mark, so the
    route passes it on the legal side instead of cutting either way. The boat leaving a mark to PORT
    keeps it on its port hand → it passes to the right of the inbound course → offset 90° right of the
    approach bearing (left for starboard). Returns (lat, lon); the real mark is still recorded for
    display. `side` other than port/starboard → no offset (gates pass between; 'none' marks unchanged)."""
    if side not in ("port", "starboard"):
        return mlat, mlon
    nm = ROUND_OFFSET_NM if nm is None else nm
    b_in = _bearing(plat, plon, mlat, mlon)
    off = (b_in + 90.0) if side == "port" else (b_in - 90.0)
    return _advance(mlat, mlon, off, nm)


def _rounding_corner(plat, plon, mlat, mlon, nlat, nlon, side, nm=None):
    """The way round a port/starboard mark after the inbound standoff (#52): points on the same
    small circle, turning the legal way (port = counter-clockwise), ending at the EXIT standoff
    (90° off the outbound bearing to the next point, same side). Each step turns at most 90°, so no
    straight step cuts across the mark. Before, the next leg started from the inbound standoff, and
    on any turn over 90° the route left the mark on the wrong side. [] when there's nothing to turn,
    or when the course bends less than 90° AWAY from the mark side (a port mark on a slight right
    bend is passed, not looped)."""
    if side not in ("port", "starboard"):
        return []
    nm = ROUND_OFFSET_NM if nm is None else nm
    b_in = _bearing(plat, plon, mlat, mlon)
    b_out = _bearing(mlat, mlon, nlat, nlon)
    if side == "port":
        a0, turn = b_in + 90.0, -((b_in - b_out) % 360.0)        # turn left: bearings fall
    else:
        a0, turn = b_in - 90.0, (b_out - b_in) % 360.0           # turn right: bearings rise
    if abs(turn) < 1.0 or abs(turn) > 270.0:
        return []              # straight on, or the course bends away from the mark: just pass it
    n = max(1, int(math.ceil(abs(turn) / 90.0)))
    return [_advance(mlat, mlon, (a0 + turn * k / n) % 360.0, nm) for k in range(1, n + 1)]


def _corner_speed(wf, P, lat, lon, t, hdg, helm=1.0):
    """Speed (kn) for one step round a mark on heading `hdg`: the polar at the wind there, or the
    beat VMG made good along `hdg` when it points inside the no-go zone. None: no wind, no speed."""
    w = wf.wind_at(lat, lon, t) or _nearby_wind(wf, lat, lon, t)
    if not w:
        return None
    tws, twd = w
    twa = abs(_wrap180(hdg - twd))
    sp = _polar_speed(P, tws, twa)
    if sp < 0.3:
        beat = _vmg_twa(P, tws, "beat")
        # both tacks together: the upwind VMG, stretched onto a course `twa` off the wind
        sp = (_polar_speed(P, tws, beat) * math.cos(math.radians(beat)) / math.cos(math.radians(twa))
              if beat is not None and twa < beat else 0.0)
    sp *= helm
    return sp if sp >= 0.3 else None


def _corner_tacks(wf, P, a, b, t):
    """The two ways to tack from `a` to `b` round a mark when the step points inside the no-go
    zone: [(a, c, b)] with c the tack point (one tack first, or the other), or None when the step
    is sailed straight (or there's no wind: `_corner_speed` refuses that step anyway). The route
    sails one of them: its two boards are checked, priced and put in the path, not the chord."""
    w = wf.wind_at(a[0], a[1], t) or _nearby_wind(wf, a[0], a[1], t)
    if not w:
        return None
    tws, twd = w
    if _polar_speed(P, tws, abs(_wrap180(_bearing(*a, *b) - twd))) >= 0.3:
        return None
    beat = _vmg_twa(P, tws, "beat")
    if beat is None:
        return None
    k = math.cos(math.radians(a[0]))
    ve, vn = (b[1] - a[1]) * 60.0 * k, (b[0] - a[0]) * 60.0        # a → b, nm east / north
    u = [(math.sin(math.radians(h)), math.cos(math.radians(h))) for h in (twd + beat, twd - beat)]
    det = u[0][0] * u[1][1] - u[0][1] * u[1][0]
    if abs(det) < 1e-9:
        return None
    s1 = (ve * u[1][1] - vn * u[1][0]) / det                         # v = s1·u0 + s2·u1
    s2 = (u[0][0] * vn - u[0][1] * ve) / det
    out = []
    for d, (ue, un) in ((s1, u[0]), (s2, u[1])):
        out.append((a, (a[0] + d * un / 60.0, a[1] + d * ue / (60.0 * max(0.1, k))), b))
    return out


def _corner_leg(wf, P, leg, pts, dlat, dlon, leg_obs, helm_factor):
    """Sail the rounding `pts` [(lat, lon)] from the end of `leg` round the mark at (dlat, dlon):
    `leg` with the corner's steps added to its path, ETA, distance and tacks, or None when there's
    no clean way round (a board crosses an obstacle, or no wind/speed on a step).

    The straight boards actually sailed: a step that points inside the no-go zone is tacked, and
    its two boards (not the chord) are what's checked, priced and drawn (Codex on #287). Steps are
    a 0.1 nm circle's chords, so the boards stay close. Each tack put in costs TACK_COST_S of
    clock, like a tack anywhere else on the route (#293)."""
    boards, n_tacks = [], 0                 # (a, b, tack at b?)
    for a, b in zip(pts, pts[1:]):
        if leg_obs is not None and leg_obs.crosses(*a, *b):
            return None
        tk = _corner_tacks(wf, P, a, b, leg["eta"])
        if tk is None:
            boards.append((a, b, False))
            continue
        clear = [o for o in tk if leg_obs is None or not (
            leg_obs.crosses(*o[0], *o[1]) or leg_obs.crosses(*o[1], *o[2]))]
        if not clear:
            return None
        # the tack point farther from the mark: the boards stay outside the circle
        _p, c, _q = max(clear, key=lambda o: _hav_nm(*o[1], dlat, dlon))
        boards += [(a, c, True), (c, b, False)]
        n_tacks += 1
    # priced like any step: the polar at the wind there (forecast, or borrowed within the fill
    # limit), helm included. No wind or no speed: no corner, and the side check judges.
    tt, extra, steps = leg["eta"], 0.0, []
    for a, b, tack in boards:
        d = _hav_nm(*a, *b)
        kn = _corner_speed(wf, P, a[0], a[1], tt, _bearing(*a, *b), helm_factor)
        if kn is None:
            return None
        tt += d / kn * 3600.0
        extra += d
        steps.append({"lat": round(b[0], 5), "lon": round(b[1], 5), "t": tt})
        if tack:
            tt += TACK_COST_S
    if not steps:
        return None
    return dict(leg, path=list(leg["path"]) + steps, eta=tt,
                sailed_nm=round(leg["sailed_nm"] + extra, 2), tacks=leg["tacks"] + n_tacks)


def _sweep(pts, mlat, mlon):
    """Signed angle (deg) the bearing from the mark to the boat turns through along `pts`
    [(lat, lon)]: negative = the boat went counter-clockwise round it (mark on its left)."""
    tot, prev = 0.0, None
    for la, lo in pts:
        if _hav_nm(la, lo, mlat, mlon) < 1e-4:
            continue
        b = _bearing(mlat, mlon, la, lo)
        if prev is not None:
            tot += _wrap180(b - prev)
        prev = b
    return tot


def _wrong_side(pts, mlat, mlon, side, touch=True):
    """Did the path `pts` (the leg(s) around a mark) leave the mark on the wrong side? (#52, #242)
    The string test: the taut path from its first point to its last must lie on the `side`
    hand of the mark. A rounding mark (`touch`) must also be touched, so the ideal path bends
    round it; an island only has to be on the right hand. Extra loops the legal way are fine."""
    if side not in ("port", "starboard") or len(pts) < 2:
        return False
    (alat, alon), (blat, blon) = pts[0], pts[-1]
    if _hav_nm(alat, alon, mlat, mlon) < 1e-4 or _hav_nm(blat, blon, mlat, mlon) < 1e-4:
        return False
    sw = _sweep(pts, mlat, mlon)
    sgn = -1.0 if side == "port" else 1.0
    b_in, b_out = _bearing(alat, alon, mlat, mlon), _bearing(mlat, mlon, blat, blon)
    turn = ((b_in - b_out) if side == "port" else (b_out - b_in)) % 360.0
    if touch and turn <= 270.0:
        ideal = sgn * (turn + 180.0)
    else:
        # an island, or a mark the course bends less than 90° away from: on the right hand only
        # (the same rule `_rounding_corner` passes it by)
        st = _sweep([pts[0], pts[-1]], mlat, mlon)
        ideal = st if st * sgn >= 0 else st + sgn * 360.0
    k = round((sw - ideal) / 360.0)
    return k * sgn < 0


# --- sparse-GRIB coverage gate + route-sanity guard --------------------------
def _wind_coverage(wf, full_path):
    """Fraction of the routed path that had REAL multi-model coverage (vs the optimizer's constant
    fallback wind). A sparse GRIB silently routes on `route_leg`'s fallback; this measures that."""
    if not full_path:
        return 0.0
    covered = sum(1 for p in full_path if wf.detail_at(p["lat"], p["lon"], p["t"]) is not None)
    return round(covered / len(full_path), 2)


def _leg_self_crossings(full_path, legs, start_epoch):
    """Per-leg self-intersection count of the routed path — a self-crossing leg is the light-air
    'loops at the mark' tell. Legs are checked separately: crossing an EARLIER leg at a rounding is
    normal course geometry (a W/L course crosses itself by design); a leg crossing ITSELF is not."""
    def ccw(p, q, r):
        return (r[1] - p[1]) * (q[0] - p[0]) > (q[1] - p[1]) * (r[0] - p[0])

    def cross(a, b, c, d):
        return ccw(a, c, d) != ccw(b, c, d) and ccw(a, b, c) != ccw(a, b, d)

    out = {}
    bounds = [float(start_epoch)] + [l["eta_epoch"] for l in legs]
    for i, leg in enumerate(legs):
        pts = [(p["lat"], p["lon"]) for p in full_path
               if bounds[i] <= p["t"] <= bounds[i + 1]][:400]
        n = 0
        for a in range(len(pts) - 1):
            for b in range(a + 2, len(pts) - 1):
                if cross(pts[a], pts[a + 1], pts[b], pts[b + 1]):
                    n += 1
        if n:
            out[leg["to"]] = n
    return out


def _route_sanity(wf, legs, coverage, P, timed_out):
    """Flag a route that's likely wrong because the wind field was sparse/degraded. Returns
    (warnings, degraded). `degraded` means: do not trust this route — the inputs were too thin."""
    warnings, degraded = [], False
    if not wf.loaded:
        warnings.append("No forecast loaded. No wind to route on. Don't use it. "
                        "Try again when a forecast is out.")
        degraded = True
    elif coverage < COVERAGE_MIN:
        warnings.append(f"The forecast covers only {int(coverage * 100)}% of the route. Don't trust "
                        "the legs it misses.")
        degraded = True
    pmax = max((s for _, _, s in P), default=0.0)
    for l in legs:
        mins = l.get("leg_minutes") or 0.0
        if mins > 0 and pmax > 0 and l.get("sailed_nm"):
            spd = l["sailed_nm"] / (mins / 60.0)
            if spd > pmax * 1.2:
                warnings.append(f"Leg to {l['to']} averages {spd:.1f} kts, faster than the boat can go "
                                f"(~{pmax:.0f} kts). Probably missing wind data.")
                degraded = True
        if l.get("wind") is None:
            warnings.append(f"Leg to {l['to']}: no forecast wind mid-leg. Its sail call is a guess.")
    if timed_out:
        warnings.append("Routing ran out of time. The route may be cut short. Run it again.")
    return warnings, degraded


FINISH_TOL_NM = 3.0     # a route whose path ends farther than this from the finish didn't finish


def incomplete_reason(result, finish_pt=None, tol_nm=FINISH_TOL_NM):
    """Why a course result can't stand for a whole course, or None when it can (#31). Reads the
    router's own `incomplete` flag (a leg that stalled short of its mark) AND — for results built
    elsewhere, or when the flag is missing — the end-of-path distance to `finish_pt` (lat, lon), the
    same 3 nm rule the retro study uses. Callers drop / retry / refuse to score on a reason."""
    if not result or not result.get("available", True) or not result.get("path"):
        return (result or {}).get("note") or "no route"
    inc = result.get("incomplete")
    if inc:
        return f"stopped {inc.get('short_nm')} nm short of {inc.get('to')} ({inc.get('reason')})"
    if finish_pt and finish_pt[0] is not None:
        last = result["path"][-1]
        gap = _hav_nm(last["lat"], last["lon"], finish_pt[0], finish_pt[1])
        if gap > tol_nm:
            return f"ends {gap:.1f} nm short of the finish"
    return None


# --- full course -------------------------------------------------------------
PER_MODEL_BUDGET_S = float(os.environ.get("ROUTE_PER_MODEL_BUDGET_S", "120"))  # total budget for the path fan


# Routing resolution presets (2.5): heading-fan degrees, per-leg step ceiling (h), time budget (s).
# Fine = sharper near shore / tight marks but slower; Fast = quicker, coarser; Auto = balanced default.
RESOLUTIONS = {
    "fast": {"hstep": 18, "dt_cap": 1.5, "budget": 60},
    "auto": {"hstep": 12, "dt_cap": 1.0, "budget": 90},
    "fine": {"hstep": 8, "dt_cap": 0.6, "budget": 200},
}


def _stop_reason(leg):
    """Plain words for why a leg stopped short of its mark (#326), from route_leg's `stop`."""
    stop = leg.get("stop")
    if stop == "forecast_end":
        return ("becalmed: no wind until the forecast ends" if leg.get("calm_end")
                else "the forecast ends before the boat gets there")
    if stop == "no_wind":
        return "no forecast wind where the boat got to"
    if stop == "steps":
        return "the search ran out of steps (very light wind)"
    if stop == "time_budget":
        return "time budget"
    return "no clear heading reached the mark"


def _resolution(name):
    return RESOLUTIONS.get((name or "auto").strip().lower(), RESOLUTIONS["auto"])


def optimize_course(definition: dict, course_id, start_epoch, wf, time_budget_s=None,
                    obstacles=None, avoid=True, source=None, safety_depth=None,
                    jib_crossovers=None, emit_exploration=True, per_model=False,
                    resolution="auto", cur=None, waves=None, helm_factor=1.0,
                    polar_adjustments=None, wave_coeffs=None, polar=None,
                    from_mark=0, exclude_sails=None, maneuver_prune_mult=1.0, sail_config=None,
                    leg_obstacles=None, boat_polar=None):
    """Route the whole course from its start through every mark to the finish via `wf`.

    The polar is the BOAT's (#303): `boat_polar` is its polar block (`boats.polar_for(boat)`:
    envelope, per-sail curves, crossover table), or `polar` an injected envelope table (the fleet
    retro's ORC cert path). With neither the run refuses ("No polar for this boat.") — there is
    no default polar.

    Returns one optimal route with per-leg ETAs, total time/distance/tacks and a route confidence
    (mean of the wind field's per-point model agreement sampled along the path).

    `obstacles` (an ObstacleField) keeps the route off land/islands/exclusion-zones; if None and
    `avoid` is set, one is built from the course bbox + this race's zones + island marks. `source`
    (Natural Earth vs NOAA ENC) and `safety_depth` (the active boat draft + margin) flow into it.
    `leg_obstacles` (optional) is one ObstacleField per routed leg, used in place of `obstacles` for
    that leg. With neither given and `avoid` set, both are built here (`geo.build_leg_fields`): land,
    zones and island disks on every leg, an island's rounding barrier only on the leg that rounds it
    (#141), so a course passing the same island twice, once each way, doesn't wall itself off."""
    marks, skipped, cid = race_def.course_to_marks(definition, course_id)
    if from_mark:
        # PACE PLAYS (Playbook v2 Phase C): route the REMAINING course from an intermediate mark —
        # "if you reach this mark N hours late/early, what does the rest of the race become?"
        # start_epoch is then the (delayed) arrival epoch at that mark.
        marks = marks[int(from_mark):]
    if len(marks) < 2:
        return {"available": False, "note": "course needs at least a start and one mark/finish",
                "skipped": skipped}
    roundings = race_def.course_roundings(definition, course_id)   # #3 rounding side per nav mark
    xo = None                                    # the boat's sail crossover table (sail calls)
    if polar is not None:
        # an INJECTED polar table [(tws,twa,stw)] — the fleet-retro per-boat path (an ORC cert's
        # Allowances envelope). Own-boat refinements (adjustments/jibs/per-sail curves) don't apply
        # to another boat, so sail-aware routing is off → envelope routing, like SAIL_AWARE=0.
        P = [tuple(p) for p in polar]
        SP = {}
    else:
        P = POL.polars_stw(boat_polar)
        if not P:
            return {"available": False, "note": NO_POLAR, "skipped": skipped}
        xo = sailplan.of(boat_polar)
        # Lab-4 human-approved refined-polar overlay — on the envelope AND the per-sail curves, so a
        # held non-optimal sail (and a gear-loss envelope rebuilt from the curves) sees it too. The
        # helm factor is applied once, later, on whichever speed the router picks.
        SP = POL.adjust_sail_polars(POL.sail_polars(boat_polar), polar_adjustments, P)  # {} → envelope
        P = POL.apply_adjustments(P, polar_adjustments)
    if exclude_sails and SP:
        # GEAR-LOSS PLAYS (Playbook v2 Phase C): route as if a sail is out of service — the router
        # must genuinely sail without it, so its curve is removed AND the envelope is rebuilt as the
        # max over the REMAINING sails (the cert envelope assumes the full inventory).
        gone = {str(s).upper() for s in exclude_sails}
        SP = {s: pts for s, pts in SP.items() if s.upper() not in gone}
        if SP:
            env = {}
            for pts in SP.values():
                for tws, twa, stw in pts:
                    k = (tws, twa)
                    if stw is not None and (k not in env or stw > env[k]):
                        env[k] = stw
            P = sorted((t, a, v) for (t, a), v in env.items())
    if not P:
        return {"available": False, "note": "no polars loaded"}

    _gone = {str(s).upper() for s in (exclude_sails or ())}
    _dom = _sail_domains(SP) if (_gone and SP) else {}

    def _headline_sail(tws, twa):
        """Per-leg display sail — remapped to the fastest REMAINING sail under a gear-loss
        exclusion (the crossover table doesn't know a sail is out of service)."""
        s = sailplan.optimal_sail(tws, twa, jib_crossovers, sail_config, xo=xo)
        if s and _gone and _sail_polar_key(s).upper() in _gone and SP:
            s = max(SP, key=lambda k: _sail_speed(SP, _dom, k, tws, twa), default=s)
        return s

    all_leg_obstacles = leg_obstacles            # every leg of the course (the per-model fan's)
    land_failed = False                          # #51: land was asked for and couldn't be checked
    if obstacles is None and leg_obstacles is None and avoid:
        bbox = course_bbox(definition, course_id)
        if bbox:
            try:
                from .geo import build_leg_fields
                obstacles, all_leg_obstacles = build_leg_fields(
                    definition, cid or course_id, bbox, source=source, safety_depth=safety_depth)
                # PACE PLAYS route from mark `from_mark`: their leg 0 is the course's leg from_mark
                leg_obstacles = all_leg_obstacles[int(from_mark or 0):]
            except Exception as exc:  # noqa: BLE001 — reported on the route, never open water
                print(f"[optimize] obstacle build failed: {type(exc).__name__}: {exc}", flush=True)
                obstacles = leg_obstacles = all_leg_obstacles = None
                land_failed = True
    land_failed = land_failed or _land_failed(obstacles, all_leg_obstacles)

    # reshape gate: does the (spatially-varying) sea state get to BEND the route, or only tax the ETA?
    wave_stats = _wave_field_stats(waves, marks)
    wave_uniform_hs, wave_reshape, wave_reshape_note = _wave_reshape_decision(waves, wave_stats)

    rp = _resolution(resolution)
    if time_budget_s is None:                          # caller didn't pin a budget → use the preset's
        time_budget_s = rp["budget"]
    deadline = time.time() + time_budget_s
    legs = []
    t = float(start_epoch)
    slat, slon = marks[0][2], marks[0][3]
    confs = []
    full_path = [{"lat": slat, "lon": slon, "t": t}]
    isochrones = []
    laylines = []
    cur_sail = None           # the sail carried into a leg — threaded across marks so a peel at a
    route_track = []          # rounding counts once, and the whole-route sail plan is continuous (2g)
    incomplete = None         # #31: set when a leg never reaches its mark — the course stops there
    leg_end_idx = []          # full_path index where each REACHED leg ends — the mark vertices,
                              # known at construction (gpsout/synthesis keep them; no proximity guess)
    for mi, (seq, name, dlat, dlon) in enumerate(marks[1:], 1):
        # #3 rounding side: route to a small standoff on the legal side of a port/starboard mark (the
        # real mark is still displayed + recorded). Gates pass between, finish/none unchanged.
        side = roundings.get(name, "none")
        rlat, rlon = _rounding_offset(slat, slon, dlat, dlon, side)
        leg_obs = (leg_obstacles[len(legs)] if leg_obstacles and len(legs) < len(leg_obstacles)
                   else obstacles)
        # #242: a start or mark the chart puts on land, in shallow water or a zone can't be
        # reached: stop here at once and say so, instead of searching the whole time budget
        on_chart = None
        blk = getattr(leg_obs, "blocked", None)
        if blk is not None:
            if not legs and blk(slat, slon):
                on_chart = marks[0][1]
            elif blk(dlat, dlon) or blk(rlat, rlon):
                on_chart = name
        if on_chart:
            incomplete = {"leg": len(legs), "to": name, "short_nm": round(_hav_nm(slat, slon, dlat, dlon), 2),
                          "reason": f"{on_chart} is on land, shallow water or a no-go zone on the chart",
                          "blocked_mark": on_chart,
                          "legs_not_routed": [n for _s, n, _a, _o in marks[1:]][len(legs):]}
            break
        leg = route_leg(wf, P, slat, slon, t, rlat, rlon, deadline=deadline, obstacles=leg_obs,
                        capture=emit_exploration, hstep=rp["hstep"], dt_cap=rp["dt_cap"], cur=cur,
                        sail_polars=SP, jib_crossovers=jib_crossovers, start_sail=cur_sail,
                        waves=waves, helm_factor=helm_factor, wave_coeffs=wave_coeffs,
                        wave_uniform_hs=wave_uniform_hs, maneuver_prune_mult=maneuver_prune_mult,
                        sail_config=sail_config, xo=xo)
        # #52: round the mark the legal way and leave from the EXIT standoff, not the inbound one
        nxt = marks[mi + 1] if mi + 1 < len(marks) else None
        corner = (_rounding_corner(slat, slon, dlat, dlon, nxt[2], nxt[3], side)
                  if leg.get("reached", True) and nxt is not None and leg["path"] else [])
        if corner:
            last = leg["path"][-1]
            pts = [(last["lat"], last["lon"])] + corner
            rounded = _corner_leg(wf, P, leg, pts, dlat, dlon, leg_obs, helm_factor)
            if rounded is None:
                corner = []            # no clean way round at the standoff: the side check judges
            else:
                leg = rounded
        # sample wind + confidence at the leg's midpoint and end (for the briefing)
        mid = leg["path"][len(leg["path"]) // 2] if leg["path"] else {"lat": dlat, "lon": dlon}
        det = wf.detail_at(mid["lat"], mid["lon"], (t + leg["eta"]) / 2.0)
        if det:
            confs.append(det["confidence"])
        twa = None
        if det:
            twa = abs(_wrap180(_bearing(slat, slon, dlat, dlon) - det["twd"]))
        pos = _point_of_sail(twa) if twa is not None else None
        _sail = (_headline_sail(det["tws"], twa) if det and twa is not None else None)
        # main reef decoration (crew thresholds — depower in breeze / open the A3 slot); label-only
        _reef = (sailplan.reef_for(det["tws"], pos, _sail,
                                   (sail_config or {}).get("main_reefs"))
                 if det and _sail else None)
        legs.append({
            "to": name, "seq": seq,
            "direct_nm": leg["direct_nm"], "sailed_nm": leg["sailed_nm"], "tacks": leg["tacks"],
            "leg_minutes": round((leg["eta"] - t) / 60.0, 1),
            "eta_epoch": round(leg["eta"]),
            "first_heading": leg["first_heading"],
            "blocked_steps": leg.get("blocked_steps", 0),
            "point_of_sail": pos,
            "sail": _sail,
            "reef": (_reef or {}).get("reef"),
            "reef_why": (_reef or {}).get("why"),
            "peels": leg.get("peels", 0),     # sail changes the router actually made on this leg (2g)
            "realized_factor": leg.get("realized_factor", 1.0),   # % of flat-water polar achieved (helm × sea state)
            "hs_mean": leg.get("hs_mean", 0.0),                   # mean sea state on this leg (m)
            "wind": ({"tws": det["tws"], "twd": det["twd"], "confidence": det["confidence"]}
                     if det else None),
            "reached": leg.get("reached", True),
            "wind_filled": leg.get("wind_filled", 0),    # nodes on borrowed nearby wind (#203)
            "becalmed_h": leg.get("becalmed_h", 0.0),    # hours the route waits out a calm (#326)
        })
        # accumulate the real sails-flown track + carry the ending sail into the next leg (2g)
        route_track += leg.get("sail_track", [])
        cur_sail = leg.get("sail_end") or cur_sail
        full_path += [p for p in leg["path"][1:]]
        if leg.get("reached", True):
            leg_end_idx.append(len(full_path) - 1)
        if emit_exploration:
            if len(isochrones) < ISO_MAX:
                isochrones += leg.get("isochrones", [])[:ISO_MAX - len(isochrones)]
            # laylines into this mark when the approach is a beat or run (reaches have none)
            if det and pos in ("beat", "run"):
                laylines += _layline_pair(P, det["tws"], det["twd"], pos, dlat, dlon,
                                          leg["direct_nm"])
        if not leg.get("reached", True):
            # #31: the leg stalled short of its mark (time budget out / every heading blocked). Stop
            # the course HERE — chaining the next leg from the mark would teleport the boat to it
            # (possibly across land) at no time cost and report a too-short finish.
            incomplete = {"leg": len(legs), "to": name, "short_nm": leg.get("short_nm"),
                          "reason": ("time budget" if (deadline and time.time() > deadline)
                                     else _stop_reason(leg)),
                          "stop": leg.get("stop"),
                          "legs_not_routed": [n for _s, n, _a, _o in marks[1:]][len(legs):]}
            t = leg["eta"]
            break
        # continue from the exit standoff (or the inbound one when there's no corner) ≈ the mark
        slat, slon, t = leg["path"][-1]["lat"], leg["path"][-1]["lon"], leg["eta"]
        if not corner:
            slat, slon = rlat, rlon

    total_min = round((t - float(start_epoch)) / 60.0, 1)
    timed_out = time.time() > deadline
    coverage = _wind_coverage(wf, full_path)
    warnings, degraded = _route_sanity(wf, legs, coverage, P, timed_out)
    if incomplete and incomplete.get("blocked_mark"):
        degraded = True
        warnings.insert(0, f"No route: {incomplete['blocked_mark']} is on land, in shallow water or "
                           "in a no-go zone on the chart. Check the mark.")
    elif incomplete:
        degraded = True
        rest = incomplete["legs_not_routed"]
        warnings.insert(0, f"Route INCOMPLETE. The leg to {incomplete['to']} stopped "
                           f"{incomplete['short_nm']} nm short of the mark ({incomplete['reason']})"
                           + (f". Not routed after it: {', '.join(rest)}" if rest else "")
                           + (". Don't use this route. Try again with a newer forecast."
                              if incomplete.get("stop") in ("forecast_end", "no_wind", "steps")
                              else ". Don't use this route. Run it again, or check for land near that mark."))
    if land_failed:
        degraded = True
        warnings.insert(0, LAND_FAILED)
    if any(l.get("wind_filled") for l in legs):
        warnings.append(GAPS_FILLED)
    held = _forecast_held(wf, full_path, float(start_epoch))
    if held:                       # #51: wind past every model's end is the last frame, held
        degraded = True
        warnings.append(FORECAST_ENDS if held == "end" else FORECAST_STARTS_LATE)
    for name, n in _leg_self_crossings(full_path, legs, start_epoch).items():
        warnings.append(f"Route crosses itself {n}x on the leg to {name}. Light, shifty wind. "
                        "Don't trust that leg's line. Sail the shifts.")
    # route-level sail plan (2g): the PHYSICALLY-REAL sequence of sails flown, from the isochrone's own
    # sail track (where it actually peeled), collapsed across legs — not a post-hoc per-leg guess. Falls
    # back to the per-leg headline sail when sail-aware routing is off / has no per-sail polars.
    sail_seq = []
    for entry in route_track:
        s = entry.get("sail")
        if s and (not sail_seq or sail_seq[-1]["sail"] != s):
            sail_seq.append({"sail": s, "lat": entry.get("lat"), "lon": entry.get("lon"),
                             "t": round(entry["t"]) if entry.get("t") is not None else None})
    if not sail_seq:                          # sail-aware off → fall back to the per-leg headline sails
        for lg in legs:
            s = lg.get("sail")
            if s and (not sail_seq or sail_seq[-1]["sail"] != s):
                sail_seq.append({"sail": s, "from_leg": lg["to"]})
            elif s and sail_seq:
                sail_seq[-1]["to_leg"] = lg["to"]
    total_peels = sum(l.get("peels", 0) for l in legs)
    # realized (achievable) speed roll-up — time-weighted mean % of the flat-water polar the route
    # sails at (helm skill × sea state), + mean sea state. The gap to 100% is the coaching number;
    # None when nothing degrades it (helm 1.0 + flat water) so the UI hides it.
    realized = None
    if (abs(helm_factor - 1.0) > 1e-3) or (waves is not None and getattr(waves, "loaded", False)):
        wmin = [(l.get("realized_factor", 1.0), l.get("leg_minutes") or 0.0) for l in legs]
        tot = sum(m for _f, m in wmin) or 1.0
        realized = {
            "realized_pct": round(sum(f * m for f, m in wmin) / tot, 3),
            "helm_factor": round(helm_factor, 3),
            "sea_state_hs_mean": round(sum((l.get("hs_mean") or 0.0) * (l.get("leg_minutes") or 0.0)
                                           for l in legs) / tot, 2),
            "wave_source": waves.status().get("source") if waves is not None else None,
        }
        if wave_stats is not None:                 # the reshape gate: did the sea bend the route or only tax ETA?
            realized["wave_reshape"] = wave_reshape
            realized["wave_reshape_note"] = wave_reshape_note
            realized["wave_spread_hs"] = wave_stats["spread"]
    # per-model candidate paths (the confidence moat made VISUAL — the fan the blended route summarizes)
    candidate_paths = []
    if per_model and not land_failed:          # no land check → no model routes drawn either
        candidate_paths = _per_model_paths(definition, course_id, start_epoch, wf, obstacles, P, marks,
                                           source, safety_depth, jib_crossovers, total_min / 60.0,
                                           leg_obstacles=all_leg_obstacles,
                                           cur=cur, waves=waves, helm_factor=helm_factor,
                                           wave_coeffs=wave_coeffs, polar=polar,
                                           polar_adjustments=polar_adjustments, sail_config=sail_config,
                                           exclude_sails=exclude_sails, boat_polar=boat_polar)
    safety = route_safety(definition, cid or course_id, marks, full_path, leg_end_idx, incomplete,
                          obstacles, leg_obstacles, land_failed, from_mark=int(from_mark or 0))
    return {
        "available": True, "course_id": cid,
        "safety": safety,                  # #242: can this route be frozen, deployed, sent as GPX?
        "start_epoch": round(float(start_epoch)), "finish_epoch": round(t),
        "total_minutes": total_min, "total_hours": round(total_min / 60.0, 1),
        "total_sailed_nm": round(sum(l["sailed_nm"] for l in legs), 1),
        "total_direct_nm": round(sum(l["direct_nm"] for l in legs), 1),
        "total_tacks": sum(l["tacks"] for l in legs),
        "total_peels": total_peels,
        "realized": realized,
        "route_confidence": round(sum(confs) / len(confs), 2) if confs else None,
        "min_confidence": round(min(confs), 2) if confs else None,
        "wind_coverage": coverage,
        "resolution": (resolution or "auto").strip().lower(),
        "degraded": degraded,
        "complete": incomplete is None,
        "leg_end_idx": leg_end_idx,
        "incomplete": incomplete,
        "warnings": warnings,
        "legs": legs,
        "sail_plan": sail_seq,
        "roundings": race_def.marks_with_side(definition, course_id),   # crew-facing required sides
        "skipped_marks": skipped,
        "marks": [{"seq": s, "name": n, "lat": la, "lon": lo} for s, n, la, lo in marks],
        "isochrones": isochrones,
        "laylines": laylines,
        "candidate_paths": candidate_paths,
        "path": [{"lat": p["lat"], "lon": p["lon"], "t": round(p["t"])} for p in full_path],
        "windfield": wf.status(),
        "obstacles": dict((_obstacle_summary(obstacles, leg_obstacles) if leg_obstacles
                           else obstacles.summary() if obstacles is not None
                           else {"active": False, "land": "off"}),
                          **({"land": "failed"} if land_failed else {})),
        "obstacle_steps_avoided": sum(l.get("blocked_steps", 0) for l in legs),
        "timed_out": timed_out,
    }


# --- route acceptance (#242) -------------------------------------------------------------------
# The Lab refuses a route it can't prove safe and complete: no freeze, no Deploy, no GPX. The
# problems, in the crew's words. A chart override (reviewed, per mark) only opens the chart at that
# mark; it never waives a problem.
SAFE_LAND_OFF = "Land not checked."
SAFE_LAND_FAILED = "Shoreline didn't load. Land not checked."
SAFE_DEPTH_NONE = "No depth chart. Depths not checked."
SAFE_DEPTH_FAILED = "Depth chart didn't load. Depths not checked."
SAFE_BOX = "Route leaves the chart area."
SAFE_BLOCKED = "{mark} is on land or in shallow water on the chart."
SAFE_UNREACHED = "Route doesn't reach {mark}."
SAFE_SIDE = "Route leaves {mark} on the wrong side."
SAFE_BLOCKED_FIXED = "{mark} is in a race zone, by an island or off the chart."
# what to click, beside each problem (#325): the Lab shows it under the problem; Freeze, Lock-in,
# Deploy and GPX refusals say it too (routesafety.refusal)
FIX_OVERRIDE = "Chart wrong? Click Override."
FIX_MOVE = "Move it in Course & Marks."
FIX_RERUN = "Run it again."
FIX_AVOID = "Tick Avoid land & zones and run again."
FIX_CHARTS = "Pick charts with depths and run again."
# not a problem (Cole's decision on #242): water no chart or depth grid gives a depth for (NOAA ENC
# is US-only). The route isn't refused there; the verdict marks the legs and the Lab shows this.
SAFE_NO_DEPTH = "No depth data: check the paper chart."
# depths from a depth grid that is not a chart (#292): checked, but the leg says which grid
SAFE_GRID_DEPTH = "Depths from {label}, not a chart: check the paper chart."


def _in_ring(lat, lon, ring):
    """Ray cast: is (lat, lon) inside `ring` [(lon, lat), ...]?"""
    inside, n = False, len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _override_helps(definition, course_id, name, marks, fields):
    """Would a chart override open nav mark `name`, which the route found blocked? (#325) An
    override cuts a pocket in the shoreline layers only (land, shoal, obstruction:
    `geo.obstacles.build_for_course`); a race zone, an island disk (the coastline backstop) and
    the edge of the chart are never opened. So: no if the mark already has an override (then
    something else blocks it), lies (or a port/starboard mark's rounding standoff may lie) in a
    race zone or an island disk, or is outside every chart; yes otherwise."""
    from .geo import obstacles as geo
    if name in geo.chart_overrides(definition, course_id):
        return False
    at = next(((la, lo) for _s, n, la, lo in marks if n == name), None)
    if at is None:
        return False
    lat, lon = at
    boxed = [f for f in fields if hasattr(f, "inside")]
    if boxed and not any(f.inside(lat, lon) for f in boxed):
        return False
    # the mark, and anywhere its rounding standoff could be (ROUND_OFFSET_NM off it, either side)
    near = [(lat, lon)]
    if race_def.course_roundings(definition, course_id).get(name) in ("port", "starboard"):
        near += [_advance(lat, lon, b, ROUND_OFFSET_NM) for b in range(0, 360, 30)]
    for z in (definition or {}).get("zones") or []:
        if z.get("type") not in ("exclusion", "hazard", "tss"):
            continue
        rings = geo._zone_rings(z.get("geometry"))
        if rings and any(_in_ring(a, o, rings[0]) and not any(_in_ring(a, o, h) for h in rings[1:])
                         for a, o in near):
            return False
    if any(getattr(f, "source", None) != "enc" for f in fields):
        for isl in geo._course_islands(definition or {}, course_id):
            if any(_hav_nm(a, o, isl["lat"], isl["lon"]) <= isl["radius_nm"] for a, o in near):
                return False
    return True


def route_safety(definition, course_id, marks, full_path, leg_end_idx, incomplete, obstacles,
                 leg_obstacles, land_failed, from_mark=0):
    """{ok, problems: [{code, mark, text}], notes: [{code, text, legs}], overrides: [{mark, by, at,
    why}]} for a routed course. `ok` only when land was checked over the whole route, the ENC
    depth chart loaded, every mark was reached and every port/starboard mark and island was left
    on its side. Depths are checked wherever ENC charts them; a leg through water it doesn't
    chart (Canada) isn't refused but listed in `notes` ("no depth data")."""
    from .geo import obstacles as geo
    problems = []

    def add(code, text, mark=None, fix=FIX_RERUN, **extra):
        if not any(p["code"] == code and p.get("mark") == mark for p in problems):
            problems.append(dict({"code": code, "mark": mark, "text": text, "fix": fix}, **extra))

    fields = [f for f in [obstacles] + list(leg_obstacles or []) if f is not None]
    if land_failed:
        add("land", SAFE_LAND_FAILED)
    elif not fields or all(getattr(f, "land", "off") == "off" for f in fields):
        add("land", SAFE_LAND_OFF, fix=FIX_AVOID)
    depths = {getattr(f, "depth", "none") for f in fields}
    if "failed" in depths:
        add("depth", SAFE_DEPTH_FAILED)
    elif not fields or depths != {"ok"}:
        add("depth", SAFE_DEPTH_NONE, fix=FIX_CHARTS)
    if incomplete:
        if incomplete.get("blocked_mark"):
            # the chart (or a race zone / island) puts the mark itself where the boat can't go.
            # `override`: would a chart override open it? (#325) Only the Lab offers the button then
            m = incomplete["blocked_mark"]
            if _override_helps(definition, course_id, m, marks, fields):
                add("blocked", SAFE_BLOCKED.format(mark=m), m, fix=FIX_OVERRIDE, override=True)
            else:
                add("blocked", SAFE_BLOCKED_FIXED.format(mark=m), m, fix=FIX_MOVE, override=False)
        else:
            # the router ran out of time or found no clear heading: the mark itself is open
            # water, so a chart override (a pocket AT the mark) can't help (#325)
            add("unreached", SAFE_UNREACHED.format(mark=incomplete.get("to")), incomplete.get("to"),
                override=False)

    pts = [(p["lat"], p["lon"]) for p in full_path]

    def leg_pts(k):                         # the path of routed leg k, or None if not reached
        if k < 0 or k >= len(leg_end_idx):
            return None
        a = 0 if k == 0 else leg_end_idx[k - 1]
        return pts[a:leg_end_idx[k] + 1]

    no_depth_legs = []
    grid_legs = {}                          # grid label -> legs whose depths came from it (#292)
    for k in range(len(leg_end_idx)):       # every reached leg inside its own field's box (#258)
        f = (leg_obstacles[k] if leg_obstacles and k < len(leg_obstacles) else obstacles)
        lp = leg_pts(k)
        if f is not None and hasattr(f, "inside") and not all(f.inside(*p) for p in lp):
            add("box", SAFE_BOX)
        # depths checked over the whole route: a leg through water no ENC chart covers is marked
        nd = getattr(f, "no_depth", None)
        if "depth" not in {p["code"] for p in problems} and nd is not None and \
                any(nd(*lp[i], *lp[i + 1]) for i in range(len(lp) - 1)):
            a = marks[k][1] if k < len(marks) else "?"
            b = marks[k + 1][1] if k + 1 < len(marks) else "?"
            no_depth_legs.append(f"{a} → {b}")
        nn = getattr(f, "nonnav_labels", None)
        if "depth" not in {p["code"] for p in problems} and nn is not None and lp:
            labels = {lb for i in range(len(lp) - 1) for lb in nn(*lp[i], *lp[i + 1])}
            a = marks[k][1] if k < len(marks) else "?"
            b = marks[k + 1][1] if k + 1 < len(marks) else "?"
            for lb in sorted(labels):
                grid_legs.setdefault(lb, []).append(f"{a} → {b}")

    roundings = race_def.course_roundings(definition, course_id)
    for j in range(1, len(marks) - 1):      # a nav mark: the legs either side of it, touched
        _s, name, mla, mlo = marks[j]
        a, b = leg_pts(j - 1), leg_pts(j)
        if a and b and _wrong_side(a + b[1:], mla, mlo, roundings.get(name, "none"), touch=True):
            add("side", SAFE_SIDE.format(mark=name), name)
    course = geo._course(definition, course_id) or {}
    cmarks = course.get("marks") or []
    for ci, leg in geo.island_legs(definition, course_id).items():   # an island: on its own leg
        m = cmarks[ci]
        seg = leg_pts(leg - from_mark)
        if seg and m.get("lat") is not None and \
                _wrong_side(seg, m["lat"], m["lon"], m.get("rounding", "none"), touch=False):
            name = m.get("name", "island")
            add("side", SAFE_SIDE.format(mark=name), name)

    names = {n for _s, n, _a, _o in marks}
    overrides = [{"mark": n, "by": str(o.get("by") or ""), "at": o.get("at"),
                  "why": str(o.get("why") or "")}
                 for n, o in geo.chart_overrides(definition, course_id).items() if n in names]
    notes = ([{"code": "no_depth", "text": SAFE_NO_DEPTH, "legs": no_depth_legs}]
             if no_depth_legs else [])
    notes += [{"code": "grid_depth:" + lb, "text": SAFE_GRID_DEPTH.format(label=lb), "legs": legs}
              for lb, legs in sorted(grid_legs.items())]
    return {"ok": not problems, "problems": problems, "notes": notes, "overrides": overrides}


# #51: the shoreline didn't load. Shown first on the route; the route is marked degraded.
LAND_FAILED = "Shoreline didn't load. Land not checked: the route may cross land. Don't use it."


# #51: past every model's last frame the route sails on that frame, held. Kept, but never silent.
FORECAST_ENDS = "Forecast ends before the finish: last hours use the final forecast."
FORECAST_STARTS_LATE = "Forecast starts after the gun: first hours use the first forecast."


def _forecast_held(wf, full_path, start_epoch=None):
    """Did the route sail on a held edge frame (no model's forecast reached that time)? → "end"
    (past the last frame), "start" (before the first) or None. A wind field without the `held`
    flag (a fixed test wind, the boat's own) never holds."""
    hit = None
    for p in full_path or []:
        d = wf.detail_at(p["lat"], p["lon"], p["t"])
        if not d:
            # borrowed wind carries its donor's health (Codex on #220): a held frame borrowed
            # from nearby is still a held frame
            q = _nearby_point(wf, p["lat"], p["lon"], p["t"])
            d = wf.detail_at(*q, p["t"]) if q else None
        if d and d.get("held"):
            # the wind field says which edge it held, from the series it actually used (Codex on
            # #200: comparing against every model's bounds, off-position grids included, called a
            # held END a late start)
            if d.get("held_side", "end") == "end":
                return "end"
            hit = "start"
    return hit


def _land_failed(obstacles, leg_obstacles):
    """Was land asked for on any of these fields and failed to load? (geo ObstacleField.land)"""
    return any(getattr(f, "land", None) == "failed" for f in [obstacles] + list(leg_obstacles or []))


def _obstacle_summary(obstacles, leg_obstacles):
    """The result's obstacle summary with per-leg masks: every leg's barriers drawn on the map."""
    from .geo.obstacles import ObstacleField, legs_summary
    fields = [obstacles] + list(leg_obstacles)
    if all(f is None or isinstance(f, ObstacleField) for f in fields):
        return legs_summary(obstacles, leg_obstacles)
    return obstacles.summary() if obstacles is not None else {"active": False}


def _per_model_paths(definition, course_id, start_epoch, wf, obstacles, P, marks,
                     source, safety_depth, jib_crossovers, blended_hours, cur=None,
                     waves=None, helm_factor=1.0, wave_coeffs=None, polar=None,
                     polar_adjustments=None, sail_config=None, exclude_sails=None,
                     leg_obstacles=None, boat_polar=None):
    """Route the course through EACH model's OWN sub-field (its series only) → the per-model candidate
    paths the blended route's confidence number summarizes. Same split as the playbook's `_subfields`,
    but here it feeds the Gameplan map's 'Model routes' overlay (PR-4): the user literally sees the fan
    — tight where the models agree (high confidence), spread where they disagree. Reuses the
    already-built obstacle field (no rebuild) and skips isochrone capture.

    The FAN (which side each model commits to) is the signal, NOT a solo-model ETA — a single model's
    sub-field can be too thin to route honestly. So we DROP any candidate that came back degraded /
    timed-out / wildly off the blended solution (0.5×–1.6× its hours), rather than draw a route we
    don't trust. Returns the kept candidates + a `dropped` count is left to the caller's discretion."""
    by_model = {}
    for (model, member), frames in wf.series.items():
        by_model.setdefault(model, {})[(model, member)] = frames
    if len(by_model) < 2:
        return []                       # one model → no fan to show
    from .wind.windfield import WindField
    rhumb = (_bearing(marks[0][2], marks[0][3], marks[1][2], marks[1][3])
             if len(marks) >= 2 and marks[0][2] is not None and marks[1][2] is not None else None)
    per = max(30, int(PER_MODEL_BUDGET_S / len(by_model)))
    lo, hi = (blended_hours * 0.5, blended_hours * 1.6) if blended_hours else (0, 1e9)
    out = []
    for model, series in by_model.items():
        meta = [m for m in wf.meta if m["model"] == model]
        sub = WindField(series, meta, wf.bbox, wf.t_start, wf.t_end)
        r = optimize_course(definition, course_id, start_epoch, sub, time_budget_s=per,
                            obstacles=obstacles, leg_obstacles=leg_obstacles, avoid=False,
                            source=source, safety_depth=safety_depth,
                            jib_crossovers=jib_crossovers, emit_exploration=False, per_model=False, cur=cur,
                            waves=waves, helm_factor=helm_factor, wave_coeffs=wave_coeffs,
                            polar=polar, polar_adjustments=polar_adjustments, sail_config=sail_config,
                            exclude_sails=exclude_sails, boat_polar=boat_polar)
        hrs = r.get("total_hours")
        if (not r.get("available") or not r.get("path") or r.get("degraded") or r.get("timed_out")
                or hrs is None or hrs < lo or hrs > hi):
            continue                    # untrustworthy single-model route → don't draw it
        fh = (r.get("legs") or [{}])[0].get("first_heading")
        side = "middle"
        if rhumb is not None and fh is not None:
            d = ((fh - rhumb + 540) % 360) - 180
            side = "left" if d < -10 else "right" if d > 10 else "middle"
        out.append({"model": model, "total_hours": hrs, "favored_side": side,
                    "path": [[p["lat"], p["lon"]] for p in r["path"]]})
    return out


# --- course extent / horizon -------------------------------------------------
def course_bbox(definition: dict, course_id=None, pad=0.5):
    """(north, south, west, east) bounding the course marks, padded. None if no coords."""
    marks, _skip, _cid = race_def.course_to_marks(definition, course_id)
    pts = [(la, lo) for _s, _n, la, lo in marks if la is not None]
    if not pts:
        return None
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    return (max(lats) + pad, min(lats) - pad, min(lons) - pad, max(lons) + pad)


def estimate_hours(definition: dict, course_id=None, kn=5.0, margin=1.6, cap=72):
    """Rough course duration (h) from summed direct mark-to-mark distance / a nominal speed —
    used to size the wind-field time window before the route is known."""
    marks, _skip, _cid = race_def.course_to_marks(definition, course_id)
    dist = sum(_hav_nm(marks[i][2], marks[i][3], marks[i + 1][2], marks[i + 1][3])
               for i in range(len(marks) - 1))
    return min(cap, max(2.0, dist / max(1.0, kn) * margin))


# --- briefing ----------------------------------------------------------------
def briefing(result: dict, race_name: str = "") -> str:
    """An Opus-written pre-race routing briefing from the optimizer result. Falls back to a
    deterministic template when no API key is set, so the optimizer always returns a briefing."""
    if not result.get("available"):
        return result.get("note", "No route available.")
    legs = result["legs"]
    warnings = result.get("warnings") or []
    roundings = result.get("roundings") or []

    def _round_phrase(r):
        if r.get("side") == "gate":
            return f"{r['name']} (gate — pass between)"
        return f"leave {r['name']} to {r['side']}"
    roundings_text = "; ".join(_round_phrase(r) for r in roundings)

    cur = result.get("current") or {}
    current_text = ""
    if cur.get("loaded"):
        src = cur.get("source", "current model")
        if cur.get("source") == "constant":
            bits = f"set {cur.get('set_deg','?')}° / drift {cur.get('drift_kn','?')} kts"
        else:
            bits = f"{cur.get('slices', '?')} time slices"
        current_text = (f"Water current: {src} ({bits}) — leg ETAs already account for set & drift "
                        "(the route crabs into a cross stream).")

    rz = result.get("realized") or {}
    realized_text = ""
    if rz:
        pct = int(round(rz.get("realized_pct", 1.0) * 100))
        helm = int(round(rz.get("helm_factor", 1.0) * 100))
        hs = rz.get("sea_state_hs_mean") or 0.0
        wsrc = rz.get("wave_source")
        wvia = " (GLWU)" if wsrc == "glwu" else ""
        sea = f", sea state ~{hs:.1f} m{wvia}" if hs > 0.05 else ""
        reshape = ""
        if hs > 0.05 and "wave_reshape" in rz:
            reshape = (" The sea state varies enough that the route bends toward calmer water."
                       if rz.get("wave_reshape") else
                       " The sea is near-uniform, so it only affects the ETA, not the route shape.")
        realized_text = (f"Achievable speed: routing at ~{pct}% of the flat-water polar "
                         f"(helm {helm}%{sea}) — ETAs are realistic, not theoretical. The gap to 100% "
                         "is the boatspeed left to find (trim, helm, sea-state technique)." + reshape)

    sail_plan_seq = [s["sail"] for s in (result.get("sail_plan") or []) if s.get("sail")]
    facts = {
        "race": race_name, "total_hours": result["total_hours"],
        "total_sailed_nm": result["total_sailed_nm"], "total_tacks": result["total_tacks"],
        "total_peels": result.get("total_peels"),
        "sail_plan": sail_plan_seq,            # the sails actually flown, in order (2g — real, not post-hoc)
        "route_confidence": result["route_confidence"], "min_confidence": result["min_confidence"],
        "wind_coverage": result.get("wind_coverage"),
        "degraded": result.get("degraded", False), "warnings": warnings,
        "models": [m["model"] for m in result["windfield"]["models"]],
        "current": cur if cur.get("loaded") else None,    # set & drift folded into the ETAs (2d)
        "realized": result.get("realized"),               # achievable speed = helm × sea state (2d-d)
        "roundings": roundings,
        "legs": [{"to": l["to"], "minutes": l["leg_minutes"], "point_of_sail": l["point_of_sail"],
                  "tacks": l["tacks"], "sail": l.get("sail"), "peels": l.get("peels"),
                  "wind": l["wind"]} for l in legs],
    }
    if API_KEY:
        try:
            import json

            from . import llm as lab_llm
            txt, _model = lab_llm.complete(
                system="You are a yacht race navigator writing a concise PRE-RACE routing briefing "
                       "for the crew from an optimizer result. Explain the recommended route leg by "
                       "leg, the wind story, where to expect tacks/gybes and sail changes, and — "
                       "importantly — call out where model CONFIDENCE is low (models disagree) so "
                       "the crew sails conservatively there. WIND LANGUAGE: never say 'veer'/'back' — "
                       "say the wind shifts RIGHT/LEFT, and give wind directions in degrees (baseline "
                       "and new value) rather than a bare change. State the required mark ROUNDINGS "
                       "explicitly (which side to leave each mark — from 'roundings'). If 'degraded' is "
                       "true or 'warnings' are present, OPEN with a clear forecast-reliability warning "
                       "(the wind data was sparse) before anything else. Be specific and brief; no preamble.",
                user="Optimizer result:\n" + json.dumps(facts, indent=2),
                max_tokens=1200,
            )
            if txt:
                return txt
        except Exception:
            pass
    # deterministic fallback
    lines = []
    if warnings:
        lines.append("⚠ DEGRADED FORECAST — read before trusting this route:" if result.get("degraded")
                     else "⚠ Notes:")
        lines += [f"  • {w}" for w in warnings]
        lines.append("")
    _peels = result.get("total_peels")
    lines += [f"Optimal route: {result['total_sailed_nm']} nm sailed, "
              f"~{result['total_hours']} h, {result['total_tacks']} tacks/gybes"
              f"{f', {_peels} sail peel(s)' if _peels else ''}.",
              f"Model agreement (confidence): {result['route_confidence']} "
              f"(lowest leg {result['min_confidence']}); wind coverage "
              f"{int((result.get('wind_coverage') or 0) * 100)}% of the route.", ""]
    if sail_plan_seq:
        lines.append(f"Sail plan: {' → '.join(sail_plan_seq)}.")
        lines.append("")
    if current_text:
        lines.append(current_text)
        lines.append("")
    if realized_text:
        lines.append(realized_text)
        lines.append("")
    if roundings_text:
        lines.append(f"Roundings: {roundings_text}.")
        lines.append("")
    for l in legs:
        w = l["wind"] or {}
        lines.append(f"• To {l['to']}: {l['leg_minutes']} min, {l['point_of_sail'] or '?'}, "
                     f"{l['tacks']} tacks; wind {w.get('tws','?')} kts @ {w.get('twd','?')}° "
                     f"(conf {w.get('confidence','?')}).")
    if result.get("skipped_marks"):
        lines.append("")
        lines.append("Marks skipped (no coordinates — review): " + ", ".join(result["skipped_marks"]))
    return "\n".join(lines)
