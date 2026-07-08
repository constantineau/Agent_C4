# SR33 "C4" — Sensor / Instrument Integration Matrix

> **Research snapshot (2026-06-16) — historical record.** The device facts remain valid
> reference; the "planned" implementation items at the bottom have since been **built**
> (AIS ingestion + the onboard `/ais` + fleet tactics; heel/pitch/ROT flow with
> collect-everything), and the wide `telemetry` table it mentions was superseded by
> `telemetry_raw` and dropped (migration 006).


The onboard instrument package and how each device reaches the Agent_C4 pipeline
(NMEA 2000 → PICAN-M/SocketCAN `can0` → Signal K (canboatjs) → uplink → cloud).
Researched 2026-06-16 from manufacturer manuals + the Signal K/canboat source; see
per-device source links at the bottom of each section.

**Bottom line:** all nine devices are on the NMEA 2000 bus and **decode with stock
canboatjs/signalk-server — no custom plugins** for the core telemetry. The GST 43 (speed)
needs the **GST 10** analog→N2K converter; the GDT 43 (depth) needs a passive Garmin→N2K
adapter. **Heel/attitude is available on N2K (PGN 127257) from the Orca Core (10 Hz) and the
GPS 24xd (1 Hz), independent of the autopilot.** The autopilot (Reactor 40) is OFF during
racing, so its rudder/ROT/attitude are non-racing-only bonus data, and **Garmin autopilot
mode/state is the only thing we cannot get** (proprietary PGN 126720, not reverse-engineered).

## Matrix

| # | Device | Role | Interface | Key PGNs transmitted | Signal K paths | Our channel / table | Status |
|---|--------|------|-----------|----------------------|----------------|---------------------|--------|
| 1 | **Garmin GND 10** | Wind bridge (Nexus/gWind → N2K) | N2K (bridges Nexus FDX) | 130306 Wind | `environment.wind.speedApparent`,`angleApparent` (+`speedTrue`/`directionTrue` if true-ref) | `aws`,`awa` (`tws`,`twa`,`twd`) | ✅ handled (true wind via ref field or derived-data) |
| 2 | **Garmin GPS 24xd** | GPS + heading + 9-axis MEMS | N2K (010‑02316‑10) | 129025,129026,129029,127250,**127257 Attitude @1 Hz**,127258 | `navigation.position`,`courseOverGroundTrue`,`speedOverGround`,`headingMagnetic`→`headingTrue`,`attitude`,`magneticVariation` | `lat`,`lon`,`cog`,`sog`,`heading`; **`heel`(roll)**,**`pitch`** (backup source) | ✅ handled; attitude confirmed @1 Hz |
| 3 | **Garmin GDT 43** | Depth + water temp | N2K (via passive Garmin adapter) | 128267 Water Depth; 130312/130316 Temp (Sea) | `environment.depth.belowTransducer`; `environment.water.temperature` | `depth`; **`water_temp`** new | ✅ handled + new temp |
| 4 | **Garmin GST 43** | Speed thru water + temp | **analog → GST 10 → N2K** | 128259 Speed Water Ref; 130312 Temp (Sea) | `navigation.speedThroughWater`; `environment.water.temperature` | `stw`; `water_temp` | ✅ handled (needs **GST 10** module) |
| 5 | **Garmin GPSMAP 943** | Chartplotter / GPS map; **calibration console** | N2K + 0183 + Marine Net + WiFi | 129025,129026,129029,127250,129283 XTE,129284 Nav | position/COG/SOG/heading; `navigation.courseGreatCircle.*` | redundant nav source; **route/XTE** optional | ✅ handled; key calibration role (below) |
| 6 | **em‑trak B951** | Class B AIS (5 W) + internal GPS | N2K + 0183(38400) + USB | 129038,129039,129040,129041,129794,129809,129810,… | AIS targets as separate `vessels.urn:mrn:imo:mmsi:*` contexts | **`ais_targets` table** (needs uplink ingestion) | 🔧 decoded by SK; uplink AIS ingestion = new work |
| 7 | **Garmin Reactor 40** | Autopilot (CCU 9‑axis AHRS) | N2K | 127245 Rudder, 127251 ROT, 127257 Attitude (126720 proprietary) | `steering.rudderAngle`,`navigation.rateOfTurn`,`navigation.attitude` | **`rudder_angle`**,**`rate_of_turn`**,`heel`/`pitch` new | ✅ rudder/ROT/attitude; ❌ pilot mode (proprietary); **see racing note** |
| 8 | **Garmin gWind Race** | Masthead wind sensor (feeds device 1) | Nexus FDX → GND 10 | (via GND 10) 130306 | `environment.wind.speedApparent`,`angleApparent` | `aws`,`awa` → `tws`/`twa`/`twd` derived | ✅ apparent wind; **heel-uncompensated** |
| 9 | **Orca Core** | Nav hub: 9-axis IMU + GPS + compass (existing) | N2K + WiFi | **127257 Attitude @10 Hz**,127250,127251,129025/26/29,130306(heel-comp),127508 | `navigation.attitude`,`headingMagnetic`,`rateOfTurn`,`position`,`environment.wind.*` | **`heel`(roll) PRIMARY**,`pitch`,`rate_of_turn` | ✅ heel via 127257 (10 Hz, <2°) — needs Core calibrated + sharing on |

## Per-device notes (key gotchas only)

**1. GND 10 (wind).** Bridges the legacy Nexus/gWind transducer onto N2K as PGN 130306.
PGN 130306 has a *Reference* field, so it can carry apparent **or** true wind. Whether the
GND 10 emits true-referenced wind on N2K is **unconfirmed by Garmin docs — verify on the
bus**; regardless, we can derive true wind in Signal K (`signalk-derived-data`) from
apparent + STW + heading. **Wind angle offset/damping must be calibrated via NexusRace
(USB) or a Garmin display — not from the Pi.** A stale canboatjs can mis-read the 130306
reference bit (old bug) → keep canboatjs current.

**2. GPS 24xd (GPS + heading).** Use the **N2K** part number 010‑02316‑10. Outputs position,
COG/SOG, magnetic heading (±3°), attitude. **No rate-of-turn** (that comes from the Reactor
40). Heading **must be calibrated** (compass cal + heading alignment) via the GPSMAP 943 —
the Pi can't. Emits magnetic heading; Signal K derives true heading from variation —
don't double-correct. Up to 10 Hz → decimate before the cloud uplink.

**3. GDT 43 (depth).** Self-contained smart transducer; only needs a passive Garmin→N2K
adapter cable (no sounder box). Depth offset (keel/waterline) is set on the device **via the
943**; with no display it emits offset 0 → only `belowTransducer`, so apply the offset in
Signal K.

**4. GST 43 (speed).** Paddlewheel — **requires the Garmin GST 10 analog→N2K converter**
(5 Hz). Paddlewheel **speed calibration via a Garmin display**; known gotcha: calibrating
with a GPS on the bus can fail — calibrate against a known SOG run. Both GDT 43 and GST 43
report water temp tagged "Sea Temperature" → if both are present they **collide on
`environment.water.temperature`**; set distinct instances or use Signal K source priority.

**5. GPSMAP 943 (chartplotter).** Primarily our **calibration & N2K-setup console** —
*Settings ▸ Communications ▸ NMEA 2000 Setup ▸ Device List* configures the 24xd heading,
GDT 43 depth offset, and GST 43 speed factor. **Signal K/the Pi cannot run Garmin's
calibration UI, so keep the 943 on the bus for commissioning.** It also has an internal GPS;
which node actually sources position on the bus depends on N2K source-selection — verify.
Garmin Marine Network (Ethernet) is proprietary/undocumented — no telemetry value; use N2K.

**6. em‑trak B951 (AIS).** Each AIS target becomes a **separate Signal K vessel context**
(`vessels.urn:mrn:imo:mmsi:<MMSI>`), not a field on our own boat — so our uplink needs a
new path to read other-vessel contexts and write the `ais_targets` table. Own-ship GPS PGNs
(129025/26/29) are **off until the RMC/"Enable GPS sentences" toggle** is set (proAIS2 over
USB). **CPA/TCPA is not transmitted — it's computed downstream** (Signal K plugin or our
agent). N2K is the recommended feed; 0183@38400 or USB are debug fallbacks. MMSI must be
programmed for it to transmit.

**7. Reactor 40 (autopilot).** The CCU has a 9-axis AHRS — our **best source of rate-of-turn
and attitude (heel/pitch)**, which the 24xd lacks. Decodes stock to `steering.rudderAngle`,
`navigation.rateOfTurn`, `navigation.attitude`. **Autopilot mode/engaged/target-heading is
Garmin-proprietary (PGN 126720) and not decodable** — no Signal K plugin reads Garmin pilot
state (the `signalk-autopilot-garmin` plugin is command-only). **READ-ONLY SAFETY:** we only
ingest — never command the pilot. Defense in depth: (a) load no N2K *write* plugins in Signal
K; (b) optionally bring `can0` up `listen-only on` (controller can't transmit/ACK). Worth
logging raw 126720 frames now in case we ever reverse-engineer pilot state later.
**RACING REALITY (confirmed):** the boat is hand-steered and the **autopilot is OFF during
races** (most sailing). So the Reactor 40's rudder/ROT/attitude are **only available in
non-racing use** (deliveries, motoring, practice with the pilot on) — treat them as bonus
data for those modes, NOT a racing telemetry source. Heel for racing comes from the Orca Core
/ GPS 24xd instead (see device 9 and the resolved heel-source note below).

**8. gWind Race (masthead wind).** The actual wind sensor feeding device 1 (GND 10) over the
Nexus FDX bus; reaches us as PGN 130306 → `aws`/`awa`. **It measures APPARENT wind only and
is NOT heel/motion-compensated** — a heeled/pitching masthead reads AWA/AWS errors (worst
upwind in breeze). It supports good static calibration (angle offset, speed gain ~70% for the
3-blade prop, per-angle TWA/TWS race tables) via **NexusRace over USB or a Garmin display** —
not from the Pi. The "Race" version adds the per-angle correction tables and a factory cal
certificate; set wind-angle offset and speed gain before trusting AWA/AWS. The GND 10 forwards
**apparent** wind on N2K (true wind is computed downstream), so we compute true wind ourselves.

**9. Orca Core (nav hub / heel source).** Existing on the bus (the brief's untouched Orca
system). 9-axis IMU + GNSS + compass; **transmits PGN 127257 Attitude on N2K at 10 Hz,
<2° heel accuracy** → decodes stock to `navigation.attitude.roll` = our **primary heel
source**, independent of the autopilot. Also emits heading, rate-of-turn, position/COG/SOG,
battery, and **heel-compensated wind on 130306** (its "Sailing Processor" already corrects
wind for heel/leeway/mast motion). **Gotchas:** (a) the Core won't broadcast heading/attitude/
ROT until it has been **calibrated** (calm water, 360° turn, ~3 min); (b) N2K sensor sharing
must be **enabled** in the Orca app (My Boat ▸ Connected Devices); (c) it duplicates
position/heading/wind sources already on the bus → use Signal K source priorities (prefer
Orca for attitude/heel; pick one canonical position/heading source). No local API needed —
heel is on N2K. Verify PGN 127257 with `candump`/canboat once wired.

## New telemetry this package unlocks (beyond the current schema)

Current `telemetry` channels: `aws awa tws twa twd stw sog cog heading lat lon depth`. The
real sensors add:

- **`heel`** (= `navigation.attitude.roll`) — directly comparable to the Speed Guide's target
  heel per TWS/TWA. High coaching value, AND used to correct the masthead wind for heel.
  **Heel-source: RESOLVED — available on N2K independent of the autopilot.** Primary =
  **Orca Core** PGN 127257 @10 Hz (<2°); backup = **GPS 24xd** PGN 127257 @1 Hz. Both decode
  to `navigation.attitude.roll`; use Signal K source priority to prefer the Orca Core. The
  Reactor 40 also provides attitude but only when the pilot is powered (non-racing), so it's
  not the racing source.
- **`pitch`** (`navigation.attitude.pitch`) — fore/aft trim, sea-state proxy.
- **`rate_of_turn`** (`navigation.rateOfTurn`) — from the Reactor 40; useful for maneuver/tack detection.
- **`rudder_angle`** (`steering.rudderAngle`) — helm load / autopilot activity proxy.
- **`water_temp`** (`environment.water.temperature`) — current/front detection, comfort.
- **AIS targets** → the existing `ais_targets` table, populated from Signal K's other-vessel
  contexts (new uplink ingestion path; CPA/TCPA computed by us).

Not available: **Garmin autopilot mode/state** (proprietary). True wind depends on the GND 10
or `signalk-derived-data`.

## Implementation impact (planned)

1. **True wind — two possible sources to reconcile:** (a) the **Orca Core already broadcasts
   heel-compensated wind on 130306** (its Sailing Processor), which may be the better true-wind
   source out of the box; (b) compute it ourselves with **`signalk-derived-data`** from the
   gWind Race apparent + STW + heading + heel. Plan: capture both on the bus, compare, and pick
   the canonical wind source via Signal K source priority. Either way, heel (now resolved) feeds
   accuracy. This is the next build step.
2. **Extend `telemetry`** with `heel, pitch, rate_of_turn, rudder_angle, water_temp`; extend the
   uplink `PATH_MAP` + `shared/units`/`tool_contracts` accordingly.
3. **AIS ingestion:** uplink subscribes to other-vessel contexts → `ais_targets`; compute CPA/TCPA.
4. **Agent:** use `heel` vs target heel for coaching; surface rudder/ROT for maneuver context.
5. **Ops doc:** the GPSMAP 943 is the calibration console; autopilot is read-only.
