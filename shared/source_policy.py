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
import time as _time

from . import n2k_sources

# A ranked source must be fresher than this to be used before falling back to the next rank.
# Mirrors tools.FAILOVER_AGE_S; the same 45 s applies onboard.
#
# "Fresher" means age against the WALL CLOCK (`now - sample_time`), the same test `/conditions`
# applies (`tools._choose_preferred`, `onboard_conditions._choose_preferred`, both on `age_s`).
# Until #221 this module measured age from the newest sample of any publisher instead, so with
# the Orca last seen 90 s ago and `derived` 60 s ago tactics followed the Orca (30 s behind the
# newest) while `/conditions` followed `derived` (both stale, freshest wins). Two rules for one
# question let two screens name different leads. The relative rule existed so an archive replay
# would not call every source stale; the replay rig now freezes the process clock (freezegun,
# tools/replay/harness.py), so the wall clock is the replay's clock too. Onboard, a lead that has
# gone quiet must lose even when every other publisher went quiet with it.
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
    # `b951` — the em-trak AIS transceiver's own-ship GPS — was rank 4 on all four of these until
    # 2026-09-08. The argument for it was sound in isolation (its own-ship fix is a real fix, and
    # its foreign traffic is filtered upstream) and it is removed anyway, because it is the one
    # ranking whose *only* protection is a filter elsewhere in the stack. Reading another
    # vessel's position as own-ship already cost this project a race; a policy that says "use the
    # AIS box for lat/lon" is that bug written down as an intention, waiting for the filter to
    # regress. `sensor_health.choose_source()` refuses AIS-bearing sources outright for the same
    # reason. The 943 chartplotter is the third GPS and stays.
    "sog": ["24xd", "orca", "943"],
    "cog": ["24xd", "orca", "943"],
    "lat": ["24xd", "orca", "943"],
    "lon": ["24xd", "orca", "943"],
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


def series_publisher(path_or_channel, rows, devices, priority=None,
                     failover_s=FAILOVER_AGE_S, now=None):
    """Name the ONE publisher a window series should follow, or None when `rows` is empty.

    `rows` is a `series_by_source` result: [(source, epoch, value)]. A window read that keeps
    "whichever source wrote last each second" alternates between devices sample to sample; two
    TWD publishers ~10 deg apart then read as a 10 deg oscillation, and when one drops out
    mid-window as a lasting shift (#167). So the whole window follows one device.

    The pick mirrors the live reads (`OnboardSource._prefer`, `tools._choose_preferred`): the
    best-ranked source that is still current wins, current meaning its last sample is within
    `failover_s` of `now` (the wall clock unless given; see FAILOVER_AGE_S). A ranked lead that went quiet mid-window is passed over for the whole window,
    which removes the handover step. Within the winning rank — or among all current sources when
    none is ranked — the one with the latest sample wins, label breaking a tie: exactly the
    `preferred()` live pick, so the window and the live value never name different devices
    (picking by sample count here read 200 deg history against a 210 deg live value). Unranked
    sources are never dropped, only ranked below the policy's picks."""
    last = {}
    for src, t, _v in rows:
        last[src] = max(last.get(src, t), t)
    best = preferred(path_or_channel, [(s, t, None) for s, t in last.items()], devices,
                     priority, failover_s, now)
    return best[0] if best else None


def preferred(path_or_channel, candidates, devices, priority=None,
              failover_s=FAILOVER_AGE_S, now=None):
    """The live pick: one (source, epoch, value) from each publisher's latest sample, or None.

    Same rule as `OnboardSource._prefer`, `series_publisher` and `/conditions`: the best-ranked
    source whose sample is at most `failover_s` old wins (freshest within that rank), else the
    freshest of all. Age is `now - epoch`; `now` defaults to the wall clock (see FAILOVER_AGE_S)
    and is a parameter so a caller holding a better-trusted time can pass it. A live read that took "whoever wrote last" disagreed with the window read
    whenever two publishers differ, and tactics reported the gap as a current shift (#167).
    Ties on time go to the larger label, so the pick never depends on row order."""
    if not candidates:
        return None
    if now is None:
        now = _time.time()
    latest = lambda r: (r[1], r[0])
    freshest = max(candidates, key=latest)
    for m in matchers_for(path_or_channel, priority):
        ranked = [r for r in candidates if n2k_sources.matches(r[0], m, devices)
                  and now - r[1] <= failover_s]
        if ranked:
            return max(ranked, key=latest)
    return freshest


def one_publisher(path, rows, devices, priority=None):
    """[(epoch, value)] of the `series_publisher` pick, from a `series_by_source` result."""
    pick = series_publisher(path, rows, devices, priority)
    return [(t, v) for s, t, v in rows if s == pick]


def all_matchers(priority=None):
    """Every matcher named anywhere in the policy, de-duplicated — feed to
    `n2k_sources.unresolved()` to assert the policy can bind."""
    prio = DEFAULT_PRIORITY if priority is None else priority
    return sorted({m for ms in prio.values() for m in ms})
