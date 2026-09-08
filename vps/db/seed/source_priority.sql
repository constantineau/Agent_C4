-- Preferred source order per quantity for the SR33. rank 1 = lead source; higher ranks are
-- automatic fallbacks if the preferred one is stale/silent. `match` is a substring tested
-- against the source's DEVICE IDENTITY (manufacturer + model + label) via
-- `shared/n2k_sources.py` — not against the bare `$source` label. Idempotent.
--
-- ⚠️ Keep in lockstep with `shared/source_policy.DEFAULT_PRIORITY`, which is what the BOAT
-- uses (the Pi has no Postgres). `vps/agent/test_source_priority.py` parses this file and
-- asserts the two agree, and that every matcher below resolves to something that publishes.
--
-- Revised 2026-09-07 after measuring the Jul 18 race. Until then the matchers below were
-- tested with `match in $source.lower()` against labels like `n2k-socketcan.15`, so NONE of
-- them ever matched and every channel silently used "freshest source wins". Two of the ranks
-- were also fiction; both are corrected here, and each correction cites what the data showed.
DELETE FROM source_priority WHERE boat_id = 'sr33';
INSERT INTO source_priority (boat_id, channel, rank, match, note) VALUES
 -- Attitude: Orca Core 9-axis IMU (<2 deg, 10 Hz) preferred; 24xd 1 Hz backup; pilot when on.
 -- MEASURED Jul 18: the Orca published NO roll/pitch/ROT at all (its N2K attitude sharing is
 -- not enabled — see source_notes), so the race ran on the Reactor 40 and the 24xd. Orca stays
 -- rank 1 deliberately: failover already covers a silent lead source, and this is the ranking
 -- we want the moment attitude sharing is switched on. The two that DID publish agree to
 -- 1.29 deg median / 2.98 deg max, so the inversion did not corrupt this race.
 ('sr33','heel',1,'orca','Orca Core IMU, heel-comp grade — SILENT on Jul 18 (N2K attitude sharing off)'),
 ('sr33','heel',2,'24xd','GPS 24xd 9-axis @1Hz backup'),
 ('sr33','heel',3,'reactor','autopilot AHRS (non-racing only)'),
 -- PITCH, measured over the healthy race window: the autopilot reads mean -0.39 deg
 -- (range -7.5..+7.8, symmetric about zero — a boat). The 24xd reads mean +10.95 deg and
 -- NEVER crosses zero (range +4.3..+19.9), i.e. it is mounted ~11 deg nose-up and has never
 -- been calibrated. So the pilot outranks it here even though it is "non-racing" for heel.
 ('sr33','pitch',1,'orca','silent on Jul 18, see heel'),
 ('sr33','pitch',2,'reactor','autopilot AHRS — the only pitch source that reads plausibly'),
 ('sr33','pitch',3,'24xd','~+11 deg mounting bias, uncalibrated (measured Jul 18)'),
 ('sr33','rate_of_turn',1,'orca','Orca ROT — silent on Jul 18, see heel'),
 ('sr33','rate_of_turn',2,'reactor','autopilot ROT (non-racing) — the only ROT publisher on Jul 18'),
 -- Heading: Orca compass preferred, 24xd (+/-3 deg) backup, chartplotter last.
 -- MEASURED Jul 18: only the 24xd published headingTrue (the Orca was silent here too).
 ('sr33','heading_true',1,'orca','silent on Jul 18'),
 ('sr33','heading_true',2,'24xd',NULL),
 ('sr33','heading_true',3,'943',NULL),
 ('sr33','heading_mag',1,'orca','silent on Jul 18'),
 ('sr33','heading_mag',2,'24xd',NULL),
 ('sr33','heading_mag',3,'943',NULL),
 -- Apparent wind: the gWind Race masthead is the real measurement — but it is NOT a distinct
 -- N2K device. It reaches the bus THROUGH the GND 10, so the old rank-1 `gwind` could never
 -- resolve. `gnd` IS the masthead. MEASURED Jul 18: GND 10 (130,990 samples) and the Orca
 -- (128,107) alternated ~50/50 under freshest-wins, and they disagree on AWS by 0.35 kn
 -- median / 1.75 kn p95 / 5.9 kn max — against a 0.6 kn "sensors disagree" threshold. So the
 -- displayed apparent wind was jittering by whichever device reported last.
 ('sr33','aws',1,'gnd','gWind Race masthead apparent, bridged by the GND 10 (raw, not heel-comp)'),
 ('sr33','aws',2,'orca','Orca apparent (heel-compensated processing)'),
 ('sr33','awa',1,'gnd',NULL),
 ('sr33','awa',2,'orca',NULL),
 -- True wind: Orca Sailing Processor (heel-compensated) preferred over our derived calc.
 -- MEASURED Jul 18: unbiased against derived-data (both mean 15.40 kn) but 0.15 kn median /
 -- 0.84 kn p95 instantaneous disagreement, which freshest-wins injected into the TWS trend.
 ('sr33','tws',1,'orca','Orca heel-compensated true wind'),
 ('sr33','tws',2,'derived','signalk-derived-data fallback'),
 ('sr33','twa',1,'orca',NULL),
 ('sr33','twa',2,'derived',NULL),
 ('sr33','twd',1,'orca',NULL),
 ('sr33','twd',2,'derived',NULL),
 -- Position/COG/SOG: dedicated 24xd GPS preferred; Orca and the chartplotter as backups.
 -- `b951`, the em-trak AIS transceiver's own-ship GPS, was rank 4 on all four of these until
 -- 2026-09-08. The argument was sound in isolation — its own-ship fix is a real fix and its
 -- foreign traffic is filtered upstream — and it is removed anyway, because it is the one
 -- ranking whose only protection is a filter somewhere else. Reading another vessel's position
 -- as own-ship already cost a race (see the archiver context filter and its regression test);
 -- ranking the AIS box for lat/lon is that bug written down as an intention, waiting for the
 -- filter to regress. Keep in lockstep with shared/source_policy.DEFAULT_PRIORITY.
 ('sr33','sog',1,'24xd',NULL),('sr33','sog',2,'orca',NULL),('sr33','sog',3,'943',NULL),
 ('sr33','cog',1,'24xd',NULL),('sr33','cog',2,'orca',NULL),('sr33','cog',3,'943',NULL),
 ('sr33','lat',1,'24xd',NULL),('sr33','lat',2,'orca',NULL),('sr33','lat',3,'943',NULL),
 ('sr33','lon',1,'24xd',NULL),('sr33','lon',2,'orca',NULL),('sr33','lon',3,'943',NULL),
 -- Single-source quantities (ranked so future redundancy slots in cleanly).
 -- `gdt` (Garmin GDT 43) was rank 1 for depth and water_temp, but there is no GDT on this bus:
 -- depth comes from a Garmin Intelliducer Thru-hull (n2k-socketcan.2, the only depth publisher
 -- on Jul 18) and water temp from that Intelliducer plus the GST 10 paddlewheel.
 ('sr33','stw',1,'gst','paddlewheel STW'),
 ('sr33','depth',1,'intelliducer','Garmin Intelliducer Thru-hull, the only depth source on the bus'),
 ('sr33','water_temp',1,'intelliducer','thru-hull temp'),('sr33','water_temp',2,'gst','paddlewheel temp'),
 ('sr33','rudder_angle',1,'reactor','autopilot rudder feedback (silent unless the pilot is powered)'),
 -- The house bank. Read by app/power.py; the Orca Core is the only publisher.
 ('sr33','bank_voltage',1,'orca','house bank voltage @0.7Hz — the Jul 18 discharge nobody was told about');
