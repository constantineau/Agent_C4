# Built from repo root (context: .).
FROM nginx:1.27-alpine

# nginx:alpine runs /docker-entrypoint.d/*.sh (in order) then envsubst over the *.template files
# in /etc/nginx/templates → /etc/nginx/conf.d. Both candidate configs are staged in template-src/;
# 10-tls-select.sh picks HTTP-only (dev / pre-cert) vs TLS (prod with a cert) before envsubst runs,
# substituting AGENT_HOST / AGENT_PORT / SERVER_NAME from the environment.
COPY vps/web/default.conf.template      /etc/nginx/template-src/default.conf.template
COPY vps/web/default.ssl.conf.template  /etc/nginx/template-src/default.ssl.conf.template
COPY vps/web/docker-entrypoint.d/10-tls-select.sh /docker-entrypoint.d/10-tls-select.sh
RUN chmod +x /docker-entrypoint.d/10-tls-select.sh
COPY vps/web/public/ /usr/share/nginx/html/

EXPOSE 80 443
