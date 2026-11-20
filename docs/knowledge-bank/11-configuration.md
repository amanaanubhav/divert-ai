# 11 · Configuration

**What this covers:** every knob that changes how DIVERT runs, and where each is set:
- the 51 backend settings in `backend/src/divert/config.py`
- the frontend build-time variables
- the nginx template variables
- docker compose
- the Kubernetes ConfigMap and Secret
- `.env.example`
- the lint, type and test tool configuration

It ends with which settings to change for a test environment and for production. For what the thresholds mean in the domain, see [Domain rules](02-domain-rules.md). For how to run things, see [Engineering workflows](08-engineering-workflows.md).

---

## 1. How backend settings are loaded

`Settings` (`backend/src/divert/config.py`) is a pydantic-settings class:

| Rule | Code | Consequence |
|---|---|---|
| Environment variables only, prefix `DIVERT_` | `SettingsConfigDict(env_prefix="DIVERT_", ...)` | `corridor_m` is read from `DIVERT_CORRIDOR_M`. Names are case-insensitive. |
| **No `.env` file is read** | `env_file=None` | The backend never loads `.env` itself. docker compose injects it into the containers. Outside compose, export the variables yourself. The defaults already match the compose ports on localhost. |
| Unknown variables are ignored | `extra="ignore"` | Compose-only variables such as `DIVERT_PG_PORT`, `DIVERT_WEB_TILE_URL` and `DIVERT_TEST_DATABASE_URL` are harmless. **A misspelled setting is silently ignored too.** |
| Immutable, read once per process | `frozen=True`, `get_settings()` is `@lru_cache(maxsize=1)` | Changing a setting needs a process restart. |
| Validated at start-up | `Field(ge=..., gt=..., le=...)`, validators | An out-of-range value stops the process with a `ValidationError`. |
| Lists are comma-separated | `_split_csv` on `cities` and `cors_origins` (`NoDecode`) | `DIVERT_CITIES=delhi, kolkata` works. JSON is not needed. |
| City ids are lower-cased | `_lower_cities` | `Delhi` becomes `delhi`. |
| Wildcard CORS is rejected | `_no_wildcard_cors` (model validator) | `DIVERT_CORS_ORIGINS=*` fails with "DIVERT_CORS_ORIGINS must list explicit origins (credentials on)". |
| Secrets are `SecretStr` | `database_url`, `redis_url`, `api_key` | They are hidden in `repr()` (tested in `tests/unit/test_config_and_enums.py` `test_settings_from_env`). |

Tests strip every `DIVERT_*` variable except `DIVERT_TEST_*` (`backend/tests/conftest.py` `_isolated_settings`), so unit tests always see the defaults.

---

## 2. Backend settings reference

"Used by" comes from grepping `settings.<name>` / `cfg.<name>` in `backend/src/divert`. Services: **api**, **worker**, **persister**, **cli** (`divert` command, including the `migrate` job).

### 2.1 General and PostgreSQL

| Env var | Type | Default | Constraint | Used by | Meaning / notes |
|---|---|---|---|---|---|
| `DIVERT_ENV` | `dev` \| `test` \| `staging` \| `prod` | `dev` | literal | **none** | Declared and validated, but no code reads it. It changes no behaviour today. |
| `DIVERT_LOG_LEVEL` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` | `INFO` | literal | all | Root log level (`logging.configure_logging`). |
| `DIVERT_LOG_JSON` | bool | `true` | | all | JSON lines (`JsonFormatter`), or plain text when false. The backend image also sets `DIVERT_LOG_JSON=true`. |
| `DIVERT_INSTANCE_ID` | str | `""` = `hostname-pid` | | worker, persister | Worker: leader-lock owner and heartbeat `instance`. Persister: Redis consumer name. k8s sets it to the pod name. |
| `DIVERT_DATABASE_URL` | secret str | `postgresql+asyncpg://divert:divert@localhost:5432/divert` | | all (+ `migrations/env.py`) | SQLAlchemy URL. Must use the `asyncpg` driver. |
| `DIVERT_DB_POOL_SIZE` | int | `10` | >= 1 | all | Per process. Size PostgreSQL `max_connections` for replicas x (pool + overflow). |
| `DIVERT_DB_MAX_OVERFLOW` | int | `10` | >= 0 | all | Extra connections above the pool. |
| `DIVERT_DB_POOL_TIMEOUT_S` | float | `10.0` | > 0 | all | Wait for a pooled connection. |
| `DIVERT_DB_STATEMENT_TIMEOUT_MS` | int | `15000` | >= 0 | all | Sent as the PostgreSQL `statement_timeout` per connection. `0` sends none. |
| `DIVERT_DB_ECHO` | bool | `false` | | all | SQLAlchemy SQL echo (debug only). |

### 2.2 Redis and OSRM

| Env var | Type | Default | Constraint | Used by | Meaning / notes |
|---|---|---|---|---|---|
| `DIVERT_REDIS_URL` | secret str | `redis://localhost:6379/0` | | api, worker, persister, cli (`dlq`) | Use `rediss://` for TLS. A single primary is required. **Redis Cluster is not supported** (see [Data stores](05-data-stores.md)). |
| `DIVERT_REDIS_SOCKET_TIMEOUT_S` | float | `5.0` | > 0 | worker, persister, cli, api pub/sub client | Socket and connect timeout of the "patient" client (`create_redis`, retries on timeout). |
| `DIVERT_REDIS_REQUEST_TIMEOUT_S` | float | `0.5` | > 0, <= 10 | api | Socket timeouts of the request-path client (no retries) and the `RedisGuard` per-call deadline. |
| `DIVERT_REDIS_CIRCUIT_COOLDOWN_S` | float | `5.0` | > 0 | api | How long the `RedisGuard` skips Redis after a failure. |
| `DIVERT_OSRM_URL` | str | `https://router.project-osrm.org` | | api | OSRM base URL. The default is the **public demo: fair use only, no SLA**. |
| `DIVERT_OSRM_TIMEOUT_S` | float | `8.0` | > 0 | api | httpx timeout. The connect timeout is `min(timeout, 3.0)` (`services/routing.OsrmRouter`). |
| `DIVERT_OSRM_RETRIES` | int | `2` | >= 0 | api | Retries on `RETRYABLE_STATUS` (429, 500, 502, 503, 504) and transport errors. |
| `DIVERT_OSRM_CACHE_TTL_S` | int | `3600` | >= 0 | api | TTL of the cached routes `divert:osrm:driving:{digest}`. `0` disables the cache. |

### 2.3 Reference data, dispatch and pre-emption

| Env var | Type | Default | Constraint | Used by | Meaning / notes |
|---|---|---|---|---|---|
| `DIVERT_CITIES` | CSV list | `delhi,kolkata,bengaluru` | lower-cased | api, worker, cli | Cities served, simulated and seeded. Keep it identical across api, worker and migrate. |
| `DIVERT_UNITS_PER_STATION` | int | `2` | 1..20 | cli (seed) | Units created per active station. Re-seeding only adds missing units. |
| `DIVERT_CANDIDATE_STATIONS` | int | `3` | 1..10 | api | Nearest stations with a free unit (PostGIS KNN). Each one is routed with OSRM and ranked by ETA. |
| `DIVERT_CORRIDOR_M` | float | `30.0` | > 0, <= 200 | api | Signals within this distance of the route form the corridor (`ST_DWithin`). Fixed per dispatch. |
| `DIVERT_PREEMPT_LEAD_S` | float | `45.0` | > 0 | worker | Claim a signal when the ETA to it is at most this... |
| `DIVERT_PREEMPT_DISTANCE_M` | float | `600.0` | > 0 | worker | ...or when it is within this distance ahead along the route. |
| `DIVERT_PREEMPT_RELEASE_PAST_M` | float | `20.0` | >= 0 | worker | Release a claim once the vehicle is this far past the signal. |
| `DIVERT_CONFLICT_TIE_S` | float | `5.0` | >= 0 | worker | ETAs within this window of the best are a tie, broken by service priority, then by vehicle id. |

The worker reads the four pre-emption settings once, when it becomes leader (`worker.Worker.take_over` -> `PreemptionRules`).

### 2.4 Simulation

| Env var | Type | Default | Constraint | Used by | Meaning / notes |
|---|---|---|---|---|---|
| `DIVERT_SIM_TICK_S` | float | `0.5` | > 0, <= 5 | worker | Wall-clock tick period. |
| `DIVERT_SIM_TIME_SCALE` | float | `1.0` | > 0, <= 100 | worker, api | Simulated seconds per wall second. When it is not 1, the worker writes `divert:sim:clock` and the API reads it for the dispatch ETA's congestion hour (`DispatchService.sim_clock`). **Set the same value on api and worker.** |
| `DIVERT_SCENE_TIME_S` | float | `120.0` | >= 0 | worker | Simulated seconds on scene before the incident is cleared (RESOLVED). |
| `DIVERT_POSITION_SAMPLE_S` | float | `5.0` | > 0 | worker | Simulated seconds between `vehicle_position` history samples. |
| `DIVERT_POSITION_RETENTION_DAYS` | int | `30` | >= 1 | persister | `vehicle_positions` older than this are pruned. |

### 2.5 Worker, persister and outbox

| Env var | Type | Default | Constraint | Used by | Meaning / notes |
|---|---|---|---|---|---|
| `DIVERT_LEADER_LOCK_TTL_MS` | int | `5000` | >= 500 | worker | Leader lease TTL. It is renewed every TTL/3, the leader stops TTL/5 before expiry (`LeaderLock.margin_s`), and commands idle this long are reclaimed. Keep it identical on all workers. |
| `DIVERT_HEARTBEAT_MAX_AGE_S` | float | `10.0` | > 0 | api | `/readyz` reports the worker `stale` (and `degraded: true`) beyond this age. |
| `DIVERT_COMMAND_BLOCK_MS` | int | `200` | >= 10 | worker | XREADGROUP block time on `divert:commands`. |
| `DIVERT_FINISHED_DISPATCH_TTL_S` | int | `86400` | >= 60 | worker | How long finished dispatch ids stay in `divert:{city}:finished` (idempotent `start_dispatch`). |
| `DIVERT_PERSISTER_BATCH` | int | `200` | >= 1 | persister | Messages per read and per transaction. |
| `DIVERT_PERSISTER_MAX_RETRIES` | int | `5` | >= 1 | persister | Deliveries before a failing message goes to `divert:history:dlq`. |
| `DIVERT_PERSISTER_RETRY_IDLE_MS` | int | `5000` | >= 100 | persister | Idle time before XAUTOCLAIM takes over a pending message. |
| `DIVERT_PERSISTER_BLOCK_MS` | int | `1000` | >= 10 | persister | XREADGROUP block time on `divert:history`. |
| `DIVERT_RETENTION_INTERVAL_S` | float | `3600.0` | > 0 | persister | How often retention runs (one persister at a time, through an advisory lock). |
| `DIVERT_OUTBOX_RETENTION_DAYS` | int | `7` | >= 1 | persister | Relayed outbox rows older than this are deleted. |
| `DIVERT_OUTBOX_RELAY_INTERVAL_S` | float | `2.0` | > 0 | api | Period of the background outbox relay. |
| `DIVERT_OUTBOX_RELAY_MIN_AGE_S` | float | `2.0` | >= 0 | api | Rows unsent for at least this long are re-sent by the relay. |
| `DIVERT_METRICS_PORT` | int | `9100` | 0..65535 | worker, persister | Prometheus HTTP port. `0` turns it off. A worker and a persister on one host clash; the second one logs a warning and runs without metrics. The API always serves `/metrics` on its own port. |

### 2.6 API

| Env var | Type | Default | Constraint | Used by | Meaning / notes |
|---|---|---|---|---|---|
| `DIVERT_API_KEY` | secret str or unset | unset | | api | When non-empty, POST/DELETE require `X-API-Key`. Empty (compose passes `""`) means open. Reads are always open. |
| `DIVERT_CORS_ORIGINS` | CSV list | `http://localhost:5173,http://localhost:8080` | no `*` | api | Explicit origins, credentials allowed. Not needed when the UI is served through the frontend nginx (same origin). |
| `DIVERT_RATE_LIMIT_PER_MIN` | int | `60` | >= 1 | api | Fixed one-minute window for POST/DELETE, per API key or client address. Fails open if Redis is down. |
| `DIVERT_TRUST_PROXY_HEADERS` | bool | `false` | | api | Rate-limit on the first `X-Forwarded-For` address. Enable only behind a proxy you control. |
| `DIVERT_WS_MAX_CLIENTS_PER_CITY` | int | `500` | >= 1 | api | Per API process. Beyond it, new sockets are closed with 1013. |
| `DIVERT_WS_QUEUE_SIZE` | int | `256` | >= 8 | api | Per-client message queue. On overflow the client gets close 1013 and must resync. |
| `DIVERT_REFERENCE_CACHE_TTL_S` | float | `300.0` | >= 0 | api | Per-process cache of cities, stations and signals. Unit availability is never cached. |

Constants that are **not** settings, to change in code: `api/ratelimit.WINDOW_S = 60`, the readiness and metrics timeouts (2.0 s in `api/v1/health.py`), `OutboxRelay(batch=100)`, `OsrmRouter(backoff_s=0.25)`, the stream caps (`STREAM_COMMANDS_MAXLEN = 100_000`, `STREAM_HISTORY_MAXLEN = 1_000_000`), the heartbeat key expiry (3600 s in `Worker._heartbeat`), and everything in `services/speed_model.py`. See [Backend codebase](09-codebase-backend.md) section 7.3.

---

## 3. Frontend build-time variables

Vite inlines `import.meta.env.VITE_*` into the bundle at **build** time (`frontend/src/config.ts`, typed in `src/vite-env.d.ts`). Changing them needs a rebuild of the image. A blank value means "use the default" (`envValue`).

| Variable | Default | Effect | How to set |
|---|---|---|---|
| `VITE_API_BASE_URL` | `""` (same origin) | Absolute API origin for REST and WebSocket (`apiUrl`, `wsUrl`). | Dockerfile `ARG`. The recommended value is empty: nginx proxies `/api`. |
| `VITE_API_KEY` | none | Default `X-API-Key` for POST/DELETE. A key the operator saves in the UI (localStorage `divert.apiKey`) takes precedence. **It is visible to anyone who loads the bundle.** | Local builds only (`npm run build` / `npm run dev` env). The Dockerfile has **no** `ARG VITE_API_KEY`, and `.dockerignore` excludes `.env*`. |
| `VITE_TILE_URL` | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` | Light raster basemap. **OSM standard tiles are for development and demos only** (OSMF tile usage policy). | Dockerfile `ARG`. Compose passes `DIVERT_WEB_TILE_URL`. |
| `VITE_TILE_URL_DARK` | none | Dark basemap. Without it, dark mode shows the light tiles through a CSS filter (`.tiles-dark` in `src/index.css`). | Dockerfile `ARG`. Compose passes `DIVERT_WEB_TILE_URL_DARK`. |
| `VITE_TILE_ATTRIBUTION` | OpenStreetMap contributors link | Attribution HTML. | Dockerfile `ARG`. Compose passes `DIVERT_WEB_TILE_ATTRIBUTION`. |
| `DIVERT_DEV_API` | `http://localhost:8000` | Target of the Vite dev-server proxy for `/api` (with WebSocket), `/readyz` and `/healthz` (`frontend/vite.config.ts`). Not bundled. | `DIVERT_DEV_API=http://host:8000 npm run dev` |

Hard-coded in `src/config.ts`: `defaultCity: 'delhi'`; WebSocket backoff `initialDelayMs: 500`, `maxDelayMs: 15_000`; `pingIntervalMs: 20_000`; tile `maxZoom: 19`.

If you set `VITE_API_BASE_URL` to another origin, two more changes are needed. Add the UI origin to `DIVERT_CORS_ORIGINS`. Widen the nginx CSP, whose `connect-src` allows only `'self' ws: wss:` (section 4).

---

## 4. nginx template variables (frontend image)

`frontend/nginx/default.conf.template` is rendered to `/etc/nginx/conf.d/default.conf` when the container starts, by the `nginxinc/nginx-unprivileged:1.27-alpine` entrypoint (envsubst).

| Variable | Set by | Default | Meaning |
|---|---|---|---|
| `DIVERT_API_UPSTREAM` | Dockerfile `ENV`. Compose `environment`. k8s `env` | `api:8000` (compose). k8s: `divert-api.$(POD_NAMESPACE).svc.cluster.local:80` | `host:port` of the API. Resolved per request (variable `proxy_pass`). The resolver applies no search domains, so in k8s it must be a full name. |
| `NGINX_LOCAL_RESOLVERS` | the image entrypoint, because the Dockerfile sets `NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1` | from `/etc/resolv.conf` | DNS servers for `resolver ... valid=10s ipv6=off`. That is Docker's 127.0.0.11 or kube-dns. |
| `NGINX_ENVSUBST_FILTER` | Dockerfile `ENV` | `^(DIVERT_API_UPSTREAM\|NGINX_LOCAL_RESOLVERS)$` | Limits envsubst to those two names, so nginx's own `$variables` survive. |

Fixed in the template:
- **Listener:** `listen 8080`; `resolver_timeout 3s`; gzip level 5 for bodies of at least 1024 bytes.
- **`/api/`:** HTTP/1.1, WebSocket upgrade, `proxy_read_timeout` / `proxy_send_timeout` 3600 s, `proxy_buffering off`. It sets `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Request-ID $request_id`.
- **`/healthz` and `/readyz`:** proxied to the API.
- **`/nginx-health`:** answered by nginx itself (k8s liveness).
- **`/assets/`:** `Cache-Control: public, max-age=31536000, immutable`.
- **`/index.html`:** `no-cache`. Unknown paths fall back to `index.html` (SPA).
- **Security headers:** `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' ws: wss:; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'`, plus `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy: geolocation=(), camera=(), microphone=()`. `img-src https:` is what lets any HTTPS tile server work.

---

## 5. docker compose (`docker-compose.yml`)

Project name `divert`. Compose reads `.env` from the repo root for `${...}` interpolation. The backend services also get it through `env_file: .env` (`required: false`).

### 5.1 Shared backend definition

- `x-backend` sets:
  - image `${DIVERT_BACKEND_IMAGE:-divert-backend}:${DIVERT_TAG:-dev}`, built from `./backend`
  - `restart: unless-stopped`
  - json-file logs, max 10 MB x 3 files
- `x-backend-env` sets:

| Variable | Value |
|---|---|
| `DIVERT_ENV` | `${DIVERT_ENV:-dev}` |
| `DIVERT_LOG_LEVEL` | `${DIVERT_LOG_LEVEL:-INFO}` |
| `DIVERT_DATABASE_URL` | `postgresql+asyncpg://${POSTGRES_USER:-divert}:${POSTGRES_PASSWORD:-divert}@postgres:5432/${POSTGRES_DB:-divert}` |
| `DIVERT_REDIS_URL` | `redis://redis:6379/0` |
| `DIVERT_OSRM_URL` | `${DIVERT_OSRM_URL:-https://router.project-osrm.org}` |
| `DIVERT_CORS_ORIGINS` | `${DIVERT_CORS_ORIGINS:-http://localhost:8080,http://localhost:5173}` |
| `DIVERT_API_KEY` | `${DIVERT_API_KEY:-}` |
| `DIVERT_SIM_TIME_SCALE` | `${DIVERT_SIM_TIME_SCALE:-1}` |
| `DIVERT_RATE_LIMIT_PER_MIN` | `${DIVERT_RATE_LIMIT_PER_MIN:-60}` |
| `DIVERT_METRICS_PORT` | `"9100"` |

- **Precedence.** `environment:` wins over `env_file:`. So the localhost `DIVERT_DATABASE_URL` and `DIVERT_REDIS_URL` from `.env` never reach the containers, and `DIVERT_METRICS_PORT` is pinned to 9100. Every other `DIVERT_*` line in `.env` (for example `DIVERT_CORRIDOR_M`) does reach the backend containers.

### 5.2 Services

| Service | Image / command | Host port | Profile | Health check | Depends on | Limits |
|---|---|---|---|---|---|---|
| `postgres` | `postgis/postgis:16-3.4`, flags `shared_buffers=256MB`, `max_connections=200`, `log_min_duration_statement=500`. `shm_size: 256mb` | `${DIVERT_PG_PORT:-5432}` | default | `pg_isready` every 5 s, timeout 3 s, 20 retries, start 10 s | | 2 CPU, 1 GB |
| `redis` | `redis:7-alpine`, `--appendonly yes --appendfsync everysec --maxmemory 512mb --maxmemory-policy noeviction` | `${DIVERT_REDIS_PORT:-6379}` | default | `redis-cli ping` every 5 s, timeout 3 s, 20 retries | | 1 CPU, 640 MB |
| `migrate` | backend, `divert migrate`, `restart: "no"` | | default | disabled | postgres healthy | 1 CPU, 512 MB |
| `api` | backend, `uvicorn divert.main:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips * --no-access-log` | `${DIVERT_API_PORT:-8000}` | default | GET `/readyz` every 10 s, timeout 3 s, 6 retries, start 15 s | postgres + redis healthy, migrate completed | 1 CPU, 512 MB |
| `worker` | backend, `python -m divert.worker`, **2 replicas**, `stop_grace_period: 15s` | | default | GET `:9100/metrics` every 15 s, timeout 3 s, 3 retries, start 15 s | same as api | 1 CPU, 512 MB each |
| `persister` | backend, `python -m divert.persister`, 1 replica, `stop_grace_period: 20s` | | default | same as worker | same as api | 1 CPU, 384 MB |
| `frontend` | `${DIVERT_FRONTEND_IMAGE:-divert-frontend}:${DIVERT_TAG:-dev}`. Build args `VITE_TILE_URL`, `VITE_TILE_URL_DARK`, `VITE_TILE_ATTRIBUTION` from `DIVERT_WEB_TILE_*`. Env `DIVERT_API_UPSTREAM: api:8000` | `${DIVERT_WEB_PORT:-8080}` | default | `wget /healthz` through the proxy every 15 s, timeout 3 s, 3 retries, start 10 s | api healthy (`restart: true`) | 0.5 CPU, 128 MB |
| `osrm` | `ghcr.io/project-osrm/osrm-backend:v5.27.1`, `osrm-routed --algorithm mld --max-table-size 1000 /data/divert-cities.osrm`, volume `./infra/osrm/data:/data:ro` | `${DIVERT_OSRM_PORT:-5000}` | `osrm` | `wget .../nearest/v1/driving/77.209,28.6139` every 15 s, timeout 5 s, 10 retries | | 2 CPU, 4 GB |
| `prometheus` | `prom/prometheus:v2.55.1`, retention 15 d, config `infra/observability/prometheus.yml` | `${DIVERT_PROMETHEUS_PORT:-9090}` | `observability` | | | |
| `grafana` | `grafana/grafana:11.3.1`, `GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD:-change-me}`, sign-up off, provisioning from `infra/observability/grafana` | `${DIVERT_GRAFANA_PORT:-3000}` | `observability` | | prometheus | |

Named volumes: `pgdata`, `redisdata`, `prometheus`, `grafana`.

### 5.3 Notes

- **Self-hosted OSRM has not been run as part of CI.** The `osrm` service needs data built by `infra/osrm/prepare.sh`, whose header says "Documented, not run in CI". To use it, set `DIVERT_OSRM_URL=http://osrm:5000` in `.env`.
- Prometheus (`infra/observability/prometheus.yml`) scrapes every 15 s through DNS service discovery: `api:8000`, `worker:9100`, `persister:9100`, which covers every replica. Grafana gets the Prometheus datasource (`uid: divert-prometheus`) and the dashboards in `infra/observability/grafana/dashboards/`.
- `DIVERT_SIM_TIME_SCALE=10 docker compose up -d worker` (in the README and runbook) recreates only the workers. The API keeps scale 1, so dispatch ETAs use the wall-clock congestion hour (`DispatchService.sim_clock`). Recreate `api` too if predicted ETAs should follow the simulation clock.

---

## 6. Kubernetes (`infra/k8s/base`)

A kustomize base (namespace `divert`) with 14 resources: Namespace, ConfigMap, migrate Job, api (Deployment, Service, HPA, PDB), worker (Deployment, PDB), persister (Deployment, PDB), frontend (Deployment, Service) and Ingress. The images are placeholders (`ghcr.io/example/divert-backend:2.0.0`, `ghcr.io/example/divert-frontend:2.0.0`), and so is the host (`divert.example.org`). Overlays are expected to set real values.

**Status:** the base has been validated offline only (`kubectl kustomize` + `kubeconform`, per [deployment.md](../deployment.md)). CI does not render or apply it, and the repo has no overlay and no record of an apply to a real cluster.

### 6.1 ConfigMap `divert-config` (`configmap.yaml`)

Every backend pod and the migrate Job load it with `envFrom`.

| Key | Value | Differs from code default? |
|---|---|---|
| `DIVERT_ENV` | `prod` | yes (no behavioural effect) |
| `DIVERT_LOG_LEVEL` / `DIVERT_LOG_JSON` | `INFO` / `"true"` | no |
| `DIVERT_CITIES` | `delhi,kolkata,bengaluru` | no |
| `DIVERT_OSRM_URL` | `http://osrm.divert.svc.cluster.local:5000` | yes. **The base contains no OSRM Deployment or Service.** Deploy it separately. |
| `DIVERT_OSRM_TIMEOUT_S` / `DIVERT_OSRM_CACHE_TTL_S` | `"5"` / `"3600"` | timeout yes (default 8) |
| `DIVERT_CORS_ORIGINS` | `https://divert.example.org` | yes (placeholder) |
| `DIVERT_CORRIDOR_M`, `DIVERT_PREEMPT_LEAD_S`, `DIVERT_PREEMPT_DISTANCE_M` | `"30"`, `"45"`, `"600"` | no |
| `DIVERT_SIM_TICK_S`, `DIVERT_SIM_TIME_SCALE`, `DIVERT_SCENE_TIME_S` | `"0.5"`, `"1"`, `"120"` | no |
| `DIVERT_POSITION_RETENTION_DAYS`, `DIVERT_DB_POOL_SIZE` | `"30"`, `"10"` | no |
| `DIVERT_LEADER_LOCK_TTL_MS`, `DIVERT_HEARTBEAT_MAX_AGE_S` | `"5000"`, `"10"` | no |
| `DIVERT_PERSISTER_BATCH`, `DIVERT_PERSISTER_MAX_RETRIES`, `DIVERT_OUTBOX_RELAY_INTERVAL_S` | `"200"`, `"5"`, `"2"` | no |
| `DIVERT_RATE_LIMIT_PER_MIN` | `"120"` | yes (default 60) |
| `DIVERT_TRUST_PROXY_HEADERS` | `"true"` | yes (behind ingress-nginx) |
| `DIVERT_REDIS_REQUEST_TIMEOUT_S`, `DIVERT_REDIS_CIRCUIT_COOLDOWN_S` | `"0.5"`, `"5"` | no |

### 6.2 Secret `divert-secrets` (`secret.template.yaml`)

This is a **template only**. It is not listed in `kustomization.yaml`. Create the Secret out of band, preferably with External Secrets or Sealed Secrets.

| Key | Template value | Notes |
|---|---|---|
| `DIVERT_DATABASE_URL` | `postgresql+asyncpg://divert:CHANGE_ME@pg.internal:5432/divert?ssl=require` | Managed PostgreSQL 16 + PostGIS 3.4, TLS |
| `DIVERT_REDIS_URL` | `rediss://:CHANGE_ME@redis.internal:6380/0` | Managed Redis 7 with AOF, TLS |
| `DIVERT_API_KEY` | `CHANGE_ME` | Optional. Turns on `X-API-Key` for POST/DELETE. |

### 6.3 Per-workload environment and settings

| Workload | Extra `env` | Other settings |
|---|---|---|
| `divert-api` | none | 2 replicas, HPA 2..10 at 65 % CPU, PDB `minAvailable: 1`, readiness `/readyz`, liveness and startup `/healthz`, `prometheus.io/port: "8000"` |
| `divert-worker` | `DIVERT_METRICS_PORT=9100`, `DIVERT_INSTANCE_ID` = pod name | 2 replicas, `maxSurge: 0`, `maxUnavailable: 1`, preferred pod anti-affinity, PDB `minAvailable: 1`, probes on `:9100/metrics`, `terminationGracePeriodSeconds: 20` |
| `divert-persister` | `DIVERT_METRICS_PORT=9100`, `DIVERT_INSTANCE_ID` = pod name | 2 replicas, PDB `minAvailable: 1`, probes on `:9100/metrics` |
| `divert-migrate` (Job) | none | `divert migrate`, Argo CD `PreSync` hook, `backoffLimit: 3`, `activeDeadlineSeconds: 600` |
| `divert-frontend` | `POD_NAMESPACE` (downward API), `DIVERT_API_UPSTREAM=divert-api.$(POD_NAMESPACE).svc.cluster.local:80` | 2 replicas, uid 101, readiness `/`, liveness `/nginx-health` |
| Ingress `divert` | n/a | ingress-nginx, TLS secret `divert-tls`, `/api` to the API and `/` to the frontend, proxy read/send timeout 3600 s, body size 1 MB |

All backend pods run as uid/gid 10001, non-root, `readOnlyRootFilesystem: true`, all capabilities dropped, seccomp `RuntimeDefault`.

---

## 7. `.env.example` walkthrough

Copy it to `.env` in the repo root (`cp .env.example .env`). Never commit `.env`. It has three groups:

1. **docker compose.**
   - `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` (all `divert`): the postgres container's credentials, also used to build the backend's `DIVERT_DATABASE_URL` inside compose.
   - Published host ports: `DIVERT_PG_PORT=5432`, `DIVERT_REDIS_PORT=6379`, `DIVERT_API_PORT=8000`, `DIVERT_WEB_PORT=8080`.
   - Basemap build args: `DIVERT_WEB_TILE_URL`, `DIVERT_WEB_TILE_URL_DARK`, `DIVERT_WEB_TILE_ATTRIBUTION`. Empty means OSM standard tiles, for development only.
   - `GRAFANA_ADMIN_PASSWORD=change-me`.
2. **Backend settings** (section 2). Every value equals the code default:
   - `DIVERT_ENV=dev`, `DIVERT_LOG_LEVEL=INFO`, `DIVERT_LOG_JSON=true`
   - localhost `DIVERT_DATABASE_URL` / `DIVERT_REDIS_URL`
   - `DIVERT_REDIS_REQUEST_TIMEOUT_S=0.5`, `DIVERT_REDIS_CIRCUIT_COOLDOWN_S=5`
   - `DIVERT_CITIES`, `DIVERT_UNITS_PER_STATION=2`
   - OSRM 8 s / 2 retries / 3600 s
   - corridor and pre-emption 30 / 45 / 600 / 20 / 5
   - simulation 0.5 / 1 / 120 / 5 / 30
   - worker and persister 5000 / 10 / 200 / 5 / 2 / 7 / 9100
   - API key empty, CORS `http://localhost:8080,http://localhost:5173`, rate 60, `DIVERT_TRUST_PROXY_HEADERS=false`

   Inside compose, the database and Redis URLs are overridden (section 5.1). The file's comment says these lines are for running the backend "outside compose (e.g. `uv run divert migrate`)". The backend does not read `.env` (`env_file=None`), so outside compose they take effect only if you export them, for example `set -a; . ../.env; set +a`.
3. **Tests.** `DIVERT_TEST_DATABASE_URL=postgresql+asyncpg://divert:divert@localhost:5432/divert_test` and `DIVERT_TEST_REDIS_URL=redis://localhost:6379/15`. **The integration suite DROPs and recreates the test database and FLUSHes the test Redis database.** Never point these at real data.

Compose variables that are not in `.env.example`: `DIVERT_BACKEND_IMAGE`, `DIVERT_FRONTEND_IMAGE`, `DIVERT_TAG`, `DIVERT_OSRM_PORT`, `DIVERT_PROMETHEUS_PORT`, `DIVERT_GRAFANA_PORT`.

---

## 8. Tool configuration

### 8.1 Backend (`backend/pyproject.toml`)

| Tool | Configuration |
|---|---|
| **ruff** | `line-length = 100`, `target-version = "py312"`, `src = ["src", "tests", "scripts"]`. Rule sets: `E W F I B UP SIM ASYNC RUF N C4 PT RET TID S DTZ PL`. Ignored: `B008` (FastAPI `Depends()` defaults), `PLR0913` (keyword-heavy constructors), `PLR2004` (magic numbers), `S101` (assert), `N818` (exception names without `Error`). Per-file: migrations ignore `E501`; tests ignore `S` and `PLR` (integration also `E501`); scripts ignore `T201`. isort `known-first-party = ["divert"]`. Formatter: `ruff format`. |
| **mypy** | `strict = true`, `plugins = ["pydantic.mypy"]`, `mypy_path = "src"`, `packages = ["divert"]`, excludes `src/divert/migrations/versions/`. `ignore_missing_imports` for `fakeredis.*`, `respx.*`, `geoalchemy2.*`. pydantic-mypy: `init_forbid_extra`, `init_typed`, `warn_required_dynamic_aliases`. **Scope: only the `divert` package. Tests and scripts are not type-checked.** |
| **pytest** | `testpaths = ["tests"]`, `asyncio_mode = "auto"`, session-scoped fixture and test loops, `addopts = "-ra --strict-markers -m 'not network'"`. Markers: `integration` (needs real PostgreSQL/PostGIS + Redis) and `network` (public Overpass/OSRM, opt-in with `-m network`). `filterwarnings`: `error::pytest.PytestUnraisableExceptionWarning` (an un-awaited coroutine or leaked exception fails the test) and an ignore for Starlette's TestClient `httpx` deprecation. |
| **Alembic** | `alembic.ini`: `script_location = divert:migrations`, `file_template = %%(year)d%%(month).2d%%(day).2d_%%(rev)s_%%(slug)s`, `prepend_sys_path = src`, `timezone = UTC`. The URL comes from `DIVERT_DATABASE_URL` (`migrations/env.py`). `env.py` uses `compare_type=True` and `transaction_per_migration=True`. |
| **pre-commit** | `.pre-commit-config.yaml`: whitespace and EOF fixers (city data excluded), `check-yaml --allow-multiple-documents`, `check-json`, merge-conflict and private-key detection, `check-added-large-files --maxkb=4096`, ruff + ruff-format (v0.8.6) on `backend/`, and a local `mypy --strict` hook. |
| **EditorConfig** | UTF-8, LF, final newline, 2-space indent. Python uses 4 spaces and max line 100. Makefile uses tabs. |

### 8.2 Frontend (`frontend/`)

| Tool | Configuration |
|---|---|
| **TypeScript** (`tsconfig.app.json`) | `strict`, `noUncheckedIndexedAccess`, `noImplicitOverride`, `noImplicitReturns`, `noFallthroughCasesInSwitch`, `noUnusedLocals`, `noUnusedParameters`, `useUnknownInCatchVariables`, `verbatimModuleSyntax`, `isolatedModules`, `forceConsistentCasingInFileNames`. Target ES2022, `moduleResolution: bundler`, `jsx: react-jsx`, `noEmit`. `tsconfig.node.json` covers `vite.config.ts` and `tailwind.config.ts` with `strict`. |
| **ESLint** (`eslint.config.js`, flat) | `@eslint/js` recommended + `typescript-eslint` `strictTypeChecked` and `stylisticTypeChecked` (`projectService`), `jsx-a11y` recommended, `react-hooks` recommended, `react-refresh` (vite), and `eslint-config-prettier` last. Rules: `consistent-type-imports`, `no-unused-vars` (args `^_` allowed), `no-console` (warn/error allowed), `eqeqeq: smart`, template expressions allow numbers. Tests relax `no-non-null-assertion` and the `no-unsafe-*` rules. Config files are not type-checked. Run with `--max-warnings 0`. |
| **Prettier** (`.prettierrc.json`) | `semi: false`, `singleQuote: true`, `trailingComma: "all"`, `printWidth: 100`. Ignores `dist`, `node_modules`, `coverage`, `package-lock.json`. |
| **Vite** (`vite.config.ts`) | Dev server on 5173 with the proxy (section 3), preview on 4173. Build `target: 'es2022'`, `sourcemap: true`, `chunkSizeWarningLimit: 700`. |
| **Vitest** (in `vite.config.ts`) | `environment: 'jsdom'`, `setupFiles: ['./src/test/setup.ts']`, `css: false`, `restoreMocks: true`, `unstubGlobals: true`, `include: ['src/**/*.test.{ts,tsx}']`. |
| **Tailwind** (`tailwind.config.ts`) | Content `index.html` + `src/**/*.{ts,tsx}`. Extra `ink` palette (950..600), Inter font stack, `pulse-ring` and `slide-in` animations. PostCSS: `tailwindcss` + `autoprefixer`. |
| **Node** | `engines.node >= 22.12.0`. CI uses Node 22 and `npm ci`. |

---

## 9. Profiles: which settings to change

Defaults are tuned for a laptop running docker compose. Change these for other environments.

| Setting | Local dev (defaults) | Test / QA environment | Production |
|---|---|---|---|
| `DIVERT_API_KEY` | empty (open) | Set it, to exercise 401 handling and the UI key button | **Set it** (from the Secret). Operators enter it in the UI. Avoid `VITE_API_KEY`: it is public once built. |
| `DIVERT_CORS_ORIGINS` | localhost 8080 and 5173 | The test UI origin(s) | Exact production origin(s). Not needed if the UI is same-origin through nginx. Never `*` (rejected). |
| `DIVERT_TRUST_PROXY_HEADERS` | `false` | `true` only behind your own proxy | `true` behind ingress-nginx (ConfigMap) |
| `DIVERT_RATE_LIMIT_PER_MIN` | 60 | Raise it for load tests | 120 in the ConfigMap. Size per operator count. |
| `DIVERT_OSRM_URL` | public demo (fair use) | Public demo for light manual tests. Self-host for load tests. | **Self-hosted OSRM** (`infra/osrm/prepare.sh`). Never the public demo. |
| `DIVERT_OSRM_TIMEOUT_S` | 8 | 8 | 5 (ConfigMap, in-cluster OSRM) |
| `DIVERT_WEB_TILE_URL*` / `VITE_TILE_*` | empty = OSM standard tiles (development and demos only) | OSM standard tiles are acceptable for light demos | **A keyed provider or self-hosted tiles**, plus a matching attribution |
| `DIVERT_SIM_TIME_SCALE` | 1 | e.g. 10 to shorten runs (max 100). Set it on api **and** worker. | 1 |
| `DIVERT_SCENE_TIME_S` | 120 | Lower (the unit tests use 30) for faster resolution | 120 |
| `DIVERT_POSITION_RETENTION_DAYS` / `DIVERT_OUTBOX_RETENTION_DAYS` | 30 / 7 | Short (e.g. 1-7 / 1) | Per data-retention policy |
| `DIVERT_DATABASE_URL` / `DIVERT_REDIS_URL` | compose containers | Dedicated instances | Managed, TLS (`?ssl=require`, `rediss://`), from the Secret |
| `DIVERT_DB_POOL_SIZE` / `DIVERT_DB_MAX_OVERFLOW` | 10 / 10 | 10 / 10 | Size so that replicas x (pool + overflow) fits `max_connections`, or use PgBouncer |
| `DIVERT_INSTANCE_ID` | hostname-pid | hostname-pid | Pod name (set in the manifests) |
| `DIVERT_LOG_JSON` / `DIVERT_LOG_LEVEL` | `true` / `INFO` (`false` / `DEBUG` is easier to read locally) | `true` / `INFO` | `true` / `INFO` |
| `DIVERT_TEST_DATABASE_URL` / `DIVERT_TEST_REDIS_URL` | `divert_test` / db 15 | CI service containers | **Never set**: the suite drops that database |
| `POSTGRES_PASSWORD`, `GRAFANA_ADMIN_PASSWORD` | `divert`, `change-me` | Change them | Managed services. Grafana password from a secret. |

Keep these identical across every process: `DIVERT_CITIES`, `DIVERT_SIM_TIME_SCALE` (api and worker), `DIVERT_LEADER_LOCK_TTL_MS` (all workers), and the pre-emption settings (all workers, since any of them can become leader).

---

## Related

- [Backend codebase](09-codebase-backend.md): where each setting is read, and recipes
- [Frontend codebase](10-codebase-frontend.md): build and serving
- [Architecture](03-architecture.md), [Data stores](05-data-stores.md), [Engineering workflows](08-engineering-workflows.md)
- [Domain rules](02-domain-rules.md): what the pre-emption and speed settings mean
- Reference: [deployment.md](../deployment.md), [runbook.md](../runbook.md), [api.md](../api.md)
