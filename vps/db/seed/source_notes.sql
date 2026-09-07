-- Curated reliability notes per sensor source (the agent reads these for skepticism).
-- `match` is a substring tested against the source's DEVICE IDENTITY (manufacturer + model +
-- label) via `shared/n2k_sources.py` — the bare Signal K `$source` label is an N2K address
-- (`n2k-socketcan.15`) and never contained these words. Idempotent.
--
-- Reconciled 2026-09-07 against the devices actually on the bus, read from the boat's Signal K
-- sources cache (2026-08-30 pull): .0 GND10 · .1 Reactor 40 · .2 Intelliducer Thru-hull ·
-- .3 GPS24xd · .4 GST10 · .6 GHC 50 · .7-.9 GNX120 · .10 GMI20 · .11 GPSMAP 943 · .12 GNX20 ·
-- .13 Fusion MS-RA210 · .15 Orca Core · .43 em-trak B951.
DELETE FROM source_notes WHERE boat_id = 'sr33';
INSERT INTO source_notes (boat_id, match, device, reliability, note) VALUES
 ('sr33','orca','Orca Core','high','9-axis IMU heel/attitude @10Hz <2 deg; heel-compensated wind. Valid only once the Core is calibrated and N2K sharing is enabled. MEASURED Jul 18 2026: it published wind, SOG/COG and position but NO roll/pitch/rateOfTurn — attitude sharing is still off, so every attitude channel fell to its backup.'),
 ('sr33','24xd','Garmin GPS 24xd','medium','GPS/heading + 9-axis attitude @1Hz (heel backup to Orca). Magnetic heading +/-3 deg, needs compass calibration. Sole headingTrue publisher on Jul 18.'),
 ('sr33','gwind','gWind Race (via GND 10)','medium','Masthead APPARENT wind; NOT heel-compensated; needs angle-offset + speed-gain calibration; errors when heeled. NOT A DISTINCT N2K SOURCE — it reaches the bus through the GND 10, so priority must be expressed as `gnd`.'),
 ('sr33','gnd','Garmin GND 10','medium','Nexus->N2K wind bridge for the gWind Race; see gWind note. This IS the masthead apparent wind on the bus. Disagrees with the Orca''s apparent by 0.35 kn median / 1.75 kn p95 (Jul 18) — cross-check, do not average blindly.'),
 ('sr33','gst','Garmin GST 43 / GST 10','needs-calibration','Paddlewheel speed-through-water; speed factor MUST be calibrated or STW is wrong. Sole STW publisher; also reports water temp.'),
 ('sr33','intelliducer','Garmin Intelliducer Thru-hull','medium','Depth + water temp; keel/depth offset set on the device. Sole depth publisher on the bus. Replaces the earlier `gdt` note — there is no GDT 43 on this boat.'),
 ('sr33','943','Garmin GPSMAP 943','high','Chartplotter internal GPS; also the N2K calibration console. Silent on the priority channels during Jul 18.'),
 ('sr33','b951','em-trak B951','high','AIS transceiver. Own-ship GPS only if RMC/GPS-sentences enabled; its FOREIGN AIS traffic was archived as own-ship telemetry until the 2026-09-07 context filter — treat any historical own-ship read from it before that date as suspect.'),
 ('sr33','reactor','Garmin Reactor 40','high','Autopilot AHRS rudder/ROT/attitude — only when the pilot is powered (NON-RACING). Pilot mode not decodable. Despite the non-racing caveat it supplied heel, pitch and rateOfTurn for the whole Jul 18 race, because the Orca was silent.');
