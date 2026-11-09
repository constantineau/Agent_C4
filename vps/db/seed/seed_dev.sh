#!/usr/bin/env bash
# Seed the DEV stack: placeholder metadata (polars/waypoints/AIS) + fake telemetry.
# Run after `docker compose -f compose.dev.yml up -d`.
set -euo pipefail
cd "$(dirname "$0")/../../.."   # repo root

COMPOSE="docker compose -f compose.dev.yml"
PGUSER="${POSTGRES_USER:-sr33}"
PGDB="${POSTGRES_DB:-sr33_dev}"

echo "==> loading real SR33 ORC polars into ${PGDB}"
$COMPOSE exec -T timescaledb psql -U "$PGUSER" -d "$PGDB" < vps/db/seed/polars_sr33.sql

echo "==> loading source reliability notes + priority into ${PGDB}"
$COMPOSE exec -T timescaledb psql -U "$PGUSER" -d "$PGDB" < vps/db/seed/source_notes.sql
$COMPOSE exec -T timescaledb psql -U "$PGUSER" -d "$PGDB" < vps/db/seed/source_priority.sql

echo "==> loading placeholder metadata (waypoints, AIS) into ${PGDB}"
$COMPOSE exec -T timescaledb psql -U "$PGUSER" -d "$PGDB" < vps/db/seed/dev_seed.sql

# Fake telemetry and AIS go under boat id demo-boat, never a real boat (#39): on the VPS this
# compose stack is the one the boat posts to. fake_telemetry.py refuses a real id on its own.
echo "==> posting fake telemetry through the ingestion API (boat id demo-boat)"
python3 vps/db/seed/fake_telemetry.py --minutes "${MINUTES:-120}" \
  --token "${INGEST_TOKEN:-dev-ingest-token}" --boat-id demo-boat

echo "==> done. The fake data is under boat id demo-boat: start the agent with BOAT_ID=demo-boat"
echo "    to see it, e.g.  curl -s localhost:8102/conditions | python3 -m json.tool"
