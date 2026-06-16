-- Source priority: keep ALL sources (collect-everything), but rank a preferred source per
-- quantity so the agent leads with the best-calibrated sensor and falls back automatically
-- when it goes stale/silent (race redundancy). Non-destructive — every source is still
-- stored and visible; this only chooses a default + failover order.

CREATE TABLE IF NOT EXISTS source_priority (
    id       SERIAL PRIMARY KEY,
    boat_id  TEXT    NOT NULL DEFAULT 'sr33',
    channel  TEXT    NOT NULL,   -- our channel name (heel, tws, sog, depth, …)
    rank     INTEGER NOT NULL,   -- 1 = most preferred
    match    TEXT    NOT NULL,   -- substring matched against the Signal K $source / device
    note     TEXT,
    UNIQUE (boat_id, channel, rank)
);
