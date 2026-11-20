"""Integration helpers: a deterministic router stub, the API under its lifespan, and a
pipeline driver that runs worker ticks + persister rounds against real PG + Redis."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.cities.loader import load_snapshot
from divert.config import Settings
from divert.domain.geo import LatLng, haversine_m
from divert.domain.route import Route
from divert.main import create_app
from divert.persister import Persister
from divert.services.routing import RoutingError
from divert.worker import Worker
from tests.support import route_through


class StubRouter:
    """Straight polyline station -> incident at 12 m/s (a routing stub, not map data)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def route(self, origin: LatLng, destination: LatLng) -> Route:
        self.calls += 1
        if self.fail:
            raise RoutingError("stub OSRM is down", kind="unavailable")
        return route_through([origin, destination], step_m=25.0, speed=12.0)

    async def alternatives(self, origin: LatLng, destination: LatLng) -> list[Route]:
        return [await self.route(origin, destination)]


def signal_near_center(city_id: str) -> LatLng:
    """A real junction (from the committed OSM snapshot) close to the city centre."""
    snap = load_snapshot(city_id)
    return min(snap.signals, key=lambda s: haversine_m(snap.city.center, s.pos)).pos


@asynccontextmanager
async def api(
    settings: Settings, redis: Redis, router: Any = None, **kw: Any
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    app = create_app(settings, router=router or StubRouter(), redis=redis, background=False, **kw)

    async def settle(_: Response) -> None:
        # post-commit XADD/PUBLISH run in the background; finish them before the test looks
        await app.state.ctx.dispatch.drain()

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://divert.test",
            event_hooks={"response": [settle]},
        ) as client,
    ):
        yield app, client


def new_worker(
    settings: Settings,
    redis: Redis,
    sm: async_sessionmaker[AsyncSession],
    clock: Clock,
    name: str,
    *,
    router: Any = None,
) -> Worker:
    """The real worker; its LegPlanner routes TRANSPORT / RETURN legs with ``router``
    (default: :class:`StubRouter`) against the real PostGIS (hospitals, corridors)."""
    return Worker(
        settings, redis, sm, instance_id=name, wall_clock=clock, router=router or StubRouter()
    )


@asynccontextmanager
async def not_an_ed(sm: async_sessionmaker[AsyncSession], station_id: str) -> AsyncIterator[None]:
    """Temporarily tag a hospital ``emergency=no`` (not eligible for TRANSPORT), so a unit
    based there takes its patient elsewhere and drives a real RETURN leg. Restored after."""
    async with sm() as s, s.begin():
        before = await s.scalar(
            sa.text("SELECT emergency FROM stations WHERE id = :s"), {"s": station_id}
        )
        await s.execute(
            sa.text("UPDATE stations SET emergency = 'no' WHERE id = :s"), {"s": station_id}
        )
    try:
        yield
    finally:
        async with sm() as s, s.begin():
            await s.execute(
                sa.text("UPDATE stations SET emergency = :e WHERE id = :s"),
                {"s": station_id, "e": before},
            )


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def drain_persister(p: Persister, *, rounds: int = 200) -> int:
    total = 0
    for _ in range(rounds):
        n = await p.process_once(block_ms=None)
        total += n
        if n == 0:
            break
    return total


async def run_pipeline(
    worker: Worker,
    persister: Persister,
    clock: Clock,
    *,
    until: Callable[[], bool],
    dt: float = 0.5,
    max_ticks: int = 20_000,
    persist_every: int = 50,
) -> int:
    """Drive the worker (commands + ticks, simulated time) and the persister."""
    ticks = 0
    while not until():
        clock.advance(dt)
        await worker.lock.renew()
        await worker.process_commands()
        await worker.tick_once()
        await asyncio.sleep(0)  # the LegPlanner's tasks run on this loop
        ticks += 1
        if ticks % persist_every == 0:
            await drain_persister(persister)
        assert ticks < max_ticks, "pipeline did not finish"
    await drain_persister(persister)
    return ticks


async def scalar(sm: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> Any:
    async with sm() as s:
        return await s.scalar(sa.text(sql), params)


async def rows(sm: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> list[Any]:
    async with sm() as s:
        return list((await s.execute(sa.text(sql), params)).all())
