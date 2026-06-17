#!/bin/sh
# Phase 7 — pick the nginx config before the stock envsubst step runs (this file sorts before
# /docker-entrypoint.d/20-envsubst-on-templates.sh). The image holds both candidate templates in
# /etc/nginx/template-src/; we copy the chosen one to /etc/nginx/templates/ for envsubst to render.
#
#   TLS_ENABLED=true AND the cert for $SERVER_NAME exists  -> TLS template (80 redirect + 443)
#   otherwise (dev, or prod before certbot has issued)     -> HTTP-only template
#
# The cert-existence check is the safety net: prod web always starts (HTTP first), serves the ACME
# challenge, and flips to HTTPS on the next restart once certbot has written the cert.
set -e
SRC=/etc/nginx/template-src
DST=/etc/nginx/templates
mkdir -p "$DST"
rm -f "$DST"/default*.template 2>/dev/null || true

CERT="/etc/letsencrypt/live/${SERVER_NAME:-_}/fullchain.pem"
if [ "${TLS_ENABLED:-false}" = "true" ] && [ -f "$CERT" ]; then
    echo "[tls-select] TLS on — serving HTTPS for ${SERVER_NAME}"
    cp "$SRC/default.ssl.conf.template" "$DST/default.conf.template"
else
    if [ "${TLS_ENABLED:-false}" = "true" ]; then
        echo "[tls-select] TLS requested but no cert at $CERT yet — HTTP-only (ACME bootstrap)"
    fi
    cp "$SRC/default.conf.template" "$DST/default.conf.template"
fi
