"""Helm fatigue index — detect a tiring driver and recommend a crew rotation.

A tired helmsman steers worse, and worse steering shows up two ways: the boat WANDERS
(more variance in heading / heel / apparent wind) and it goes SLOWER versus its own
potential. This module blends several such "tells" into one 0–100 index and a rotation
recommendation, surfaced on the instrument strip and as an agent tool.

Design choices (see CLAUDE.md "Helm fatigue index"):
- ANONYMOUS current-helm. We don't track who is driving. The index is baselined against
  the BOAT'S OWN recent steering — a recent short window vs. a longer trailing baseline —
  so it auto-normalises for sea state, breeze, and the individual's skill, and needs zero
  crew input. The signal is *degradation over time within a stint*, not an absolute number.
- MULTI-SIGNAL. No single metric is trusted; a weighted composite is far harder to fool
  (a wind shift spikes AWA but not heel + speed together).
- MANEUVER-AWARE. Tacks, gybes and big course changes legitimately spike variance, so
  samples during high-rate turns are excluded before computing steering variance.
- CONFOUNDER-AWARE. AWA wander is de-trended by true-wind-direction variance so a shifty
  breeze isn't read as a tired driver. (Wind-strength normalisation beyond the baseline
  comparison is a documented v1 limitation.)

stdlib only (no numpy in the agent image). Cached briefly so the 5-s strip poll doesn't
re-run the windowed queries every call.
"""
import math
import os
import time
from collections import Counter
from datetime import datetime, timezone

from . import datasource

BOAT_ID = os.environ.get("BOAT_ID", "sr33")

# Windows (minutes). Recent = "how is the helm doing now"; baseline = the boat's own
# earlier steering this stint to measure against.
RECENT_MIN = float(os.environ.get("FATIGUE_RECENT_MIN", "8"))
BASELINE_MIN = float(os.environ.get("FATIGUE_BASELINE_MIN", "40"))
# Don't emit an index until there's enough history to baseline against (avoids garbage in
# the first part of a sail) and enough recent data to be meaningful.
MIN_BASELINE_MIN = float(os.environ.get("FATIGUE_MIN_BASELINE_MIN", "12"))
MIN_RECENT_MIN = float(os.environ.get("FATIGUE_MIN_RECENT_MIN", "3"))
# A heading rate above this (deg/s) is a maneuver/glitch, not steering — exclude it.
MANEUVER_RATE = float(os.environ.get("FATIGUE_MANEUVER_RATE", "8.0"))
MANEUVER_PAD_S = 12.0
TTL_S = float(os.environ.get("FATIGUE_TTL_S", "20"))

# How much WORSE than baseline saturates a component to 100 (ratio sensitivity), and the
# per-component weights of the composite. Tunable against real race archives later.
SENS = float(os.environ.get("FATIGUE_SENS", "1.2"))   # ratio 1+SENS vs baseline -> full score
SPEED_FULL_DEFICIT = 0.10       # 10 extra %-off-target speed -> full score
# Absolute floors on the baseline used in the ratio, so a very tight (or near-zero) baseline
# doesn't make modest, normal wander read as catastrophic. Units match the metric.
FLOORS = {
    "heading_instability": 2.0,   # ° stdev
    "steering_reversals":  1.0,   # reversals/min
    "heel_instability":    1.0,   # ° stdev
    "awa_wander":          2.0,   # ° stdev
}
WEIGHTS = {
    "heading_instability": 0.30,
    "steering_reversals":  0.15,
    "heel_instability":    0.20,
    "awa_wander":          0.15,
    "speed_deficit":       0.20,
}

_MS_TO_KN = 1.943844
_RAD_TO_DEG = 57.295779513

# Channels we pull, with the Signal K path(s) and a display converter. Heading prefers true,
# falls back to magnetic.
_HEADING_PATHS = ["navigation.headingTrue", "navigation.headingMagnetic"]

_cache = {"t": 0.0, "value": None}


# --- data access -------------------------------------------------------------
def _series(path, since_min):
    """[(epoch_s, value)] for the dominant source of a path over the window, time-ordered."""
    rows = datasource.active().series_by_source(path, since_min)   # [(source, t, value)], raw SI
    if not rows:
        return []
    dom = Counter(r[0] for r in rows).most_common(1)[0][0]
    return [(t, v) for (src, t, v) in rows if src == dom]


def _heading_series(since_min):
    for p in _HEADING_PATHS:
        s = _series(p, since_min)
        if len(s) > 5:
            return [(t, v * _RAD_TO_DEG) for t, v in s]
    return []


def _target_stw(tws_kn, twa_deg):
    row = datasource.active().polar_nearest(tws_kn, twa_deg)
    return row["target_stw"] if row and row.get("target_stw") else None


# --- stats helpers -----------------------------------------------------------
def _wrap180(d):
    return (d + 180) % 360 - 180


def _circ_std_deg(vals):
    """Circular standard deviation (deg) — correct for heading/AWA/TWD wrap-around."""
    if len(vals) < 2:
        return None
    n = len(vals)
    s = sum(math.sin(math.radians(v)) for v in vals) / n
    c = sum(math.cos(math.radians(v)) for v in vals) / n
    r = min(1.0, max(1e-9, math.hypot(s, c)))
    return math.degrees(math.sqrt(-2.0 * math.log(r)))


def _std(vals):
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def _maneuver_intervals(heading):
    """Time ranges to exclude: high heading-rate turns (tacks/gybes/glitches), padded."""
    bad = []
    for (t0, v0), (t1, v1) in zip(heading, heading[1:]):
        dt = t1 - t0
        if 0 < dt < 30 and abs(_wrap180(v1 - v0)) / dt > MANEUVER_RATE:
            bad.append((t0 - MANEUVER_PAD_S, t1 + MANEUVER_PAD_S))
    return bad


def _excluded(t, intervals):
    return any(lo <= t <= hi for lo, hi in intervals)


def _reversals_per_min(heading):
    """Steering-reversal rate: sign changes in heading delta (ignoring sub-0.5° noise)."""
    if len(heading) < 3:
        return None
    span_min = (heading[-1][0] - heading[0][0]) / 60.0
    if span_min < 0.5:
        return None
    prev_sign = 0
    flips = 0
    for (t0, v0), (t1, v1) in zip(heading, heading[1:]):
        d = _wrap180(v1 - v0)
        if abs(d) < 0.5:
            continue
        sign = 1 if d > 0 else -1
        if prev_sign and sign != prev_sign:
            flips += 1
        prev_sign = sign
    return flips / span_min


# --- component scoring -------------------------------------------------------
def _ratio_score(recent, baseline, floor):
    """0–100 for a 'higher is worse' metric: how far recent exceeds baseline (floored)."""
    if recent is None or baseline is None:
        return None
    base = max(baseline, floor)
    return max(0.0, min(100.0, (recent / base - 1.0) / SENS * 100.0))


def _window(series, lo, hi):
    return [(t, v) for t, v in series if lo <= t < hi]


def _metrics(chans, lo, hi, maneuvers):
    """Compute the raw fatigue metrics over the [lo, hi) window."""
    def vals(name, mask_maneuvers=False):
        pts = _window(chans.get(name, []), lo, hi)
        if mask_maneuvers:
            pts = [(t, v) for t, v in pts if not _excluded(t, maneuvers)]
        return [v for _, v in pts], pts

    hv, hpts = vals("heading", mask_maneuvers=True)
    heelv, _ = vals("heel", mask_maneuvers=True)
    awav, _ = vals("awa", mask_maneuvers=True)
    twdv, _ = vals("twd")
    stwv, _ = vals("stw")
    twsv, _ = vals("tws")
    twav, _ = vals("twa")

    m = {}
    m["heading_instability"] = _circ_std_deg(hv)
    m["steering_reversals"] = _reversals_per_min(hpts)
    m["heel_instability"] = _std(heelv)
    # AWA wander with the wind's own shift removed, so a shifty breeze isn't blamed on the helm
    awa_std = _circ_std_deg(awav)
    twd_std = _circ_std_deg(twdv)
    m["awa_wander"] = None if awa_std is None else max(0.0, awa_std - (twd_std or 0.0))
    # speed deficit vs polar target at the window's representative TWS/TWA
    m["speed_deficit"] = None
    if stwv and twsv and twav:
        tws = sorted(twsv)[len(twsv) // 2]
        twa = sorted(abs(x) for x in twav)[len(twav) // 2]
        tgt = _target_stw(tws, twa)
        if tgt:
            m["speed_deficit"] = max(0.0, 1.0 - (sum(stwv) / len(stwv)) / tgt)
    m["_n"] = len(hpts)
    return m


def compute_fatigue_index():
    """The 0–100 helm fatigue index + per-component breakdown + rotation recommendation."""
    if _cache["value"] is not None and (time.monotonic() - _cache["t"]) < TTL_S:
        return _cache["value"]
    out = _compute()
    _cache.update(t=time.monotonic(), value=out)
    return out


def _compute():
    total = RECENT_MIN + BASELINE_MIN
    heading = _heading_series(total)
    chans = {
        "heading": heading,
        "heel": [(t, v * _RAD_TO_DEG) for t, v in _series("navigation.attitude.roll", total)],
        "awa": [(t, v * _RAD_TO_DEG) for t, v in _series("environment.wind.angleApparent", total)],
        "twd": [(t, v * _RAD_TO_DEG) for t, v in _series("environment.wind.directionTrue", total)],
        "stw": [(t, v * _MS_TO_KN) for t, v in _series("navigation.speedThroughWater", total)],
        "tws": [(t, v * _MS_TO_KN) for t, v in _series("environment.wind.speedTrue", total)],
        "twa": [(t, v * _RAD_TO_DEG) for t, v in _series("environment.wind.angleTrueWater", total)],
    }
    now = time.time()
    t_split = now - RECENT_MIN * 60.0
    t_lo = now - total * 60.0

    if not heading:
        return {"available": False, "status": "no_helm_data",
                "note": "no heading data — can't assess steering"}
    base_span_min = (t_split - min(t for t, _ in heading)) / 60.0
    recent_span_min = (max(t for t, _ in heading) - t_split) / 60.0
    if base_span_min < MIN_BASELINE_MIN or recent_span_min < MIN_RECENT_MIN:
        return {"available": False, "status": "warming_up",
                "note": (f"need ~{MIN_BASELINE_MIN:.0f} min of baseline + {MIN_RECENT_MIN:.0f} min "
                         f"recent steering; have {base_span_min:.0f}/{recent_span_min:.0f} min"),
                "baseline_min_have": round(base_span_min, 1),
                "recent_min_have": round(recent_span_min, 1)}

    maneuvers = _maneuver_intervals(heading)
    recent = _metrics(chans, t_split, now + 1, maneuvers)
    baseline = _metrics(chans, t_lo, t_split, maneuvers)

    components, weighted, wsum = {}, 0.0, 0.0
    for name, w in WEIGHTS.items():
        if name == "speed_deficit":
            r, b = recent.get(name), baseline.get(name)
            score = None if r is None or b is None else \
                max(0.0, min(100.0, (r - b) / SPEED_FULL_DEFICIT * 100.0))
            unit = "frac below target"
        else:
            r, b = recent.get(name), baseline.get(name)
            score = _ratio_score(r, b, FLOORS[name])
            unit = "reversals/min" if name == "steering_reversals" else "° stdev"
        if score is None:
            continue
        components[name] = {"recent": None if r is None else round(r, 3),
                            "baseline": None if b is None else round(b, 3),
                            "score": round(score, 1), "unit": unit}
        weighted += score * w
        wsum += w

    if wsum < 0.4:  # too few signals to be credible
        return {"available": False, "status": "insufficient_signals",
                "note": "not enough steering/performance channels to assess fatigue",
                "components": components}

    index = round(weighted / wsum)
    level, rec = _level(index)
    return {
        "available": True,
        "index": index,
        "level": level,
        "recommendation": rec,
        "components": components,
        "windows": {"recent_min": RECENT_MIN, "baseline_min": round(base_span_min, 1)},
        "maneuvers_excluded": len(maneuvers),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "method": ("Anonymous current-helm. 0–100; recent steering vs the boat's own trailing "
                   "baseline (auto-normalises for conditions/skill). Maneuvers excluded; AWA "
                   "de-trended by TWD. Not wind-strength normalised beyond the baseline — a big "
                   "build in breeze can read high. Tune thresholds on real race archives."),
    }


def _level(index):
    if index < 35:
        return "fresh", "Helm steady — no change needed."
    if index < 60:
        return "watch", "Helm slipping a little versus baseline — keep an eye on the driver."
    if index < 80:
        return "rotate_soon", "Steering quality is fading — line up a helm change."
    return "rotate_now", "Helm well off baseline — rotate the driver now."
