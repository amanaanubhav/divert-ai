"""Redis-outage resilience of the API request path, the sim clock used for dispatch ETAs,
and the tick counter surviving a leader change (fakeredis; no external services)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from divert.domain.enums import ServiceType
from divert.services.dispatch import DispatchService
from divert.services.eta import EtaQuery, KinematicEtaModel, congestion_at
from divert.store import redis_store as keys
from divert.store.guard import RedisCircuitOpen, RedisGuard
from divert.store.live import last_tick_no, read_sim_clock
from tests.support import Clock, city, corridor_at, start_cmd, straight_route
from tests.unit.conftest import UnitWorker, unit_settings

# ------------------------------------------------------------------ RedisGuard


class _Tick:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def test_guard_bounds_a_hanging_call_and_then_short_circuits() -> None:
    now = _Tick()
    guard = RedisGuard(timeout_s=0.05, cooldown_s=5.0, name="t1", clock=now)
    calls = {"n": 0}

    async def hangs() -> str:
        calls["n"] += 1
        await asyncio.sleep(10)
        return "never"

    started = time.perf_counter()
    with pytest.raises(RedisTimeoutError):
        await guard.call(hangs)
    assert time.perf_counter() - started < 1.0  # bounded by timeout_s, not the socket
    assert guard.is_open
    assert guard.state == "open"

    with pytest.raises(RedisCircuitOpen):  # skipped without touching Redis
        await guard.call(hangs)
    assert calls["n"] == 1
    assert issubclass(RedisCircuitOpen, RedisError)  # callers keep `except RedisError`


async def test_guard_half_open_trial_closes_or_reopens() -> None:
    now = _Tick()
    guard = RedisGuard(timeout_s=1.0, cooldown_s=5.0, name="t2", clock=now)

    async def refused() -> None:
        raise ConnectionRefusedError("no redis")  # an OSError, like DNS failures

    async def ok() -> str:
        return "PONG"

    with pytest.raises(RedisConnectionError):  # OSError normalised to a RedisError
        await guard.call(refused)
    now.t = 4.9
    with pytest.raises(RedisCircuitOpen):
        await guard.call(ok)
    now.t = 5.1  # cooldown over: the next call is a trial
    assert guard.state == "half_open"
    with pytest.raises(RedisConnectionError):
        await guard.call(refused)
    assert guard.is_open  # failed trial re-opens for another cooldown
    now.t = 10.2
    assert await guard.call(ok) == "PONG"
    assert guard.state == "closed"
    assert guard.last_error is None


# ------------------------------------------------------------------ sim clock for ETAs


def _service(redis: Redis, **settings: Any) -> DispatchService:
    return DispatchService(
        settings=unit_settings(**settings),
        sessionmaker=None,  # type: ignore[arg-type]
        redis=redis,
        router=None,  # type: ignore[arg-type]
        eta_model=KinematicEtaModel(),
        reference=None,  # type: ignore[arg-type]
        outbox=None,  # type: ignore[arg-type]
        guard=RedisGuard(timeout_s=0.5, cooldown_s=5.0, name="t3"),
    )


def _hour_with_other_congestion(wall: datetime) -> datetime:
    """A sim instant (wall +/- whole hours) whose Delhi congestion differs most from wall's."""
    delhi = city("delhi")
    here = congestion_at(delhi, wall)
    return max(
        (wall + timedelta(hours=h) for h in range(1, 24)),
        key=lambda t: abs(congestion_at(delhi, t) - here),
    )


async def test_dispatch_eta_uses_the_sim_clock_when_time_is_scaled(fake_redis: Redis) -> None:
    wall = datetime(2026, 11, 17, 23, 1, tzinfo=UTC)  # 04:31 IST
    sim = _hour_with_other_congestion(wall)
    await fake_redis.set(keys.SIM_CLOCK, sim.timestamp())
    assert await read_sim_clock(fake_redis) == sim

    scaled = _service(fake_redis, sim_time_scale=10.0)
    assert await scaled.sim_clock(wall) == sim
    real_time = _service(fake_redis, sim_time_scale=1.0)
    assert await real_time.sim_clock(wall) == wall  # scale 1: sim clock == wall clock

    # ...and it matters: the same route gets a different ETA at the two hours
    delhi = city("delhi")
    route = straight_route(delhi.center, n=80, step_m=40.0, speed=12.0)
    corridor = corridor_at(route, [20, 40, 60])
    model = KinematicEtaModel()

    def eta(at: datetime) -> float:
        q = EtaQuery(route, ServiceType.AMBULANCE, delhi, corridor, 0.0, 0.0, at)
        return model.estimate(q).to_destination_s

    assert congestion_at(delhi, sim) != congestion_at(delhi, wall)
    assert eta(sim) != pytest.approx(eta(wall), rel=0.02)


async def test_sim_clock_falls_back_to_wall_time(fake_redis: Redis) -> None:
    wall = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    svc = _service(fake_redis, sim_time_scale=10.0)
    assert await svc.sim_clock(wall) == wall  # no leader has written the clock yet
    await fake_redis.set(keys.SIM_CLOCK, "garbage")
    assert await svc.sim_clock(wall) == wall
    await fake_redis.set(keys.SIM_CLOCK, (wall + timedelta(hours=3)).timestamp())
    svc.guard._trip("simulated outage")
    assert await svc.sim_clock(wall) == wall  # Redis down: never blocks or fails a dispatch


# ------------------------------------------------------------------ tick continuity


async def test_new_leader_resumes_the_tick_counter(
    make_worker: Callable[..., UnitWorker], fake_redis: Redis, clock: Clock
) -> None:
    w1, w2 = make_worker("w1"), make_worker("w2")
    assert await w1.try_lead()
    route = straight_route(city().center, n=40)
    await w1.send(start_cmd(route, corridor_at(route, [10, 20])))
    for _ in range(12):
        clock.advance(0.5)
        await w1.process_commands()
        await w1.tick_once()
    last = await last_tick_no(fake_redis, ["delhi", "bengaluru"])
    assert last == 12

    await fake_redis.delete(keys.LEADER_LOCK)  # w1 dies
    assert await w2.try_lead()
    clock.advance(0.5)
    batches = await w2.tick_once()
    ticks = {c: b.tick["tick"] for c, b in batches.items() if b.tick is not None}
    assert ticks
    assert all(t == last + 1 for t in ticks.values())
    tick_hash = await fake_redis.hgetall(keys.city_tick("delhi"))
    assert int(tick_hash["tick"]) == last + 1


# ------------------------------------------------------------------ log noise


async def test_abandoned_dns_lookup_is_one_warning_not_an_error_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import socket  # noqa: PLC0415

    from divert.logging import quiet_abandoned_dns_lookups  # noqa: PLC0415

    loop = asyncio.get_running_loop()
    seen: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _lp, ctx: seen.append(ctx))
    try:
        quiet_abandoned_dns_lookups(loop)
        handler = loop.get_exception_handler()
        assert handler is not None
        with caplog.at_level("WARNING", logger="divert.asyncio"):
            handler(
                loop,
                {
                    "message": "Future exception was never retrieved",
                    "exception": socket.gaierror(-2, "Name or service not known"),
                },
            )
        handler(loop, {"message": "something else", "exception": RuntimeError("x")})
    finally:
        loop.set_exception_handler(previous)
    assert [r.getMessage() for r in caplog.records] == ["abandoned DNS lookup failed"]
    assert [c["message"] for c in seen] == ["something else"]  # others still reported
