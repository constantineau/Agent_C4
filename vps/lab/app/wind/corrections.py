"""Manual per-model corrections (#384) — the navigator's "this forecast is 10° left, 2 kn light".

Each weather model can carry one correction, applied in the wind field to that model's own wind
BEFORE the blend (after its venue-skill de-bias):

  * `rotate_deg` — turn the wind direction. + = shifted right (clockwise), − = shifted left.
  * `scale_pct`  — wind speed as a percent of the forecast (100 = unchanged, 80 = 20 % lighter).
  * `shift_h`    — time shift in hours. +1 = the weather comes 1 h later than forecast, so at time
                   t the model is sampled at t − 1 h.
  * `fade_min` / `fade_from` — optional fade back to the raw forecast: full correction until
                   `fade_from` (epoch s; blank = the route's start), then linear to none at
                   `fade_from + fade_min` minutes, raw forecast after that.

One implementation, used by the Lab route and the Orin re-route (both build the wind field through
`windfield.build_windfield`). Pure: no Lab imports.
"""
from __future__ import annotations

import datetime as dt
import math

ROTATE_MAX = 90.0          # deg either way
SCALE_MIN, SCALE_MAX = 25.0, 300.0
SHIFT_MAX = 24.0           # hours either way
FADE_MAX = 7 * 24 * 60     # minutes
EPOCH_MIN, EPOCH_MAX = 0.0, 4_102_444_800.0   # 1970 .. 2100: a fade start outside is not a time


def _num(v, lo, hi, default):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(x):
        return default
    return max(lo, min(hi, x))


def normalize(raw):
    """A request's corrections → {model: {rotate_deg, scale_pct, shift_h, fade_min, fade_from}}.

    Accepts None, a dict keyed by model, or a list of {model, ...}. Values are clamped to sane
    ranges; a model whose correction changes nothing is dropped, so an empty dict = raw forecasts."""
    if isinstance(raw, list):
        raw = {str(c.get("model")): c for c in raw if isinstance(c, dict) and c.get("model")}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for model, c in raw.items():
        if not isinstance(c, dict) or not model:
            continue
        rot = round(_num(c.get("rotate_deg"), -ROTATE_MAX, ROTATE_MAX, 0.0), 1)
        scale = round(_num(c.get("scale_pct"), SCALE_MIN, SCALE_MAX, 100.0), 1)
        shift = round(_num(c.get("shift_h"), -SHIFT_MAX, SHIFT_MAX, 0.0), 2)
        if rot == 0 and scale == 100 and shift == 0:
            continue
        fade = _num(c.get("fade_min"), 0.0, FADE_MAX, 0.0)
        fade_from = c.get("fade_from")
        try:
            fade_from = float(fade_from) if fade_from not in (None, "") else None
        except (TypeError, ValueError):
            fade_from = None
        if fade_from is not None and not (math.isfinite(fade_from)
                                          and EPOCH_MIN <= fade_from <= EPOCH_MAX):
            fade_from = None                  # not a real time → the fade starts at the route start
        out[str(model).lower()] = {"rotate_deg": rot, "scale_pct": scale, "shift_h": shift,
                                   "fade_min": round(fade, 1) if fade > 0 else None,
                                   "fade_from": fade_from if fade > 0 else None}
    return out


def weight(c, epoch, t_start=None):
    """How much of the correction applies at `epoch`: 1 before the fade starts, linear down to 0 at
    `fade_from + fade_min`, 0 after. No fade → always 1."""
    if not c:
        return 0.0
    fade = c.get("fade_min")
    if not fade:
        return 1.0
    t0 = c.get("fade_from")
    if t0 is None:
        t0 = t_start if t_start is not None else epoch
    f = (float(epoch) - float(t0)) / (float(fade) * 60.0)
    return max(0.0, min(1.0, 1.0 - f))


def fetch_window(c, t_start, t_end):
    """The forecast valid times a model must be loaded over so its time shift has frames: a +6 h
    shift reads the model 6 h before the route start, a −6 h one 6 h past the end. The fade only
    ever shrinks the shift, so the full shift bounds it."""
    sh = (c or {}).get("shift_h") or 0.0
    return t_start - max(0.0, sh) * 3600.0, t_end + max(0.0, -sh) * 3600.0


def sample_epoch(c, epoch, t_start=None):
    """The model time to sample for real time `epoch`: shifted back by `shift_h` (faded)."""
    if not c or not c.get("shift_h"):
        return epoch
    return epoch - c["shift_h"] * 3600.0 * weight(c, epoch, t_start)


def apply_uv(c, u, v, epoch, t_start=None):
    """Rotate + scale one model's (u, v) m/s at real time `epoch` (faded). → (u, v)."""
    if not c:
        return u, v
    w = weight(c, epoch, t_start)
    if w <= 0:
        return u, v
    rot = (c.get("rotate_deg") or 0.0) * w
    k = 1.0 + ((c.get("scale_pct") or 100.0) / 100.0 - 1.0) * w
    if rot == 0 and k == 1.0:
        return u, v
    tws = math.hypot(u, v) * k
    twd = ((270.0 - math.degrees(math.atan2(v, u))) + rot) % 360.0     # FROM bearing, turned
    rad = math.radians(twd)
    return (-tws * math.sin(rad), -tws * math.cos(rad))


def _fmt_num(x):
    return f"{x:g}"


def _hhmm(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%d %b %H:%MZ")


def describe_one(model, c):
    """Plain text for one model's correction, e.g. "GFS: 10° right, speed 80%, 1 h later,
    back to raw over 60 min from start"."""
    bits = []
    rot = c.get("rotate_deg") or 0
    if rot:
        bits.append(f"{_fmt_num(abs(rot))}° {'right' if rot > 0 else 'left'}")
    scale = c.get("scale_pct")
    if scale is not None and scale != 100:
        bits.append(f"speed {_fmt_num(scale)}%")
    sh = c.get("shift_h") or 0
    if sh:
        bits.append(f"{_fmt_num(abs(sh))} h {'later' if sh > 0 else 'earlier'}")
    if c.get("fade_min"):
        when = _hhmm(c["fade_from"]) if c.get("fade_from") is not None else "start"
        bits.append(f"back to raw over {_fmt_num(c['fade_min'])} min from {when}")
    return f"{str(model).upper()}: " + ", ".join(bits)


def describe(corrections):
    """One plain line per corrected model (sorted). Empty list = raw forecasts."""
    return [describe_one(m, c) for m, c in sorted((corrections or {}).items())]


def summary(corrections, used_models=None):
    """The block a result carries: what was asked, in words. `used_models` (the models that
    actually loaded) marks a correction for a model that gave no wind as unused."""
    corr = corrections or {}
    if not corr:
        return None
    used = None if used_models is None else {str(m).lower() for m in used_models}
    items = []
    for m, c in sorted(corr.items()):
        row = dict(c, model=m, text=describe_one(m, c))
        if used is not None and m not in used:
            row["unused"] = True
        items.append(row)
    applied = [i["text"] for i in items if not i.get("unused")]
    return {"applied": bool(applied), "items": items, "text": applied}
