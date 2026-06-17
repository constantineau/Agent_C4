#!/usr/bin/env bash
# Phase 7 — obtain the initial Let's Encrypt cert for the prod web app, then flip nginx to HTTPS.
# Run ONCE on the VPS after DNS for $SERVER_NAME points at this box and ports 80/443 are free.
# Renewal afterwards is automatic (the `certbot` service renews every 12h).
#
# Prereqs in prod .env: SERVER_NAME (domain), CERTBOT_EMAIL, plus the usual prod secrets.
# Use --staging first to avoid Let's Encrypt rate limits while testing.
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root

[[ -f .env ]] || { echo "ERROR: prod .env missing." >&2; exit 1; }
set -a; . ./.env; set +a
: "${SERVER_NAME:?set SERVER_NAME in .env}"
: "${CERTBOT_EMAIL:?set CERTBOT_EMAIL in .env}"

STAGING=""
[[ "${1:-}" == "--staging" ]] && STAGING="--staging" && echo "(using Let's Encrypt STAGING)"

COMPOSE="docker compose -f compose.prod.yml"

echo "==> 1/3 starting web in HTTP-only mode (serves the ACME challenge; no cert yet)"
# 10-tls-select.sh falls back to HTTP automatically while no cert exists, so a normal up is fine.
$COMPOSE up -d --build web

echo "==> 2/3 requesting a cert for ${SERVER_NAME} via webroot"
$COMPOSE run --rm --entrypoint certbot certbot \
  certonly --webroot -w /var/www/certbot \
  -d "${SERVER_NAME}" --email "${CERTBOT_EMAIL}" \
  --agree-tos --no-eff-email --non-interactive ${STAGING}

echo "==> 3/3 cert issued — recreating web (TLS) + starting the renewal service"
$COMPOSE up -d web certbot
echo "Done. Verify: https://${SERVER_NAME}/api/health  (and that http:// redirects to https)."
