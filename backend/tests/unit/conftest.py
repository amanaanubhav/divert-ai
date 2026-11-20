"""Unit fixtures: fakeredis + a worker whose PostgreSQL touch-points are stubbed."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any

import fakeredis.aioredis
import pytest
from redis.asyncio import Redis

from divert.config import Settings
from divert.domain.models import City
from divert.services.eta import EtaModel
from divert.services.simulation import SimulationEngine
from divert.store import redis_store as keys
from divert.store.streams import xadd_json
from divert.worker import Worker
from tests.support import Clock, city


@pytest.fixture
async def fake_redis() -> AsyncIterator[Redis]:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.flushall()
    await r.aclose()


@pytest.fixture
def clock() -> Clock:
    return Clock()


class UnitWorker(Worker):
    """The real worker with PostgreSQL replaced by the committed city snapshots."""

    async def _load_cities(self) -> dict[str, City]:
        return {c: city(c) for c in self.settings.cities}

    async def _ended_in_pg(self, dispatch_ids: list[str]) -> set[str]:
        return set()

    async def _rebuild_from_pg(self, engine: SimulationEngine) -> int:
        return 0

    async def send(self, cmd: dict[str, Any]) -> str:
        return await xadd_json(self.redis, keys.STREAM_COMMANDS, cmd)

    async def run_until(
        self, clock: Clock, done: Callable[[], bool], *, max_ticks: int = 20_000, dt: float = 0.5
    ) -> int:
        n = 0
        while not done():
            clock.advance(dt)
            await self.process_commands()
            await self.tick_once()
            n += 1
            assert n < max_ticks, "simulation did not finish"
        return n


def unit_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "cities": ["delhi", "bengaluru"],
        "sim_tick_s": 0.5,
        "sim_time_scale": 1.0,
        "leader_lock_ttl_ms": 3000,
        "scene_time_s": 30.0,
        "metrics_port": 0,
        "log_json": False,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def make_worker(fake_redis: Redis, clock: Clock) -> Callable[..., UnitWorker]:
    def _make(name: str = "w1", eta_model: EtaModel | None = None, **overrides: Any) -> UnitWorker:
        def wall() -> datetime:
            return clock.now

        return UnitWorker(
            unit_settings(**overrides),
            fake_redis,
            None,  # type: ignore[arg-type] - never used (PG methods stubbed)
            eta_model=eta_model,
            instance_id=name,
            wall_clock=wall,
        )

    return _make


async def history(redis: Redis) -> list[dict[str, Any]]:
    entries = await redis.xrange(keys.STREAM_HISTORY)
    return [json.loads(f["data"]) for _, f in entries]


async def claim_keys(redis: Redis, city_id: str = "delhi") -> list[str]:
    out = []
    async for k in redis.scan_iter(match=f"divert:{{{city_id}}}:claims:*"):
        out.append(k)
    async for k in redis.scan_iter(match=f"divert:{{{city_id}}}:vclaims:*"):
        out.append(k)
    return sorted(out)


async def live_vehicles(redis: Redis, city_id: str = "delhi") -> dict[str, dict[str, Any]]:
    raw = await redis.hgetall(keys.vehicles(city_id))
    return {k: json.loads(v) for k, v in raw.items()}


async def active_signals(redis: Redis, city_id: str = "delhi") -> dict[str, dict[str, Any]]:
    raw = await redis.hgetall(keys.active_signals(city_id))
    return {k: json.loads(v) for k, v in raw.items()}
