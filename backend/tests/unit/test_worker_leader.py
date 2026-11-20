"""Leader lock and fail-over of the simulation worker (fakeredis)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from redis.asyncio import Redis

from divert.store import redis_store as keys
from divert.store.locks import LeaderLock
from tests.support import Clock, city, corridor_at, start_cmd, straight_route
from tests.unit.conftest import UnitWorker, claim_keys, history, live_vehicles


async def test_lock_acquire_renew_release(fake_redis: Redis) -> None:
    a = LeaderLock(fake_redis, "lock", ttl_ms=1000, owner="a")
    b = LeaderLock(fake_redis, "lock", ttl_ms=1000, owner="b")
    assert await a.acquire()
    assert a.is_held()
    assert not await b.acquire()
    assert not await b.renew()
    assert await a.renew()
    assert not await b.release()  # cannot release someone else's lock
    assert await a.current_owner() == a.token
    assert await a.release()
    assert not a.is_held()
    assert await b.acquire()


async def test_lease_expires_locally_before_the_key(fake_redis: Redis) -> None:
    t = [0.0]
    lock = LeaderLock(fake_redis, "lock", ttl_ms=1000, owner="a", clock=lambda: t[0])
    assert await lock.acquire()
    t[0] = 0.79
    assert lock.is_held()
    t[0] = 0.81  # ttl - ttl/5 margin: stop acting as leader before the key can expire
    assert not lock.is_held()


async def test_only_one_leader_and_standby_takes_over_from_redis_state(
    make_worker: Callable[..., UnitWorker], fake_redis: Redis, clock: Clock
) -> None:
    w1, w2 = make_worker("w1"), make_worker("w2")
    assert await w1.try_lead()
    assert not await w2.try_lead()
    assert w2.engine is None

    route = straight_route(city().center, n=60)
    cmd = start_cmd(route, corridor_at(route, [20, 40]))
    await w1.send(cmd)
    for _ in range(30):
        clock.advance(0.5)
        await w1.process_commands()
        await w1.tick_once()
    before = (await live_vehicles(fake_redis))["AMB-1"]
    assert before["along_m"] > 0

    # w1 dies without a goodbye: its lock key expires, w2 takes over
    await fake_redis.delete(keys.LEADER_LOCK)
    assert not await w1.lock.renew()
    assert await w2.try_lead()
    assert w2.engine is not None
    v = w2.engine.vehicles["delhi"]["AMB-1"]
    assert v.along_m == pytest.approx(before["along_m"], abs=0.01)  # resumed, not restarted
    assert v.dispatch_id == cmd["dispatch_id"]

    while (await live_vehicles(fake_redis)).get("AMB-1", {}).get("status") != "ON_SCENE":
        clock.advance(0.5)
        await w2.lock.renew()  # wall time passes while the machine is loaded
        await w2.tick_once()
    events = await history(fake_redis)
    assert [e["type"] for e in events].count("en_route") == 1  # not replayed on take-over
    assert [e["type"] for e in events].count("arrived") == 1
    assert await claim_keys(fake_redis) == []


async def test_new_leader_reclaims_commands_the_dead_leader_never_applied(
    make_worker: Callable[..., UnitWorker], fake_redis: Redis, clock: Clock
) -> None:
    w1 = make_worker("w1", leader_lock_ttl_ms=500)
    w2 = make_worker("w2", leader_lock_ttl_ms=500)
    assert await w1.try_lead()
    route = straight_route(city().center, n=10)
    cmd = start_cmd(route)
    await w1.send(cmd)
    assert await w1.process_commands() == 1  # read into w1's PEL ...
    # ... and w1 crashes before its tick commits the effects + ack
    await fake_redis.delete(keys.LEADER_LOCK)
    assert await w2.try_lead()
    await asyncio.sleep(0.6)  # the command must be idle > lock TTL before it is stolen
    assert await w2.lock.acquire()  # (the real leader loop renews every TTL/3)
    w2._last_claim_sweep = 0.0
    assert await w2.process_commands() == 1
    clock.advance(0.5)
    await w2.tick_once()
    assert "AMB-1" in await live_vehicles(fake_redis)
    pending = await fake_redis.xpending(keys.STREAM_COMMANDS, keys.GROUP_WORKER)
    assert pending["pending"] == 0


async def test_failed_tick_write_is_retried_merged_into_the_next(
    make_worker: Callable[..., UnitWorker],
    fake_redis: Redis,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError  # noqa: PLC0415

    import divert.worker as worker_mod  # noqa: PLC0415

    w = make_worker()
    assert await w.try_lead()
    route = straight_route(city().center, n=20)
    await w.send(start_cmd(route, corridor_at(route, [5])))
    await w.process_commands()
    real = worker_mod.write_city_batch
    calls = {"n": 0}

    async def flaky(*a: object, **k: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RedisConnectionError("blip")
        await real(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_mod, "write_city_batch", flaky)
    clock.advance(0.5)
    await w.tick_once()  # fails: kept pending
    assert "AMB-1" not in await live_vehicles(fake_redis)
    clock.advance(0.5)
    await w.tick_once()  # retried merged
    assert "AMB-1" in await live_vehicles(fake_redis)
    types = [e["type"] for e in await history(fake_redis)]
    assert types[:2] == ["en_route", "vehicle_position"]
