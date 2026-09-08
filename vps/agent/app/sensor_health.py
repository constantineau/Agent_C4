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

from shared import n2k_sources
from shared import source_policy

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

# --- provenance / policy-in-force checks ------------------------------------
BOAT_ID = os.environ.get("BOAT_ID", "sr33")
# Sources that publish computed values rather than measuring anything. Not a fault — `derived`
# is a legitimately ranked matcher for the true-wind channels — but the crew is entitled to know
# which numbers on the iPad are measured and which are arithmetic.
SYNTHETIC_HINTS = ("derived", "signalk", "n2k-socketcan.100", "virtual")


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


def assess_provenance(channels, ais_excluded=(), boat_id=None):
    """Is the sensor policy actually IN FORCE, and where is each number coming from?

    Pure function of a `/conditions/full` channels payload, so the replay rig and the tests see
    exactly what the boat sees. Three checks plus a per-channel provenance table:

      - `policy_binds` — every matcher in `source_policy` names a device that exists. For most
        of this project's life none of them did (`orca` vs `n2k-socketcan.15`), and an unmatched
        matcher is **indistinguishable from a satisfied one** unless something asserts it. That
        is the sixth instance of this repo's recurring defect shape: designed, seeded, wired,
        and silently not in force. It is a `warn`, not a `danger` — the numbers are real, the
        *ranking* is not happening.
      - `lead_source` — which channels are running on a backup. `fell_back` already rides
        `/conditions/full` per channel and nothing displayed it; on Jul 18 freshest-wins put
        heel on the autopilot AHRS (the source the policy annotates "non-racing only") for 58%
        of reads and nobody could have known. Two causes, and they are **not** the same alarm:
        a ranked device that was publishing and **went silent** is happening now and is a
        `warn`; a ranked device that has **never published** the channel at all is a false
        premise in the policy (the Orca Core is ranked first for heel/pitch/rate-of-turn and
        published none of them during the race — its N2K attitude sharing is off) and reports
        as a standing `note` with the status left `ok`. Conflating them puts the chip
        permanently yellow, which is the same as switching it off.
      - `own_ship` — the AIS read filter. `ais_excluded` is *positive* confirmation the filter
        bound to something; a channel whose lead is an AIS-bearing source means it did not, and
        that is the bug that fed another vessel's position to the engine for a sixth of a race.
        Loud on purpose: `danger`.

    The provenance table is the point of the whole thing — every number the iPad shows, with the
    device that produced it, its rank, its age and whether it is a fallback."""
    devices = n2k_sources.devices_for(BOAT_ID if boat_id is None else boat_id)
    channels = channels or {}
    ais = set(ais_excluded or ())
    if not channels:
        # No readings means no basis for any of the three verdicts — in particular the policy
        # check compares matchers against the sources actually observed, so an empty window
        # would report every non-device matcher as unbindable. Say "unknown" and stop.
        return {"available": False, "status": "unknown", "checks": {}, "flags": [], "notes": [],
                "channels": {}, "reason": "no live channels to attribute"}

    labels = sorted({r["source"] for c in channels.values() for r in c.get("readings") or []})
    unresolvable = n2k_sources.unresolved(source_policy.all_matchers(), devices, labels)

    prov, went_silent, unmet, unranked, ais_leading, synthetic = {}, [], [], [], [], []
    for ch, c in sorted(channels.items()):
        pref = c.get("preferred") or {}
        src_label = pref.get("source")
        readings = c.get("readings") or []
        matchers = source_policy.matchers_for(ch)
        rank = source_policy.rank_for(ch, src_label, devices) if src_label else None
        # Is the rank-1 device publishing this channel AT ALL in the window? `readings` is every
        # source reporting the channel, so "absent from readings" distinguishes a device that
        # went quiet mid-race from one that has never been a real option.
        lead_m = matchers[0] if matchers else None
        lead_seen = [r for r in readings
                     if lead_m and n2k_sources.matches(r["source"], lead_m, devices)]
        entry = {
            "source": src_label,
            "device": pref.get("device") or n2k_sources.resolve(src_label, devices) or None,
            "value": pref.get("value"), "unit": c.get("unit"),
            "age_s": pref.get("age_s"),
            "rank": None if rank is None else rank + 1,
            "ranked_first": lead_m,
            "lead_publishes": bool(lead_seen),
            "reason": c.get("preferred_reason"),
            "fell_back": bool(c.get("fell_back")),
            "sources": len(readings),
            "spread": c.get("spread"),
            "disagreement": bool(c.get("disagreement")),
        }
        if isinstance(entry["device"], dict):
            entry["device"] = entry["device"].get("model")
        # Match on the DEVICE identity, not the bare label: `n2k-socketcan.5` is Garmin's
        # "Virtual N2K Input Handler" and `.100` is signalk-server, neither of which is
        # recognisable from the address alone.
        ident = n2k_sources.identity(src_label, devices) if src_label else ""
        entry["measured"] = not any(h in ident for h in SYNTHETIC_HINTS)
        prov[ch] = entry
        if entry["fell_back"]:
            item = {"channel": ch, "expected": lead_m,
                    "using": entry["device"] or src_label, "age_s": entry["age_s"]}
            if lead_seen:
                # `default=None` and the generator guard: a health check must never take the
                # engine down, and a reading without an age is a payload we did not write.
                item["lead_age_s"] = min(
                    (r["age_s"] for r in lead_seen if r.get("age_s") is not None), default=None)
                went_silent.append(item)
            else:
                unmet.append(item)
        if not matchers:
            unranked.append(ch)
        if src_label in ais:
            ais_leading.append({"channel": ch, "source": src_label})
        if not entry["measured"]:
            synthetic.append(ch)

    checks = {
        "policy_binds": {
            "status": "warn" if unresolvable else "ok",
            "unresolvable": unresolvable,
            "reason": (f"{len(unresolvable)} priority matcher(s) name no device on this bus "
                       f"({', '.join(unresolvable)}) — those channels are unranked in practice"
                       if unresolvable else
                       f"all {len(source_policy.all_matchers())} priority matchers bind"),
        },
        "lead_source": {
            "status": "warn" if went_silent else "ok",
            "went_silent": went_silent, "policy_unmet": unmet, "unranked": unranked,
            "reason": (", ".join(f"{f['channel']} on {f['using'] or '?'} — ranked "
                                 f"{f['expected']} went silent"
                                 + (f" ({f['lead_age_s']:.0f} s ago)"
                                    if f.get("lead_age_s") is not None else "")
                                 for f in went_silent)
                       if went_silent else
                       "every ranked channel is on its rank-1 device"
                       + (f"; {len(unranked)} unranked channel(s) take the freshest source"
                          if unranked else "")),
            # A standing configuration fact, not an in-race alarm: it will be true on every poll
            # of every race until someone turns the Orca's attitude sharing on.
            "note": (f"{len(unmet)} channel(s) never see their ranked lead ("
                     + ", ".join(f"{f['channel']}→{f['expected']}" for f in unmet)
                     + ") — the policy names a device that does not publish them"
                     if unmet else None),
        },
        "own_ship": {
            "status": "danger" if ais_leading else "ok",
            "ais_excluded": sorted(ais), "ais_leading": ais_leading, "synthetic": synthetic,
            "reason": ("AIS traffic is leading " + ", ".join(x["channel"] for x in ais_leading)
                       + " — another vessel's data is being read as own-ship"
                       if ais_leading else
                       f"AIS filter excluding {', '.join(sorted(ais))} from own-ship reads"
                       if ais else
                       "no AIS-bearing source identified — nothing to exclude"),
            # The excluded list is POSITIVE evidence the filter bound to something. An empty list
            # on a boat that carries an AIS transceiver is worth an eyebrow, but it is normal on
            # the bench, so it is a note rather than a status.
            "note": (f"{', '.join(synthetic)} computed, not measured" if synthetic else None),
        },
    }

    order = {"danger": 3, "warn": 2, "ok": 0}
    worst = max((c["status"] for c in checks.values()), key=lambda s: order.get(s, 0))
    flags = [c["reason"] for c in checks.values() if c["status"] in ("warn", "danger")]
    notes = [c["note"] for c in checks.values() if c.get("note")]
    return {"available": bool(channels), "status": worst if channels else "unknown",
            "checks": checks, "flags": flags, "notes": notes, "channels": prov,
            "reason": ("; ".join(flags) if flags else
                       checks["own_ship"]["reason"] if channels else
                       "no live channels to attribute")}


def assess(source=None, minutes=None, conditions=None):
    """Sensor health through the active datasource: attitude range + heading-vs-COG bias, plus
    the provenance/policy checks when a `/conditions/full` payload is supplied.

    `conditions` is passed in rather than fetched here so this module stays independent of
    `onboard_conditions` (which the cloud image does not ship) and stays a pure-ish function of
    data the caller already has — the engine polls conditions anyway."""
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

    ais = set()
    try:                                     # onboard only; the cloud source has no such method
        ais = set(src.ais_sources() or ())
    except Exception:
        pass
    prov = assess_provenance((conditions or {}).get("channels") or {}, ais_excluded=ais)

    statuses = (att["status"], hb.get("status"), prov["status"])
    worst = "danger" if "danger" in statuses else (
        "warn" if "warn" in statuses else
        "unknown" if "unknown" in statuses else "ok")
    # One line the iPad's health chip can show verbatim: every check that is not clean, worst
    # first. A chip that says "3 flags" and makes the crew go looking is a chip they ignore.
    flags = []
    if att["status"] != "ok":
        flags.append(att["reason"])
    if hb.get("status") in ("warn", "danger"):
        flags.append(hb["reason"])
    flags.extend(prov["flags"])
    return {"available": True, "status": worst, "attitude": att, "heading": hb,
            "provenance": prov, "flags": flags, "notes": prov.get("notes") or [],
            "reason": "; ".join(flags) if flags else "instruments cross-check clean",
            "window_min": minutes}
