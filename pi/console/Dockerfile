# Built from repo root (context: .). The onboard race console: the same web app (vps/web/public)
# served from the boat, pointed at the onboard engine. Overwrites the stock nginx default.conf
# (which listens on :80) with the onboard config that listens on :8091 and proxies /api → :8200.
FROM nginx:1.27-alpine

COPY vps/web/public/ /usr/share/nginx/html/
# Phase-1 crew dashboard prototype (docs/COPILOT_DASHBOARD.md) — served at /dashboard/.
COPY pi/console/dashboard/ /usr/share/nginx/html/dashboard/
# nginx template — stock entrypoint runs envsubst on ${COPILOT_UPSTREAM} at start.
COPY pi/console/default.conf.template /etc/nginx/templates/default.conf.template
# default if the env var is unset (the Orin's Tailscale IP on the bench; boat overrides it).
ENV COPILOT_UPSTREAM=http://100.70.110.72:8300/

EXPOSE 8091
