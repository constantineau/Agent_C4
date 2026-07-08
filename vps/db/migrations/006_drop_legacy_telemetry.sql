-- 006: drop the legacy wide `telemetry` table (one column per channel, migration 001).
-- Superseded by the collect-everything `telemetry_raw` (migration 002) — every reader
-- (datasource/tools/summarizer/polar_tool) queries telemetry_raw; the only writer of the
-- wide table was the retired ingestion `POST /ingest`, whose only client was the dev seed
-- fake_telemetry.py (now repointed at /ingest/raw). Nothing ever read it back.
--
-- Migrations auto-run on first DB init only; apply to a running DB by hand:
--   docker compose -f compose.dev.yml exec -T timescaledb psql -U sr33 -d sr33_dev \
--     < vps/db/migrations/006_drop_legacy_telemetry.sql
DROP TABLE IF EXISTS telemetry;
