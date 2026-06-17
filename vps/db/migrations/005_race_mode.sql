-- Phase 9.2 — server-side, fail-closed race-mode gate (RRS 41). Until now race/practice mode
-- lived only in the browser (localStorage) and gated the UI; the chat/LLM and every REST route
-- still answered tactical questions. These two tables move the gate server-side:
--   app_state  — a tiny key/value for the authoritative race-mode flag (key 'race_mode').
--   audit_log  — a tamper-evident record of mode changes + every withheld (refused) request,
--                so a protest committee can see the agent withheld outside help while racing.
-- Fail-closed: if no row exists, the agent treats the boat as RACING (restricted) by default
-- (overridable via RACE_MODE_DEFAULT; dev compose sets 'practice').

CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT         PRIMARY KEY,
    value      TEXT         NOT NULL,
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      BIGSERIAL    PRIMARY KEY,
    time    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    boat_id TEXT         NOT NULL DEFAULT 'sr33',
    event   TEXT         NOT NULL,   -- 'mode_change' | 'refusal'
    detail  JSONB                    -- {mode,...} for mode_change; {channel,tool,intent,message} for refusal
);

CREATE INDEX IF NOT EXISTS audit_log_time_idx ON audit_log (time DESC);
