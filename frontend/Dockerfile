# syntax=docker/dockerfile:1.7
# Build the SPA with node, serve it with unprivileged nginx on :8080 (proxying /api to the api).
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY . .
# Empty = same origin (recommended: nginx proxies /api). Set only to serve the API elsewhere.
ARG VITE_API_BASE_URL=""
# Basemap (empty = OpenStreetMap standard tiles; see src/config.ts).
ARG VITE_TILE_URL=""
ARG VITE_TILE_URL_DARK=""
ARG VITE_TILE_ATTRIBUTION=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_TILE_URL=${VITE_TILE_URL} \
    VITE_TILE_URL_DARK=${VITE_TILE_URL_DARK} \
    VITE_TILE_ATTRIBUTION=${VITE_TILE_ATTRIBUTION}
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
# nginx/default.conf.template is rendered to conf.d/default.conf at start-up by the image's
# entrypoint (envsubst limited to the two variables below; nginx's own $vars are untouched).
# DIVERT_API_UPSTREAM: compose service name by default; k8s sets the Service FQDN.
ENV DIVERT_API_UPSTREAM=api:8000     NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1     NGINX_ENVSUBST_FILTER="^(DIVERT_API_UPSTREAM|NGINX_LOCAL_RESOLVERS)$"
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
USER 101
HEALTHCHECK --interval=15s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/nginx-health || exit 1
