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
from . import polars as POL
from . import sailplan

HSTEP = 12          # heading fan resolution (deg)
SECTOR = 3.0        # isochrone pruning bucket (deg of bearing from leg start)
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
COVERAGE_MIN = float(os.environ.get("GRIB_COVERAGE_MIN", "0.6"))   # below this → degraded route
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
ROUND_OFFSET_NM = float(os.environ.get("ROUTE_ROUND_OFFSET_NM", "0.10"))  # standoff to the correct side at a port/stbd rounding mark (nm)
# --- finish/mark over-tack ("scramble") fixes (routing fidelity 2e) — each env-flagged for A/B ---
def _flag(name, default="1"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")
# #1 LAYLINE COMMIT: once a node can lay the mark (bears more than the VMG angle off the wind axis),
# drop the opposite-tack headings so it sails the layline instead of free-tacking up to the mark.
LAYLINE_COMMIT = _flag("ROUTE_LAYLINE_COMMIT")
LAYLINE_COMMIT_EPS = float(os.environ.get("ROUTE_LAYLINE_COMMIT_EPS", "2.0"))  # deg slack before committing
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
SAIL_AWARE = _flag("ROUTE_SAIL_AWARE")
PEEL_COST_S = float(os.environ.get("ROUTE_PEEL_COST_S", "90"))      # clock a sail peel costs (honest ETA)
# Prune regularizer (like TACK_PRUNE_S): a one-off per-peel penalty into the path score so the isochrone
# disfavors a course that needs an extra peel for ~equal gain — decoupled from the ETA cost above.
PEEL_PRUNE_S = float(os.environ.get("ROUTE_PEEL_PRUNE_S", "180"))
# Hysteresis: hold a sub-optimal sail until it's THIS fraction off the optimal sail's speed, then peel.
# A dead-band across the crossover so the route doesn't thrash on noise / a wind clocking past a boundary
# (you peel A2→A3 only when A3 is materially faster, not for the first 0.1 kn). > this → peel.
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
    if not P or twa < 30:
        return 0.0
    return min(P, key=lambda p: abs(p[0] - tws) + abs(p[1] - twa))[2]


def _wave_factor(hs, twa):
    """Speed-retention fraction (≤1) for a sea state `hs` (m) at this TWA. CONSERVATIVE by design: a
    DEADBAND (small chop costs nothing), then a gentle linear slope on the excess Hs, scaled by point of
    sail (a head sea slows you most, a following sea least), with a FLOOR so it can never run away.
    hs at/below the deadband → 1.0 (no route distortion from ripples)."""
    eff = hs - WAVE_HS_DEADBAND
    if eff <= 0:
        return 1.0
    a = abs(((twa + 180) % 360) - 180)
    k = WAVE_K_UP if a < 70 else (WAVE_K_REACH if a < 120 else WAVE_K_DOWN)
    return max(WAVE_FLOOR, 1.0 - k * eff)


def _realized_factor(hs, twa, helm):
    """The fraction of the FLAT-WATER polar the boat actually achieves here = helm skill × sea state."""
    return helm * _wave_factor(hs, twa)


def _sail_polar_key(sail):
    """The per-sail polar-curve key for a DISPLAY sail. The ORC cert rates ONE jib (J1); J2/J3 are the
    crew's change-downs sharing the upwind slot, so they map to the J1 curve for SPEED (the polar can't
    distinguish them — a jib change is a wind/depower call, not a speed one)."""
    return "J1" if sail and sail[0] == "J" else sail


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
    """A specific sail's polar boatspeed at (tws, twa) — nearest sample, 0 outside its rated TWA domain.
    This is the speed of HOLDING `sail` here; ≤ the envelope (which is the best sail's speed)."""
    key = _sail_polar_key(sail)
    pts = SP.get(key)
    if not pts:
        return 0.0
    lo, hi = dom.get(key, (0.0, 180.0))
    if twa < lo - SAIL_DOMAIN_MARGIN or twa > hi + SAIL_DOMAIN_MARGIN:
        return 0.0
    return min(pts, key=lambda p: abs(p[0] - tws) + abs(p[1] - twa))[2]


def _point_of_sail(twa):
    return "beat" if twa < 70 else ("reach" if twa < 130 else "run")


def _vmg_headings(P, tws, twd):
    """The VMG-optimal upwind (beat) and downwind (run) headings at this TWS, as compass headings
    relative to TWD. Injected into the heading fan so the router can sail the TRUE best-VMG tacking/
    gybing angle instead of being limited to the nearest coarse-grid heading — the routing-fidelity-2c
    'VMG gate'. Returns up to 4 headings (port+stbd × upwind+downwind)."""
    band = [(a, s) for t, a, s in P if abs(t - tws) <= 1.5 and s > 0]
    if not band:
        band = [(a, s) for _t, a, s in P if s > 0]
    if not band:
        return []
    out = []
    ups = [(s * math.cos(math.radians(a)), a) for a, s in band if a < 90]
    downs = [(-s * math.cos(math.radians(a)), a) for a, s in band if a > 90]
    if ups:
        beat = max(ups)[1]
        out += [(twd + beat) % 360, (twd - beat) % 360]
    if downs:
        run = max(downs)[1]
        out += [(twd + run) % 360, (twd - run) % 360]
    return out


def _vmg_twa(P, tws, pos):
    """The VMG-optimal TWA (deg off the wind) for a beat or run at this TWS — the half-angle of the
    layline cone. `pos` is 'beat' (upwind) or 'run' (downwind); None for reaches or no polar band."""
    band = [(a, s) for t, a, s in P if abs(t - tws) <= 1.5 and s > 0] or \
           [(a, s) for _t, a, s in P if s > 0]
    if not band:
        return None
    if pos == "beat":
        cands = [(s * math.cos(math.radians(a)), a) for a, s in band if a < 90]
    elif pos == "run":
        cands = [(-s * math.cos(math.radians(a)), a) for a, s in band if a > 90]
    else:
        return None
    return max(cands)[1] if cands else None


def _layline_pair(P, tws, twd, pos, mlat, mlon, length_nm):
    """The two laylines into a mark = the VMG-optimal approach corridor (the lines along which the
    boat, sailing its best beat/run angle on each board, just lays the mark). `pos` is 'beat' or
    'run'; returns up to 2 {tack, twa, pts:[[mlat,mlon],[endlat,endlon]]} extending the reciprocal of
    each VMG sailing heading back from the mark. Reaches have no meaningful laylines → []."""
    band = [(a, s) for t, a, s in P if abs(t - tws) <= 1.5 and s > 0] or \
           [(a, s) for _t, a, s in P if s > 0]
    if not band:
        return []
    if pos == "beat":
        cands = [(s * math.cos(math.radians(a)), a) for a, s in band if a < 90]
    elif pos == "run":
        cands = [(-s * math.cos(math.radians(a)), a) for a, s in band if a > 90]
    else:
        return []
    if not cands:
        return []
    vmg_twa = max(cands)[1]
    length_nm = max(0.5, min(length_nm, LAYLINE_NM))
    out = []
    for tack, h in (("stbd", (twd + vmg_twa) % 360), ("port", (twd - vmg_twa) % 360)):
        elat, elon = _advance(mlat, mlon, (h + 180) % 360, length_nm)
        out.append({"tack": tack, "twa": round(vmg_twa), "pos": pos,
                    "pts": [[round(mlat, 5), round(mlon, 5)], [round(elat, 5), round(elon, 5)]]})
    return out


# --- one leg -----------------------------------------------------------------
def route_leg(wf, P, slat, slon, t0, dlat, dlon, fallback=(12.0, 0.0), deadline=None,
              obstacles=None, capture=False, hstep=HSTEP, dt_cap=1.0, cur=None,
              sail_polars=None, jib_crossovers=None, start_sail=None,
              waves=None, helm_factor=1.0):
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
    `start_sail` = the sail the boat is already carrying into this leg (continuity across marks)."""
    direct = _hav_nm(slat, slon, dlat, dlon)
    SP = sail_polars or {}
    sail_aware = SAIL_AWARE and bool(SP)
    dom = _sail_domains(SP) if sail_aware else {}
    # realized (achievable) speed = helm × sea state; only do the work when it actually bites. Helm may
    # be >1.0 (boat rated soft / sails above the cert), so trigger on any departure from 1.0, not just <1.
    realized_on = (abs(helm_factor - 1.0) > 1e-3) or (waves is not None and getattr(waves, "loaded", False))

    def sail_step(cur_sail, tws, twa, sp_env):
        """Decide the sail + speed for one step from a node currently flying `cur_sail`. Returns
        (sail, speed, is_peel). HOLD the current sail (its own, slower-off-optimal speed) until it's
        PEEL_HOLD_TOL off the optimal sail, then PEEL to the optimal sail at full (envelope) speed +
        a peel cost. A jib change-down (same aero family) is a free relabel, not a routing peel."""
        opt = sailplan.optimal_sail(tws, twa, jib_crossovers)
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
        w = wf.wind_at(lat, lon, epoch)
        return w if w else fallback

    # Overstand axis (#2 layline gate): on a beat the mark is to windward, on a run to leeward — in
    # both cases sailing PAST the mark along the wind axis is overstanding (the "go past then double
    # back" the user sees near a mark). Classify the leg from the wind AT THE MARK (which sets the
    # laylines) and build a unit (north,east) vector pointing the OVERSTAND way; reaches → no gate.
    _twd_m = wind(dlat, dlon, t0)[1]
    _twa_m0 = abs(_wrap180(_bearing(slat, slon, dlat, dlon) - _twd_m))
    _pos0 = _point_of_sail(_twa_m0)
    leg_axis = None
    if LAYLINE_GATE and _pos0 in ("beat", "run"):
        wb = _twd_m if _pos0 == "beat" else (_twd_m + 180.0)        # windward (beat) / leeward (run) bearing
        leg_axis = (math.cos(math.radians(wb)), math.sin(math.radians(wb)))
    coslat = math.cos(math.radians(dlat))

    def expand(node, hdgs, tws, twd, dt_h, cand):
        """Fan `hdgs` from `node`, advancing each by its polar speed; keep the farthest-along candidate
        per bearing sector (the classic isochrone prune — it routes through shifts best and converges
        fast). Candidates that have sailed PAST the mark on the up/down-wind axis are rejected (the
        layline gate, #2) so the route lays the mark instead of overstanding and doubling back.
        Returns (n_placed, n_blocked) — 0-placed-but-blocked means this node is boxed in."""
        placed = blocked = 0
        for hdg in hdgs:
            twa = abs(_wrap180(hdg - twd))
            sp_env = _polar_speed(P, tws, twa)
            if sp_env < 0.3:
                continue
            # sail-aware: hold the current sail (its own per-sail speed) or peel to the optimal sail.
            sail, sp, is_peel = sail_step(node.get("sail"), tws, twa, sp_env)
            # realized: degrade to ACHIEVABLE speed (helm skill × sea state at this node/angle)
            if realized_on:
                hs = waves.wave_at(node["lat"], node["lon"], node["t"]) if waves is not None else 0.0
                sp *= _realized_factor(hs, twa, helm_factor)
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
            if is_tack and TACK_COST_S > 0:
                if TACK_CUMULATIVE:
                    pen += sp * (TACK_PRUNE_S / 3600.0)   # prune regularizer — suppresses the staircase
                    tack_s = TACK_COST_S                  # realistic ETA cost — don't inflate predicted time
                    tk += 1
                else:
                    step_nm = max(0.0, step_nm - sp * (TACK_COST_S / 3600.0))
            # peel cost (2g) — mirrors the cumulative tack cost: a one-off prune penalty + honest ETA per
            # sail change, so the isochrone disfavors a course needing an extra peel and predicted times
            # carry the change-down. The PEEL_HOLD_TOL hysteresis already stops boundary thrash.
            peel_s = 0.0
            if is_peel and PEEL_COST_S > 0:
                pen += sp * (PEEL_PRUNE_S / 3600.0)
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
            # layline gate: drop a candidate that has sailed PAST the mark along the wind axis.
            if leg_axis is not None:
                wp = (nlat - dlat) * 60.0 * leg_axis[0] + (nlon - dlon) * 60.0 * coslat * leg_axis[1]
                if wp > OVERSTAND_NM:
                    blocked += 1
                    continue
            rng = _hav_nm(slat, slon, nlat, nlon)
            # PRUNE KEY + SCORE. DMG_PRUNE (root-cause fix): rank by distance MADE GOOD toward the mark,
            # bucketed by CROSS-TRACK LANE off the start→mark rhumb — one leading node per lane, so a beat
            # lays the layline in one tack instead of ballooning sideways and weaving. Legacy paths:
            # #3 position-cell near the mark, else the classic bearing-from-start sector; both score by
            # distance-from-start. Score is net of the accumulated maneuver penalty (#2).
            if DMG_PRUNE:
                sec = ("dmg", round(_xtrack_nm(slat, slon, dlat, dlon, nlat, nlon) / LANE_NM))
                score = (direct - _hav_nm(nlat, nlon, dlat, dlon)) - pen
            elif MARK_POS_PRUNE and _hav_nm(nlat, nlon, dlat, dlon) <= MARK_PRUNE_NM:
                cell = MARK_PRUNE_CELL_NM / 60.0
                sec = ("p", round(nlat / cell), round(nlon / max(1e-6, cell * coslat)))
                score = rng - pen
            else:
                sec = round(_bearing(slat, slon, nlat, nlon) / SECTOR)
                score = rng - pen
            if sec not in cand or score > cand[sec]["rng_eff"]:
                cand[sec] = {"lat": nlat, "lon": nlon,
                             "t": node["t"] + dt_h * 3600 + tack_s + peel_s,
                             "parent": node, "hdg": hdg, "rng": rng, "rng_eff": score,
                             "pen": pen, "tk": tk, "sail": sail, "pl": pl}
            placed += 1
        return placed, blocked

    start = {"lat": slat, "lon": slon, "t": t0, "parent": None, "hdg": None, "sail": start_sail}
    frontier = [start]
    reached = None
    snaps = []
    for _ in range(max_steps):
        if deadline and time.time() > deadline:
            break
        cand = {}
        for node in frontier:
            tws, twd = wind(node["lat"], node["lon"], node["t"])
            dmark = _hav_nm(node["lat"], node["lon"], dlat, dlon)
            bmark = _bearing(node["lat"], node["lon"], dlat, dlon)
            twa_m = abs(_wrap180(bmark - twd))
            sp_m = _polar_speed(P, tws, twa_m)
            if sp_m > 0.3 and dmark <= sp_m * dt_h and not (
                    obstacles and obstacles.crosses(node["lat"], node["lon"], dlat, dlon)):
                reached = {"lat": dlat, "lon": dlon, "t": node["t"] + (dmark / sp_m) * 3600,
                           "parent": node, "hdg": bmark}
                break
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
                half = _vmg_twa(P, tws, _pos0)
                if half is not None:
                    axis = twd if _pos0 == "beat" else (twd + 180.0)
                    off = _wrap180(bmark - axis)
                    if abs(off) >= half - LAYLINE_COMMIT_EPS:
                        side = 1.0 if off > 0 else -1.0
                        committed = [h for h in fan
                                     if (1.0 if _wrap180(h - axis) >= 0 else -1.0) == side]
                        fan = committed or [bmark]   # never strand the node — keep the fetch heading
            placed, blocked = expand(node, fan, tws, twd, dt_h, cand)
            if placed == 0 and blocked > 0 and obstacles is not None:
                _p, blocked = expand(node, headings + vmg, tws, twd, dt_h, cand)
            blocked_hits += blocked
        if reached or not cand:
            break
        frontier = list(cand.values())
        if capture:
            snaps.append(frontier)
        best = min(frontier, key=lambda n: _hav_nm(n["lat"], n["lon"], dlat, dlon))
        if _hav_nm(best["lat"], best["lon"], dlat, dlon) < 0.05:
            reached = best
            break
    if not reached:
        reached = min(frontier, key=lambda n: _hav_nm(n["lat"], n["lon"], dlat, dlon))

    path, node, hdgs, sails, peels = [], reached, [], [], 0
    while node is not None:
        path.append({"lat": round(node["lat"], 5), "lon": round(node["lon"], 5), "t": node["t"]})
        sails.append(node.get("sail"))
        peels = max(peels, node.get("pl", 0))     # modeled peels accrue monotonically along the path
        if node["hdg"] is not None:
            hdgs.append(node["hdg"])
        node = node["parent"]
    path.reverse(); hdgs.reverse(); sails.reverse()
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
    # tacks/gybes = genuine port↔starboard crossings along the path. `hdgs[k]` is the heading sailed on
    # the segment LEAVING `path[k]` at `path[k]["t"]`, so classify the board against the wind LOCAL to
    # that point/time — NOT a single frozen leg-start wind. On a leg where the breeze clocks (e.g. a long
    # light beat) the stale-wind version miscounts every shift-following heading swing as a tack and badly
    # over-reports; sampling local wind makes the count the real maneuver tally (tacks upwind / gybes down).
    tacks = 0
    prev_side = None
    rf_sum = hs_sum = 0.0          # realized-factor + sea-state accumulators (the coaching number)
    for k, h in enumerate(hdgs):
        p = path[k] if k < len(path) else path[-1]
        w = wind(p["lat"], p["lon"], p["t"])
        side = "stbd" if _wrap180(w[1] - h) > 0 else "port"
        if prev_side and side != prev_side:
            tacks += 1
        prev_side = side
        if realized_on:
            hs = waves.wave_at(p["lat"], p["lon"], p["t"]) if waves is not None else 0.0
            rf_sum += _realized_factor(hs, abs(_wrap180(h - w[1])), helm_factor)
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
            "direct_nm": round(direct, 2), "tacks": tacks,
            "first_heading": round(hdgs[0]) if hdgs else None,
            "blocked_steps": blocked_hits, "isochrones": isochrones,
            "sail_track": sail_track, "peels": peels, "sail_end": sail_end,
            "realized_factor": realized_mean, "hs_mean": hs_mean}


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


# --- sparse-GRIB coverage gate + route-sanity guard --------------------------
def _wind_coverage(wf, full_path):
    """Fraction of the routed path that had REAL multi-model coverage (vs the optimizer's constant
    fallback wind). A sparse GRIB silently routes on `route_leg`'s fallback; this measures that."""
    if not full_path:
        return 0.0
    covered = sum(1 for p in full_path if wf.detail_at(p["lat"], p["lon"], p["t"]) is not None)
    return round(covered / len(full_path), 2)


def _route_sanity(wf, legs, coverage, P, timed_out):
    """Flag a route that's likely wrong because the wind field was sparse/degraded. Returns
    (warnings, degraded). `degraded` means: do not trust this route — the inputs were too thin."""
    warnings, degraded = [], False
    if not wf.loaded:
        warnings.append("No weather-model data loaded — the route ran entirely on a constant "
                        "fallback wind. Do NOT trust it; re-run when a model is posted.")
        degraded = True
    elif coverage < COVERAGE_MIN:
        warnings.append(f"Wind coverage only {int(coverage * 100)}% of the route — the remainder ran "
                        "on fallback wind. Treat the low-coverage legs as unreliable.")
        degraded = True
    pmax = max((s for _, _, s in P), default=0.0)
    for l in legs:
        mins = l.get("leg_minutes") or 0.0
        if mins > 0 and pmax > 0 and l.get("sailed_nm"):
            spd = l["sailed_nm"] / (mins / 60.0)
            if spd > pmax * 1.2:
                warnings.append(f"Leg to {l['to']} averages {spd:.1f} kn — above the boat's polar max "
                                f"(~{pmax:.0f} kn); almost certainly a wind-data gap.")
                degraded = True
        if l.get("wind") is None:
            warnings.append(f"Leg to {l['to']}: no model wind at its midpoint (sparse GRIB) — its "
                            "point-of-sail and sail call are fallbacks.")
    if timed_out:
        warnings.append("Optimizer hit its time budget — the route may be truncated; re-run for a "
                        "complete solution.")
    return warnings, degraded


# --- full course -------------------------------------------------------------
PER_MODEL_BUDGET_S = float(os.environ.get("ROUTE_PER_MODEL_BUDGET_S", "120"))  # total budget for the path fan


# Routing resolution presets (2.5): heading-fan degrees, per-leg step ceiling (h), time budget (s).
# Fine = sharper near shore / tight marks but slower; Fast = quicker, coarser; Auto = balanced default.
RESOLUTIONS = {
    "fast": {"hstep": 18, "dt_cap": 1.5, "budget": 60},
    "auto": {"hstep": 12, "dt_cap": 1.0, "budget": 90},
    "fine": {"hstep": 8, "dt_cap": 0.6, "budget": 200},
}


def _resolution(name):
    return RESOLUTIONS.get((name or "auto").strip().lower(), RESOLUTIONS["auto"])


def optimize_course(definition: dict, course_id, start_epoch, wf, time_budget_s=None,
                    obstacles=None, avoid=True, source=None, safety_depth=None,
                    jib_crossovers=None, emit_exploration=True, per_model=False,
                    resolution="auto", cur=None, waves=None, helm_factor=1.0,
                    polar_adjustments=None):
    """Route the whole course from its start through every mark to the finish via `wf`.

    Returns one optimal route with per-leg ETAs, total time/distance/tacks and a route confidence
    (mean of the wind field's per-point model agreement sampled along the path).

    `obstacles` (an ObstacleField) keeps the route off land/islands/exclusion-zones; if None and
    `avoid` is set, one is built from the course bbox + this race's zones + island marks. `source`
    (Natural Earth vs NOAA ENC) and `safety_depth` (the active boat draft + margin) flow into it."""
    marks, skipped, cid = race_def.course_to_marks(definition, course_id)
    if len(marks) < 2:
        return {"available": False, "note": "course needs at least a start and one mark/finish",
                "skipped": skipped}
    roundings = race_def.course_roundings(definition, course_id)   # #3 rounding side per nav mark
    P = POL.polars_stw()
    if not P:
        return {"available": False, "note": "no polars loaded"}
    P = POL.apply_adjustments(P, polar_adjustments)   # Lab-4 human-approved refined-polar overlay
    SP = POL.sail_polars()                 # per-sail curves for sail-aware routing (2g); {} → envelope only

    if obstacles is None and avoid:
        bbox = course_bbox(definition, course_id)
        if bbox:
            try:
                from .geo import build_for_course
                obstacles = build_for_course(definition, cid or course_id, bbox,
                                             source=source, safety_depth=safety_depth)
            except Exception:
                obstacles = None

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
    for seq, name, dlat, dlon in marks[1:]:
        # #3 rounding side: route to a small standoff on the legal side of a port/starboard mark (the
        # real mark is still displayed + recorded). Gates pass between, finish/none unchanged.
        rlat, rlon = _rounding_offset(slat, slon, dlat, dlon, roundings.get(name, "none"))
        leg = route_leg(wf, P, slat, slon, t, rlat, rlon, deadline=deadline, obstacles=obstacles,
                        capture=emit_exploration, hstep=rp["hstep"], dt_cap=rp["dt_cap"], cur=cur,
                        sail_polars=SP, jib_crossovers=jib_crossovers, start_sail=cur_sail,
                        waves=waves, helm_factor=helm_factor)
        # sample wind + confidence at the leg's midpoint and end (for the briefing)
        mid = leg["path"][len(leg["path"]) // 2] if leg["path"] else {"lat": dlat, "lon": dlon}
        det = wf.detail_at(mid["lat"], mid["lon"], (t + leg["eta"]) / 2.0)
        if det:
            confs.append(det["confidence"])
        twa = None
        if det:
            twa = abs(_wrap180(_bearing(slat, slon, dlat, dlon) - det["twd"]))
        pos = _point_of_sail(twa) if twa is not None else None
        legs.append({
            "to": name, "seq": seq,
            "direct_nm": leg["direct_nm"], "sailed_nm": leg["sailed_nm"], "tacks": leg["tacks"],
            "leg_minutes": round((leg["eta"] - t) / 60.0, 1),
            "eta_epoch": round(leg["eta"]),
            "first_heading": leg["first_heading"],
            "blocked_steps": leg.get("blocked_steps", 0),
            "point_of_sail": pos,
            "sail": (sailplan.optimal_sail(det["tws"], twa, jib_crossovers)
                     if det and twa is not None else None),
            "peels": leg.get("peels", 0),     # sail changes the router actually made on this leg (2g)
            "realized_factor": leg.get("realized_factor", 1.0),   # % of flat-water polar achieved (helm × sea state)
            "hs_mean": leg.get("hs_mean", 0.0),                   # mean sea state on this leg (m)
            "wind": ({"tws": det["tws"], "twd": det["twd"], "confidence": det["confidence"]}
                     if det else None),
        })
        # accumulate the real sails-flown track + carry the ending sail into the next leg (2g)
        route_track += leg.get("sail_track", [])
        cur_sail = leg.get("sail_end") or cur_sail
        full_path += [p for p in leg["path"][1:]]
        if emit_exploration:
            if len(isochrones) < ISO_MAX:
                isochrones += leg.get("isochrones", [])[:ISO_MAX - len(isochrones)]
            # laylines into this mark when the approach is a beat or run (reaches have none)
            if det and pos in ("beat", "run"):
                laylines += _layline_pair(P, det["tws"], det["twd"], pos, dlat, dlon,
                                          leg["direct_nm"])
        slat, slon, t = rlat, rlon, leg["eta"]   # continue from the rounding standoff (≈ the mark)

    total_min = round((t - float(start_epoch)) / 60.0, 1)
    timed_out = time.time() > deadline
    coverage = _wind_coverage(wf, full_path)
    warnings, degraded = _route_sanity(wf, legs, coverage, P, timed_out)
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
    if (helm_factor < 0.999) or (waves is not None and getattr(waves, "loaded", False)):
        wmin = [(l.get("realized_factor", 1.0), l.get("leg_minutes") or 0.0) for l in legs]
        tot = sum(m for _f, m in wmin) or 1.0
        realized = {
            "realized_pct": round(sum(f * m for f, m in wmin) / tot, 3),
            "helm_factor": round(helm_factor, 3),
            "sea_state_hs_mean": round(sum((l.get("hs_mean") or 0.0) * (l.get("leg_minutes") or 0.0)
                                           for l in legs) / tot, 2),
            "wave_source": waves.status().get("source") if waves is not None else None,
        }
    # per-model candidate paths (the confidence moat made VISUAL — the fan the blended route summarizes)
    candidate_paths = []
    if per_model:
        candidate_paths = _per_model_paths(definition, course_id, start_epoch, wf, obstacles, P, marks,
                                           source, safety_depth, jib_crossovers, total_min / 60.0,
                                           cur=cur, waves=waves, helm_factor=helm_factor)
    return {
        "available": True, "course_id": cid,
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
        "obstacles": obstacles.summary() if obstacles is not None else {"active": False},
        "obstacle_steps_avoided": sum(l.get("blocked_steps", 0) for l in legs),
        "timed_out": timed_out,
    }


def _per_model_paths(definition, course_id, start_epoch, wf, obstacles, P, marks,
                     source, safety_depth, jib_crossovers, blended_hours, cur=None,
                     waves=None, helm_factor=1.0):
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
                            obstacles=obstacles, avoid=False, source=source, safety_depth=safety_depth,
                            jib_crossovers=jib_crossovers, emit_exploration=False, per_model=False, cur=cur,
                            waves=waves, helm_factor=helm_factor)
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
            bits = f"set {cur.get('set_deg','?')}° / drift {cur.get('drift_kn','?')} kn"
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
        realized_text = (f"Achievable speed: routing at ~{pct}% of the flat-water polar "
                         f"(helm {helm}%{sea}) — ETAs are realistic, not theoretical. The gap to 100% "
                         "is the boatspeed left to find (trim, helm, sea-state technique).")

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
            import anthropic
            client = anthropic.Anthropic(api_key=API_KEY)
            resp = client.messages.create(
                model=MODEL, max_tokens=1200,
                system="You are a yacht race navigator writing a concise PRE-RACE routing briefing "
                       "for the crew from an optimizer result. Explain the recommended route leg by "
                       "leg, the wind story, where to expect tacks/gybes and sail changes, and — "
                       "importantly — call out where model CONFIDENCE is low (models disagree) so "
                       "the crew sails conservatively there. State the required mark ROUNDINGS "
                       "explicitly (which side to leave each mark — from 'roundings'). If 'degraded' is "
                       "true or 'warnings' are present, OPEN with a clear forecast-reliability warning "
                       "(the wind data was sparse) before anything else. Be specific and brief; no preamble.",
                messages=[{"role": "user", "content":
                           "Optimizer result:\n" + json.dumps(facts, indent=2)}],
            )
            txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
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
                     f"{l['tacks']} tacks; wind {w.get('tws','?')} kn @ {w.get('twd','?')}° "
                     f"(conf {w.get('confidence','?')}).")
    if result.get("skipped_marks"):
        lines.append("")
        lines.append("Marks skipped (no coordinates — review): " + ", ".join(result["skipped_marks"]))
    return "\n".join(lines)
