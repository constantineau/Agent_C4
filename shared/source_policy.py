"""Sensor-priority policy: which device leads each channel, and how a path maps to a channel.

Why this exists as committed Python and not only as SQL: the cloud reads `source_priority`
(migration 003) out of Postgres so it can be tuned live, but **the boat has no Postgres** — the
onboard engine reads a SQLite archive and the Signal K live cache. Before this module the
onboard read path (`datasource_onboard.latest_value`) ignored priority completely and simply
took whichever source wrote last.

So the policy lives here as the committed default, `vps/db/seed/source_priority.sql` seeds the
same content into the cloud DB, and `vps/agent/test_source_priority.py` parses the SQL and
asserts the two agree — they cannot silently drift.

Matchers are device names, resolved against `$source` labels by `shared/n2k_sources.py`.
Read that module first: for most of this project's life these matchers matched *nothing*,
which is indistinguishable from a policy that is being honoured.
"""
from . import n2k_sources

# A ranked source must be fresher than this to be used before falling back to the next rank.
# Mirrors tools.FAILOVER_AGE_S; the same 45 s applies onboard.
FAILOVER_AGE_S = 45

# channel -> device matchers, best first. Keep in lockstep with source_priority.sql.
DEFAULT_PRIORITY = {
    "heel": ["orca", "24xd", "reactor"],
    # pitch: the 24xd is mounted ~11 deg nose-up and uncalibrated — measured, see the seed
    "pitch": ["orca", "reactor", "24xd"],
    "rate_of_turn": ["orca", "reactor"],
    "heading_true": ["orca", "24xd", "943"],
    "heading_mag": ["orca", "24xd", "943"],
    "aws": ["gnd", "orca"],
    "awa": ["gnd", "orca"],
    "tws": ["orca", "derived"],
    "twa": ["orca", "derived"],
    "twd": ["orca", "derived"],
    "sog": ["24xd", "orca", "943", "b951"],
    "cog": ["24xd", "orca", "943", "b951"],
    "lat": ["24xd", "orca", "943", "b951"],
    "lon": ["24xd", "orca", "943", "b951"],
    "stw": ["gst"],
    "depth": ["intelliducer"],
    "water_temp": ["intelliducer", "gst"],
    "rudder_angle": ["reactor"],
    "bank_voltage": ["orca"],
}

# Signal K path -> channel. Mirrors the keys of tools.PRESENT (which also carries display units
# and converters the boat does not need); the test asserts the two agree.
CHANNEL_FOR_PATH = {
    "navigation.speedThroughWater": "stw",
    "navigation.speedOverGround": "sog",
    "navigation.courseOverGroundTrue": "cog",
    "navigation.headingTrue": "heading_true",
    "navigation.headingMagnetic": "heading_mag",
    "navigation.attitude.roll": "heel",
    "navigation.attitude.pitch": "pitch",
    "navigation.rateOfTurn": "rate_of_turn",
    "navigation.position.latitude": "lat",
    "navigation.position.longitude": "lon",
    "environment.wind.speedApparent": "aws",
    "environment.wind.angleApparent": "awa",
    "environment.wind.speedTrue": "tws",
    "environment.wind.angleTrueWater": "twa",
    "environment.wind.directionTrue": "twd",
    "environment.depth.belowTransducer": "depth",
    "environment.water.temperature": "water_temp",
    "steering.rudderAngle": "rudder_angle",
    "electrical.batteries.0.voltage": "bank_voltage",
}


def matchers_for(path_or_channel, priority=None):
    """Ranked matchers for a channel (or a Signal K path), best first; [] when unranked."""
    prio = DEFAULT_PRIORITY if priority is None else priority
    channel = CHANNEL_FOR_PATH.get(path_or_channel, path_or_channel)
    return list(prio.get(channel, ()))


def rank_for(path_or_channel, source, devices, priority=None):
    """Rank index of `source` for this channel (0 = lead), or None when it is not ranked.

    None is *not* "worst" — it means unranked, and callers must keep serving unranked sources
    (a source we have no opinion about is still real data). The whole reason this project's
    AIS bug ran for a race is that a read path silently preferred the wrong row; a priority
    that *drops* data would be the same mistake with better intentions."""
    for i, m in enumerate(matchers_for(path_or_channel, priority)):
        if n2k_sources.matches(source, m, devices):
            return i
    return None


def all_matchers(priority=None):
    """Every matcher named anywhere in the policy, de-duplicated — feed to
    `n2k_sources.unresolved()` to assert the policy can bind."""
    prio = DEFAULT_PRIORITY if priority is None else priority
    return sorted({m for ms in prio.values() for m in ms})
