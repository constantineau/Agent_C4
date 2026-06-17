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

from . import navigator as NAV
from . import datasource

WINDOW_MIN = 12
SR33_LEN_FT = 33.0      # ~10 m → boatlengths of leverage


def _twd_series(minutes):
    series = datasource.active().series("environment.wind.directionTrue", minutes)
    return [(t, math.degrees(v) % 360) for t, v in series]


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
    s = NAV._latest()
    if s["twd"] is None or s["lat"] is None:
        return {"available": False, "note": "need live wind + position for tactics"}
    series = _twd_series(WINDOW_MIN)
    if len(series) < 6:
        return {"available": False, "note": "not enough wind history yet (building baseline)"}

    twds = [v for _, v in series]
    mean = _circ_mean(twds)
    cur = s["twd"]
    devs = [NAV._wrap180(v - mean) for v in twds]
    shift = NAV._wrap180(cur - mean)
    osc = max(devs) - min(devs)
    t0 = series[0][0]
    slope = _slope([(t - t0) / 60 for t, _ in series], devs)     # deg/min
    span_min = (series[-1][0] - t0) / 60
    persistent = abs(slope) * span_min > max(3.0, osc * 0.6)
    trend = "veering" if slope > 0.2 else ("backing" if slope < -0.2 else "steady")

    # current tack + lifted/headed (a veer lifts starboard, heads port)
    hdg = s["heading"] if s["heading"] is not None else s["cog"]
    tack = "—"
    phase = "even"
    if hdg is not None:
        rel = NAV._wrap180(cur - hdg)        # wind source vs bow; >0 → wind from stbd
        tack = "starboard" if rel > 0 else "port"
        lift = shift if tack == "starboard" else -shift
        phase = "lifted" if lift > 1.5 else ("headed" if lift < -1.5 else "even")

    # favored side
    if persistent:
        favored = "right" if slope > 0 else "left"
        fav_reason = f"persistent {trend} {abs(round(slope, 1))}°/min"
    else:
        favored = "either"
        fav_reason = f"oscillating ±{round(osc / 2)}° — play the shifts, tack on the headers"

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

    # recommendation
    bits = []
    if phase == "headed":
        bits.append(f"Headed {abs(round(shift))}° on {tack} — look for the tack onto the lift.")
    elif phase == "lifted":
        bits.append(f"Lifted on {tack} — stay with it.")
    else:
        bits.append(f"Even on {tack}.")
    if favored == "either":
        bits.append(fav_reason + ".")
    else:
        bits.append(f"{favored.capitalize()} side favored ({fav_reason}).")
    if leverage and leverage["nm"] >= 0.02:
        bits.append(f"{leverage['boatlengths']} BL {leverage['side']} leverage.")

    return {
        "available": True, "route": nav.get("route"),
        "tack": tack, "phase": phase,
        "wind": {"now": round(cur, 1), "mean_12min": round(mean, 1),
                 "shift_deg": round(shift, 1), "oscillation_deg": round(osc, 1),
                 "trend": trend, "slope_deg_min": round(slope, 2), "persistent": persistent},
        "favored_side": favored, "favored_reason": fav_reason,
        "leverage": leverage,
        "recommendation": " ".join(bits),
        "caveat": "Tactical advice — practice/debrief use; may be 'outside help' under RRS 41 in a race.",
    }
