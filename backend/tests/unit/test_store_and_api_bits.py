"""Small units: batch merging, event ids, rate limit, API key, error body, DLQ poison."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis

from divert.api.deps import require_api_key
from divert.api.errors import install_error_handlers
from divert.api.middleware import RequestContextMiddleware
from divert.api.ratelimit import check_rate_limit
from divert.persister import Persister
from divert.services.simulation import CityBatch
from divert.store import redis_store as keys
from divert.store.events import event_id
from divert.store.live import merge_batches, write_city_batch
from divert.store.streams import FIELD, ensure_group
from tests.unit.conftest import unit_settings


def test_merge_keeps_the_newest_value_of_every_field() -> None:
    old = CityBatch("delhi")
    old.vehicle_sets["A"] = {"v": 1}
    old.claim_sets[("S1", "A")] = {"c": 1}
    old.vclaim_adds["A"] = {"S1"}
    old.history.append({"event_id": "e1"})
    old.acks.append("1-0")
    new = CityBatch("delhi")
    new.vehicle_sets["A"] = {"v": 2}
    new.claim_dels.add(("S1", "A"))
    new.vclaim_rems["A"] = {"S1"}
    new.history.append({"event_id": "e2"})
    m = merge_batches(old, new)
    assert m.vehicle_sets["A"] == {"v": 2}
    assert ("S1", "A") not in m.claim_sets
    assert ("S1", "A") in m.claim_dels
    assert m.vclaim_adds["A"] == set()
    assert m.vclaim_rems["A"] == {"S1"}
    assert [e["event_id"] for e in m.history] == ["e1", "e2"]
    assert m.acks == ["1-0"]


async def test_write_city_batch_is_one_transaction_with_events_and_acks(fake_redis: Redis) -> None:
    await ensure_group(fake_redis, keys.STREAM_COMMANDS, keys.GROUP_WORKER)
    cmd_id = await fake_redis.xadd(keys.STREAM_COMMANDS, {FIELD: "{}"})
    await fake_redis.xreadgroup(keys.GROUP_WORKER, "w", {keys.STREAM_COMMANDS: ">"})
    b = CityBatch("delhi")
    b.vehicle_sets["A"] = {"id": "A"}
    b.claim_sets[("S1", "A")] = {"state": "PREEMPTED"}
    b.vclaim_adds["A"] = {"S1"}
    b.signal_sets["S1"] = {"signal_id": "S1", "state": "PREEMPTED"}
    b.history.append({"type": "en_route", "event_id": "enr:x"})
    b.acks.append(cmd_id)
    b.tick = {"type": "tick", "v": 1, "tick": 7, "sim_ts": "t"}
    await write_city_batch(fake_redis, b)
    assert json.loads(await fake_redis.hget(keys.vehicles("delhi"), "A")) == {"id": "A"}
    assert await fake_redis.smembers(keys.vehicle_claims("delhi", "A")) == {"S1"}
    assert await fake_redis.hkeys(keys.active_signals("delhi")) == ["S1"]
    assert (await fake_redis.xpending(keys.STREAM_COMMANDS, keys.GROUP_WORKER))["pending"] == 0
    assert await fake_redis.xlen(keys.STREAM_HISTORY) == 1
    assert (await fake_redis.hgetall(keys.city_tick("delhi")))["tick"] == "7"


def test_event_ids_are_deterministic_and_bounded() -> None:
    d = "0b8c6c62-55b4-4bb0-8a8b-0d6f3a7c5e21"
    assert event_id("arr", d) == "arr:0b8c6c6255b44bb08a8b0d6f3a7c5e21"
    assert event_id("arr", d) == event_id("arr", d)
    long = event_id("clm", d, "J" * 40)
    assert len(long) <= 64
    assert long == event_id("clm", d, "J" * 40)
    assert long != event_id("clm", d, "K" * 40)


async def test_fixed_window_rate_limit(fake_redis: Redis) -> None:
    results = [await check_rate_limit(fake_redis, "mutate", "1.2.3.4", 3) for _ in range(5)]
    assert [r[0] for r in results] == [True, True, True, False, False]
    assert results[0][1] == 2
    assert 1 <= results[-1][2] <= 60
    other = await check_rate_limit(fake_redis, "mutate", "5.6.7.8", 3)
    assert other[0] is True  # per client


def _app(api_key: str | None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    class Ctx:
        settings = unit_settings(api_key=SecretStr(api_key) if api_key else None)

    app.state.ctx = Ctx()

    @app.post("/thing", dependencies=[Depends(require_api_key)])
    async def thing() -> dict[str, Any]:
        return {"ok": True}

    return app


@pytest.mark.parametrize(
    ("configured", "sent", "status"),
    [(None, None, 200), ("s3cret", None, 401), ("s3cret", "nope", 401), ("s3cret", "s3cret", 200)],
)
async def test_api_key_on_mutations(configured: str | None, sent: str | None, status: int) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app(configured)), base_url="http://t") as c:
        headers = {"X-API-Key": sent} if sent else {}
        r = await c.post("/thing", headers={**headers, "X-Request-ID": "req-123"})
    assert r.status_code == status
    assert r.headers["x-request-id"] == "req-123"
    if status == 401:
        body = r.json()
        assert body["error"]["code"] == "unauthorized"
        assert body["request_id"] == "req-123"


async def test_poison_history_message_goes_to_dlq_without_touching_pg(fake_redis: Redis) -> None:
    p = Persister(unit_settings(), fake_redis, None, instance_id="p1")  # type: ignore[arg-type]
    await p.setup()
    await fake_redis.xadd(keys.STREAM_HISTORY, {FIELD: "{not json"})
    await fake_redis.xadd(keys.STREAM_HISTORY, {"other": "x"})
    assert await p.process_once() == 2
    assert await fake_redis.xlen(keys.STREAM_HISTORY_DLQ) == 2
    dead = json.loads((await fake_redis.xrange(keys.STREAM_HISTORY_DLQ))[0][1][FIELD])
    assert dead["source_stream"] == keys.STREAM_HISTORY
    assert dead["reason"].startswith("undecodable")
    assert (await fake_redis.xpending(keys.STREAM_HISTORY, keys.GROUP_PERSISTER))["pending"] == 0
