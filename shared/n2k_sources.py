"""N2K `$source` → physical device identity, so sensor-priority policy can actually bind.

The problem this exists to solve (measured 2026-09-07 against the Jul 18 race):
`source_priority` (migration 003) and `source_notes` are written in **device** terms —
`orca`, `24xd`, `reactor`, `gnd`, `943` — but Signal K hands us **N2K addresses**:
`n2k-socketcan.15`. `tools._choose_preferred` tested `match in source.lower()`, which cannot
succeed for any of those matchers on any channel, so every channel silently took the
"no preferred source fresh — using freshest available" branch. The boat has never had a
sensor-priority policy in force. `source_priority.sql` even says so — *"refine once real bus
labels are known"* — and the labels are now known.

Identity comes from Signal K's own `sources-cache.json` (equivalently
`GET /signalk/v1/api/sources`), which carries the NMEA-2000 product info per address. The map
below was generated from the 2026-08-30 boat pull. It is **per boat** and keyed on the address
label; a source that is not in the map falls back to matching on the raw label, which is
exactly today's behaviour — so an unmapped boat or bench is never made worse.

⚠️ **N2K addresses are claimed at power-up and can move.** `serial` is recorded per entry for
exactly that reason: `drift()` compares a live sources payload against the map and reports any
address that now answers as a different device. `unresolved()` is the other half — it reports
matchers that resolve to nothing, which is the failure mode that hid this bug for a year (an
unmatched matcher and a satisfied one looked identical). Both are asserted in
`vps/agent/test_source_priority.py`.

Regenerate after a bus change:
    python3 -c "import json,sys; from shared import n2k_sources as n; \
        print(n.as_python_literal(n.build_from_signalk(json.load(open(sys.argv[1])))))" \
        sources-cache.json
"""

# source label -> physical device. Generated from the boat's Signal K sources cache
# (2026-08-30 pull); `serial` is the N2K modelSerialCode, used only for drift detection.
SR33_DEVICES = {
    "n2k-socketcan.0": {"manufacturer": "Garmin", "model": "GND10", "serial": "3478933721"},
    "n2k-socketcan.1": {"manufacturer": "Garmin", "model": "Reactor 40", "serial": "3421852328"},
    "n2k-socketcan.2": {"manufacturer": "Garmin", "model": "Intelliducer Thru-hull",
                        "serial": "3501055109"},
    "n2k-socketcan.3": {"manufacturer": "Garmin", "model": "GPS24xd-NMEA2000",
                        "serial": "3479803194"},
    "n2k-socketcan.4": {"manufacturer": "Garmin", "model": "GST10", "serial": "3482766394"},
    "n2k-socketcan.5": {"manufacturer": "Garmin", "model": "Virtual N2K Input Handler",
                        "serial": "3432723336"},
    "n2k-socketcan.6": {"manufacturer": "Garmin", "model": "GHC 50", "serial": "3424543487"},
    "n2k-socketcan.7": {"manufacturer": "Garmin", "model": "GNX120", "serial": "3472407070"},
    "n2k-socketcan.8": {"manufacturer": "Garmin", "model": "GNX120", "serial": "3472407117"},
    "n2k-socketcan.9": {"manufacturer": "Garmin", "model": "GNX120", "serial": "3489389515"},
    "n2k-socketcan.10": {"manufacturer": "Garmin", "model": "GMI20", "serial": "3456999608"},
    "n2k-socketcan.11": {"manufacturer": "Garmin", "model": "GPSMAP 943",
                         "serial": "3432723336"},
    "n2k-socketcan.12": {"manufacturer": "Garmin", "model": "GNX20", "serial": "3514775528"},
    "n2k-socketcan.13": {"manufacturer": "Fusion Electronics", "model": "MS-RA210",
                         "serial": "3458367108"},
    "n2k-socketcan.15": {"manufacturer": "Orca Technologoes AS", "model": "Orca Core",
                         "serial": "d77c42"},
    "n2k-socketcan.43": {"manufacturer": "em-trak Marine Electronics",
                         "model": "B951 AIS Class B Transceiver", "serial": "1730044"},
    "n2k-socketcan.100": {"manufacturer": "Signal K", "model": "signalk-server",
                          "serial": "255850"},
}

DEVICES_BY_BOAT = {"sr33": SR33_DEVICES}

# Paths only an AIS receiver publishes. A source carrying any of these is an AIS channel, so its
# `navigation.*` rows describe OTHER vessels and must never be read as own-ship data. Identified
# from the data rather than by source name because N2K assigns addresses dynamically and they
# differ per boat. Canonical here so the three readers cannot drift: `datasource_onboard`
# (engine + replay), `tools.ais_bearing_sources` (cloud/Postgres) and `tools/replay/truth.py`.
AIS_MARKER_PATHS = ("sensors.ais.class", "atonType.id", "design.aisShipType.id",
                    "navigation.specialManeuver", "offPosition")


def devices_for(boat_id):
    """The address→device map for a boat, or {} when we have none (label-only matching)."""
    return DEVICES_BY_BOAT.get(boat_id, {})


def resolve(source, devices):
    """The device dict for a `$source` label, or None when the label is unmapped."""
    return (devices or {}).get(source)


def identity(source, devices):
    """Lowercase haystack a priority matcher is tested against: manufacturer, model and the
    raw label. **Serial is deliberately excluded** — matchers like `943` are model numbers and
    would collide with digit runs inside a serial."""
    d = resolve(source, devices)
    if not d:
        return (source or "").lower()
    parts = [d.get("manufacturer") or "", d.get("model") or "", source or ""]
    return " ".join(p for p in parts if p).lower()


def matches(source, matcher, devices):
    """True when `matcher` (a `source_priority.match` substring) names this source's device.

    Falls back to the raw label for unmapped sources, so `derived` still matches
    `derived-data` and an unknown boat behaves exactly as it did before this module."""
    if not matcher:
        return False
    return matcher.lower() in identity(source, devices)


def unresolved(matchers, devices, labels=()):
    """Matchers that name neither a mapped device nor any label in `labels` — i.e. policy that
    cannot bind to anything.

    `labels` is for sources that are not bus devices at all: `derived-data`, `course-provider`,
    a bench provider. Pass the source labels actually observed (from `telemetry_raw` or the
    archive) and the check becomes "every matcher names something that really publishes".

    Returned sorted and de-duplicated. With no device map there are no identities to check
    against, so the result is empty rather than "everything" — an unmapped boat must not fail
    this assertion."""
    if not devices:
        return []
    hay = [identity(s, devices) for s in devices] + [(l or "").lower() for l in labels]
    want = {m.lower() for m in matchers if m}
    return sorted(m for m in want if not any(m in h for h in hay))


def drift(payload, devices):
    """[(source, expected_serial, found_serial)] where a live sources payload disagrees with
    the map — an N2K address that has been re-claimed by another device. Sources absent from
    the payload are not reported (a device can simply be powered off)."""
    live = build_from_signalk(payload)
    out = []
    for source, want in (devices or {}).items():
        got = live.get(source)
        if got and got.get("serial") and want.get("serial") and got["serial"] != want["serial"]:
            out.append((source, want["serial"], got["serial"]))
    return sorted(out)


def build_from_signalk(payload):
    """Turn a Signal K sources payload into an address→device map.

    Accepts `sources-cache.json` / `GET /signalk/v1/api/sources` shape: a dict keyed by source
    label whose `updates[].source` carries the N2K product info. Entries without product info
    are skipped rather than recorded as empty."""
    out = {}
    for label, entry in (payload or {}).items():
        if not isinstance(entry, dict):
            continue
        src = {}
        for upd in entry.get("updates") or []:
            if isinstance(upd, dict) and isinstance(upd.get("source"), dict):
                src = upd["source"]
                break
        model = src.get("modelId")
        if not model:
            continue
        out[label] = {"manufacturer": src.get("manufacturerCode"), "model": model,
                      "serial": str(src.get("modelSerialCode") or "") or None}
    return out


def as_python_literal(devices, indent="    "):
    """Render a map as the source literal above, so a regenerated map can be pasted in."""
    def addr(label):
        try:
            return int(label.rsplit(".", 1)[-1])
        except ValueError:
            return 1 << 30
    lines = []
    for label in sorted(devices, key=addr):
        d = devices[label]
        lines.append(f'{indent}"{label}": {{"manufacturer": {d.get("manufacturer")!r}, '
                     f'"model": {d.get("model")!r}, "serial": {d.get("serial")!r}}},')
    return "{\n" + "\n".join(lines) + "\n}"
