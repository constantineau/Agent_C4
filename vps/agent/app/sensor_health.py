"""Sensor health — is an instrument that is still *reporting* actually still *right*?

Written 2026-09-07 after reconstructing what happened to primary navigation on Jul 18. The crew
said someone kicked the GPS/compass; the telemetry dates it to the minute:

    22:57Z   roll 36.4°, pitch  +2.8°   — tracking the autopilot AHRS, as it had all race
    22:58Z   roll 133.1°, pitch −49.2°  — while the autopilot still read 35.3° / −3.4°
    …        roll ≈ ±175° (inverted), pitch ≈ −30..−45°, for the next hour
    ~00:00Z  roll/pitch plausible again — someone re-seated it
    00:00Z+  headingTrue − COG = −96, −90, −90, −91, −90, −89, −87, −90°, spread 2–7°

That last line is the dangerous state and the reason this module exists. Once the sensor was
put back, **every value looked sane** — a few degrees of roll, a few of pitch, a heading that
is a perfectly ordinary number — and it was wrong by a quarter turn for the rest of the trip.
No range check catches that. What catches it is the cross-check the boat already has the
ingredients for: heading against GPS course over ground. Under sail those agree within a few
degrees of leeway and current; a −90° bias held for hours with a 4° spread is not sailing.

Two checks, deliberately different in kind:

  - `attitude_plausible` — a range gate. A monohull does not sit at 133° of roll. Catches the
    violent hour immediately, on one sample, with no history and no reference sensor.
  - `heading_bias` — a *relative* check against an independent measurement. Catches the quiet
    misalignment that the range gate cannot see, and would also have caught the pre-kick drift
    (+7.0° at 21:00Z, +16.3° at 22:00Z, against −0.1..−5.7° for the whole healthy race).

Both are stateless functions of a sample window, so the replay rig produces the same verdicts
the boat would have, and neither ever *drops* data — they report. Silently substituting a
sensor is how this project lost a race to AIS contamination; the fix for a bad sensor is to say
so loudly, not to quietly pick another one.

⚠️ Note what this module does NOT claim. It cannot tell you *which* of two disagreeing sensors
is wrong. On Jul 18 the 24xd was the only own-ship `headingTrue` publisher on the bus, so there
was nothing to fail over to — the autopilot publishes `headingMagnetic` only. Deriving true
heading from magnetic + `navigation.magneticVariation` is the redundancy this boat is missing,
and it is a separate piece of work.
"""
import math
import os

from . import datasource

# --- attitude range gate ----------------------------------------------------
# Measured over the healthy Jul 18 race window (17:03–20:40Z): roll spanned −0.5..42.6° across
# both attitude sources and pitch −7.5..+7.8° on the autopilot. The limits below are far outside
# any of that, because the point is to catch a sensor that has fallen over, not to second-guess
# a big broach.
ROLL_LIMIT_DEG = float(os.environ.get("HEALTH_ROLL_LIMIT_DEG", "75"))
PITCH_LIMIT_DEG = float(os.environ.get("HEALTH_PITCH_LIMIT_DEG", "35"))

# --- heading-vs-COG cross-check ---------------------------------------------
HEADING_WINDOW_MIN = float(os.environ.get("HEALTH_HEADING_WINDOW_MIN", "20"))
# COG is meaningless at rest and noisy at crawl, so only sail speeds count.
HEADING_MIN_SOG_KN = float(os.environ.get("HEALTH_HEADING_MIN_SOG_KN", "3.0"))
# Leeway plus current plus a genuine tidal set can legitimately reach double digits; a healthy
# race read −0.1..−5.7° per hour. Warn beyond WARN, call it broken beyond BAD.
HEADING_WARN_DEG = float(os.environ.get("HEALTH_HEADING_WARN_DEG", "15"))
HEADING_BAD_DEG = float(os.environ.get("HEALTH_HEADING_BAD_DEG", "35"))
# A bias only means something if the samples agree about it. Wild spread means manoeuvring or a
# tumbling sensor, which is the range gate's business, not this one's.
HEADING_MAX_SPREAD_DEG = float(os.environ.get("HEALTH_HEADING_MAX_SPREAD_DEG", "25"))
HEADING_MIN_SAMPLES = int(os.environ.get("HEALTH_HEADING_MIN_SAMPLES", "8"))


def _wrap180(deg):
    return (deg + 540.0) % 360.0 - 180.0


def _circular(deltas_deg):
    """(mean, spread) of angular differences, in degrees. Spread is the circular standard
    deviation — sqrt(-2 ln R) — so it is 0 for perfect agreement and grows without a hard cap.

    A plain arithmetic mean of angles is wrong at the wrap: averaging 359° and 1° gives 180°.
    This project has already been bitten by exactly that (the archive's window reads decimate
    with `max()`, not `avg()`, for the same reason), and the first pass of this analysis
    produced a nonsense +152° bias before it was redone properly."""
    if not deltas_deg:
        return None, None
    x = sum(math.cos(math.radians(d)) for d in deltas_deg)
    y = sum(math.sin(math.radians(d)) for d in deltas_deg)
    mean = math.degrees(math.atan2(y, x))
    r = math.hypot(x, y) / len(deltas_deg)
    spread = math.degrees(math.sqrt(-2.0 * math.log(r))) if r > 0 else None
    return mean, spread


def attitude_plausible(roll_deg=None, pitch_deg=None):
    """Is this attitude physically possible for this boat? Returns a dict, never raises.

    One sample is enough — that is the whole value of a range gate. At 22:58Z on Jul 18 the
    first bad sample was roll 133.1°, and this would have flagged it on the spot."""
    bad = []
    if roll_deg is not None and abs(roll_deg) > ROLL_LIMIT_DEG:
        bad.append(f"roll {roll_deg:+.1f}° beyond ±{ROLL_LIMIT_DEG:g}°")
    if pitch_deg is not None and abs(pitch_deg) > PITCH_LIMIT_DEG:
        bad.append(f"pitch {pitch_deg:+.1f}° beyond ±{PITCH_LIMIT_DEG:g}°")
    return {"ok": not bad, "status": "ok" if not bad else "danger",
            "reason": "attitude within limits" if not bad else "; ".join(bad),
            "roll_deg": roll_deg, "pitch_deg": pitch_deg,
            "limits": {"roll_deg": ROLL_LIMIT_DEG, "pitch_deg": PITCH_LIMIT_DEG}}


def heading_bias(samples):
    """Compass-vs-COG bias from [(epoch_s, heading_deg_true, cog_deg_true, sog_kn)].

    Returns status ok/warn/danger plus the measured bias, so the iPad can say "heading reads
    89° off GPS course" rather than showing a plausible, wrong number. Never picks a winner:
    the bias is evidence about the pair, and on this boat there is no second compass to fail
    over to anyway."""
    usable = [(h, c) for (_t, h, c, s) in samples
              if h is not None and c is not None and s is not None and s >= HEADING_MIN_SOG_KN]
    if len(usable) < HEADING_MIN_SAMPLES:
        return {"available": False, "status": "unknown", "samples": len(usable),
                "reason": f"need {HEADING_MIN_SAMPLES} samples over "
                          f"{HEADING_MIN_SOG_KN:g} kn (COG is meaningless at rest)"}
    mean, spread = _circular([_wrap180(h - c) for h, c in usable])
    # Decide on the value we REPORT, not on the raw float. Otherwise a bias of 14.951 prints as
    # "+15.0°" and is classified `ok` against a 15° threshold — a readout that contradicts its
    # own number is how a crew learns to ignore it.
    mean = round(mean, 1)
    out = {"available": True, "bias_deg": mean,
           "spread_deg": None if spread is None else round(spread, 1),
           "samples": len(usable),
           "thresholds": {"warn_deg": HEADING_WARN_DEG, "bad_deg": HEADING_BAD_DEG,
                          "max_spread_deg": HEADING_MAX_SPREAD_DEG}}
    if spread is not None and spread > HEADING_MAX_SPREAD_DEG:
        # Manoeuvring, or a sensor mid-tumble: the samples do not agree on any bias, so this
        # check has nothing to say. The range gate covers the tumble.
        out.update(status="unknown",
                   reason=f"samples disagree ({spread:.0f}° spread) — manoeuvring or unstable")
        return out
    a = abs(mean)
    if a >= HEADING_BAD_DEG:
        out.update(status="danger",
                   reason=f"heading reads {mean:+.0f}° off GPS course, held steadily "
                          f"({spread:.0f}° spread) — the compass is misaligned, not the boat")
    elif a >= HEADING_WARN_DEG:
        out.update(status="warn",
                   reason=f"heading {mean:+.0f}° off GPS course — more than leeway and current "
                          f"explain; check the compass mounting")
    else:
        out.update(status="ok", reason=f"heading within {mean:+.0f}° of GPS course")
    return out


def assess(source=None, minutes=None):
    """Sensor health through the active datasource: attitude range + heading-vs-COG bias."""
    src = source or datasource.active()
    minutes = HEADING_WINDOW_MIN if minutes is None else minutes
    try:
        roll = src.latest_value("navigation.attitude.roll")
        pitch = src.latest_value("navigation.attitude.pitch")
        hdg = src.series("navigation.headingTrue", minutes)
        cog = src.series("navigation.courseOverGroundTrue", minutes)
        sog = src.series("navigation.speedOverGround", minutes)
    except Exception as exc:                 # health checks must never take the engine down
        return {"available": False, "status": "unknown", "note": f"unreadable: {exc}"}

    deg = 57.29577951308232
    att = attitude_plausible(None if roll is None else roll * deg,
                             None if pitch is None else pitch * deg)
    # align the three series on the heading timestamps — nearest sample, no interpolation
    def nearest(rows, t):
        if not rows:
            return None
        return min(rows, key=lambda r: abs(r[0] - t))[1]
    samples = [(t, h * deg, (lambda v: None if v is None else (v * deg) % 360)(nearest(cog, t)),
                (lambda v: None if v is None else v * 1.943844)(nearest(sog, t)))
               for t, h in hdg]
    hb = heading_bias(samples)
    worst = "danger" if "danger" in (att["status"], hb.get("status")) else (
        "warn" if "warn" in (att["status"], hb.get("status")) else
        "unknown" if hb.get("status") == "unknown" else "ok")
    return {"available": True, "status": worst, "attitude": att, "heading": hb,
            "window_min": minutes}
