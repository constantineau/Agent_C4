-- Preferred source order per quantity for the SR33. rank 1 = lead source; higher ranks are
-- automatic fallbacks if the preferred one is stale/silent. `match` is a substring tested
-- against the Signal K $source (refine once real bus labels are known). Idempotent.
DELETE FROM source_priority WHERE boat_id = 'sr33';
INSERT INTO source_priority (boat_id, channel, rank, match, note) VALUES
 -- Attitude: Orca Core 9-axis IMU (<2 deg, 10 Hz) preferred; 24xd 1 Hz backup; pilot when on.
 ('sr33','heel',1,'orca','Orca Core IMU, heel-comp grade'),
 ('sr33','heel',2,'24xd','GPS 24xd 9-axis @1Hz backup'),
 ('sr33','heel',3,'reactor','autopilot AHRS (non-racing only)'),
 ('sr33','pitch',1,'orca',NULL),
 ('sr33','pitch',2,'24xd',NULL),
 ('sr33','rate_of_turn',1,'orca','Orca ROT'),
 ('sr33','rate_of_turn',2,'reactor','autopilot ROT (non-racing)'),
 -- Heading: Orca compass preferred, 24xd (+/-3 deg) backup, chartplotter last.
 ('sr33','heading_true',1,'orca',NULL),
 ('sr33','heading_true',2,'24xd',NULL),
 ('sr33','heading_true',3,'943',NULL),
 ('sr33','heading_mag',1,'orca',NULL),
 ('sr33','heading_mag',2,'24xd',NULL),
 ('sr33','heading_mag',3,'943',NULL),
 -- Apparent wind: the gWind Race masthead is the real measurement.
 ('sr33','aws',1,'gwind','masthead apparent (raw)'),
 ('sr33','aws',2,'gnd','via GND 10'),
 ('sr33','awa',1,'gwind',NULL),
 ('sr33','awa',2,'gnd',NULL),
 -- True wind: Orca Sailing Processor (heel-compensated) preferred over our derived calc.
 ('sr33','tws',1,'orca','Orca heel-compensated true wind'),
 ('sr33','tws',2,'derived','signalk-derived-data fallback'),
 ('sr33','twa',1,'orca',NULL),
 ('sr33','twa',2,'derived',NULL),
 ('sr33','twd',1,'orca',NULL),
 ('sr33','twd',2,'derived',NULL),
 -- Position/COG/SOG: dedicated 24xd GPS preferred; Orca, chartplotter, AIS GPS as backups.
 ('sr33','sog',1,'24xd',NULL),('sr33','sog',2,'orca',NULL),('sr33','sog',3,'943',NULL),('sr33','sog',4,'b951',NULL),
 ('sr33','cog',1,'24xd',NULL),('sr33','cog',2,'orca',NULL),('sr33','cog',3,'943',NULL),('sr33','cog',4,'b951',NULL),
 ('sr33','lat',1,'24xd',NULL),('sr33','lat',2,'orca',NULL),('sr33','lat',3,'943',NULL),('sr33','lat',4,'b951',NULL),
 ('sr33','lon',1,'24xd',NULL),('sr33','lon',2,'orca',NULL),('sr33','lon',3,'943',NULL),('sr33','lon',4,'b951',NULL),
 -- Single-source quantities (ranked so future redundancy slots in cleanly).
 ('sr33','stw',1,'gst','paddlewheel STW'),
 ('sr33','depth',1,'gdt','depth transducer'),
 ('sr33','water_temp',1,'gdt',NULL),('sr33','water_temp',2,'gst',NULL),
 ('sr33','rudder_angle',1,'reactor','autopilot rudder feedback');
