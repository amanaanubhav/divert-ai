"""Unit fixtures: fakeredis + a worker whose PostgreSQL touch-points are stubbed."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
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
from tests.support import Clock, city, stub_leg_command


@pytest.fixture
async def fake_redis() -> AsyncIterator[Redis]:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.flushall()
    await r.aclose()


@pytest.fixture
def clock() -> Clock:
    return Clock()


class StubPlanner:
    """In-memory LegPlanner: answers every plan request with a straight stub leg through
    the real command stream (no OSRM, no PostgreSQL). ``hold`` keeps requests unanswered
    (a slow planner); ``release()`` answers them."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.requests: list[dict[str, Any]] = []
        self.reroutes: list[dict[str, Any]] = []
        self.cancelled: list[dict[str, Any]] = []
        self.hold = False
        self.corridor_every: int | None = None  # stub junctions on TRANSPORT / RETURN legs
        self._held: list[dict[str, Any]] = []
        self._tasks: set[asyncio.Task[Any]] = set()

    def submit(self, messages: Sequence[dict[str, Any]]) -> None:
        for m in messages:
            if m.get("type") == "plan_request":
                self.requests.append(m)
                if self.hold:
                    self._held.append(m)
                else:
                    self._answer(m)
            elif m.get("type") == "reroute_request":
                self.reroutes.append(m)
            elif m.get("type") == "plan_cancel":
                self.cancelled.append(m)

    def _answer(self, req: dict[str, Any]) -> None:
        task = asyncio.ensure_future(
            xadd_json(
                self.redis,
                keys.STREAM_COMMANDS,
                stub_leg_command(req, corridor_every=self.corridor_every),
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def release(self) -> None:
        held, self._held = self._held, []
        for m in held:
            self._answer(m)

    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


class UnitWorker(Worker):
    """The real worker with PostgreSQL replaced by the committed city snapshots and the
    LegPlanner by :class:`StubPlanner` (one per leadership term: ``self.planner``)."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, planner_factory=lambda w, _e: StubPlanner(w.redis), **kw)

    def stub(self) -> StubPlanner:
        assert isinstance(self.planner, StubPlanner)
        return self.planner

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
            await self.lock.renew()  # wall time passes while a long simulation runs
            await self.process_commands()
            await self.tick_once()
            await asyncio.sleep(0)  # let the stub planner answer
            n += 1
            assert n < max_ticks, "simulation did not finish"
        return n


def unit_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "cities": ["delhi", "bengaluru"],
        "sim_tick_s": 0.5,
        "sim_time_scale": 1.0,
        # long lease: these tests tick in a loop without the renew task, and a loaded
        # machine can take longer than a short TTL; take-over tests delete the key
        "leader_lock_ttl_ms": 60_000,
        "scene_time_s": 30.0,  # deprecated fallback: every service
        "hospital_handover_s": 30.0,
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
