-- DEV-ONLY placeholder metadata so the agent's tools return something in Phase 0.
-- NOT loaded in prod (prod gets real polars/waypoints via the §9 open items).
-- Safe to re-run: upserts / time-relative AIS rows.

-- Synthetic placeholder polar (clearly not the real SR33 VPP — flagged in §9).
INSERT INTO polars (boat_id, tws, twa, target_stw, target_vmg) VALUES
  ('sr33',  6,  40, 5.2, 3.98),
  ('sr33',  6,  90, 6.4, 0.0),
  ('sr33',  6, 150, 5.6, 4.85),
  ('sr33', 10,  40, 6.3, 4.83),
  ('sr33', 10,  90, 7.8, 0.0),
  ('sr33', 10, 150, 7.1, 6.15),
  ('sr33', 14,  40, 6.8, 5.21),
  ('sr33', 14,  90, 8.6, 0.0),
  ('sr33', 14, 150, 8.4, 7.27),
  ('sr33', 20,  40, 7.1, 5.44),
  ('sr33', 20,  90, 9.4, 0.0),
  ('sr33', 20, 150, 9.9, 8.57)
ON CONFLICT (boat_id, tws, twa) DO UPDATE
  SET target_stw = EXCLUDED.target_stw, target_vmg = EXCLUDED.target_vmg;

-- Port Huron -> Mackinac-ish placeholder course (coords approximate, dev only).
DELETE FROM waypoints WHERE route = 'default';
INSERT INTO waypoints (route, seq, name, lat, lon) VALUES
  ('default', 1, 'Start (Port Huron)',   43.000, -82.420),
  ('default', 2, 'Cove Island',          45.300, -81.730),
  ('default', 3, 'Finish (Mackinac)',    45.847, -84.618);

-- A couple of recent AIS targets so get_ais_targets() is non-empty.
INSERT INTO ais_targets (time, boat_id, mmsi, name, lat, lon, sog, cog, range_nm, bearing, cpa_nm, tcpa_min) VALUES
  (now() - interval '20 seconds', 'sr33', 366123456, 'GORDON C', 43.05, -82.40, 11.2, 200, 3.1, 015, 0.4, 12.5),
  (now() - interval '25 seconds', 'sr33', 316987654, 'CSL NIAGARA', 43.12, -82.30, 13.8, 185, 7.8, 040, 2.2, 28.0);
