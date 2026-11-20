# 08 - Engineering workflows

## What this covers

This page is the hands-on guide for engineers and testers: setting up a Windows machine,
the two ways to run DIVERT locally, every Makefile target, the testing strategy, the Vite
dev proxy, CI, database migrations, refreshing the OpenStreetMap data, building and
tagging images for a test release, the optional OSRM and observability profiles, the
simulation time scale, and troubleshooting. Commands are given for Git Bash unless marked
PowerShell. For what the running system does, see [Runtime workflows](07-runtime-workflows.md);
for every setting, see [Configuration](11-configuration.md).

## Tooling at a glance

| Tool | Version | Why | Source |
|---|---|---|---|
| Docker Desktop (WSL 2 backend) | recent, with Compose v2 | PostgreSQL/PostGIS, Redis, the full stack | `docker-compose.yml` |
| Git for Windows | any | clone; provides Git Bash | |
| uv | 0.13.5 in CI and the image | **the** Python package and environment manager | `.github/workflows/ci.yml` `UV_VERSION`, `backend/Dockerfile` `UV_VERSION` |
| Python | 3.12 (`>=3.12,<3.13`), installed by uv | backend | `backend/pyproject.toml` `requires-python` |
| Node.js | 22 (`>=22.12.0`) | frontend; CI and the frontend image use 22 | `frontend/package.json` `engines`, `frontend/Dockerfile` |
| GNU make | optional | shortcuts in `Makefile`; not bundled with Git for Windows | `Makefile` |
| osmium-tool | only for self-hosted OSRM | clips the India extract | `infra/osrm/prepare.sh` |

**uv is the package manager. pip and Poetry are not used.** Dependencies are declared in
`backend/pyproject.toml` (runtime) and its `[dependency-groups] dev` group (pytest,
pytest-asyncio, fakeredis, respx, ruff, mypy, asyncpg-stubs), and pinned in the committed
`backend/uv.lock`. The image runs `uv sync --frozen --no-dev` and CI runs
`uv sync --frozen`, so the lock file is the single source of versions everywhere. Add a
dependency with `uv add <pkg>` (or `uv add --dev <pkg>`) and commit both files.
`tzdata` is a runtime dependency because Windows and slim images have no IANA time zone
database (`backend/pyproject.toml` comment).

---

## Local setup on Windows

1. **Install** Docker Desktop (WSL 2 backend), Git for Windows, uv and Node.js 22. The
   official uv installer for PowerShell is
   `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`.
   You do not need to install Python: `uv sync` downloads 3.12.
2. **Clone with LF line endings.** The repo has no `.gitattributes`, `.editorconfig` asks
   for LF, and Git for Windows often defaults to `core.autocrlf=true`. CRLF breaks bash
   scripts such as `infra/osrm/prepare.sh`. Clone with:
   ```bash
   git clone -c core.autocrlf=input <repo-url> divert-ai
   cd divert-ai
   ```
3. **Create `.env`.** `cp .env.example .env` (PowerShell: `Copy-Item .env.example .env`).
   Never commit it (`.gitignore`). Change `POSTGRES_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`
   and, if you want auth, `DIVERT_API_KEY`.
4. **Start the stack.** `docker compose up -d --build` (same as `make up`). This starts
   postgres, redis, the one-off `migrate` job, api, two workers, persister and frontend.
   `depends_on` orders them: postgres and redis healthy, then `migrate` completed
   successfully, then the app services; the frontend waits for a healthy api.
5. **Check it.**
   ```bash
   docker compose ps                      # migrate "exited (0)", the rest healthy
   curl -s localhost:8000/readyz          # "ready": true, "worker": {"status": "ok"}
   ```
   PowerShell: use `curl.exe` or `Invoke-RestMethod http://localhost:8000/readyz`.
6. **Open** the dashboard at <http://localhost:8080> and the OpenAPI UI at
   <http://localhost:8000/docs>.
7. **Smoke-test a dispatch** (PowerShell):
   ```powershell
   Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/cities/delhi/incidents `
     -ContentType 'application/json' -Body '{"type": "POLICE", "location": [28.6271, 77.2166]}'
   ```
   A 201 returns the incident and the vehicle with its route; the vehicle appears on the
   Live map. This calls the public OSRM demo unless you self-host (see below).
8. **Developer extras.** `cd backend && uv sync` (creates `backend/.venv` with the dev
   group) and `cd frontend && npm ci`. Optional: `uv tool install pre-commit` and
   `pre-commit install` (`.pre-commit-config.yaml`: whitespace, YAML/JSON checks, ruff,
   ruff-format, mypy --strict).

Published ports (override in `.env`): PostgreSQL `DIVERT_PG_PORT` 5432, Redis
`DIVERT_REDIS_PORT` 6379, api `DIVERT_API_PORT` 8000, frontend `DIVERT_WEB_PORT` 8080,
OSRM `DIVERT_OSRM_PORT` 5000, Prometheus `DIVERT_PROMETHEUS_PORT` 9090, Grafana
`DIVERT_GRAFANA_PORT` 3000.

---

## Two dev modes

| | (a) Full docker compose | (b) Hybrid |
|---|---|---|
| For | testers, QA, demos, the pre-production test environment | developers changing backend or frontend code |
| In Docker | everything: postgres, redis, migrate, api, worker x2, persister, frontend | only postgres and redis (`make infra`) |
| On the host | nothing | api, worker, persister with `uv run`; frontend with `npm run dev` |
| Start | `docker compose up -d --build` | `make infra`, `cd backend && uv run divert migrate`, then `make api`, `make worker`, `make persister` in three shells, and `npm run dev` in `frontend/` |
| UI | <http://localhost:8080> (nginx) | <http://localhost:5173> (Vite, proxies to :8000) |
| Code change | rebuild: `docker compose up -d --build` | api reloads itself (`uvicorn --reload`); restart worker or persister; Vite hot-reloads the UI |
| Leader failover | two worker replicas, real failover | one worker unless you start a second one |

### (a) Full compose: recommended for testing

It runs exactly the images you would release, with two workers (`deploy.replicas: 2`),
health checks, restart policies, resource limits and log rotation. Tag the images (see
[Building and tagging](#building-and-tagging-images-for-a-test-release)) so every bug
report cites a build. Useful commands: `docker compose logs -f --tail=100` (`make logs`),
`make psql`, `make redis-cli`, `docker compose restart worker`.

### (b) Hybrid: recommended for development

Why:

- **Hot reload and debugging.** `make api` runs `uvicorn ... --reload`; you can attach a
  debugger to processes running from `backend/.venv`.
- **Same dependencies as the image.** Both use `backend/uv.lock`; `uv sync` locally and
  `uv sync --frozen` in `backend/Dockerfile` resolve the same versions.
- **No native PostGIS or Redis on Windows.** PostGIS 3.4 for PostgreSQL 16 is awkward to
  install natively on Windows, and Redis has no native Windows build. The
  `postgis/postgis:16-3.4` and `redis:7-alpine` containers avoid both.
- The backend defaults already point at the containers: `Settings.database_url` =
  `postgresql+asyncpg://divert:divert@localhost:5432/divert`, `Settings.redis_url` =
  `redis://localhost:6379/0` (`backend/src/divert/config.py`).

Make targets wrap these commands (from `backend/`):

```bash
uv run divert migrate                                         # alembic upgrade head + seed
uv run uvicorn divert.main:create_app --factory --reload      # api on :8000
uv run python -m divert.worker                                # simulation worker
uv run python -m divert.persister                             # history persister
```

Hybrid-mode pitfalls (all from the code):

| Pitfall | Why | Fix |
|---|---|---|
| `.env` is ignored by `uv run` | `Settings` uses `env_file=None`: configuration is environment only | `uv run --env-file ../.env ...`, or export the variables (`$env:DIVERT_SIM_TIME_SCALE = "10"` in PowerShell) |
| Port 8000 already in use | the compose api publishes 8000 | stop the compose app services (`docker compose stop api worker persister frontend`) or run `uvicorn ... --port 8001` and `DIVERT_DEV_API=http://localhost:8001 npm run dev` |
| Your local worker never leads, or leads instead of the containers | it joins the same leader election on the same Redis | run either the compose workers or local ones, not both |
| `metrics port unavailable` warning | worker and persister both default to `DIVERT_METRICS_PORT` 9100 | harmless; or set `DIVERT_METRICS_PORT=0` (off) for one process |
| Ctrl+C on a worker does not hand over at once | Windows has no `loop.add_signal_handler`, so the lock is not released | the next worker leads once the 5 s lease expires |
| `make: command not found` | Git for Windows has no GNU make | install it (for example `choco install make` or `scoop install make`) and run `make` from Git Bash (the Makefile sets `SHELL := bash` and `make clean` uses `find`), or run the commands directly |

---

## Makefile targets

All backend commands run through uv from `backend/` (`Makefile`).

| Target | Runs | Notes |
|---|---|---|
| `help` | lists targets with their `##` comments | |
| `infra` | `docker compose up -d --wait postgres redis` | waits until both are healthy |
| `up` | `docker compose up -d --build` | full stack |
| `down` | `docker compose --profile osrm --profile observability down` | stops everything, keeps volumes |
| `migrate` | `infra`, then `uv run divert migrate` | alembic upgrade head + idempotent seed against the compose postgres |
| `seed` | `uv run divert seed` | idempotent; never resets a live unit |
| `fetch-data` | `uv run divert fetch-data --cache-dir ../.work/osm-cache` | network; see [Refreshing OSM data](#refreshing-osm-data) |
| `api` | `uv run uvicorn divert.main:create_app --factory --reload` | api on :8000 with reload |
| `worker` | `uv run python -m divert.worker` | one worker |
| `persister` | `uv run python -m divert.persister` | one persister |
| `dlq` | `uv run divert dlq` | lists dead-lettered history events |
| `lint` | `ruff check` + `ruff format --check` on `src tests scripts` | |
| `fmt` | `ruff check --fix` + `ruff format` | rewrites files |
| `typecheck` | `uv run mypy` | `--strict` via `[tool.mypy]` |
| `test-unit` | `uv run pytest tests/unit` | no services needed |
| `test-integration` | `infra`, then `uv run pytest -m integration tests/integration` | real PostGIS + Redis |
| `test-network` | `uv run pytest -m network tests/network -s` | public OSRM, 3 requests |
| `test` | `test-unit` + `test-integration` | |
| `check` | `lint` + `typecheck` + `test` | the Makefile calls this "everything CI runs for the backend"; CI also runs the migration checks below |
| `build` | `docker compose build api frontend` | builds the backend image (shared by api, worker, persister, migrate) and the frontend image |
| `logs` | `docker compose logs -f --tail=100` | |
| `psql` | `docker compose exec postgres psql -U divert -d divert` | |
| `redis-cli` | `docker compose exec redis redis-cli` | |
| `clean` | removes `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache` | |

---

## Testing strategy

### Backend layers

| Layer | Path | How it is selected | Needs | Default run |
|---|---|---|---|---|
| Unit | `backend/tests/unit` | by path (no marker) | nothing: fakeredis replaces Redis, respx mocks HTTP, PostgreSQL methods are stubbed | yes |
| Integration | `backend/tests/integration` | `pytestmark = pytest.mark.integration` in each module | PostgreSQL 16 + PostGIS 3.4 and Redis 7 | yes; tests **skip** if the servers are unreachable |
| Network | `backend/tests/network` | `pytestmark = pytest.mark.network` | internet; public OSRM demo (3 requests) | no: `addopts = "-m 'not network'"` |

Markers are declared in `backend/pyproject.toml` `[tool.pytest.ini_options]` with
`--strict-markers`. `asyncio_mode = "auto"` with a session-scoped event loop. Plain
`uv run pytest` runs unit + integration.

### Fixtures

| Fixture | Scope | What it does | File |
|---|---|---|---|
| `_isolated_settings` (autouse) | test | removes every `DIVERT_*` variable except `DIVERT_TEST_*` and clears the settings cache, so your shell cannot leak in | `tests/conftest.py` |
| `fake_redis` | test | `fakeredis.aioredis.FakeRedis(decode_responses=True)`, flushed after | `tests/unit/conftest.py` |
| `clock` | test | deterministic wall/sim clock starting at `T0` (2026-11-17 06:30 UTC, 12:00 IST) | `tests/unit/conftest.py`, `tests/support.py` |
| `make_worker` | test | a real `Worker` (`UnitWorker`) with PostgreSQL stubbed and cities from the snapshots; `unit_settings` uses delhi + bengaluru, scene time 30 s, lock TTL 3000 ms | `tests/unit/conftest.py` |
| `test_settings` | session | `DIVERT_TEST_DATABASE_URL` (default `.../divert_test`) and `DIVERT_TEST_REDIS_URL` (default `redis://localhost:6379/15`) | `tests/integration/conftest.py` |
| `seeded_db` | session | **drops and re-creates** the test database, runs `alembic upgrade head`, seeds the snapshots | `tests/integration/conftest.py` |
| `engine`, `sessionmaker` | session | async engine on the test database | `tests/integration/conftest.py` |
| `session` | test | a session inside a transaction that is always rolled back | `tests/integration/conftest.py` |
| `redis` | test | real Redis on db 15, `FLUSHDB` before and after | `tests/integration/conftest.py` |
| `clean_db` | test | frees every unit, truncates `outbox`, deletes incidents (cascades) | `tests/integration/conftest.py` |

Safety notes:

- Integration tests drop the `divert_test` database and flush Redis **db 15** only. They
  never touch `divert` or db 0. Never point `DIVERT_TEST_*` at a database you care about.
- Redis pub/sub ignores the database number. Integration tests that run a worker publish
  on `divert:events:{city}`, so a dashboard open on the same Redis can briefly show test
  vehicles.

### Running one test

```bash
cd backend
uv run pytest tests/unit/test_preemption.py -q
uv run pytest tests/unit/test_regressions.py::test_cancel_releases_every_claim -q
uv run pytest tests/unit -k "leader and not lock" -q
uv run pytest -m integration tests/integration/test_pipeline.py -k cancel -q
uv run pytest -m network tests/network -s
```

Useful test files: `tests/unit/test_regressions.py` (the three prototype regression
guarantees), `tests/unit/test_worker_leader.py` (lock, standby take-over, command
reclaim, merged retry), `tests/unit/test_resilience.py` (RedisGuard, sim clock, tick
resume), `tests/integration/test_pipeline.py` (API to worker to persister lifecycle,
cancel, outbox, DLQ, retention, rebuild), `tests/integration/test_resilience.py` (Redis
down, rebuild after a freeze), `tests/integration/test_migrations.py` (drift and
reversibility).

### Frontend

| Item | Value | Source |
|---|---|---|
| Runner | vitest, `environment: 'jsdom'` | `frontend/vite.config.ts` `test` |
| Setup | `src/test/setup.ts`: jest-dom matchers, Testing Library `cleanup` after each test | |
| Files | `src/**/*.test.{ts,tsx}` (11 files today) | |
| Isolation | `restoreMocks: true`, `unstubGlobals: true`, `css: false` | |
| Helpers | `src/test/render.tsx`, `src/test/fixtures.ts` | |

```bash
cd frontend
npm test                                # vitest run (all)
npx vitest run src/store/live.test.ts   # one file
npx vitest run -t "reconnect"           # by test name
npm run test:watch                      # watch mode
npm run lint && npm run typecheck && npm run format:check && npm run build
```

ESLint runs with `--max-warnings 0`, so any warning fails the lint.

There are **no browser end-to-end tests** (for example Playwright) in the repo. Live
behaviour through nginx is checked by hand on the compose stack.

---

## Vite dev proxy

`frontend/vite.config.ts`:

| Setting | Value |
|---|---|
| Dev server port | 5173 (`npm run dev`) |
| Proxied paths | `/api` (with `ws: true` for the WebSocket), `/readyz`, `/healthz` |
| Target | `DIVERT_DEV_API`, default `http://localhost:8000` |
| `changeOrigin` | true |
| Preview port | 4173 (`npm run preview`) |

The browser only talks to its own origin, in development (Vite) and in production
(nginx), so no API host is baked into the bundle. `VITE_API_BASE_URL` overrides that
(`frontend/src/config.ts`). Point the dev server at another backend with
`DIVERT_DEV_API=http://host:8001 npm run dev` (PowerShell:
`$env:DIVERT_DEV_API = "http://localhost:8001"; npm run dev`). The api's default CORS
list already includes `http://localhost:5173` and `http://localhost:8080`
(`Settings.cors_origins`); a `*` origin is rejected at start-up.

---

## CI

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests, manual dispatch, and
weekly (`cron: "17 3 * * 1"`, Mondays 03:17 UTC). Older runs on the same ref are
cancelled (`concurrency`). Permissions are read-only. Pins: `UV_VERSION` 0.13.5,
`PYTHON_VERSION` 3.12, Node 22.

| Job | Needs | Runs when | Steps |
|---|---|---|---|
| `backend-lint` | none | always | `uv sync --frozen`; `ruff check src tests scripts`; `ruff format --check`; `mypy` (strict); `pytest tests/unit` |
| `backend-integration` | `backend-lint` | always | service containers `postgis/postgis:16-3.4` and `redis:7`; `uv run divert migrate`; `alembic upgrade head` + `alembic check` (idempotent at head, no model drift); `pytest -m integration tests/integration`; `alembic downgrade base` then `upgrade head` (reversible) |
| `backend-network` | none | schedule or manual only | `pytest -m network tests/network -s` against the public OSRM |
| `frontend` | none | always | `npm ci`; `format:check`; `lint`; `typecheck`; `test`; `build` |
| `docker` | `backend-lint`, `frontend` | always | buildx builds `divert-backend:ci` and `divert-frontend:ci` with the GitHub Actions cache; `push: false` |

Not in CI (gaps): images are built but never pushed or tagged for release; there is no
deploy job; `infra/osrm/prepare.sh` is not run (its header says so); the Kubernetes base
is not applied anywhere.

---

## Migrations

Alembic, async (asyncpg). Config: `backend/alembic.ini` (`script_location =
divert:migrations`, `prepend_sys_path = src`). The URL comes from `DIVERT_DATABASE_URL`
(`backend/src/divert/migrations/env.py` `_url`) unless the caller sets it, as
`divert migrate` and the tests do. Run Alembic from `backend/`.

| Task | Command |
|---|---|
| Upgrade + seed (what the `migrate` job does) | `uv run divert migrate` (`--no-seed` to skip the seed) |
| Upgrade only | `uv run alembic upgrade head` |
| Current revision / history | `uv run alembic current`, `uv run alembic history` |
| Check models vs migrations | `uv run alembic check` (fails if `db/models.py` and the migrations drift) |
| Step back one | `uv run alembic downgrade -1` |
| Back to empty | `uv run alembic downgrade base` |
| New revision | `uv run alembic revision --autogenerate --rev-id 0004_<slug> -m "<slug>"` |

Revisions today: `0001_initial`, `0002_dispatch_rebuild`, `0003_position_elapsed`
(`backend/src/divert/migrations/versions/`). Files are named `YYYYMMDD_<rev>.py`
(`file_template` in `alembic.ini`); rename the generated file to match if needed.

Writing a migration:

1. Change `backend/src/divert/db/models.py`.
2. Generate the revision (above) and **read it**. `include_object`
   (`migrations/filters.py`) keeps autogenerate away from PostGIS's own tables.
3. `uv run alembic upgrade head`, `uv run alembic check`, then `downgrade -1` and
   `upgrade head` again: every migration must be reversible.
4. Run `uv run pytest -m integration tests/integration/test_migrations.py`
   (`test_models_match_migration`, `test_downgrade_to_base_and_upgrade_again`).
5. Keep it backward compatible for one release: the `migrate` job runs before the new
   api and worker start (`docs/deployment.md`, Rollout).

**The `migrate` service** in `docker-compose.yml` runs `divert migrate` once
(`restart: "no"`); api, worker and persister wait for it with
`condition: service_completed_successfully`. Migrations ship inside the backend image, so
after adding one, rebuild and re-run: `docker compose build migrate` then
`docker compose run --rm migrate`. On Kubernetes the same command is a Job
(`infra/k8s/base/migrate-job.yaml`); that base has never been applied to a real cluster.

---

## Refreshing OSM data

The committed snapshots (`backend/src/divert/cities/data/{delhi,kolkata,bengaluru}.json`)
are the only source of stations and signals. Refreshing them needs the public Overpass
API.

1. **Clear the cache if you want new data.** `--cache-dir` reuses any raw response
   already saved for the same query (`cities/osm.py` `_cached_overpass`). The Makefile
   uses `.work/osm-cache` (gitignored). With that folder populated, a re-run rebuilds the
   same snapshot. Delete the folder (or use a new one) for a real refresh. Keep it when
   resuming an interrupted fetch.
2. **Fetch.** `make fetch-data`, or from `backend/`:
   ```bash
   uv run divert fetch-data --cache-dir ../.work/osm-cache            # all 3 cities
   uv run divert fetch-data delhi kolkata --cache-dir ../.work/osm-cache
   ```
   Each tile tries 4 mirrors for up to 6 rounds (backoff 15 s x 2^n), with a 5 s pause
   between cities. If any city fails, nothing is written.
3. **Review.** `git diff --stat backend/src/divert/cities/data/` and each file's
   `meta.counts`, `meta.fetched_at`, `meta.osm_base`.
4. **Test.** `uv run pytest tests/unit/test_city_data.py` (validity, OSM traceability of
   ids).
5. **Commit** the JSON files.
6. **Re-seed.** Hybrid: `uv run divert seed`. Compose: the JSON ships in the backend
   image, so `docker compose build migrate` then `docker compose run --rm migrate`.
   Re-seeding deactivates vanished stations and signals (never deletes) and never resets
   a live unit.
7. **Pick it up.** Each api process caches reference data for 300 s
   (`reference_cache_ttl_s`); restart the api to see it at once.

Adding a new city needs code: a profile in `cities/osm.py` `CITY_PROFILES`, the bbox in
`infra/osrm/prepare.sh`, and the id in `DIVERT_CITIES`. See [../data-sources.md](../data-sources.md).

---

## Building and tagging images for a test release

Compose names the images `${DIVERT_BACKEND_IMAGE:-divert-backend}:${DIVERT_TAG:-dev}` and
`${DIVERT_FRONTEND_IMAGE:-divert-frontend}:${DIVERT_TAG:-dev}` (`docker-compose.yml`).
api, worker, persister and migrate share the backend image.

**Recommendation:** give every build that testers receive a pre-release version tag such
as `0.1.0-rc1`, `0.1.0-rc2`, and tag the commit the same way. Then every bug report can
name the exact build.

```bash
git tag -a v0.1.0-rc1 -m "test release 0.1.0-rc1"
DIVERT_TAG=0.1.0-rc1 docker compose build api frontend
DIVERT_TAG=0.1.0-rc1 docker compose up -d
docker image ls divert-backend
```

PowerShell: `$env:DIVERT_TAG = "0.1.0-rc1"; docker compose build api frontend; docker compose up -d`.
Or put `DIVERT_TAG=0.1.0-rc1` in `.env` so every compose command uses it. To push to a
registry, set `DIVERT_BACKEND_IMAGE` / `DIVERT_FRONTEND_IMAGE` to the registry path and
run `docker compose push api frontend`.

Things to know:

- **Frontend build arguments are baked in at build time.** Compose passes only the tile
  settings (`DIVERT_WEB_TILE_URL`, `DIVERT_WEB_TILE_URL_DARK`,
  `DIVERT_WEB_TILE_ATTRIBUTION`). `VITE_API_KEY` is not wired in compose, so if the api
  has `DIVERT_API_KEY`, testers enter the key with the header's API key button
  (`frontend/src/app/ApiKeyButton.tsx`, stored in `localStorage`).
- **Not implemented: build identity at runtime.** The api reports `divert.__version__`
  (`2.0.0`) in `/openapi.json`, the frontend package is `1.0.0`, and the backend image
  sets no version label. Nothing exposes the image tag, so testers must note it.
- What a good bug report contains: the image tag; `git rev-parse --short HEAD`; the
  `/readyz` body; the `X-Request-ID` of the failing request (echoed on every response
  and in error bodies); the city and incident id; and
  `docker compose logs --since 10m api worker persister`.

---

## Optional profiles

### Self-hosted OSRM (recommended for the testing phase)

By default the api routes with the public OSRM demo, `https://router.project-osrm.org`
(`Settings.osrm_url`). It is fair-use only, has no SLA and is rate-limited
(`.env.example`, `docs/runbook.md`). An HTTP 429 is retried twice
(`Settings.osrm_retries`); if no candidate station can be routed, the dispatch fails with
`503 routing_unavailable`. The project team also reports it was intermittently
unreachable during development. For a test cycle, self-hosting gives stable and
repeatable routes.

**Status: not yet done.** `infra/osrm/prepare.sh` is documented but has not been run for
this project, and CI does not run it.

1. **Prerequisites:** bash, curl, osmium-tool and docker. On Windows the simplest route
   is WSL 2 (Ubuntu: `sudo apt install osmium-tool`) with Docker Desktop's WSL
   integration, and a clone with LF line endings.
2. **Build the data:** `infra/osrm/prepare.sh`. It downloads the Geofabrik
   `asia/india-latest.osm.pbf` (MD5 checked when available), clips the three city bboxes
   with a 0.05 degree margin (`osmium extract --strategy complete_ways`), merges them,
   and runs `osrm-extract -p /opt/car.lua`, `osrm-partition` and `osrm-customize` (MLD)
   in `ghcr.io/project-osrm/osrm-backend:v5.27.1`. The script header estimates about 2 GB
   of download plus about 1 GB of work files and 10-20 minutes. Output:
   `infra/osrm/data/divert-cities.osrm*` (gitignored). Overrides: `OSRM_IMAGE`,
   `SOURCE_URL`, `MARGIN`.
3. **Run it:** `docker compose --profile osrm up -d osrm` (port `DIVERT_OSRM_PORT` 5000,
   memory limit 4 GB).
4. **Point the api at it:** set `DIVERT_OSRM_URL=http://osrm:5000` in `.env` and
   `docker compose up -d api` (only the api calls OSRM). Hybrid mode:
   `DIVERT_OSRM_URL=http://localhost:5000`.
5. **Check:** `curl "http://localhost:5000/route/v1/driving/77.209,28.6139;77.22,28.62"`.
6. **Mind the route cache.** The cache key is the profile plus the coordinates, not the
   server (`store/redis_store.py` `osrm_route_cache`), with a 3600 s TTL. For up to an
   hour after switching, repeated origin/destination pairs still get the old server's
   routes. On a dev stack you can clear them with
   `docker compose exec redis sh -c "redis-cli --scan --pattern 'divert:osrm:*' | xargs -r redis-cli del"`.
7. **Refresh** the extract about monthly (`infra/osrm/README.md`).

### Observability

`docker compose --profile observability up -d` adds:

| Service | Port | Details |
|---|---|---|
| Prometheus `v2.55.1` | 9090 | scrapes every 15 s, keeps 15 days; DNS discovery of `api:8000`, `worker:9100`, `persister:9100` (every replica) (`infra/observability/prometheus.yml`) |
| Grafana `11.3.1` | 3000 | admin password `GRAFANA_ADMIN_PASSWORD` (default `change-me`), sign-up off, provisioned Prometheus datasource and the "DIVERT overview" dashboard |

It scrapes compose service names, so in hybrid mode it cannot see processes on the host.
Suggested alerts are listed in `docs/deployment.md` (Observability). `make down` stops
both profiles too.

---

## Simulation time scale

`DIVERT_SIM_TIME_SCALE` (`Settings.sim_time_scale`, default 1, must be > 0 and at most
100) makes simulated time run faster than the wall clock.

**Set it on both the api and the worker.** The worker uses it to advance vehicles; the
api uses it to predict the ETA at the simulation clock's hour
(`DispatchService.sim_clock`). With scale 1 on the api, the api ignores
`divert:sim:clock` and predicts at the wall-clock hour, so predicted and simulated
travel times drift apart.

```bash
# .env:  DIVERT_SIM_TIME_SCALE=10
docker compose up -d api worker
```

The README and runbook show `DIVERT_SIM_TIME_SCALE=10 docker compose up -d worker`,
which recreates only the worker and hits exactly this mismatch.

| Effect at scale `s` | Value | Code |
|---|---|---|
| Simulated seconds per 0.5 s tick | `0.5 x s` (5 s at 10x) | `Worker.tick_once` |
| Position samples | every 5 simulated s, at most one per tick (every tick at 10x) | `SimulationEngine._sample` |
| Scene time | 120 simulated s = `120 / s` wall seconds (12 s at 10x) | `scene_time_s` |
| Sim clock | starts at wall time, advances `0.5 x s` per tick, stored in `divert:sim:clock`; a later leader (or a later scaled run) resumes from it | `Worker._load_sim_clock`, `_heartbeat` |
| Congestion hour | city-local hour of the sim clock | `services/eta.py` `congestion_at` |
| Durations in history | simulated seconds (`travel_s`, `elapsed_s`); timestamps stay wall clock | `docs/simulation-model.md` section 8 |

Derived from code: large scales make the simulation coarse. Each tick moves a vehicle
`0.5 x s` simulated seconds, so at high scales a vehicle can pass a junction inside one
tick and never claim it.

Other settings testers often change (in `.env`; compose passes every `DIVERT_*` line to
the backend containers through `env_file`, except the keys set in `x-backend-env`, such
as the database and Redis URLs):

| Setting | Default | Use |
|---|---|---|
| `DIVERT_SCENE_TIME_S` | 120 | shorter scenes free units sooner |
| `DIVERT_RATE_LIMIT_PER_MIN` | 60 | raise for load tests |
| `DIVERT_API_KEY` | empty (auth off) | test 401 handling |
| `DIVERT_UNITS_PER_STATION` | 2 | applied by the next seed; only missing units are added |

---

## Troubleshooting

Commands assume the compose stack. Many come from [../runbook.md](../runbook.md).

| Symptom | Likely cause | What to do |
|---|---|---|
| `make: command not found` | no GNU make on Windows | install it, or run the command from the [Makefile table](#makefile-targets) |
| `port is already allocated` on `up` | a local PostgreSQL, Redis or another stack uses 5432/6379/8000/8080 | change `DIVERT_PG_PORT` etc. in `.env`, or stop the other service |
| `migrate` exits non-zero, app services never start | migration or seed error | `docker compose logs migrate`; fix, then `docker compose run --rm migrate` |
| Startup error `DIVERT_CORS_ORIGINS must list explicit origins` | `*` in the CORS list | list origins explicitly |
| POST returns `503 routing_unavailable` | OSRM unreachable or rate-limited | `divert_osrm_errors_total{kind}`; `curl "$DIVERT_OSRM_URL/route/v1/driving/77.209,28.6139;77.22,28.62"`; self-host OSRM. Nothing was written, so retry |
| POST returns `409 no_unit_available` | every unit of that service nearby is busy or out of service | `GET /api/v1/cities/{city}` shows `free_units`; wait for scenes to clear; check stuck units (runbook) |
| POST returns `422 invalid_incident` | unknown type, or the point is outside the city bbox (`details.bbox`) | use MEDICAL/FIRE/CRIME (aliases HEALTH, AMBULANCE, POLICE) and a point inside the bbox |
| POST returns `401` / `429` | `DIVERT_API_KEY` set / more than 60 mutations a minute | set the key in the UI or `X-API-Key`; raise `DIVERT_RATE_LIMIT_PER_MIN` for load tests |
| Map frozen, `/readyz` `degraded: true` | no worker leader, or Redis down | `$R GET divert:worker:leader`, `$R HGETALL divert:worker:heartbeat`, `docker compose logs --tail=200 worker` (look for `became leader`, `leadership lost`). Never delete the lock while its owner is alive |
| `/readyz` `checks.redis: error`, live endpoints 503, WebSocket 1013 | Redis down | expected degraded mode; when Redis returns the circuit closes, the outbox drains and the worker resumes ([workflow 11](07-runtime-workflows.md#11-behaviour-while-redis-is-down)) |
| Vehicles vanished from the map | Redis lost its data | restart the workers: the new leader rebuilds active dispatches from PostgreSQL |
| A unit looks stuck (DISPATCHED or ON_SCENE, no live vehicle) | lost terminal event or no leader | runbook query on `units` + `dispatches`; restart workers to rebuild; or cancel the incident |
| Signal stuck PREEMPTED | a claim from a vehicle that is gone | `$R HGETALL "divert:{delhi}:claims:<signal>"`; the next take-over clears stale signals |
| `divert_outbox_backlog` > 0 for minutes | commands not reaching Redis | `/readyz`, api logs `outbox relay round failed`, `SELECT id, attempts, last_error FROM outbox WHERE sent_at IS NULL` |
| History lagging (`divert_persister_lag_seconds`) | slow PostgreSQL or too few persisters | `docker compose up -d --scale persister=3` |
| DLQ not empty (`divert_dlq_messages_total`) | poison events, or a PostgreSQL outage longer than about 20 s ([workflow 9](07-runtime-workflows.md#9-persister)) | `uv run divert dlq`, fix the cause, `uv run divert dlq --replay` (safe: idempotent) |
| Predicted and actual travel times far apart at a scaled time | api and worker run different `DIVERT_SIM_TIME_SCALE` | set it for both and recreate both |
| `divert fetch-data` "refresh" changes nothing | cached Overpass responses reused | clear `.work/osm-cache` |
| Routes unchanged after switching OSRM | Redis route cache (1 h) | wait, or clear `divert:osrm:*` on a dev stack |
| Integration tests all skipped | PostgreSQL or Redis not reachable at `DIVERT_TEST_*` | `make infra`; check the URLs |
| Test vehicles flash on an open dashboard | pub/sub crosses Redis databases | close the dashboard while running integration tests, or use a separate Redis |
| WebSocket closes with 4404 and does not reconnect | city not in `DIVERT_CITIES` or not seeded | fix the config and re-seed |
| WebSocket keeps closing with 1013 | Redis down, more than 500 clients on a city, or a slow client | check Redis; the browser reconnects with backoff and re-snapshots |
| Map tiles blank or "API key required" | a tile provider that needs a key (CARTO now does) | default OpenStreetMap tiles are for development only; set `DIVERT_WEB_TILE_URL` and rebuild the frontend |
| `.env` value has no effect in hybrid mode | the backend does not read `.env` | `uv run --env-file ../.env ...` or export the variable |
| Shell script fails with `$'\r': command not found` | CRLF line endings | re-clone with `core.autocrlf=input`, or convert the file to LF |

`$R` is `docker compose exec -T redis redis-cli` and `$PSQL` is
`docker compose exec -T postgres psql -U divert -d divert -c` (runbook).

## Related

- [Specification](01-specification.md)
- [Architecture](03-architecture.md)
- [Data stores](05-data-stores.md)
- [Data flows](06-data-flows.md)
- [Runtime workflows](07-runtime-workflows.md)
- [Backend codebase](09-codebase-backend.md)
- [Frontend codebase](10-codebase-frontend.md)
- [Configuration](11-configuration.md)
- [Glossary](12-glossary.md)
- [../runbook.md](../runbook.md), [../deployment.md](../deployment.md), [../data-sources.md](../data-sources.md), [../../infra/osrm/README.md](../../infra/osrm/README.md)
