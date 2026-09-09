"""Trust over a WINDOW of the record — the debrief's guardrail (V2 backlog "Debrief", item A).

`sensor_health.assess()` answers "can you believe the instruments *right now*". The debrief needs
the same question answered for every moment of a race that already happened, because
`learning.propose()` writes to the boat model and roughly two hours of Jul 18 2026 is
instrument-corrupt with nothing marking it: heading ~90° out from 23:56:10Z, so both TWD
publishers (which derive from heading) swing 137° while TWS holds at ~27 kn. Bins built from
that window would teach the boat that the wind did something it did not do.

This module sweeps the SAME pure checks (`sensor_health.heading_bias`, `attitude_plausible`)
across a recorded window and compresses the verdicts into per-channel SEGMENTS the debrief can
gate on and a human can read. The gating rule, per Cole 2026-09-09: **refuse** bins from
`danger` segments, per-channel, and always print what was refused — never down-weight (a bin
whose (TWS, TWA) coordinate came from a broken compass is data filed in the wrong bin, wrong at
any weight) and never silently (a check nobody can see is worth what a check never written is
worth).

Design notes:
  - Pure functions over plain samples, like `race_window` — the tests drive them with the real
    Jul 18 shape and no database.
  - One publisher per channel, chosen once for the whole window (policy rank, then coverage,
    non-AIS) — a series that alternates between two compasses manufactures bias steps.
  - `unknown` is honest silence, not corruption: unknown segments are reported but NOT refused.
    The false-alarm rate of these checks is measured, not assumed — 0 non-ok frames across the
    entire healthy Jul 15 race — which is what makes refusing on `danger` safe.
"""
import math
import os

from shared import n2k_sources, source_policy

from . import sensor_health

# One verdict per step; the check itself still looks back over its own window
# (HEADING_WINDOW_MIN), so a step is a sample of a sliding check, not a bucket mean.
STEP_S = float(os.environ.get("TRUST_STEP_S", "60"))


def _wrap180(deg):
    return (deg + 540.0) % 360.0 - 180.0


def heading_segments(samples, step_s=None):
    """Sweep the compass-vs-COG cross-check over [(t, hdg_deg, cog_deg, sog_kn)].

    Returns time-ordered [{t0, t1, status, bias_deg, spread_deg, reason}] with consecutive
    same-status steps merged. `samples` must already be from ONE heading publisher and ONE
    course publisher."""
    step_s = STEP_S if step_s is None else float(step_s)
    if not samples:
        return []
    samples = sorted(samples)
    win_s = sensor_health.HEADING_WINDOW_MIN * 60.0
    t0, t1 = samples[0][0], samples[-1][0]
    segs = []
    t = t0 + win_s
    i0 = 0
    while t <= t1 + step_s * 0.5:
        # advance the left edge instead of rescanning — the sweep is O(n) over the race
        while i0 < len(samples) and samples[i0][0] < t - win_s:
            i0 += 1
        i1 = i0
        while i1 < len(samples) and samples[i1][0] <= t:
            i1 += 1
        v = sensor_health.heading_bias(samples[i0:i1])
        step = {"status": v["status"], "bias_deg": v.get("bias_deg"),
                "spread_deg": v.get("spread_deg"), "reason": v.get("reason")}
        if segs and segs[-1]["status"] == step["status"]:
            segs[-1]["t1"] = t
            segs[-1].update({k: step[k] for k in ("bias_deg", "spread_deg", "reason")})
        else:
            segs.append({"t0": t - step_s, "t1": t, **step})
        t += step_s
    return segs


def attitude_segments(samples, step_s=None):
    """Sweep the attitude range gate over [(t, roll_deg, pitch_deg)]. Same segment shape."""
    step_s = STEP_S if step_s is None else float(step_s)
    if not samples:
        return []
    samples = sorted(samples)
    segs = []
    t = samples[0][0] + step_s
    i0 = 0
    while t <= samples[-1][0] + step_s * 0.5:
        while i0 < len(samples) and samples[i0][0] < t - step_s:
            i0 += 1
        i1 = i0
        while i1 < len(samples) and samples[i1][0] <= t:
            i1 += 1
        window = samples[i0:i1]
        if not window:
            status, reason = "unknown", "no attitude samples in this step"
        else:
            # judge the step on its worst excursion — a capsized reading for one second is the
            # signal here (the Jul 18 kick was a 36->133 deg step), not the average
            worst = max(window, key=lambda r: max(abs(r[1] or 0.0), abs(r[2] or 0.0)))
            v = sensor_health.attitude_plausible(worst[1], worst[2])
            status, reason = v["status"], v["reason"]
        if segs and segs[-1]["status"] == status:
            segs[-1]["t1"] = t
            segs[-1]["reason"] = reason
        else:
            segs.append({"t0": t - step_s, "t1": t, "status": status, "reason": reason})
        t += step_s
    return segs


def danger_intervals(segments):
    """[(t0, t1)] of the danger segments — what the debrief refuses bins from."""
    return [(s["t0"], s["t1"]) for s in segments or () if s["status"] == "danger"]


def summarize(channels):
    """One human line + machine totals for {'heading': [segs], 'attitude': [segs], ...}."""
    total = {"danger_s": 0.0, "warn_s": 0.0, "unknown_s": 0.0}
    lines = []
    for ch, segs in sorted((channels or {}).items()):
        for s in segs or ():
            d = s["t1"] - s["t0"]
            key = f"{s['status']}_s"
            if key in total:
                total[key] += d
        worst = [s for s in segs or () if s["status"] == "danger"]
        if worst:
            span = sum(s["t1"] - s["t0"] for s in worst)
            lines.append(f"{ch} DANGER for {span / 60:.0f} min — {worst[-1]['reason']}")
    return {**{k: round(v) for k, v in total.items()},
            "line": "; ".join(lines) or "no danger windows"}


def pick_source(by_source, channel, devices):
    """One publisher for the whole window: resolved device first, then rank, then coverage."""
    if not by_source:
        return None

    def key(s):
        rank = source_policy.rank_for(channel, s, devices)
        return (0 if n2k_sources.resolve(s, devices) else 1,
                99 if rank is None else rank, -len(by_source[s]), s)

    return min(by_source, key=key)


def merge_heading_samples(hdg, cog, sog, tol_s=30.0):
    """Pair per-source series into heading_bias() samples: [(t, hdg, cog, sog)].

    Each input is [(t, value_deg_or_kn)], time-ordered, one publisher each. A heading sample is
    paired with the freshest cog/sog at or before it (within `tol_s`) — the same
    freshest-at-or-before rule the truth pane uses. `tol_s` is 30 s because the record changes
    resolution mid-race: full-res (5–28 Hz) while the archiver lived, 15-second uplink
    aggregates after it died at 20:40:30Z — and a 5 s tolerance silently drops most of the
    aggregate half, which on Jul 18 is exactly the half containing the compass fault."""
    out = []
    ic = isog = 0
    for t, h in hdg or ():
        while ic + 1 < len(cog) and cog[ic + 1][0] <= t:
            ic += 1
        while isog + 1 < len(sog) and sog[isog + 1][0] <= t:
            isog += 1
        c = cog[ic][1] if cog and abs(cog[ic][0] - t) <= tol_s else None
        s = sog[isog][1] if sog and abs(sog[isog][0] - t) <= tol_s else None
        out.append((t, h, c, s))
    return out
