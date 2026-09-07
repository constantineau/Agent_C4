"""Bank watch — house-battery state from the boat's own telemetry.

Why this module exists, in one paragraph. On 2026-07-18 the house bank fell monotonically from
12.91 V (pre-start) to 11.61 V mean with minima at **11.08 V** over six hours of racing. At
20:40:30Z, at the bottom of that curve, two things stopped in the same minute: the full-res
archiver (SQLite corruption — the third on that SD card) and the em-trak AIS transceiver
(67,149 rows in the previous 100 minutes, then 11 in the next 3.5 hours). Every other N2K
source kept reporting, so the bus survived and the write-sensitive/power-hungry devices did
not. The boat then retired. **The Orca publishes `electrical.batteries.0.voltage` at ~0.7 Hz
and nothing in this system read it** — not `tools.PRESENT`, not `alerts.py`, not the dashboard
(whose "energy" tile is crew energy). The data that predicted the failure was in the archive,
unread, the whole time.

Design notes, mostly inherited from mistakes made elsewhere in this repo:

  - **Stateless and replayable.** Everything is derived from the sample window, so the replay
    rig produces the same verdicts as the boat and the mark-sequencer's stored-state problem
    cannot recur here.
  - **Raise on the smoothed level, release on a sustained recovery.** Voltage under sail is
    noisy — winch, pilot and kettle loads produce second-scale sags — so decisions are taken on
    a median, never a raw sample. Release is deliberately *not* the "clear fast" convention
    used elsewhere in this repo: an unloaded flat bank reads high for a while (surface charge),
    so clearing requires the smoothed level to hold above a release band for the dwell. See
    `_tripped`, which records the two ways earlier versions of this got it wrong.
  - **No bucketing, and a robust trend.** The level is a median over overlapping sub-windows
    and the trend is a median-of-thirds slope, not least squares — a single sag dragged an OLS
    slope to -1 V/h and read as "the bank is dying". Quantising a continuous quantity to fire a
    discrete alarm is the failure shape this codebase has hit three times.
  - **Chemistry-independent trend rule.** Absolute thresholds depend on chemistry and bank
    size, which are not recorded anywhere in this repo — the defaults below are conservative
    12 V lead-acid figures and MUST be confirmed against the real bank. The drain rule (V/h
    plus a projection to the floor) needs no such knowledge and is the part to trust first.
  - **Charging is not an alarm.** A rising slope means the alternator or shore power is on;
    Jul 18's recovery to 13.30 V by 05:00Z was the motor home. A flat-but-recovering bank
    still reports `danger` on level, because it is still flat.
"""
import os
import statistics
import time as _time

from . import datasource

PATH = os.environ.get("POWER_VOLTAGE_PATH", "electrical.batteries.0.voltage")
# Trend window. Long enough that load noise averages out, short enough to react within a watch.
WINDOW_MIN = float(os.environ.get("POWER_WINDOW_MIN", "45"))
SMOOTH_MIN = float(os.environ.get("POWER_SMOOTH_MIN", "5"))     # median window for the level
SUSTAIN_MIN = float(os.environ.get("POWER_SUSTAIN_MIN", "10"))  # raise-slow dwell
# ⚠️ Confirm against the actual bank before trusting these. 12 V lead-acid, under load:
# ~12.0 V is roughly half charge, ~11.6 V is nearly flat, and the Pi/AIS start browning out
# around 11.0 V — which is where Jul 18's minima sat.
WARN_V = float(os.environ.get("POWER_WARN_V", "12.0"))
DANGER_V = float(os.environ.get("POWER_DANGER_V", "11.6"))
FLOOR_V = float(os.environ.get("POWER_FLOOR_V", "11.0"))
DRAIN_WARN_V_PER_H = float(os.environ.get("POWER_DRAIN_WARN_V_PER_H", "0.15"))
DRAIN_WARN_HOURS = float(os.environ.get("POWER_DRAIN_WARN_HOURS", "6"))
CHARGE_V_PER_H = float(os.environ.get("POWER_CHARGE_V_PER_H", "0.10"))
# Schmitt release band: a status raised at X clears only above X + this. See `_tripped`.
CLEAR_MARGIN_V = float(os.environ.get("POWER_CLEAR_MARGIN_V", "0.15"))


def _slope_v_per_h(rows):
    """Robust dV/dt in volts per hour: median of the last third minus median of the first
    third, over the time between those thirds' midpoints. None if the window is too short.

    NOT least squares. A single winch-load sag at the end of the window drags an OLS slope to
    ~-1 V/h, and with a projection rule attached that reads as "the bank is dying" — the first
    version of this module raised a warning on exactly that. Medians of the outer thirds ignore
    an outlier or two, stay continuous (no bucketing, so nothing chatters at a bucket edge), and
    still track a real decline: the Jul 18 curve reads -0.66 V/h at its steepest either way."""
    if len(rows) < 6:
        return None
    k = max(2, len(rows) // 3)
    first, last = rows[:k], rows[-k:]
    t0 = statistics.median([t for t, _ in first])
    t1 = statistics.median([t for t, _ in last])
    if t1 <= t0:
        return None
    v0 = statistics.median([v for _, v in first])
    v1 = statistics.median([v for _, v in last])
    return (v1 - v0) / (t1 - t0) * 3600.0


def _smoothed(rows, now, window_min, smooth_min=None, step_min=None):
    """[(t, median of the trailing `smooth_min`)] across the window, on OVERLAPPING sub-windows.

    Overlapping (step = half the smoothing window) rather than contiguous buckets on purpose:
    contiguous buckets put a discontinuity at every bucket edge, which is the shape that has
    already bitten this project three times. Overlap means every sample influences several
    points and no edge is special."""
    smooth = SMOOTH_MIN if smooth_min is None else smooth_min
    step = (smooth / 2.0) if step_min is None else step_min
    start = now - window_min * 60.0
    out = []
    t = start + smooth * 60.0
    while t <= now + 1e-6:
        vals = [v for ts, v in rows if t - smooth * 60.0 <= ts <= t]
        if vals:
            out.append((t, statistics.median(vals)))
        t += step * 60.0
    if not out:                      # window shorter than one smoothing period
        vals = [v for _, v in rows]
        if vals:
            out = [(now, statistics.median(vals))]
    return out


def _tripped(rows, threshold, now, window_min):
    """Schmitt test on the SMOOTHED level: has this threshold tripped and not yet released?

    Tripped = the smoothed level fell below `threshold` somewhere in the window. Released = the
    latest smoothed level is back above `threshold + CLEAR_MARGIN_V`.

    Two earlier versions of this were wrong in opposite directions, and both are worth keeping
    written down:

      1. "every raw sample in the dwell is below the threshold" made the verdict *flap*. The
         Jul 18 bank sat at 11.6–11.7 V for three hours, straddling the danger line, so the
         status oscillated danger↔warn every few minutes. The release band fixes that.
      2. "any raw sample in the window fell below the threshold" raised **danger on a single
         winch-load sag** — one 10.9 V sample on an otherwise 12.6 V bank. Deciding on the
         smoothed level instead of raw samples fixes that.

    Note that release is deliberately NOT fast here, unlike the alert convention elsewhere in
    this repo: an unloaded flat battery reads high for a while (surface charge), so a brief
    recovery is not a recovery. Clearing needs the *median* to come back up, which takes half a
    smoothing window of genuinely better readings.

    Memory lives in the sample window rather than in a variable, so this is replayable and the
    rig produces the same verdicts as the boat."""
    sm = _smoothed(rows, now, window_min)
    if not sm:
        return False
    span = rows[-1][0] - rows[0][0]
    if span < SUSTAIN_MIN * 60.0 * 0.5:
        return False              # too little history to judge — never raise on a fresh sample
    if min(v for _, v in sm) >= threshold:
        return False              # never tripped in this window
    # Release needs the smoothed level above the band for the WHOLE dwell, not just at the
    # latest point: three minutes at 12.7 V after two hours at 11.4 V is a removed load, not a
    # charged battery, and a trailing median alone flips as soon as the bounce fills half the
    # smoothing window. Asymmetric by design — noise-robust to raise, surface-charge-robust to
    # clear.
    dwell = [v for t, v in sm if t >= now - SUSTAIN_MIN * 60.0] or [sm[-1][1]]
    return not all(v >= threshold + CLEAR_MARGIN_V for v in dwell)


def assess_series(rows, now=None):
    """Bank state from [(epoch_s, volts)], newest last. Pure — the replay rig and the test feed
    it recorded data and get exactly what the boat would have said at that moment."""
    if not rows:
        return {"available": False, "status": "unknown",
                "note": f"no {PATH} in the last {WINDOW_MIN:g} min"}
    rows = sorted(rows)
    now = rows[-1][0] if now is None else now
    window = [(t, v) for t, v in rows if t >= now - WINDOW_MIN * 60.0]
    if not window:
        window = rows[-1:]

    tail = [v for t, v in window if t >= now - SMOOTH_MIN * 60.0] or [window[-1][1]]
    level = statistics.median(tail)
    slope = _slope_v_per_h(window)
    charging = slope is not None and slope >= CHARGE_V_PER_H

    hours_to_floor = None
    if slope is not None and slope < 0 and level > FLOOR_V:
        hours_to_floor = (level - FLOOR_V) / -slope

    draining = (slope is not None and slope <= -DRAIN_WARN_V_PER_H
                and hours_to_floor is not None and hours_to_floor <= DRAIN_WARN_HOURS)

    if _tripped(window, DANGER_V, now, WINDOW_MIN):
        status = "danger"
        reason = (f"bank {level:.2f} V, at or under {DANGER_V:.2f} V"
                  + (" — recovering, charge source on" if charging else ""))
    elif charging:
        status = "charging"
        reason = f"bank {level:.2f} V, rising {slope:+.2f} V/h — charge source on"
    elif _tripped(window, WARN_V, now, WINDOW_MIN):
        status = "warn"
        reason = f"bank {level:.2f} V, at or under {WARN_V:.2f} V"
        if hours_to_floor is not None:
            reason += f"; ~{hours_to_floor:.1f} h to {FLOOR_V:.1f} V at {slope:+.2f} V/h"
    elif draining:
        status = "warn"
        reason = (f"bank {level:.2f} V draining {slope:+.2f} V/h — "
                  f"~{hours_to_floor:.1f} h to {FLOOR_V:.1f} V")
    else:
        status = "ok"
        reason = f"bank {level:.2f} V" + (f", {slope:+.2f} V/h" if slope is not None else "")

    return {
        "available": True,
        "status": status,                      # ok | warn | danger | charging | unknown
        "reason": reason,
        "volts": round(level, 2),
        "volts_last": round(window[-1][1], 2),
        "volts_min": round(min(v for _, v in window), 2),
        "trend_v_per_h": None if slope is None else round(slope, 3),
        "hours_to_floor": None if hours_to_floor is None else round(hours_to_floor, 2),
        "dark_at_epoch": (None if hours_to_floor is None
                          else round(now + hours_to_floor * 3600.0)),
        "charging": charging,
        "samples": len(window),
        "window_min": WINDOW_MIN,
        "path": PATH,
        "thresholds": {"warn_v": WARN_V, "danger_v": DANGER_V, "floor_v": FLOOR_V,
                       "drain_warn_v_per_h": DRAIN_WARN_V_PER_H},
        "note": ("Absolute thresholds assume a 12 V lead-acid bank under load and are NOT yet "
                 "confirmed against this boat's chemistry/capacity — trust `trend_v_per_h` and "
                 "`hours_to_floor` first. Below ~11.0 V the Pi's SD writes and the AIS "
                 "transceiver are at risk: that is how the Jul 18 archive was lost mid-race."),
    }


def assess(source=None, now=None):
    """Bank state read through the active datasource (onboard archive + live cache, or cloud)."""
    src = source or datasource.active()
    try:
        rows = src.series(PATH, WINDOW_MIN)
    except Exception as exc:                  # a power read must never take the engine down
        return {"available": False, "status": "unknown", "note": f"{PATH} unreadable: {exc}"}
    if not rows:
        latest = None
        try:
            latest = src.latest_value(PATH)
        except Exception:
            pass
        if latest is not None:
            rows = [(_time.time() if now is None else now, float(latest))]
    return assess_series(rows, now=now)
