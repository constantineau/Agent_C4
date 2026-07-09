"""Wind TREND — deterministic rate-of-change of the boat's own observed wind.

The dashboard sparkline shows the crew the last 3.5 h of TWS; this module turns the same archive
into NUMBERS the other layers can cite: how fast the breeze is building/fading (kn per hour) and
which way it has been walking (degrees per hour, signed right/left), over a short (1 h) and a long
(3 h) window. Purely an OBSERVATION of own instruments — no forecast, no thresholds, no alarm
status; the consumers (strategy picture, play predicates, the copilot's sail-window and recap
briefs) decide what a rate means against the frozen boat model / playbook.

Method: bucket means, not endpoint samples — the head and tail `TREND_BUCKET_MIN` slices of each
window are averaged (TWD circularly, via vector mean) and the rate is their difference over the
time between bucket centers. Robust to a gusty sample or a tack right at the window edge. A window
whose data doesn't span at least half its length reports None (fresh boot / thin archive) — a
consumer that needs the trend simply doesn't get one rather than getting a lie.

Matcher signals contributed (per-hour, 1 h window): `tws_trend_kn_per_hr`,
`twd_trend_deg_per_hr` (signed, + = walking right). TIER-1: own archive only, legal in-race.
"""
from __future__ import annotations

import math
import os

from . import datasource

TREND_BUCKET_MIN = float(os.environ.get("TREND_BUCKET_MIN", "15"))
TREND_MIN_SAMPLES = int(os.environ.get("TREND_MIN_SAMPLES", "5"))
WINDOWS_MIN = (60, 180)

_MS_TO_KN = 1.943844
_RAD_TO_DEG = 57.29577951308232


def _ang(a, b):
    """Signed shortest angle from a to b (deg), + = b is clockwise (right) of a."""
    return ((b - a + 180) % 360) - 180


def _circ_mean_deg(vals):
    if not vals:
        return None
    x = sum(math.cos(math.radians(v)) for v in vals)
    y = sum(math.sin(math.radians(v)) for v in vals)
    if x == 0 and y == 0:
        return None
    return math.degrees(math.atan2(y, x)) % 360


def _bucket(rows, t0, t1):
    """(mean value, mean time) over rows with t0 <= t < t1; None when too thin."""
    sel = [(t, v) for (t, v) in rows if t0 <= t < t1 and v is not None]
    if len(sel) < TREND_MIN_SAMPLES:
        return None, None
    return sum(v for _, v in sel) / len(sel), sum(t for t, _ in sel) / len(sel)


def _bucket_circ(rows, t0, t1):
    sel = [(t, v) for (t, v) in rows if t0 <= t < t1 and v is not None]
    if len(sel) < TREND_MIN_SAMPLES:
        return None, None
    return _circ_mean_deg([v for _, v in sel]), sum(t for t, _ in sel) / len(sel)


def _window(tws_rows, twd_rows, minutes):
    """One window's trend record, or None when the archive doesn't cover it honestly."""
    rows = tws_rows or twd_rows
    if not rows:
        return None
    t_end = max(r[0] for r in rows)
    t_start = t_end - minutes * 60.0
    span_ok = (t_end - min(r[0] for r in rows)) >= minutes * 60.0 * 0.5
    if not span_ok:
        return None
    bucket_s = TREND_BUCKET_MIN * 60.0
    out = {"window_min": minutes}
    have = False

    head_v, head_t = _bucket(tws_rows, t_start, t_start + bucket_s)
    tail_v, tail_t = _bucket(tws_rows, t_end - bucket_s, t_end + 1)
    if head_v is not None and tail_v is not None and tail_t > head_t:
        hrs = (tail_t - head_t) / 3600.0
        f_kn, to_kn = head_v * _MS_TO_KN, tail_v * _MS_TO_KN
        out.update({"tws_from_kn": round(f_kn, 1), "tws_to_kn": round(to_kn, 1),
                    "tws_rate_kn_per_hr": round((to_kn - f_kn) / hrs, 2)})
        have = True

    head_d, head_t = _bucket_circ(twd_rows, t_start, t_start + bucket_s)
    tail_d, tail_t = _bucket_circ(twd_rows, t_end - bucket_s, t_end + 1)
    if head_d is not None and tail_d is not None and tail_t > head_t:
        hrs = (tail_t - head_t) / 3600.0
        delta = _ang(head_d, tail_d)
        rate = delta / hrs
        out.update({"twd_from_deg": round(head_d) % 360, "twd_to_deg": round(tail_d) % 360,
                    "twd_delta_deg": round(delta, 1),
                    "twd_rate_deg_per_hr": round(rate, 1),
                    "twd_dir": ("right" if rate > 1 else "left" if rate < -1 else "steady")})
        have = True
    return out if have else None


def _read_text(w):
    """One crew-facing sentence for a window record — racer wind language (right/left, from→to)."""
    bits = []
    r = w.get("tws_rate_kn_per_hr")
    if r is not None:
        verb = "building" if r > 0.3 else "fading" if r < -0.3 else "holding"
        if verb == "holding":
            bits.append(f"breeze holding ~{w['tws_to_kn']:g} kn")
        else:
            bits.append(f"breeze {verb} {abs(r):g} kn/hr ({w['tws_from_kn']:g}→{w['tws_to_kn']:g} kn)")
    d = w.get("twd_rate_deg_per_hr")
    if d is not None and w.get("twd_dir") in ("right", "left"):
        bits.append(f"walking {w['twd_dir']} ~{abs(d):g}°/hr (was {w['twd_from_deg']}°, "
                    f"now {w['twd_to_deg']}°)")
    hrs = w["window_min"] / 60
    tail = f" over the last {hrs:g} h" if bits else ""
    return (", ".join(bits) + tail) if bits else None


def get_trend(route=None):
    """The wind-trend read: 1 h + 3 h rate-of-change of own observed TWS/TWD. `na` when the
    archive is too thin (fresh boot / no wind data). Deterministic; an observation, not an alarm."""
    src = datasource.active()
    longest = max(WINDOWS_MIN)
    try:
        tws_rows = src.series("environment.wind.speedTrue", longest) or []
        twd_raw = src.series("environment.wind.directionTrue", longest) or []
    except Exception:
        tws_rows, twd_raw = [], []
    twd_rows = [(t, v * _RAD_TO_DEG % 360 if v is not None else None) for (t, v) in twd_raw]
    if not tws_rows and not twd_rows:
        return {"available": False, "note": "no archived wind — trend unknown",
                "based": ["own wind archive"], "conf": "engine"}

    windows = {}
    for m in WINDOWS_MIN:
        w = _window(tws_rows, twd_rows, m)
        if w:
            windows[f"h{m // 60}"] = w
    if not windows:
        return {"available": False,
                "note": "wind archive too thin for a trend (window not half-covered yet)",
                "based": ["own wind archive"], "conf": "engine"}

    # the 1 h window is the live matcher signal; 3 h is the narrative context
    h1 = windows.get("h1") or {}
    primary = windows.get("h3") or h1
    return {
        "available": True,
        **{k: v for k, v in windows.items()},
        "tws_trend_kn_per_hr": h1.get("tws_rate_kn_per_hr"),
        "twd_trend_deg_per_hr": h1.get("twd_rate_deg_per_hr"),
        "read": _read_text(primary),
        "based": ["own wind archive"], "conf": "engine",
    }
