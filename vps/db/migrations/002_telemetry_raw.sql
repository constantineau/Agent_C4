-- Collect-everything paradigm: capture every (source, path) reading from Signal K, including
-- redundant sources, with full provenance. The agent reasons over all of it and cross-checks.
-- This supersedes the single-value-per-channel `telemetry` table for live data (that table is
-- kept for now but the uplink writes here).

CREATE TABLE IF NOT EXISTS telemetry_raw (
    time      TIMESTAMPTZ      NOT NULL,
    boat_id   TEXT             NOT NULL DEFAULT 'sr33',
    source    TEXT             NOT NULL,   -- Signal K $source (bus.address / device label)
    path      TEXT             NOT NULL,   -- Signal K path, e.g. navigation.headingMagnetic
    value     DOUBLE PRECISION,            -- numeric value, SI as Signal K provides it
    str_value TEXT                         -- non-numeric values (mode strings, etc.)
);
SELECT create_hypertable('telemetry_raw', 'time', if_not_exists => TRUE);
-- query by quantity across all sources, and by source for health/freshness
CREATE INDEX IF NOT EXISTS traw_path_time_idx   ON telemetry_raw (boat_id, path, time DESC);
CREATE INDEX IF NOT EXISTS traw_source_time_idx ON telemetry_raw (boat_id, source, time DESC);

-- Human-curated reliability notes per source/device. The agent reads these so it knows which
-- sensors may be uncalibrated or flaky, independent of what the numbers look like.
CREATE TABLE IF NOT EXISTS source_notes (
    id          SERIAL PRIMARY KEY,
    boat_id     TEXT NOT NULL DEFAULT 'sr33',
    match       TEXT NOT NULL,   -- substring matched against source or device label
    device      TEXT,            -- friendly device name
    reliability TEXT,            -- 'high' | 'medium' | 'needs-calibration' | 'unreliable'
    note        TEXT
);
