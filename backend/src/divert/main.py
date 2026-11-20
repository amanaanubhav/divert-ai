"""API application factory.

Run: ``uvicorn divert.main:create_app --factory --host 0.0.0.0 --port 8000``

The API is stateless (N replicas): reference data is cached per process, live state is
read from Redis (written only by the worker leader), history comes from PostgreSQL.
Each replica also runs the outbox relay (FOR UPDATE SKIP LOCKED makes that safe).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from divert import __version__
from divert.api.context import AppContext
from divert.api.errors import install_error_handlers
from divert.api.middleware import RequestContextMiddleware
from divert.api.v1 import health
from divert.api.v1.router import api_router
from divert.api.ws_hub import CityHub
from divert.config import Settings, get_settings
from divert.db.session import create_engine, create_sessionmaker
from divert.logging import configure_logging, quiet_abandoned_dns_lookups
from divert.services.dispatch import DispatchService, Router
from divert.services.eta import EtaModel, KinematicEtaModel
from divert.services.outbox import OutboxRelay
from divert.services.reference import ReferenceCache
from divert.services.routing import OsrmRouter
from divert.store.guard import RedisGuard
from divert.store.redis_store import create_redis


def create_app(
    settings: Settings | None = None,
    *,
    router: Router | None = None,
    redis: Redis | None = None,
    eta_model: EtaModel | None = None,
    background: bool = True,
) -> FastAPI:
    """``router`` / ``redis`` / ``eta_model`` can be injected (tests); ``background=False``
    disables the outbox relay task."""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging("api", cfg.log_level, json_logs=cfg.log_json)
        quiet_abandoned_dns_lookups(asyncio.get_running_loop())
        engine = create_engine(cfg, application_name="divert-api")
        sessionmaker = create_sessionmaker(engine)
        # request path: short timeouts, no retries, behind a circuit breaker
        rds = redis or create_redis(cfg, request_path=True)
        pubsub_rds = redis or create_redis(cfg)  # long-lived subscriptions (WS hub)
        guard = RedisGuard(
            timeout_s=cfg.redis_request_timeout_s, cooldown_s=cfg.redis_circuit_cooldown_s
        )
        osrm: OsrmRouter | None = None
        if router is None:
            osrm = OsrmRouter(
                cfg.osrm_url,
                timeout_s=cfg.osrm_timeout_s,
                retries=cfg.osrm_retries,
                cache=rds,
                cache_ttl_s=cfg.osrm_cache_ttl_s,
                cache_guard=guard,
            )
        model = eta_model or KinematicEtaModel()
        reference = ReferenceCache(sessionmaker, ttl_s=cfg.reference_cache_ttl_s)
        outbox = OutboxRelay(
            sessionmaker,
            rds,
            interval_s=cfg.outbox_relay_interval_s,
            min_age_s=cfg.outbox_relay_min_age_s,
            guard=guard,
        )
        app.state.ctx = AppContext(
            settings=cfg,
            engine=engine,
            sessionmaker=sessionmaker,
            redis=rds,
            guard=guard,
            eta_model=model,
            reference=reference,
            outbox=outbox,
            dispatch=DispatchService(
                settings=cfg,
                sessionmaker=sessionmaker,
                redis=rds,
                router=router or osrm,  # type: ignore[arg-type]
                eta_model=model,
                reference=reference,
                outbox=outbox,
                guard=guard,
            ),
            hub=CityHub(
                pubsub_rds, queue_size=cfg.ws_queue_size, max_clients=cfg.ws_max_clients_per_city
            ),
        )
        stop = asyncio.Event()
        relay = asyncio.create_task(outbox.run(stop)) if background else None
        try:
            yield
        finally:
            stop.set()
            if relay is not None:
                relay.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await relay
            await app.state.ctx.dispatch.drain()
            await app.state.ctx.hub.close()
            if osrm is not None:
                await osrm.aclose()
            if redis is None:
                await rds.aclose()
                await pubsub_rds.aclose()
            await engine.dispose()

    app = FastAPI(
        title="DIVERT",
        version=__version__,
        description="Real-time emergency-vehicle traffic-signal pre-emption (green corridor).",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(api_router)
    return app
