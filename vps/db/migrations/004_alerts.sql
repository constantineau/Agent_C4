-- Phase 6.1 alerting. Conservative, debounced safety/performance alerts raised by the agent's
-- background eval loop. This one table is BOTH the live state (cleared_at IS NULL = active) and
-- the debrief history (cleared rows are retained, never deleted, so a debrief can replay what
-- fired and when). `key` is a stable per-alert identity (e.g. 'ais:366123456', 'wind_shift') so
-- a re-raise after a clear is a new row, preserving the timeline.

CREATE TABLE IF NOT EXISTS alerts (
    id         BIGSERIAL    PRIMARY KEY,
    boat_id    TEXT         NOT NULL DEFAULT 'sr33',
    key        TEXT         NOT NULL,      -- stable identity: 'ais:<mmsi>', 'wind_shift', 'depth_shoaling', …
    kind       TEXT         NOT NULL,      -- rule category (ais, polar_deficit, stale_telemetry, depth_shoaling, wind_shift, fatigue)
    severity   TEXT         NOT NULL,      -- info | warn | danger
    message    TEXT         NOT NULL,
    raised_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    cleared_at TIMESTAMPTZ,                -- NULL while active
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Fast lookup of the currently-active set (the eval loop diffs against this every tick).
CREATE INDEX IF NOT EXISTS alerts_active_idx ON alerts (boat_id, cleared_at) WHERE cleared_at IS NULL;
-- History scan for debriefs.
CREATE INDEX IF NOT EXISTS alerts_history_idx ON alerts (boat_id, raised_at DESC);
