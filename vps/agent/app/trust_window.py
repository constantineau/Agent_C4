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
    last_status = last_at = None
    while t <= t1 + step_s * 0.5:
        # advance the left edge instead of rescanning — the sweep is O(n) over the race
        while i0 < len(samples) and samples[i0][0] < t - win_s:
            i0 += 1
        i1 = i0
        while i1 < len(samples) and samples[i1][0] <= t:
            i1 += 1
        # The last DECIDED verdict (never `unknown`) and when it was decided. It carries the
        # warn release band across windows — so the strip shows one warning instead of six
        # crossings of the same line — and holds a danger across windows that cannot judge, so a
        # real fault does not go quiet every time the boat manoeuvres. A HELD danger is not a
        # decision: it must not refresh its own age, or the hold never expires (the same rule
        # the boat's live poll follows in pi/engine/engine_app.py).
        v = sensor_health.heading_bias(
            samples[i0:i1], previous=last_status,
            previous_age_s=None if last_at is None else max(0.0, t - last_at))
        if v["status"] in ("ok", "warn", "danger") and not v.get("held"):
            last_status, last_at = v["status"], t
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
    samples = sorted(samples, key=lambda r: r[0])   # rows may carry None: sort on time only
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
        # judge the step on its worst excursion — a capsized reading for one second is the
        # signal here (the Jul 18 kick was a 36->133 deg step), not the average. Roll and pitch
        # are judged separately (their limits differ, and a step may hold rows with only one
        # of them), so a pitch fault is not hidden behind a bigger but legal roll.
        rolls = [r[1] for r in window if r[1] is not None]
        pitches = [r[2] for r in window if r[2] is not None]
        if not rolls and not pitches:
            status, reason = "unknown", "no attitude samples in this step"
        else:
            v = sensor_health.attitude_plausible(max(rolls, key=abs) if rolls else None,
                                                 max(pitches, key=abs) if pitches else None)
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
    """One human line + machine totals for {'heading': [segs], 'attitude': [segs], ...}.

    A channel with no segments, or only `unknown` ones, was never checked; it is listed in
    `unchecked` and named in the line, so "no danger windows" only ever means the checks ran."""
    total = {"danger_s": 0.0, "warn_s": 0.0, "unknown_s": 0.0}
    lines, unchecked = [], []
    for ch, segs in sorted((channels or {}).items()):
        for s in segs or ():
            d = s["t1"] - s["t0"]
            key = f"{s['status']}_s"
            if key in total:
                total[key] += d
        if not any(s["status"] != "unknown" for s in segs or ()):
            unchecked.append(ch)
        worst = [s for s in segs or () if s["status"] == "danger"]
        if worst:
            span = sum(s["t1"] - s["t0"] for s in worst)
            lines.append(f"{ch} DANGER for {span / 60:.0f} min — {worst[-1]['reason']}")
    if unchecked:
        lines.append(f"{', '.join(unchecked)} not checked (no usable data)")
    elif not lines:
        lines.append("no danger windows")
    return {**{k: round(v) for k, v in total.items()},
            "unchecked": unchecked, "line": "; ".join(lines)}


def pick_source(by_source, channel, devices):
    """One publisher for the whole window: resolved device first, then rank, then coverage."""
    if not by_source:
        return None

    def key(s):
        rank = source_policy.rank_for(channel, s, devices)
        return (0 if n2k_sources.resolve(s, devices) else 1,
                99 if rank is None else rank, -len(by_source[s]), s)

    return min(by_source, key=key)


def pick_path_sources(by_path, devices):
    """{path: chosen source} for {path: {source: series}}. Ranked by the Signal K path itself,
    which source_policy maps to its channel (headingTrue -> heading_true, roll -> heel). The
    path's last segment ("headingtrue", "roll") is not a channel name and ranks nothing."""
    return {p: pick_source(by_source, p, devices) for p, by_source in (by_path or {}).items()}


def attitude_rows(roll, pitch):
    """attitude_segments() rows from [(t, roll_deg)] and [(t, pitch_deg)]: every sample its own
    row. Roll and pitch rarely share an exact epoch, and pairing on one drops pitch (and with it
    any pitch-only fault)."""
    return sorted([(t, v, None) for t, v in roll or ()] + [(t, None, v) for t, v in pitch or ()],
                  key=lambda r: r[0])


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
