"""How old a reading may be, per channel: the one table every consumer reads (#247).

Decided by Cole 2026-11-09 from the measured cadence (docs/STALENESS.md). Three limits per channel:

- **late**: show the age next to the number. Nothing is wrong yet.
- **failover**: a two-source channel passes over its lead for the next rank past this age.
- **dead**: stop using the value for advice. The raw number stays on screen with a red age,
  and the advice that needs it disappears (no sail pick, no shift call, no CPA, no ETA).

Past RAW_CAP_S the raw number goes too ("—"). Onboard limits are 5-20x the worst gap seen in
79 h of full-res data (0.6 s for 10 Hz channels, 3.8 s for depth), so they would have raised no
false alarm on Jul 18 or at the dock. Cloud limits are set by the uplink (15 s buckets plus up
to ~36 s lag), not by the sensors.

Onboard or cloud is DATA_SOURCE, the same switch the datasource uses (the Pi engine sets it).
Pure stdlib: imported by the Pi engine, the VPS agent and the tests.
"""
import os

# channel -> (late, failover, dead), seconds.
ONBOARD = {
    # wind: 10 Hz, two sources
    "aws": (2, 5, 15), "awa": (2, 5, 15), "tws": (2, 5, 15), "twa": (2, 5, 15), "twd": (2, 5, 15),
    # heading and rate of turn: 10 Hz
    "heading_true": (2, 5, 15), "heading_mag": (2, 5, 15), "rate_of_turn": (2, 5, 15),
    # GPS: 10 Hz, two GPS. A fix is dead sooner than the rest (decided: 10 s).
    "lat": (2, 5, 10), "lon": (2, 5, 10), "sog": (2, 5, 10), "cog": (2, 5, 10),
    # attitude: 4-5 Hz
    "heel": (2, 5, 15), "pitch": (2, 5, 15),
    # one source each; failover only matters for an unranked stand-in
    "stw": (2, 5, 15),
    "depth": (5, 10, 15),
    # slow channels
    "water_temp": (10, 10, 60),
    "bank_voltage": (10, 10, 60),
    "rudder_angle": (2, 5, 15),
}
_FAST_CLOUD = (45, 45, 120)
CLOUD = {ch: _FAST_CLOUD for ch in ONBOARD}
CLOUD.update({"water_temp": (60, 60, 300), "bank_voltage": (60, 60, 300)})

# A channel not in the table (a new path) gets the slow limits: never tighter than the data.
_DEFAULT = {"onboard": (10, 10, 60), "cloud": (60, 60, 300)}

# Past this the raw number is gone too: "—" (decided: 30 min).
RAW_CAP_S = 30 * 60

# A dead sensor keeps its alert after its number has gone ("—" at 30 min), up to this long since
# its last reading. Not forever: a masthead unplugged for the winter must not alarm for months.
# 24 h covers the longest race day.
ALERT_HOLD_S = 24 * 3600

# The stale alert per channel: warn at the dead limit, danger past this (decided: per channel).
ALERT_DANGER_S = {"onboard": 60, "cloud": 300}

# The collision watch's own fix: the GPS dead limit aboard, 60 s in the cloud (decided).
COLLISION_FIX_S = {"onboard": 10, "cloud": 60}


def where():
    """'onboard' or 'cloud', from DATA_SOURCE (read each call: tests and the engine set it)."""
    return "onboard" if os.environ.get("DATA_SOURCE", "cloud").strip().lower() == "onboard" else "cloud"


def _channel(path_or_channel):
    from . import source_policy
    return source_policy.CHANNEL_FOR_PATH.get(path_or_channel, path_or_channel)


def limits(path_or_channel, at=None):
    """(late, failover, dead) in seconds for a channel or a Signal K path."""
    at = at or where()
    table = ONBOARD if at == "onboard" else CLOUD
    return table.get(_channel(path_or_channel), _DEFAULT[at])


def late_s(path_or_channel, at=None):
    return limits(path_or_channel, at)[0]


def failover_s(path_or_channel, at=None):
    return limits(path_or_channel, at)[1]


def dead_s(path_or_channel, at=None):
    return limits(path_or_channel, at)[2]


def state(path_or_channel, age_s, at=None):
    """'ok' | 'late' | 'dead' | 'gone' for a reading of this age; None when the age is unknown."""
    if age_s is None:
        return None
    late, _f, dead = limits(path_or_channel, at)
    if age_s > RAW_CAP_S:
        return "gone"
    if age_s > dead:
        return "dead"
    return "late" if age_s > late else "ok"


def is_dead(path_or_channel, age_s, at=None):
    """True when a reading this old must not be used for advice. Unknown age is not dead."""
    return age_s is not None and age_s > dead_s(path_or_channel, at)


# The stale alert is per channel GROUP (decided: per channel, not one all-channels alert): one
# alert for "wind", not five for aws/awa/tws/twa/twd from the same masthead. Each group is judged
# on its key channels, the ones advice is built on; a minor channel going quiet (the autopilot's
# rate of turn when it is switched off) is not a dead sensor the crew needs told about.
ALERT_GROUPS = (
    ("wind", "Wind", ("tws", "twd", "aws")),
    ("heading", "Heading", ("heading_true",)),
    ("gps", "GPS", ("lat", "sog")),
    ("heel", "Heel", ("heel",)),
    ("stw", "Boat speed", ("stw",)),
    ("depth", "Depth", ("depth",)),
    ("bank", "Bank voltage", ("bank_voltage",)),
)


def alert_channels():
    """Every key channel of the alert groups."""
    return sorted({c for _g, _l, keys in ALERT_GROUPS for c in keys})


def dead_groups(ages, at=None):
    """[{group, label, channels, age_s, severity}] for every alert group with a key channel past
    its dead limit; `ages` is {channel: seconds old}. Severity is 'warn' at the dead limit and
    'danger' past ALERT_DANGER_S (or twice the dead limit, for the slow bank voltage). A group none of whose key channels is reporting is left out:
    the strip's `available` and the whole-boat alert cover a boat that stopped talking. Ages
    past ALERT_HOLD_S are ignored."""
    at = at or where()
    out = []
    for gid, label, keys in ALERT_GROUPS:
        dead = [(ch, ages[ch]) for ch in keys
                if ages.get(ch) is not None and is_dead(ch, ages[ch], at)
                and ages[ch] <= ALERT_HOLD_S]
        if not dead:
            continue
        age = max(a for _c, a in dead)
        danger = max(ALERT_DANGER_S[at], 2 * max(dead_s(c, at) for c, _a in dead))
        out.append({"group": gid, "label": label, "channels": [c for c, _a in dead],
                    "age_s": round(age), "severity": "danger" if age > danger else "warn"})
    return out
