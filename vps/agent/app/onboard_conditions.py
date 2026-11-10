"""Live instrument conditions for the ONBOARD engine service (Phase 9.1).

The cloud builds the instrument strip / multi-source view in `tools.py` straight off Postgres
(`get_strip` / `get_current_conditions` / `get_sources`). Onboard there is no Postgres — the
same views are built here from `datasource.active()` (an `OnboardSource`: SQLite archive + live
Signal K cache). The collect-everything ethos is preserved: every (path, source) is surfaced
with freshness and a disagreement flag.

This mirrors `tools.PRESENT` / `DISAGREE`; kept standalone so the onboard image needs none of
the cloud tool stack (DB pool, alerts, summarizer, LLM). If a channel is added to tools.PRESENT,
mirror it here.
"""
import os

from shared import n2k_sources
from shared import units
from shared import source_policy
from shared import staleness

from . import datasource
from . import fatigue

BOAT_ID = os.environ.get("BOAT_ID", "sr33")

_ms_to_kn = lambda x: x * 1.943844
_rad_to_deg = lambda x: x * 57.295779513
_k_to_c = lambda x: x - 273.15
_id = lambda x: x

# Signal K path -> (channel, display unit, SI->display converter). Mirror of tools.PRESENT.
PRESENT = {
    "navigation.speedThroughWater":    ("stw", "kn", _ms_to_kn),
    "navigation.speedOverGround":      ("sog", "kn", _ms_to_kn),
    "navigation.courseOverGroundTrue": ("cog", "°", _rad_to_deg),
    "navigation.headingTrue":          ("heading_true", "°", _rad_to_deg),
    "navigation.headingMagnetic":      ("heading_mag", "°", _rad_to_deg),
    "navigation.attitude.roll":        ("heel", "°", _rad_to_deg),
    "navigation.attitude.pitch":       ("pitch", "°", _rad_to_deg),
    "navigation.rateOfTurn":           ("rate_of_turn", "°/s", _rad_to_deg),
    "navigation.position.latitude":    ("lat", "°", _id),
    "navigation.position.longitude":   ("lon", "°", _id),
    "environment.wind.speedApparent":  ("aws", "kn", _ms_to_kn),
    "environment.wind.angleApparent":  ("awa", "°", _rad_to_deg),
    "environment.wind.speedTrue":      ("tws", "kn", _ms_to_kn),
    "environment.wind.angleTrueWater": ("twa", "°", units.rad_to_signed_twa_deg),  # signed, #47
    "environment.wind.directionTrue":  ("twd", "°", _rad_to_deg),
    "environment.depth.belowTransducer": ("depth", "m", _id),
    "environment.water.temperature":   ("water_temp", "°C", _k_to_c),
    "steering.rudderAngle":            ("rudder_angle", "°", _rad_to_deg),
    # The bank. Absent from both PRESENT tables until 2026-09-07, which is why a six-hour
    # discharge to 11.08 V went unremarked through the Jul 18 race — see app/power.py.
    "electrical.batteries.0.voltage": ("bank_voltage", "V", _id),
}
DISAGREE = {"°": 6.0, "kn": 0.6, "m": 1.0, "°C": 2.0, "°/s": 5.0, "V": 0.5}


def _age(epoch):
    import time
    return round(time.time() - epoch, 1)


def _choose_preferred(channel, readings):
    """Lead reading by device priority, failing over when the ranked source is stale/absent.
    Returns (reading, reason, fell_back) — the same contract as `tools._choose_preferred`.

    Until 2026-09-07 this was "freshest wins", with a note here saying to refine it once
    real-bus `$source` priority was available onboard. It now is: `shared/source_policy` holds
    the committed policy (the boat has no Postgres to read the table from) and
    `shared/n2k_sources` resolves N2K addresses to devices, which is what the seeded matchers
    are actually written against. Measured on Jul 18: freshest-wins put heel on the autopilot
    AHRS — the source the policy annotates "non-racing only" — for 58% of reads, and alternated
    apparent wind between two devices that disagree by up to 5.9 kn."""
    devices = n2k_sources.devices_for(BOAT_ID)
    matchers = source_policy.matchers_for(channel)
    for i, m in enumerate(matchers):
        fresh = [r for r in readings
                 if n2k_sources.matches(r["source"], m, devices)
                 and r["age_s"] <= staleness.failover_s(channel)]
        if fresh:
            return min(fresh, key=lambda r: r["age_s"]), f"priority rank {i+1} ({m})", i > 0
    best = min(readings, key=lambda r: r["age_s"])
    if matchers:
        return best, "no preferred source fresh — using freshest available", True
    return best, "no priority set — freshest available", False


def get_current_conditions(max_age_minutes: int = 5):
    """Every live quantity, from EVERY reporting source, with freshness + a disagreement flag.
    The lead per channel is the priority-ranked device with automatic failover; all readings
    stay visible."""
    rows = datasource.active().latest_per_source(list(PRESENT.keys()), max_age_minutes)
    channels = {}
    for r in rows:
        ch, unit, conv = PRESENT[r["path"]]
        channels.setdefault(ch, {"unit": unit, "readings": []})
        channels[ch]["readings"].append(
            {"source": r["source"], "value": round(conv(r["value"]), 3),
             "age_s": _age(r["epoch"])}
        )
    for ch, c in channels.items():
        vals = [x["value"] for x in c["readings"]]
        c["freshest_age_s"] = min(x["age_s"] for x in c["readings"])
        if len(vals) > 1:
            c["spread"] = round(units.channel_spread(ch, vals), 3)   # circular for angles (#47)
            c["disagreement"] = c["spread"] > DISAGREE.get(c["unit"], 1e9)
        best, reason, fell_back = _choose_preferred(ch, c["readings"])
        c["preferred"] = {"source": best["source"], "value": best["value"],
                          "age_s": best["age_s"],
                          "device": (n2k_sources.resolve(best["source"],
                                                         n2k_sources.devices_for(BOAT_ID))
                                     or {}).get("model")}
        c["preferred_reason"] = reason
        if fell_back:
            c["fell_back"] = True   # ranked sensor stale/silent — the iPad should say so
    if not channels:
        return {"available": False, "note": "no telemetry in window"}
    from datetime import datetime, timezone
    return {
        "available": True, "as_of": datetime.now(timezone.utc).isoformat(),
        "channels": channels,
        "note": ("Onboard engine: every source kept; `preferred` is the priority-ranked device "
                 "with automatic failover, and `fell_back=true` means the ranked sensor was "
                 "stale/silent and a backup is in use — say so. Cross-check disagreement; "
                 "treat stale/uncalibrated sources with caution."),
    }


def _older_readings(have):
    """Channels absent from the 5-min conditions, from each path's last sample (two
    index-bounded queries per path). Returns ({channel: {"preferred", "freshest_age_s"}} for
    those at most RAW_CAP_S old, which stay on screen, and {channel: age} for alert-group key
    channels at most ALERT_HOLD_S old, so a dead sensor's alert outlives its number)."""
    raw, alert = {}, {}
    src = datasource.active()
    sample = getattr(src, "latest_sample", None)
    if sample is None:
        return raw, alert
    keys = set(staleness.alert_channels())
    for path, (name, _unit, conv) in PRESENT.items():
        if name in have:
            continue
        try:
            smp = sample(path)
        except Exception:
            continue
        if not smp or smp[0] is None or smp[1] is None:
            continue
        age = _age(float(smp[1]))
        if name in keys and age <= staleness.ALERT_HOLD_S:
            alert[name] = age
        if age <= staleness.RAW_CAP_S:
            raw[name] = {"preferred": {"source": None, "value": round(conv(smp[0]), 3),
                                       "age_s": age},
                         "freshest_age_s": age}
    return raw, alert


def get_strip():
    """Compact best-value-per-channel for the iPad instrument strip (freshest source wins)."""
    # Raw readings stay up to RAW_CAP_S (30 min) with their age; past each channel's DEAD limit
    # the value fields below go null, so nothing built on the strip (the sail call, the depth
    # alarm, the copilot's "Now:" line) uses it (#247). The raw number and its state are in `raw`
    # for the screens.
    cc = get_current_conditions(max_age_minutes=5)
    ch = dict(cc.get("channels") or {}) if cc.get("available") else {}
    # A channel silent for over 5 min: its last sample from `latest_sample` (two index-bounded
    # queries per path) rather than widening the window to 30 min, which would aggregate 30 min
    # of 10 Hz archive rows on the Pi whenever the live cache misses a path.
    older, alert_ages = _older_readings(set(ch))
    ch.update(older)
    if not ch:
        return {"available": False}
    if not cc.get("available"):
        from datetime import datetime, timezone
        cc = {"as_of": datetime.now(timezone.utc).isoformat()}
    ages_by_ch = {name: c["preferred"]["age_s"] for name, c in ch.items()}

    def best(name):
        c = ch.get(name)
        if not c or staleness.is_dead(name, c["preferred"]["age_s"]):
            return None
        return c["preferred"]["value"]

    ages = [c["freshest_age_s"] for c in ch.values()]
    # True heading wins; a magnetic one is only a stand-in and says so (#183-8): on Lake Huron
    # variation is ~7-9°, so a bare "heading" that is really magnetic points the bow wrong.
    heading, heading_ref = best("heading_true"), "true"
    if heading is None:
        heading, heading_ref = best("heading_mag"), "magnetic"
    if heading is None:
        heading_ref = None
    fi = fatigue.compute_fatigue_index()
    return {
        "available": True, "as_of": cc["as_of"],
        "data_age_seconds": min(ages) if ages else None,
        "stale": (min(ages) if ages else 999) > 30,
        # Age of each value shown (the lead source's sample), per channel (#45.3): the two
        # fields above are the FRESHEST channel's, which the 0.7 Hz bank voltage keeps near 0.
        "ages": ages_by_ch,
        # Per channel (#247): the lead's raw value, its age and ok/late/dead against that
        # channel's limits. A dead value is still shown (red age); past 30 min it is gone.
        "raw": {name: {"value": c["preferred"]["value"], "age_s": c["preferred"]["age_s"],
                       "state": staleness.state(name, c["preferred"]["age_s"])}
                for name, c in ch.items()},
        "dead": sorted(n for n, a in ages_by_ch.items() if staleness.is_dead(n, a)),
        # The per-channel stale alert (#247): one entry per sensor group whose key channel is dead.
        # (a channel gone from the screen after 30 min keeps its alert up to ALERT_HOLD_S)
        "dead_groups": staleness.dead_groups({**alert_ages, **ages_by_ch}),
        "stw": best("stw"), "sog": best("sog"), "tws": best("tws"), "twa": best("twa"),
        "twd": best("twd"), "aws": best("aws"), "awa": best("awa"), "heading": heading,
        "heading_ref": heading_ref,
        "heel": best("heel"), "depth": best("depth"),
        "cog": best("cog"), "lat": best("lat"), "lon": best("lon"),
        "fatigue": fi.get("index"), "fatigue_level": fi.get("level"),
    }


def source_stale_s(paths):
    """How long a source may go unheard before the DATA tile calls it stale (#291): the failover
    limit of the fastest channel it sends (`shared/staleness.py`: 5 s for the fast channels
    aboard, 10 s for the slow ones). A source's last-seen time is its newest message on any
    channel, so its fastest channel sets its cadence. Unknown paths: the table's default."""
    if not paths:
        return staleness.failover_s("")
    return min(staleness.failover_s(p) for p in paths)


def get_sources(max_age_minutes: int = 10):
    """Active sensor sources onboard: what's reporting and how fresh (no curated notes onboard)."""
    out = []
    for s in datasource.active().sources(max_age_minutes):
        out.append({"source": s["source"], "last_seen_s": _age(s["last_epoch"]),
                    "paths": s["paths"], "samples": s["samples"],
                    "stale_after_s": source_stale_s(s.get("path_names")),
                    "device": None, "reliability": "unknown", "note": None})
    return {"count": len(out), "sources": out,
            "note": "Onboard source list (curated reliability notes live in the cloud DB)."}
