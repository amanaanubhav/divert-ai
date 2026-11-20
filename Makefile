# DIVERT developer entrypoints. Backend commands run through uv (Python 3.12).
SHELL := bash
BACKEND := backend
COMPOSE := docker compose

.PHONY: help up down infra migrate seed fetch-data api worker persister dlq lint fmt typecheck test test-unit test-integration test-network check build logs psql redis-cli clean

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-18s %s\n", $$1, $$2}'

infra: ## start postgres + redis (and wait until healthy)
	$(COMPOSE) up -d --wait postgres redis

up: ## full stack: postgres, redis, migrate, api, worker x2, persister, frontend
	$(COMPOSE) up -d --build

down: ## stop everything (volumes kept)
	$(COMPOSE) --profile osrm --profile observability down

migrate: infra ## alembic upgrade head + seed, against the compose postgres
	cd $(BACKEND) && uv run divert migrate

seed: ## idempotent reference-data seed
	cd $(BACKEND) && uv run divert seed

fetch-data: ## re-fetch OSM snapshots from Overpass (network; commit the JSON afterwards)
	cd $(BACKEND) && uv run divert fetch-data --cache-dir ../.work/osm-cache

api: ## run the api locally (reload)
	cd $(BACKEND) && uv run uvicorn divert.main:create_app --factory --reload

worker: ## run a simulation worker locally
	cd $(BACKEND) && uv run python -m divert.worker

persister: ## run a history persister locally
	cd $(BACKEND) && uv run python -m divert.persister

dlq: ## list dead-lettered history events
	cd $(BACKEND) && uv run divert dlq

lint: ## ruff lint + format check
	cd $(BACKEND) && uv run ruff check src tests scripts && uv run ruff format --check src tests scripts

fmt: ## auto-format
	cd $(BACKEND) && uv run ruff check --fix src tests scripts && uv run ruff format src tests scripts

typecheck: ## mypy --strict
	cd $(BACKEND) && uv run mypy

test-unit: ## unit tests (no external services)
	cd $(BACKEND) && uv run pytest tests/unit

test-integration: infra ## integration tests against real PostGIS + Redis
	cd $(BACKEND) && uv run pytest -m integration tests/integration

test-network: ## public OSRM smoke test (one real route per city)
	cd $(BACKEND) && uv run pytest -m network tests/network -s

test: test-unit test-integration ## unit + integration

check: lint typecheck test ## everything CI runs for the backend

build: ## build the backend and frontend images
	$(COMPOSE) build api frontend

logs: ## follow logs
	$(COMPOSE) logs -f --tail=100

psql: ## psql into the compose database
	$(COMPOSE) exec postgres psql -U divert -d divert

redis-cli: ## redis-cli into the compose redis
	$(COMPOSE) exec redis redis-cli

clean: ## remove caches
	find $(BACKEND) -name __pycache__ -prune -exec rm -rf {} + ; rm -rf $(BACKEND)/.mypy_cache $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache
