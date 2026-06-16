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
