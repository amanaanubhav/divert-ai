"""Failure modes on real PostGIS + Redis: dispatch with Redis down, the sim clock behind
dispatch ETAs, and rebuilding a vehicle after a no-leader freeze at time scale != 1."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from divert.cities.loader import load_snapshot
from divert.config import Settings
from divert.main import create_app
from divert.persister import Persister
from divert.services.eta import EtaEstimate, EtaQuery, KinematicEtaModel, congestion_at
from divert.services.outbox import OutboxRelay
from divert.store import redis_store as keys
from divert.store.redis_store import create_redis
from tests.integration.helpers import (
    Clock,
    StubRouter,
    api,
    drain_persister,
    new_worker,
    scalar,
    signal_near_center,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

# A blackhole address: connects neither succeed nor get refused (they time out), like a
# Redis host that vanished. Port 1 on loopback covers the "refused" case.
UNREACHABLE = ("redis://10.255.255.1:6379/0", "redis://127.0.0.1:1/0")


class SpyEta:
    """The real ETA model, recording the instant each query was made for."""

    name = "kinematic-v1"

    def __init__(self) -> None:
        self.inner = KinematicEtaModel()
        self.at: list[datetime] = []

    def estimate(self, query: EtaQuery) -> EtaEstimate:
        self.at.append(query.at)
        return self.inner.estimate(query)


def _other_congestion_hour(city_id: str, wall: datetime) -> datetime:
    c = load_snapshot(city_id).city
    here = congestion_at(c, wall)
    return max(
        (wall + timedelta(hours=h) for h in range(1, 24)),
        key=lambda t: abs(congestion_at(c, t) - here),
    )


# ------------------------------------------------------------------ sim clock


async def test_dispatch_eta_is_predicted_at_the_simulation_clock(
    test_settings: Settings, redis: Redis
) -> None:
    cfg = test_settings.model_copy(update={"sim_time_scale": 10.0})
    wall = datetime.now(UTC)
    sim = _other_congestion_hour("delhi", wall)
    await redis.set(keys.SIM_CLOCK, sim.timestamp())  # what the worker leader writes
    spy = SpyEta()
    where = signal_near_center("delhi")
    async with api(cfg, redis, eta_model=spy) as (_, client):
        r = await client.post(
            "/api/v1/cities/delhi/incidents", json={"type": "MEDICAL", "location": list(where)}
        )
    assert r.status_code == 201, r.text
    assert spy.at
    assert all(at == sim for at in spy.at)
    delhi = load_snapshot("delhi").city
    assert congestion_at(delhi, sim) != congestion_at(delhi, wall)


# ------------------------------------------------------------------ Redis outage


@pytest.mark.parametrize("dead_url", UNREACHABLE)
async def test_dispatch_and_history_keep_working_while_redis_is_down(
    test_settings: Settings,
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    dead_url: str,
) -> None:
    cfg = test_settings.model_copy(
        update={"redis_url": SecretStr(dead_url), "redis_request_timeout_s": 0.3}
    )
    dead = create_redis(cfg, request_path=True)
    where = signal_near_center("delhi")
    try:
        async with api(cfg, dead) as (_, client):
            timings: list[float] = []
            created: list[dict[str, Any]] = []
            for _ in range(2):
                t0 = time.perf_counter()
                r = await client.post(
                    "/api/v1/cities/delhi/incidents",
                    json={"type": "MEDICAL", "location": [where.lat, where.lng]},
                )
                timings.append(time.perf_counter() - t0)
                assert r.status_code == 201, r.text
                created.append(r.json())
            ready = await client.get("/readyz")
            live = await client.get("/api/v1/cities/delhi/vehicles")
            listed = await client.get("/api/v1/cities/delhi/incidents")
            detail = await client.get(f"/api/v1/incidents/{created[0]['incident']['id']}")
    finally:
        await dead.aclose()
    # bounded by the request-path deadline (+ breaker), not socket timeouts x retries
    assert max(timings) < 3.0, timings
    # ready (PG answers) but degraded; Redis-only views fail fast with a clear 503
    assert ready.status_code == 200
    body = ready.json()
    assert body["ready"] is True
    assert body["degraded"] is True
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"].startswith("error")
    assert live.status_code == 503
    assert live.json()["error"]["code"] == "live_state_unavailable"
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 2
    assert detail.status_code == 200
    # the commands wait in the outbox and are relayed once Redis is back
    assert await scalar(sessionmaker, "SELECT count(*) FROM outbox WHERE sent_at IS NULL") == 2
    relay = OutboxRelay(sessionmaker, redis, min_age_s=0.0)
    assert await relay.relay_once() == 2
    assert await redis.xlen(keys.STREAM_COMMANDS) == 2


def test_websocket_is_refused_quickly_while_redis_is_down(test_settings: Settings) -> None:
    cfg = test_settings.model_copy(
        update={"redis_url": SecretStr(UNREACHABLE[1]), "redis_request_timeout_s": 0.3}
    )
    app = create_app(cfg, router=StubRouter(), background=False)
    with TestClient(app) as client:
        t0 = time.perf_counter()
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            client.websocket_connect("/api/v1/cities/delhi/stream") as ws,
        ):
            ws.receive_text()
        assert closed.value.code == 1013  # try again later (client reconnects w/ backoff)
        assert time.perf_counter() - t0 < 5.0


# ------------------------------------------------------------------ rebuild after a freeze


async def test_rebuild_after_a_no_leader_freeze_keeps_simulated_travel_time(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Scale 10: 2 ticks, a 60 s wall-clock gap with no leader (sim time frozen), 2 more
    ticks, then Redis loses the live state and a new leader rebuilds from PostgreSQL.
    The travel time must be the simulated driving time only - the wall-clock estimate
    would have added ~600 s (60 s x 10)."""
    cfg = test_settings.model_copy(
        update={
            "sim_time_scale": 10.0,
            "scene_time_s": 20.0,
            "leader_lock_ttl_ms": 60_000,
            "persister_retry_idle_ms": 100,
        }
    )
    dt_sim = cfg.sim_tick_s * cfg.sim_time_scale  # 5 s per tick
    clock = Clock()
    w1 = new_worker(cfg, redis, sessionmaker, clock, "w-old")
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-freeze")
    await persister.setup()
    where = signal_near_center("kolkata")
    async with api(cfg, redis) as (_, client):
        r = await client.post(
            "/api/v1/cities/kolkata/incidents", json={"type": "MEDICAL", "location": list(where)}
        )
    assert r.status_code == 201, r.text
    created = r.json()
    vid, did = created["vehicle"]["id"], created["vehicle"]["dispatch_id"]

    assert await w1.try_lead()
    for i in range(4):
        if i == 2:
            clock.advance(60.0)  # leader gone for 60 s wall: the simulation is frozen
        clock.advance(cfg.sim_tick_s)
        await w1.process_commands()
        await w1.tick_once()
    await drain_persister(persister)
    assert w1.engine is not None
    elapsed_before = w1.engine.vehicles["kolkata"][vid].elapsed_s
    assert elapsed_before == pytest.approx(4 * dt_sim)  # the 60 s gap added nothing
    persisted = await scalar(
        sessionmaker,
        "SELECT elapsed_s FROM vehicle_positions WHERE dispatch_id = :d ORDER BY ts DESC LIMIT 1",
        d=did,
    )
    assert persisted == pytest.approx(elapsed_before)

    # Redis loses every live key and w1 is gone: w2 rebuilds from PostgreSQL
    async for k in redis.scan_iter(match="divert:{kolkata}:*"):
        await redis.delete(k)
    await redis.delete(keys.LEADER_LOCK)
    w2 = new_worker(cfg, redis, sessionmaker, clock, "w-new")
    assert await w2.try_lead()
    assert w2.engine is not None
    v = w2.engine.vehicles["kolkata"][vid]
    assert v.elapsed_s == pytest.approx(elapsed_before)  # not (wall gap) x scale

    ticks = 0
    while v.status.value != "ON_SCENE":
        clock.advance(cfg.sim_tick_s)
        await w2.tick_once()
        ticks += 1
        assert ticks < 2_000
    assert v.travel_s == pytest.approx(elapsed_before + ticks * dt_sim)
    await drain_persister(persister)
    actual = await scalar(
        sessionmaker, "SELECT actual_travel_s FROM dispatches WHERE id = :d", d=did
    )
    assert actual == pytest.approx(v.travel_s, abs=0.01)
