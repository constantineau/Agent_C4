"""Tactical layer (5.3) — wind shifts, favored side, leverage.

Tracks true wind direction over a rolling window to tell whether the current tack is LIFTED
or HEADED, whether the breeze is oscillating or in a persistent trend (and thus which SIDE of
the beat is favored), and how much LEVERAGE the boat has banked (cross-track from the rhumb
line to the next mark). This is the "where to be on the course" tactical brain.

RRS 41: this is shore-derived tactical advice — fine for practice, deliveries and debriefs,
but may be prohibited "outside help" in a race. The iPad only surfaces it in PRACTICE mode;
the agent caveats it. Computation always runs so debriefs have the data.
"""
import math

from shared import windphrase as wp
from . import navigator as NAV
from . import datasource

WINDOW_MIN = 12
# At least this share of the window must hold wind (#247): 6 samples were enough before, which
# for a 10 Hz channel is 0.6 s of data in 12 min, so a shift and a favoured side were called
# straight after a restart or a sensor coming back. Counted in 30 s buckets, so the cloud's 15 s
# aggregates fill every bucket.
MIN_COVERAGE = 0.5
_BUCKET_S = 30.0
SR33_LEN_FT = 33.0      # ~10 m → boatlengths of leverage


def _twd_series(minutes):
    series = datasource.active().series("environment.wind.directionTrue", minutes)
    return [(t, math.degrees(v) % 360) for t, v in series]


def _coverage(series, newest_age_s=None):
    """Share of the WINDOW_MIN window (ending now) that has at least one sample per 30 s bucket.
    The window's end is the newest sample plus its age when known, else the newest sample."""
    if not series:
        return 0.0
    end = series[-1][0] + (newest_age_s or 0.0)
    n = int(WINDOW_MIN * 60 / _BUCKET_S)
    filled = {int((end - t) // _BUCKET_S) for t, _v in series if 0 <= end - t < n * _BUCKET_S}
    return len(filled) / n


def _circ_mean(degs):
    s = sum(math.sin(math.radians(d)) for d in degs)
    c = sum(math.cos(math.radians(d)) for d in degs)
    return math.degrees(math.atan2(s, c)) % 360


def _slope(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var < 1e-9:
        return 0.0
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / var


def _cross_track_nm(a, b, plat, plon):
    R = 3440.065
    d13 = NAV._hav_nm(a["lat"], a["lon"], plat, plon) / R
    b13 = math.radians(NAV._bearing(a["lat"], a["lon"], plat, plon))
    b12 = math.radians(NAV._bearing(a["lat"], a["lon"], b["lat"], b["lon"]))
    return math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(b13 - b12)))) * R


def get_tactics(route: str = None):
    # Every input judged against its channel's dead limit (#247): aboard 15 s for wind and
    # heading, 10 s for the fix. Until then the newest TWD had no age check at all, so a dead
    # masthead kept making shift calls off its last reading.
    raw = NAV._latest()
    s = NAV._fresh(raw)
    if raw.get("twd") is not None and s["twd"] is None:
        age = (raw.get("ages") or {}).get("twd")
        return {"available": False, "note": "Wind too old for a shift call.",
                "wind_age_s": None if age is None else round(age)}
    if s["twd"] is None or s["lat"] is None:
        return {"available": False, "note": "Need live wind and position."}
    series = _twd_series(WINDOW_MIN)
    if len(series) < 6 or _coverage(series, s.get("ages", {}).get("twd")) < MIN_COVERAGE:
        return {"available": False, "note": "Not enough wind history yet."}

    twds = [v for _, v in series]
    mean = _circ_mean(twds)
    cur = s["twd"]
    devs = [NAV._wrap180(v - mean) for v in twds]
    shift = NAV._wrap180(cur - mean)
    osc = max(devs) - min(devs)
    t0 = series[0][0]
    slope = _slope([(t - t0) / 60 for t, _ in series], devs)     # deg/min
    span_min = (series[-1][0] - t0) / 60
    trend = "steady" if abs(slope) <= 0.2 else ("right" if slope > 0 else "left")  # compass-shift dir
    sign = 1 if slope > 0 else -1                                                  # +1 = shifted RIGHT
    # ONE sign drives the favoured side, the phase AND the sentence (#159). The sentence reads
    # now-vs-mean (describe_shift), so a shift only counts as lasting when now-vs-mean points the
    # same way as the slope. At the end of an oscillation they disagree (wind went right for 12 min,
    # then swung back left): that is not a lasting shift either way.
    persistent = (abs(slope) * span_min > max(3.0, osc * 0.6)
                  and wp.shift_sign(mean, cur) == sign)

    # current tack + point of sail (from the boat's true wind angle) + lifted/headed
    hdg = s["heading"] if s["heading"] is not None else s["cog"]
    tack = "—"
    phase = "even"
    pos = "upwind"                          # default when we can't see heading (conservative)
    if hdg is not None:
        rel = NAV._wrap180(cur - hdg)        # wind source vs bow; >0 → wind from stbd
        twa = abs(rel)
        pos = "upwind" if twa < 70 else ("downwind" if twa > 110 else "reach")
        tack = "starboard" if rel > 0 else "port"
        phase = wp.phase_on_tack(sign, tack, pos) if persistent else "even"

    # favored side — POINT-OF-SAIL aware (a right shift favours the right of a beat, the left of a run)
    if persistent and hdg is None:
        # No live heading or COG (both past their dead limit, #247): the point of sail is a
        # guess, and a right shift favours opposite sides upwind and downwind. No side call.
        favored = "either"
        fav_reason = "lasting shift, but no live heading to tell which side"
    elif persistent:
        favored = wp.favored_side(sign, pos) or "either"
        fav_reason = f"lasting shift {abs(round(slope, 1))}°/min"
    else:
        favored = "either"
        fav_reason = f"oscillating ±{round(osc / 2)}°, play the shifts"

    # leverage: cross-track from the current leg's rhumb line
    nav = NAV.get_navigator(route)
    leverage = None
    if nav.get("available"):
        marks = NAV._marks(nav["route"])
        nxt = next((m for m in marks if m["name"] == nav["next_mark"]["name"]), None)
        if nxt is not None:
            i = marks.index(nxt)
            start = marks[i - 1] if i > 0 else None
            if start:
                xte = _cross_track_nm(start, nxt, s["lat"], s["lon"])
                leverage = {"nm": round(abs(xte), 2), "side": "right" if xte > 0 else "left",
                            "boatlengths": round(abs(xte) * 6076 / SR33_LEN_FT)}

    # recommendation — the shared, racer-native shift phrasing (boat-frame first, baseline→now, then
    # the point-of-sail-aware favoured side), plus leverage.
    if persistent and hdg is None:
        # no point of sail: say what the wind did, never which side (see favored above)
        bits = [f"wind shifted {wp.shift_word(sign)}, from {round(mean) % 360}° to "
                f"{round(cur) % 360}°. No live heading, so no side call."]
    else:
        bits = [wp.describe_shift(mean, cur, tack=tack if tack in ("port", "starboard") else None,
                                  pos=pos, persistent=persistent, oscillation_deg=osc)]
    if leverage and leverage["nm"] >= 0.02:
        bits.append(f"{leverage['boatlengths']} BL {leverage['side']} leverage.")

    return {
        "available": True, "route": nav.get("route"),
        "tack": tack, "phase": phase, "point_of_sail": pos,
        "wind": {"now": round(cur, 1), "mean_12min": round(mean, 1),
                 "shift_deg": round(shift, 1), "oscillation_deg": round(osc, 1),
                 "trend": trend, "slope_deg_min": round(slope, 2), "persistent": persistent},
        "favored_side": favored, "favored_reason": fav_reason,
        "leverage": leverage,
        "recommendation": " ".join(bits),
        "side_unknown": bool(persistent and hdg is None),     # #247: the selector withdraws
        "caveat": "Practice use only. May count as outside help in a race (RRS 41).",
    }
