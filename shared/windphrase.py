"""Canonical, racer-native language for wind direction + shifts.

SINGLE SOURCE OF TRUTH shared by the engine (tactics/selector/strategy/drift), the onboard coach
narration, AND the LoRA training-snapshot generator — so the model is TRAINED on the exact language
the engine EMITS (no train/serve vocabulary drift). Pure stdlib so it imports anywhere the repo is
present.

Conventions (agreed with the skipper):
  * NO 'veer' / 'back' in any human- or model-facing text — say the wind shifted RIGHT / LEFT
    (clockwise / counter-clockwise on the compass).
  * ALWAYS state the baseline -> now pair in whole degrees: "from 250° to 265°" — never a bare delta
    and never a bare current value.
  * Lead with the BOAT-FRAME consequence when the tack is known: LIFTED / HEADED on <tack>.
  * The favoured side is POINT-OF-SAIL aware: a RIGHT shift favours the RIGHT of a beat but the LEFT
    of a run (and a LEFT shift the reverse). Reaches have no favoured side.
"""

_UP = ("upwind", "beat", "windward")
_DOWN = ("downwind", "run", "leeward")


def _wrap180(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


def _norm360(d: float) -> int:
    return round(d) % 360


def shift_sign(from_twd: float, to_twd: float) -> int:
    """+1 = wind shifted RIGHT (clockwise), -1 = LEFT (counter-clockwise), 0 = steady (< ~2°)."""
    d = _wrap180(to_twd - from_twd)
    return 1 if d > 1.5 else (-1 if d < -1.5 else 0)


def shift_word(sign: int) -> str:
    return {1: "right", -1: "left", 0: "steady"}[sign]


def point_of_sail(leg) -> str:
    """Map a leg label (or an explicit 'upwind'/'downwind'/'reach') to one of those three."""
    s = (leg or "").lower()
    if any(w in s for w in _UP):
        return "upwind"
    if any(w in s for w in _DOWN):
        return "downwind"
    if "reach" in s:
        return "reach"
    return "upwind"      # conservative default — most tactical shift talk is on the beat


def favored_side(sign: int, pos: str):
    """Which side of the course a compass shift favours, POINT-OF-SAIL aware.
    upwind: right shift -> right; downwind: right shift -> left; reach: neither."""
    if sign == 0 or pos == "reach":
        return None
    if pos == "downwind":
        sign = -sign
    return "right" if sign > 0 else "left"


def phase_on_tack(sign: int, tack: str, pos: str) -> str:
    """LIFTED / HEADED on the given tack for a compass shift. Upwind a RIGHT shift lifts starboard
    and heads port; downwind the sense inverts. 'even' if steady or the tack is unknown."""
    if sign == 0 or tack not in ("port", "starboard"):
        return "even"
    up = sign if pos != "downwind" else -sign          # effective upwind-sense rotation
    lift = up if tack == "starboard" else -up
    return "lifted" if lift > 0 else "headed"


def _side_of(pos: str) -> str:
    return "beat" if pos == "upwind" else ("run" if pos == "downwind" else "leg")


def describe_shift(from_twd, to_twd, *, tack=None, leg=None, pos=None,
                   persistent=True, oscillation_deg=None) -> str:
    """The on-water wind-shift read, crew-facing. Boat-frame first, then the baseline->now
    observation, then the point-of-sail-aware favoured side."""
    pos = pos or point_of_sail(leg)
    a, b = _norm360(from_twd), _norm360(to_twd)
    sign = shift_sign(from_twd, to_twd)
    mag = abs(round(_wrap180(to_twd - from_twd)))

    if not persistent:
        osc = round((oscillation_deg or 0) / 2)
        if osc:
            return f"oscillating ±{osc}° around {b}° — no persistent shift; play the shifts, tack on the headers"
        return f"wind steady near {b}° — no persistent shift"
    if sign == 0:
        return f"wind steady near {b}° — no persistent shift"

    obs = f"wind shifted {shift_word(sign)}, from {a}° to {b}°"
    lead = ""
    ph = phase_on_tack(sign, tack, pos)
    if ph in ("lifted", "headed") and tack in ("port", "starboard"):
        lead = f"{ph.upper()} on {tack} ~{mag}° and holding — "
    fav = favored_side(sign, pos)
    tail = f" → {fav} side of the {_side_of(pos)} favored" if fav else ""
    return f"{lead}{obs}{tail}"


def describe_drift(ref_twd, now_twd, *, tws_change_kn=None, leg=None, pos=None) -> str:
    """Forecast-drift read: the common forecast the plan rests on has MOVED since it was frozen.
    Always states the frozen reference -> now pair in degrees."""
    pos = pos or point_of_sail(leg)
    a, b = _norm360(ref_twd), _norm360(now_twd)
    sign = shift_sign(ref_twd, now_twd)
    mag = abs(round(_wrap180(now_twd - ref_twd)))

    if sign == 0:
        base = f"forecast holding near {b}° since the plan was frozen"
    else:
        base = f"forecast has moved {shift_word(sign)} — was {a}°, now {b}° (~{mag}°)"
    if tws_change_kn:
        base += (f", {'building' if tws_change_kn > 0 else 'easing'} "
                 f"{'+' if tws_change_kn > 0 else ''}{round(tws_change_kn, 1)} kn")
    fav = favored_side(sign, pos)
    if fav:
        base += f" — would favour the {fav}"
    return base
