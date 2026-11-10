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

Windows end at NOW, not at the newest stored sample (#163). When the newest wind sample is older
than `TREND_FRESH_S` the read is `na` ("No live wind data.") rather than an old window passed off
as "the last 1 h". Freshness is per channel: if only TWS (or only TWD) has gone quiet, that
channel drops out of the windows and the read carries "No live wind speed/direction data.".
Every read carries `as_of`, the newest sample's epoch.

Matcher signals contributed (per-hour, 1 h window): `tws_trend_kn_per_hr`,
`twd_trend_deg_per_hr` (signed, + = walking right). TIER-1: own archive only, legal in-race.
"""
from __future__ import annotations

import math
import os
import time

from . import datasource
from shared import staleness
from shared import strategy_render as sr

TREND_BUCKET_MIN = float(os.environ.get("TREND_BUCKET_MIN", "15"))
TREND_MIN_SAMPLES = int(os.environ.get("TREND_MIN_SAMPLES", "5"))
# Newest wind older than this → no live read. The consumer limit in `shared/staleness.py` (#291):
# 30 s aboard, 120 s in the cloud (it sees the boat through the uplink). TREND_FRESH_S, when set,
# overrides both.
_TREND_FRESH_ENV = os.environ.get("TREND_FRESH_S")
TREND_FRESH_S = None if _TREND_FRESH_ENV in (None, "") else float(_TREND_FRESH_ENV)


def _fresh_s():
    return TREND_FRESH_S if TREND_FRESH_S is not None else staleness.consumer_s("trend")
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


def _window(tws_rows, twd_rows, minutes, now):
    """One window's trend record ending at `now`, or None when the archive doesn't cover it
    honestly."""
    rows = tws_rows or twd_rows
    if not rows:
        return None
    t_end = now
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


_read_text = sr.trend_read_text     # shared with the strategy picture + the LoRA generators (#54)


def get_trend(route=None, now=None):
    """The wind-trend read: 1 h + 3 h rate-of-change of own observed TWS/TWD, windows ending at
    `now` (default: the clock). `na` when the archive is too thin (fresh boot / no wind data) or
    the wind has gone quiet. Deterministic; an observation, not an alarm."""
    now = time.time() if now is None else float(now)
    src = datasource.active()
    longest = max(WINDOWS_MIN)
    try:
        tws_rows = src.series("environment.wind.speedTrue", longest) or []
        twd_raw = src.series("environment.wind.directionTrue", longest) or []
    except Exception:
        tws_rows, twd_raw = [], []
    twd_rows = [(t, v * _RAD_TO_DEG % 360 if v is not None else None) for (t, v) in twd_raw]
    if not tws_rows and not twd_rows:
        return {"available": False, "note": "No wind data yet.",
                "based": ["own wind archive"], "conf": "engine"}

    as_of = max(r[0] for r in (tws_rows + twd_rows))
    fresh_s = _fresh_s()
    if now - as_of > fresh_s:
        return {"available": False, "note": "No live wind data.", "as_of": as_of,
                "based": ["own wind archive"], "conf": "engine"}

    # freshness is per channel: a quiet TWS next to a live TWD must not keep serving an old rate
    stale = []
    if tws_rows and now - max(r[0] for r in tws_rows) > fresh_s:
        tws_rows, stale = [], stale + ["speed"]
    if twd_rows and now - max(r[0] for r in twd_rows) > fresh_s:
        twd_rows, stale = [], stale + ["direction"]
    note = {"speed": "No live wind speed data.",
            "direction": "No live wind direction data."}.get(stale[0]) if stale else None

    windows = {}
    for m in WINDOWS_MIN:
        w = _window(tws_rows, twd_rows, m, now)
        if w:
            windows[f"h{m // 60}"] = w
    if not windows:
        return {"available": False,
                "note": "Not enough wind history yet.", "as_of": as_of,
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
        "as_of": as_of,
        **({"note": note} if note else {}),
        "based": ["own wind archive"], "conf": "engine",
    }
