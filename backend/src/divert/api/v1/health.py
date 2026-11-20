"""Liveness, readiness and Prometheus metrics."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.exceptions import RedisError

from divert.api.deps import CtxDep
from divert.metrics import (
    ACTIVE_VEHICLES,
    HISTORY_PENDING,
    HISTORY_STREAM_LAG,
    WORKER_HEARTBEAT_AGE,
)
from divert.store import redis_store as keys
from divert.store.live import heartbeat_age_s
from divert.store.streams import group_lag

router = APIRouter(tags=["ops"])


@router.get("/healthz", summary="Liveness (process is up)")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness: PostgreSQL (Redis + worker reported as degraded)")
async def readyz(ctx: CtxDep) -> JSONResponse:
    """Ready iff PostgreSQL answers. Redis and the worker heartbeat are reported, and make
    the instance ``degraded``, but do not take it out of rotation: with Redis down the API
    still accepts dispatches (transactional outbox) and serves history/analytics from PG;
    only live views fail fast (503 / WS 1013) until Redis returns."""
    checks: dict[str, Any] = {}
    try:
        async with asyncio.timeout(2.0):
            async with ctx.engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"
    try:
        await ctx.guard.call(ctx.redis.ping)
        checks["redis"] = "ok"
    except RedisError as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
    worker: dict[str, Any] = {"status": "unknown"}
    if checks["redis"] == "ok":
        try:
            age, hb = await ctx.guard.call(lambda: heartbeat_age_s(ctx.redis))
            if age is None:
                worker = {"status": "missing"}
            else:
                fresh = age <= ctx.settings.heartbeat_max_age_s
                worker = {
                    "status": "ok" if fresh else "stale",
                    "heartbeat_age_s": round(age, 2),
                    "instance": hb.get("instance"),
                    "tick": int(float(hb.get("tick", 0))),
                    "vehicles": int(float(hb.get("vehicles", 0))),
                }
        except Exception as exc:
            worker = {"status": f"error: {type(exc).__name__}"}
    checks["worker"] = worker
    ready = checks["postgres"] == "ok"
    degraded = checks["redis"] != "ok" or worker.get("status") != "ok"
    body = {"ready": ready, "degraded": degraded, "checks": checks}
    return JSONResponse(body, status_code=200 if ready else 503)


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics(ctx: CtxDep) -> Response:
    with contextlib.suppress(Exception):  # metrics must render even if a source is down
        await asyncio.wait_for(_refresh(ctx), timeout=2.0)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def _refresh(ctx: CtxDep) -> None:
    await ctx.outbox.backlog()
    if ctx.guard.is_open:  # Redis is down: keep the last values, do not wait on it
        return
    age, _ = await heartbeat_age_s(ctx.redis)
    if age is not None:
        WORKER_HEARTBEAT_AGE.set(age)
    lag = await group_lag(ctx.redis, keys.STREAM_HISTORY, keys.GROUP_PERSISTER)
    if lag is not None:
        HISTORY_STREAM_LAG.set(lag[0])
        HISTORY_PENDING.set(lag[1])
    for city in ctx.settings.cities:
        ACTIVE_VEHICLES.labels(city=city).set(await ctx.redis.hlen(keys.vehicles(city)))
