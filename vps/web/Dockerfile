# Built from repo root (context: .).
FROM nginx:1.27-alpine

# nginx:alpine runs envsubst over *.template into /etc/nginx/conf.d at startup,
# so AGENT_HOST / AGENT_PORT from the environment are baked into the proxy config.
COPY vps/web/default.conf.template /etc/nginx/templates/default.conf.template
COPY vps/web/public/ /usr/share/nginx/html/

EXPOSE 80
