-- Agent_C4 / SR33 AI Navigator — initial schema.
-- Runs once on first DB init (mounted into /docker-entrypoint-initdb.d).
-- TimescaleDB hypertables for time-series; plain tables for metadata.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- Time-series: 15-s telemetry aggregates pushed from the boat.
-- Wide row (one column per normalized Signal K channel) — simple to query.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry (
    time      TIMESTAMPTZ      NOT NULL,
    boat_id   TEXT             NOT NULL DEFAULT 'sr33',
    aws       DOUBLE PRECISION,   -- apparent wind speed (kn)
    awa       DOUBLE PRECISION,   -- apparent wind angle (deg, +stbd)
    tws       DOUBLE PRECISION,   -- true wind speed (kn)
    twa       DOUBLE PRECISION,   -- true wind angle (deg, +stbd)
    twd       DOUBLE PRECISION,   -- true wind direction (deg true)
    stw       DOUBLE PRECISION,   -- speed through water (kn)
    sog       DOUBLE PRECISION,   -- speed over ground (kn)
    cog       DOUBLE PRECISION,   -- course over ground (deg true)
    heading   DOUBLE PRECISION,   -- heading (deg true)
    lat       DOUBLE PRECISION,
    lon       DOUBLE PRECISION,
    depth     DOUBLE PRECISION    -- water depth (m)
);
SELECT create_hypertable('telemetry', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS telemetry_boat_time_idx ON telemetry (boat_id, time DESC);

-- ---------------------------------------------------------------------------
-- Time-series: AIS targets (one row per target observation).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ais_targets (
    time      TIMESTAMPTZ      NOT NULL,
    boat_id   TEXT             NOT NULL DEFAULT 'sr33',
    mmsi      BIGINT           NOT NULL,
    name      TEXT,
    lat       DOUBLE PRECISION,
    lon       DOUBLE PRECISION,
    sog       DOUBLE PRECISION,
    cog       DOUBLE PRECISION,
    range_nm  DOUBLE PRECISION,   -- range from us (nm)
    bearing   DOUBLE PRECISION,   -- bearing from us (deg true)
    cpa_nm    DOUBLE PRECISION,   -- closest point of approach (nm)
    tcpa_min  DOUBLE PRECISION    -- time to CPA (min); negative = opening
);
SELECT create_hypertable('ais_targets', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ais_boat_time_idx ON ais_targets (boat_id, time DESC);

-- ---------------------------------------------------------------------------
-- Metadata (plain relational tables).
-- ---------------------------------------------------------------------------

-- Boat polar table: target boatspeed by (TWS, TWA). get_polar_target() reads this.
CREATE TABLE IF NOT EXISTS polars (
    boat_id     TEXT             NOT NULL DEFAULT 'sr33',
    tws         DOUBLE PRECISION NOT NULL,   -- true wind speed bucket (kn)
    twa         DOUBLE PRECISION NOT NULL,   -- true wind angle (deg)
    target_stw  DOUBLE PRECISION NOT NULL,   -- target boatspeed (kn)
    target_vmg  DOUBLE PRECISION,            -- target VMG (kn)
    PRIMARY KEY (boat_id, tws, twa)
);

-- Route waypoints / marks. get_route_status() tracks these in sequence.
CREATE TABLE IF NOT EXISTS waypoints (
    id        SERIAL PRIMARY KEY,
    route     TEXT             NOT NULL DEFAULT 'default',
    seq       INTEGER          NOT NULL,
    name      TEXT             NOT NULL,
    lat       DOUBLE PRECISION NOT NULL,
    lon       DOUBLE PRECISION NOT NULL
);

-- Race metadata.
CREATE TABLE IF NOT EXISTS race_info (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    route       TEXT,
    start_time  TIMESTAMPTZ,
    notes       TEXT
);

-- Crew observations written to the timeline by log_note().
CREATE TABLE IF NOT EXISTS crew_notes (
    id      SERIAL PRIMARY KEY,
    time    TIMESTAMPTZ NOT NULL DEFAULT now(),
    boat_id TEXT NOT NULL DEFAULT 'sr33',
    author  TEXT,
    text    TEXT NOT NULL
);

-- Agent's periodic conditions/performance summaries (compact long-term memory).
CREATE TABLE IF NOT EXISTS agent_summaries (
    id            SERIAL PRIMARY KEY,
    time          TIMESTAMPTZ NOT NULL DEFAULT now(),
    boat_id       TEXT NOT NULL DEFAULT 'sr33',
    window_start  TIMESTAMPTZ,
    window_end    TIMESTAMPTZ,
    summary       TEXT NOT NULL
);
