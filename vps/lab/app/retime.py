"""Re-time a FIXED route on another wind (#404).

The debrief needs "how long would the plan's own route have taken on the wind that actually
blew?". That splits the old regret (plan's forecast ETA minus the hindsight-best route) into:
  - forecast error: plan's forecast ETA vs the same route sailed on the real wind, and
  - the route call:  the plan's route vs the best route, both on the real wind.

`sail_path` walks a route's own points in order and times each stretch with the boat model the
router uses (polar envelope with the approved adjustments, helm x sea state, along-track current,
the boat's tack cost). A stretch the wind no longer lets the boat point at is beaten (or gybed)
at the best VMG angle, so a route planned on a different wind can still be sailed.

It is simpler than the isochrone router (no sail-aware peels, no cross-current crab), so the
debrief never compares a re-timed number with a routed one: it re-times BOTH routes with this
and scales by the routed best route's own time (`calibrated_hours`), so the simplification
cancels out.
"""
import math

from . import optimizer as OPT
from . import polars as POL

STEP_NM = 1.0             # longest stretch timed on one wind sample
MIN_KN = 0.3              # below this the boat is treated as becalmed at this speed
MAX_MISSING = 0.2         # more than this share of the distance with no wind: refuse
CAL_RANGE = (0.75, 1.33)  # re-timer vs router on the same route; outside this, don't trust it


def boat_table(boat_polar, polar_adjustments=None):
    """The envelope polar the router uses: the boat's own, with the approved adjustments."""
    P = POL.polars_stw(boat_polar)
    if not P:
        return None
    return POL.apply_adjustments(P, polar_adjustments)


def _along_speed(P, tws, twa):
    """Speed made good along a bearing `twa` off the wind: the polar where the boat can point
    there, else two boards at the best VMG angle (beating or gybing).

    Two boards at the VMG angle are the best possible inside it, not an approximation: in
    velocity space the boat can make any mix of two headings, and the upwind edge of that
    hull is the straight segment between the two VMG-angle points, both at the same upwind
    component V(beat)·cos(beat). Along a bearing `twa` inside it that gives
    V(beat)·cos(beat)/cos(twa); pinching straight at `twa` can't beat it, or `beat` wouldn't be
    the VMG angle. Same downwind. `test_debrief_weather_basis` checks this against a brute-force
    search over every pair of headings (Codex on #412 asked)."""
    beat = OPT._vmg_twa_cached(P, tws, "beat")
    run = OPT._vmg_twa_cached(P, tws, "run")
    if beat is not None and twa < beat:
        return (OPT._polar_speed(P, tws, beat) * math.cos(math.radians(beat))
                / max(0.05, math.cos(math.radians(twa)))), beat
    if run is not None and twa > run:
        return (OPT._polar_speed(P, tws, run) * abs(math.cos(math.radians(run)))
                / max(0.05, abs(math.cos(math.radians(twa))))), run
    return OPT._polar_speed(P, tws, twa), twa


def _forecast_wind(wf, lat, lon, t):
    """(tws, twd) where the field really has wind for this time, else None. A field past its last
    frame hands back the edge frame as `held` (Codex on #412): that is stale wind, so it counts
    as missing here, not as a normal sample."""
    detail = getattr(wf, "detail_at", None)
    if detail is None:
        return wf.wind_at(lat, lon, t)
    d = detail(lat, lon, t)
    if not d or d.get("held"):
        return None
    return (d["tws"], d["twd"])


def sail_path(path, wf, P, start_epoch, helm_factor=1.0, cur=None, waves=None,
              wave_coeffs=None, tack_s=None):
    """Sail `path` ([{lat, lon}, ...], in order) from `start_epoch` on `wf` → {hours, missing}
    or None when the route can't be timed (too short, no polar, or too much of it has no wind).
    `missing` is the share of the distance sailed on borrowed or held (past the field's frames)
    wind; past MAX_MISSING the route isn't timed."""
    pts = [(p["lat"], p["lon"]) for p in (path or [])
           if p and p.get("lat") is not None and p.get("lon") is not None]
    if len(pts) < 2 or not P:
        return None
    tack_s = OPT.tack_cost_s() if tack_s is None else tack_s
    wave_on = waves is not None and getattr(waves, "loaded", False)
    t = float(start_epoch)
    total = missing = 0.0
    last_wind, last_side = None, None
    for (lat0, lon0), (lat1, lon1) in zip(pts, pts[1:]):
        seg = OPT._hav_nm(lat0, lon0, lat1, lon1)
        if seg < 1e-4:
            continue
        brg = OPT._bearing(lat0, lon0, lat1, lon1)
        n = max(1, int(math.ceil(seg / STEP_NM)))
        step = seg / n
        for k in range(n):
            f = (k + 0.5) / n
            lat, lon = lat0 + (lat1 - lat0) * f, lon0 + (lon1 - lon0) * f
            w = _forecast_wind(wf, lat, lon, t)
            if not w:
                # borrowed (nearby) or held (stale) wind still times the stretch, but counts missing
                w = OPT._nearby_wind(wf, lat, lon, t) or wf.wind_at(lat, lon, t) or last_wind
                missing += step
            if not w:
                return None
            last_wind = w
            tws, twd = w
            rel = OPT._wrap180(brg - twd)
            twa = abs(rel)
            sp, sailed_twa = _along_speed(P, tws, twa)
            if helm_factor != 1.0 or wave_on:
                hs = waves.wave_at(lat, lon, t) if wave_on else 0.0
                sp *= OPT._realized_factor(hs, sailed_twa, helm_factor, wave_coeffs)
            if cur is not None:
                cset, cdrift = cur.current_at(lat, lon, t)
                if cdrift and cdrift > 0.01:
                    sp += cdrift * math.cos(math.radians(cset - brg))
            sp = max(MIN_KN, sp)
            t += step / sp * 3600.0
            # a tack (or gybe) where the route itself changes tack — the router charges the same
            side = rel > 0
            if last_side is not None and side != last_side and k == 0:
                t += tack_s
            last_side = side
        total += seg
    if total <= 0 or missing / total > MAX_MISSING:
        return None
    return {"hours": (t - float(start_epoch)) / 3600.0, "missing": round(missing / total, 3)}


def calibrated_hours(plan_path, oracle_path, oracle_hours, wf, P, start_epoch, **kw):
    """The plan's route on `wf`, in the router's own hours → (hours, note). Both routes are
    re-timed the same way and the plan's time is scaled by the routed best route's time over its
    own re-timed time, so what's left is the difference between the routes, not between the
    re-timer and the router. (None, why) when it can't be done honestly."""
    if not oracle_hours or not plan_path or not oracle_path:
        return None, "no saved route to re-time"
    plan = sail_path(plan_path, wf, P, start_epoch, **kw)
    best = sail_path(oracle_path, wf, P, start_epoch, **kw)
    if not plan or not best or best["hours"] <= 0:
        return None, "not enough wind along the routes to re-time them"
    ratio = oracle_hours / best["hours"]
    if not CAL_RANGE[0] <= ratio <= CAL_RANGE[1]:
        return None, "the re-timed best route is too far from its routed time"
    return plan["hours"] * ratio, None
