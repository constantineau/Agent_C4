"""Sensor-priority policy: which device leads each channel, and how a path maps to a channel.

Why this exists as committed Python and not only as SQL: the cloud reads `source_priority`
(migration 003) out of Postgres so it can be tuned live, but **the boat has no Postgres** — the
onboard engine reads a SQLite archive and the Signal K live cache. Before this module the
onboard read path (`datasource_onboard.latest_value`) ignored priority completely and simply
took whichever source wrote last.

So the policy is the boat's (its profile's `hardware.source_priority`, #313), the onboard reads
take it from the current boat's profile, `vps/db/seed/source_priority.sql` seeds the same content
into the cloud DB, and `vps/agent/test_source_priority.py` parses the SQL and asserts it matches
the profile — they cannot silently drift.

Matchers are device names, resolved against `$source` labels by `shared/n2k_sources.py`.
Read that module first: for most of this project's life these matchers matched *nothing*,
which is indistinguishable from a policy that is being honoured.
"""
import time as _time

from . import n2k_sources
from . import staleness

# A ranked source must be fresher than its channel's failover limit to be used before falling back
# to the next rank: `staleness.failover_s` (#247, decided 2026-11-09): 5 s aboard for the fast
# channels, 10 s for the slow ones, 45 s in the cloud (its 15 s buckets). FAILOVER_AGE_S is the
# LONGEST of those, kept as the bound archive and cloud queries read candidates within.
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


def _failover(path_or_channel, failover_s):
    return staleness.failover_s(path_or_channel) if failover_s is None else failover_s

# channel -> device matchers, best first: the BOAT's, from its profile (`hardware.source_priority`,
# shared/boat_hardware.py, #313). Each boat has its own instruments, so no device name lives here.
# The engine installs where the current boat's priority comes from (`set_provider`, app/hardware.py);
# a test pins one (`set_priority`). Neither → {}: no priority, freshest wins, and
# sensor health says so. The cloud reads the same policy from Postgres (`source_priority`, seeded
# from the boat's profile by vps/db/seed/source_priority.sql; test_source_priority asserts the two
# agree). Never rank an AIS transceiver for position: its own-ship fix shares a source with other
# vessels' (see sensor_health.choose_source, which refuses AIS-bearing sources outright).
_POLICY = {"pinned": None, "provider": None}


def set_provider(fn):
    """Install `fn() -> {channel: [matchers]} | None`, the current boat's priority (None clears)."""
    _POLICY["provider"] = fn


def set_priority(priority):
    """Pin one priority for every read (tests); None unpins."""
    _POLICY["pinned"] = None if priority is None else {
        ch: [m.lower() for m in ms] for ch, ms in priority.items()}


def active_priority():
    """The priority in force: the pinned one, else the provider's, else {} (no priority). A
    provider that fails gives {}: a priority read must never take the engine down."""
    if _POLICY["pinned"] is not None:
        return _POLICY["pinned"]
    fn = _POLICY["provider"]
    if fn is None:
        return {}
    try:
        p = fn()
    except Exception:  # noqa: BLE001
        return {}
    return p if isinstance(p, dict) else {}


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
    prio = active_priority() if priority is None else priority
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
                     failover_s=None, now=None):
    """Name the ONE publisher a window series should follow, or None when `rows` is empty.

    `rows` is a `series_by_source` result: [(source, epoch, value)]. A window read that keeps
    "whichever source wrote last each second" alternates between devices sample to sample; two
    TWD publishers ~10 deg apart then read as a 10 deg oscillation, and when one drops out
    mid-window as a lasting shift (#167). So the whole window follows one device.

    The pick mirrors the live reads (`OnboardSource._prefer`, `tools._choose_preferred`): the
    best-ranked source that is still current wins, current meaning its last sample is within
    `failover_s` of `now` (the wall clock unless given; default: the channel's
    `staleness.failover_s`). A ranked lead that went quiet mid-window is passed over for the whole window,
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
              failover_s=None, now=None):
    """The live pick: one (source, epoch, value) from each publisher's latest sample, or None.

    Same rule as `OnboardSource._prefer`, `series_publisher` and `/conditions`: the best-ranked
    source whose sample is at most `failover_s` (default: the channel's limit) old wins (freshest within that rank), else the
    freshest of all. Age is `now - epoch`; `now` defaults to the wall clock (see FAILOVER_AGE_S)
    and is a parameter so a caller holding a better-trusted time can pass it. A live read that took "whoever wrote last" disagreed with the window read
    whenever two publishers differ, and tactics reported the gap as a current shift (#167).
    Ties on time go to the larger label, so the pick never depends on row order."""
    if not candidates:
        return None
    if now is None:
        now = _time.time()
    failover_s = _failover(path_or_channel, failover_s)
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
    prio = active_priority() if priority is None else priority
    return sorted({m for ms in prio.values() for m in ms})
