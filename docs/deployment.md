# Deployment

One backend image (`backend/Dockerfile`: multi-stage uv build, non-root uid 10001,
healthcheck) runs four roles; the frontend image is nginx serving the SPA on :8080 and
proxying `/api` (including the WebSocket) to the API.

| Role | Command | Probes | Replicas |
|---|---|---|---|
| migrate | `divert migrate` (alembic upgrade head + idempotent seed) | exit code | one-off, before every rollout |
| api | `uvicorn divert.main:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers` | liveness `/healthz`, readiness `/readyz` | >= 2 (HPA) |
| worker | `python -m divert.worker` | `:9100/metrics` | exactly 2 (leader + standby) |
| persister | `python -m divert.persister` | `:9100/metrics` | >= 1 (2 for HA) |

## Single host: docker compose

```bash
cp .env.example .env            # change POSTGRES_PASSWORD, GRAFANA_ADMIN_PASSWORD, DIVERT_API_KEY
docker compose up -d --build    # postgres, redis, migrate, api, worker x2, persister, frontend
open http://localhost:8080      # dashboard; API on :8000, docs on :8000/docs
docker compose --profile observability up -d   # prometheus :9090, grafana :3000 (dashboard "DIVERT overview")
docker compose --profile osrm up -d osrm       # self-hosted OSRM after infra/osrm/prepare.sh
```

Ordering is enforced with `depends_on`: postgres/redis `service_healthy` ->
migrate `service_completed_successfully` -> api/worker/persister; frontend waits for a
healthy api. Every service has a restart policy, resource limits and log rotation.
Redis runs with `appendonly yes`, `appendfsync everysec`, `maxmemory-policy noeviction`
(live state and streams must never be evicted; caches carry TTLs).

## Kubernetes: `infra/k8s/base` (kustomize)

`kubectl apply -k infra/k8s/base` after creating an overlay that sets images, the
ingress host, replica counts and the managed endpoints. Contents: Namespace, ConfigMap
(`divert-config`), migrate Job (Argo CD PreSync hook), api Deployment + Service + HPA
(2..10 on CPU) + PDB, worker Deployment (2 replicas, anti-affinity, PDB), persister
Deployment + PDB, frontend Deployment + Service, Ingress (TLS, 3600 s proxy timeouts for
WebSockets). Pods run non-root, read-only root filesystem, all capabilities dropped,
seccomp RuntimeDefault. `secret.template.yaml` documents `divert-secrets`
(`DIVERT_DATABASE_URL`, `DIVERT_REDIS_URL`, `DIVERT_API_KEY`) - create it with External
Secrets / Sealed Secrets; it is not part of the base. The rendered base validates with
`kubectl kustomize` + `kubeconform -strict` (14 resources, Kubernetes 1.31).

Frontend: the browser talks only to the frontend origin (nginx proxies `/api`, including
the WebSocket upgrade, to the API), so no API host is baked into the bundle and no CORS
is needed. The nginx config is a template rendered at container start:
`DIVERT_API_UPSTREAM` names the API (`api:8000` by default for compose; the k8s base sets
`divert-api.$(POD_NAMESPACE).svc.cluster.local:80` - adjust `cluster.local` if your cluster
domain differs) and the container's DNS servers (`/etc/resolv.conf`: Docker's 127.0.0.11
or kube-dns) re-resolve it every 10 s. nginx therefore starts even when the API is not
up yet, follows a recreated API to its new address and spreads requests over every
address the name returns. The k8s liveness probe uses `/nginx-health` (nginx only); the
compose healthcheck probes `/healthz` through the proxy. nginx sends a CSP (`script-src 'self'`, `connect-src 'self' ws: wss:`, images
from `https:` for map tiles), `X-Frame-Options DENY`, `nosniff`, immutable caching for
hashed `/assets/*` and `no-cache` for `index.html`. The basemap is a build argument
(`DIVERT_WEB_TILE_URL`, `DIVERT_WEB_TILE_URL_DARK`, `DIVERT_WEB_TILE_ATTRIBUTION` in
`.env`): the default OpenStreetMap standard tiles are for development and demos only
(OSMF tile usage policy). CARTO basemaps now return an "API key required" placeholder
without a key. Production should use a keyed provider or self-hosted tiles.

Rollout: (1) migrate Job (migrations are backward compatible for one release), (2) api,
persister, worker (rolling; `maxSurge 0` for the worker so the standby takes over during
the restart; SIGTERM makes the leader flush and release the lock for an instant hand-over).

## Managed services

**PostgreSQL 16 with PostGIS 3.4** (e.g. Cloud SQL, RDS, Azure Flexible Server):
`CREATE EXTENSION postgis` is run by the first migration (the role needs the right to
create it, or pre-create it). Enable automated backups with **point-in-time recovery**
(WAL archiving, >= 7 days), a standby replica in another zone, `max_connections` sized for
`(api replicas + persister replicas + 2 workers + migrate) x (DIVERT_DB_POOL_SIZE + DIVERT_DB_MAX_OVERFLOW)`
(or put PgBouncer in transaction mode in front). `statement_timeout` is set per
connection (`DIVERT_DB_STATEMENT_TIMEOUT_MS`). `vehicle_positions` is the only large
table: BRIN-indexed on `ts`, pruned after `DIVERT_POSITION_RETENTION_DAYS` by the
persister; partition it by month if retention grows beyond a few months.

**Redis 7** (e.g. Memorystore, ElastiCache, Azure Cache): single primary with a replica
and automatic fail-over, **AOF persistence enabled**, `maxmemory-policy noeviction`, TLS
(`rediss://`) and AUTH in `DIVERT_REDIS_URL`. Losing Redis data is survivable: the next
worker leader rebuilds every active dispatch from PostgreSQL. Redis Cluster is not used
(the tick transaction spans the per-city keys and the global streams; see
architecture.md section 9).

**OSRM**: the public demo (`https://router.project-osrm.org`) is for development only
(fair use, no SLA). Production self-hosts it: `infra/osrm/prepare.sh` builds an MLD
graph from the Geofabrik India extract clipped to the three city bboxes; run it in a
Deployment with the data on a read-only volume (~4 GB RAM) and set `DIVERT_OSRM_URL`.

## Configuration

Environment only (`DIVERT_` prefix, `backend/src/divert/config.py`, `.env.example`).
Secrets: `DIVERT_DATABASE_URL`, `DIVERT_REDIS_URL`, `DIVERT_API_KEY`. Set
`DIVERT_TRUST_PROXY_HEADERS=true` only behind a proxy you control (the rate limiter then
keys on `X-Forwarded-For`). `DIVERT_CORS_ORIGINS` must list explicit origins.
`DIVERT_INSTANCE_ID` (k8s: pod name) names the Redis stream consumer.

## Scaling and capacity

| Component | Scale by | Watch |
|---|---|---|
| api | replicas (HPA) | `divert_http_request_duration_seconds`, CPU |
| worker | one leader; shard by city if `divert_tick_duration_seconds` p95 approaches `DIVERT_SIM_TICK_S` | `divert_tick_overruns_total` |
| persister | replicas | `divert_persister_lag_seconds`, `divert_history_stream_lag` |
| PostgreSQL | vertical + read replica for analytics | slow-query log (500 ms) |
| Redis | vertical | memory (`noeviction`), stream lengths (MAXLEN ~ trims) |

## Failure modes

See architecture.md section 8 for the full table. Summary: OSRM down -> 503, nothing
written; Redis blip -> outbox relay and merged tick writes; Redis down -> the API stays
ready and keeps accepting dispatches (short request-path deadlines + circuit breaker,
`/readyz` depends on PostgreSQL only and reports `degraded`), live views return 503 until
Redis is back; leader loss -> standby within the lease TTL (5 s); Redis data loss ->
rebuild from PostgreSQL; PostgreSQL down -> live simulation continues, history buffers in
the stream, API readiness fails.

## Observability

Prometheus scrapes the api (`/metrics`), the worker and the persister (`:9100/metrics`);
Grafana is provisioned with the Prometheus datasource and the "DIVERT overview" dashboard
(`infra/observability`). Logs are JSON lines on stdout with `service`, `request_id`
(api) and structured fields. Suggested alerts: no leader (`sum(divert_worker_is_leader) == 0`
for 30 s), heartbeat age > 10 s, `divert_persister_lag_seconds > 60`,
`divert_outbox_backlog > 0` for 2 min, `increase(divert_dlq_messages_total[10m]) > 0`,
OSRM error rate > 5 %, tick p95 > 0.25 s.
