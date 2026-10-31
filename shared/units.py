"""Unit conventions shared across the Agent_C4 pipeline.

Single source of truth so the Pi uplink, ingestion API, agent tools, and web app
all agree on what a number means. Signal K emits SI (m/s, radians, metres); we store
and present in sailing-conventional units (knots, degrees, metres).
"""

# Canonical stored/presented units per telemetry channel.
CHANNEL_UNITS = {
    "aws": "kn",       # apparent wind speed
    "awa": "deg",      # apparent wind angle, + to starboard
    "tws": "kn",       # true wind speed
    "twa": "deg",      # true wind angle, + to starboard
    "twd": "deg",      # true wind direction, degrees true
    "stw": "kn",       # speed through water
    "sog": "kn",       # speed over ground
    "cog": "deg",      # course over ground, degrees true
    "heading": "deg",  # heading, degrees true
    "lat": "deg",
    "lon": "deg",
    "depth": "m",      # water depth
}

TELEMETRY_CHANNELS = tuple(CHANNEL_UNITS.keys())

# Conversions from Signal K SI to our stored units (applied on the Pi uplink).
MS_TO_KN = 1.943844
RAD_TO_DEG = 57.295779513


def ms_to_kn(v):
    return None if v is None else v * MS_TO_KN


def rad_to_deg(v):
    return None if v is None else v * RAD_TO_DEG


# True wind angle. The instruments publish a SIGNED angle (+ starboard, − port), but the record
# carries it WRAPPED to 0–360, so a port-tack beat at 36° arrives as 324°. Anything that takes
# abs() of the raw value reads that beat as a 324°→"180°-ish" run (issue #47: the Jul 18 replay
# called the J1 wrong for the whole race). Normalise once at the read path with these.

def signed_twa(deg):
    """A TWA in degrees, any wrap, as signed [−180, 180): + starboard, − port. None passes."""
    if deg is None:
        return None
    return ((float(deg) + 180.0) % 360.0) - 180.0


def fold_twa(deg):
    """A TWA in degrees, any wrap, as the polar knows it: 0–180, tack dropped. None passes."""
    if deg is None:
        return None
    return abs(signed_twa(deg))


def rad_to_signed_twa_deg(v):
    """Signal K's angleTrueWater (radians, any wrap) → signed degrees, [−180, 180)."""
    return None if v is None else signed_twa(v * RAD_TO_DEG)
