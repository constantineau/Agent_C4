-- Curated reliability notes per sensor source (the agent reads these for skepticism).
-- `match` is a substring tested against the Signal K $source string; refine once the real
-- bus $source labels are known. Idempotent.
DELETE FROM source_notes WHERE boat_id = 'sr33';
INSERT INTO source_notes (boat_id, match, device, reliability, note) VALUES
 ('sr33','orca','Orca Core','high','9-axis IMU heel/attitude @10Hz <2 deg; heel-compensated wind. Valid only once the Core is calibrated and N2K sharing is enabled.'),
 ('sr33','24xd','Garmin GPS 24xd','medium','GPS/heading + 9-axis attitude @1Hz (heel backup to Orca). Magnetic heading +/-3 deg, needs compass calibration.'),
 ('sr33','gwind','gWind Race (via GND 10)','medium','Masthead APPARENT wind; NOT heel-compensated; needs angle-offset + speed-gain calibration; errors when heeled.'),
 ('sr33','gnd','Garmin GND 10','medium','Nexus->N2K wind bridge for the gWind Race; see gWind note.'),
 ('sr33','gst','Garmin GST 43 / GST 10','needs-calibration','Paddlewheel speed-through-water; speed factor MUST be calibrated or STW is wrong.'),
 ('sr33','gdt','Garmin GDT 43','medium','Depth + water temp; keel/depth offset set on the device.'),
 ('sr33','943','Garmin GPSMAP 943','high','Chartplotter internal GPS; also the N2K calibration console.'),
 ('sr33','b951','em-trak B951','high','AIS targets; own-ship GPS only if RMC/GPS-sentences enabled.'),
 ('sr33','reactor','Garmin Reactor 40','high','Autopilot AHRS rudder/ROT/attitude — only when the pilot is powered (NON-RACING). Pilot mode not decodable.');
