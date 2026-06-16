#!/usr/bin/env bash
# Deploy the VPS services to the PROD stack. Run on the VPS after merging to main.
# Prod has its own DB (sr33_prod) and env — dev work never touches it.
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root

BRANCH="${BRANCH:-main}"
COMPOSE="docker compose -f compose.prod.yml"

echo "==> on branch ${BRANCH}: pulling latest"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

if [[ ! -f .env ]]; then
  echo "ERROR: prod .env missing. Copy .env.example -> .env and set prod secrets first." >&2
  exit 1
fi

echo "==> building + restarting prod stack (DB left running; migrations are init-only)"
$COMPOSE up -d --build

echo "==> prod status"
$COMPOSE ps
echo "==> tail logs with: $COMPOSE logs -f --tail=80"
