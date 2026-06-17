# Built from repo root (context: .). The onboard race console: the same web app (vps/web/public)
# served from the boat, pointed at the onboard engine. Overwrites the stock nginx default.conf
# (which listens on :80) with the onboard config that listens on :8091 and proxies /api → :8200.
FROM nginx:1.27-alpine

COPY vps/web/public/ /usr/share/nginx/html/
COPY pi/console/default.conf /etc/nginx/conf.d/default.conf

EXPOSE 8091
