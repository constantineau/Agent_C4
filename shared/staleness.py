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
import math
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
CLOUD.update({"water_temp": (60, 45, 300), "bank_voltage": (60, 45, 300)})   # failover 45 s, as every cloud channel

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

# The chartplotter feed's own fix (gpsout, n2kout): 5 s aboard (docs/STALENESS.md). n2kout polls
# the engine at 1 Hz, so 5 s is four missed polls; 10 s at 8 kn put the boat 40 m off on the
# plotter. Aboard only: the cloud has no plotter. n2kout can't import shared/ (its image holds
# three files), so it keeps its own 5 and a test pins it to this one.
CHARTPLOTTER_FIX_S = 5

# Consumers whose limit on the NEWEST reading differs from the channel's dead limit (the
# channel x consumer table, #291): (onboard, cloud) seconds. They read a window of history and
# judge a trend or a mean, not one live value, so a few seconds' gap costs nothing; past this the
# window is old wind passed off as now, and the read disappears.
CONSUMER_S = {
    "trend": (30, 120),       # wind trend: newest TWS/TWD ("No live wind")
    "fatigue": (30, 120),     # helm fatigue: newest heading, heel, STW
    "eta_wind": (30, 120),    # navigator ETA: newest TWS/TWD of its 20-min wind window
}

# A windowed read needs at least this share of its window holding data, counted in 30 s buckets
# so the cloud's 15 s aggregates fill every bucket (tactics, the matcher's % of polar).
MIN_WINDOW_COVERAGE = 0.5
COVERAGE_BUCKET_S = 30.0


def consumer_s(name, at=None):
    """A consumer's limit (s) on the newest reading, from CONSUMER_S."""
    on, cloud = CONSUMER_S[name]
    return on if (at or where()) == "onboard" else cloud


def coverage(times, window_s, end):
    """Share of the window (`window_s` long, ending at `end`) with at least one sample per
    COVERAGE_BUCKET_S bucket. `times` are epoch seconds."""
    n = max(1, int(window_s // COVERAGE_BUCKET_S))
    filled = {int((end - t) // COVERAGE_BUCKET_S) for t in times
              if 0 <= end - t < n * COVERAGE_BUCKET_S}
    return len(filled) / n


# Every number above is a DEFAULT, measured on one boat's instruments. Another boat's sensors
# send at other rates, so an install may override any of them with STALENESS_OVERRIDES: inline
# JSON, or the path of a JSON file, e.g.
#   {"onboard": {"depth": [5, 10, 30]}, "cloud": {"tws": [60, 60, 180]},
#    "consumer": {"trend": [45, 120]}, "chartplotter_fix_s": 5}
# Channels take [late, failover, dead]; consumers take [onboard, cloud]. A bad entry is skipped
# with a log line and the default stays: a typo must not take the limits away.
def apply_overrides(cfg):
    """Merge an override dict into the tables (see above). Returns the list of skipped entries."""
    global CHARTPLOTTER_FIX_S
    bad = []
    if not isinstance(cfg, dict):
        return ["not a JSON object"]

    def nums(v, n):
        ok = (isinstance(v, (list, tuple)) and len(v) == n
              and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                      and math.isfinite(x) and x > 0 for x in v))
        return tuple(float(x) for x in v) if ok else None

    for at, table in (("onboard", ONBOARD), ("cloud", CLOUD)):
        for ch, v in (cfg.get(at) or {}).items():
            t = nums(v, 3)
            if t is None or not t[0] <= t[2]:
                bad.append(f"{at}.{ch}")
            else:
                table[ch] = t
    for name, v in (cfg.get("consumer") or {}).items():
        t = nums(v, 2)
        if t is None or name not in CONSUMER_S:
            bad.append(f"consumer.{name}")
        else:
            CONSUMER_S[name] = t
    if "chartplotter_fix_s" in cfg:
        t = nums([cfg["chartplotter_fix_s"]], 1)
        if t is None:
            bad.append("chartplotter_fix_s")
        else:
            CHARTPLOTTER_FIX_S = t[0]
    return bad


def _load_env_overrides():
    raw = os.environ.get("STALENESS_OVERRIDES", "").strip()
    if not raw:
        return
    try:
        import json
        if not raw.startswith("{"):
            with open(raw, encoding="utf-8") as f:
                raw = f.read()
        bad = apply_overrides(json.loads(raw))
    except Exception as e:  # noqa: BLE001 - a broken override keeps the defaults
        bad = [f"unreadable ({e})"]
    if bad:
        print(f"[staleness] STALENESS_OVERRIDES skipped: {', '.join(bad)}", flush=True)


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


_load_env_overrides()
