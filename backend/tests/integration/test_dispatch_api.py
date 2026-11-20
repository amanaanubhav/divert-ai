"""POST/DELETE incidents against real PostGIS + Redis (OSRM stubbed)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.config import Settings
from divert.store import redis_store as keys
from tests.integration.helpers import StubRouter, api, rows, scalar, signal_near_center

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


async def _counts(sm: async_sessionmaker[AsyncSession]) -> tuple[int, int, int, int]:
    async with sm() as s:
        r = (
            await s.execute(
                sa.text(
                    "SELECT (SELECT count(*) FROM incidents), (SELECT count(*) FROM dispatches),"
                    " (SELECT count(*) FROM outbox), (SELECT count(*) FROM units WHERE status <> 'AVAILABLE')"
                )
            )
        ).one()
    return (r[0], r[1], r[2], r[3])


async def _commands(redis: Redis) -> list[dict[str, Any]]:
    return [json.loads(f["data"]) for _, f in await redis.xrange(keys.STREAM_COMMANDS)]


@pytest.mark.parametrize("reported", ["POLICE", "police", " Police ", "CRIME"])
async def test_police_alias_dispatches_a_police_unit(
    test_settings: Settings,
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    reported: str,
) -> None:
    where = signal_near_center("delhi")
    async with api(test_settings, redis) as (_, client):
        r = await client.post(
            "/api/v1/cities/delhi/incidents",
            json={"type": reported, "location": [where.lat, where.lng], "description": "test"},
        )
    assert r.status_code == 201, r.text
    body = r.json()
    inc, veh = body["incident"], body["vehicle"]
    assert inc["type"] == "CRIME"
    assert inc["reported_type"] == reported.strip()
    assert inc["status"] == "DISPATCHED"
    assert inc["location"] == pytest.approx([where.lat, where.lng])
    assert veh["service"] == "POLICE"
    assert veh["id"].startswith("PCR-")
    assert veh["route"][0] == pytest.approx(veh["station"]["location"])
    assert veh["eta_s"] > 0
    assert any(c["offset_m"] < 1.0 for c in veh["corridor"]), "the junction at the scene"
    # PG: unit locked to the dispatch, outbox row relayed, timeline started
    unit = (
        await rows(
            sessionmaker, "SELECT status, current_dispatch_id FROM units WHERE id = :u", u=veh["id"]
        )
    )[0]
    assert unit.status == "DISPATCHED"
    assert str(unit.current_dispatch_id) == veh["dispatch_id"]
    assert await scalar(sessionmaker, "SELECT count(*) FROM outbox WHERE sent_at IS NULL") == 0
    events = await rows(
        sessionmaker,
        "SELECT type FROM incident_events WHERE incident_id = :i ORDER BY id",
        i=inc["id"],
    )
    assert [e.type for e in events] == ["reported", "dispatched"]
    (cmd,) = await _commands(redis)
    assert cmd["type"] == "start_dispatch"
    assert cmd["dispatch_id"] == veh["dispatch_id"]
    assert cmd["vehicle_id"] == veh["id"]
    assert cmd["service"] == "POLICE"
    assert cmd["outbox_id"] >= 1


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"type": "BOGUS", "location": [28.61, 77.21]}, "invalid_incident"),
        ({"type": "", "location": [28.61, 77.21]}, "invalid_incident"),
        ({"type": "MEDICAL", "location": [19.07, 72.87]}, "invalid_incident"),  # Mumbai
        ({"type": "MEDICAL", "location": [95.0, 77.21]}, "validation_error"),
        ({"type": "MEDICAL", "location": [28.61]}, "validation_error"),
        ({"type": "MEDICAL", "location": [28.61, 77.21], "x": 1}, "validation_error"),
    ],
)
async def test_invalid_reports_return_422_and_create_nothing(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession],
    payload: dict[str, Any], code: str,
) -> None:  # fmt: skip
    router = StubRouter()
    before = await _counts(sessionmaker)
    async with api(test_settings, redis, router) as (_, client):
        r = await client.post("/api/v1/cities/delhi/incidents", json=payload)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code
    if code == "invalid_incident" and payload["type"] in ("BOGUS", ""):
        assert "POLICE" in r.json()["error"]["details"]["valid_types"]
    assert await _counts(sessionmaker) == before
    assert router.calls == 0
    assert await redis.exists(keys.STREAM_COMMANDS) == 0


async def test_routing_failure_returns_503_and_writes_nothing(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    before = await _counts(sessionmaker)
    where = signal_near_center("kolkata")
    async with api(test_settings, redis, StubRouter(fail=True)) as (_, client):
        r = await client.post(
            "/api/v1/cities/kolkata/incidents",
            json={"type": "FIRE", "location": [where.lat, where.lng]},
        )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "routing_unavailable"
    assert await _counts(sessionmaker) == before
    assert await redis.exists(keys.STREAM_COMMANDS) == 0


async def test_no_free_unit_returns_409(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as s, s.begin():
        await s.execute(
            sa.text(
                "UPDATE units SET status='OUT_OF_SERVICE' WHERE city_id='kolkata' AND service='FIRE_ENGINE'"
            )
        )
    before = await _counts(sessionmaker)
    where = signal_near_center("kolkata")
    async with api(test_settings, redis) as (_, client):
        r = await client.post(
            "/api/v1/cities/kolkata/incidents",
            json={"type": "fire", "location": [where.lat, where.lng]},
        )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_unit_available"
    assert (await _counts(sessionmaker))[:3] == before[:3]


async def test_concurrent_dispatches_never_share_a_unit(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """SKIP LOCKED under contention: 10 simultaneous reports at one spot."""
    where = signal_near_center("bengaluru")
    async with api(test_settings, redis) as (_, client):
        results = await asyncio.gather(
            *(
                client.post(
                    "/api/v1/cities/bengaluru/incidents",
                    json={"type": "MEDICAL", "location": [where.lat, where.lng]},
                )
                for _ in range(10)
            )
        )
    codes = sorted(r.status_code for r in results)
    assert set(codes) <= {201, 409}, codes
    units = [r.json()["vehicle"]["id"] for r in results if r.status_code == 201]
    assert len(units) >= 6
    assert len(units) == len(set(units)), "a unit was dispatched twice"
    busy = await scalar(sessionmaker, "SELECT count(*) FROM units WHERE status = 'DISPATCHED'")
    assert busy == len(units)
    assert await scalar(
        sessionmaker, "SELECT count(*) FROM dispatches WHERE ended_at IS NULL"
    ) == len(units)


async def test_cancel_writes_cancel_command_and_is_idempotent(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    where = signal_near_center("delhi")
    async with api(test_settings, redis) as (_, client):
        created = (
            await client.post(
                "/api/v1/cities/delhi/incidents",
                json={"type": "FIRE", "location": [where.lat, where.lng]},
            )
        ).json()
        iid = created["incident"]["id"]
        r1 = await client.delete(f"/api/v1/cities/delhi/incidents/{iid}")
        r2 = await client.delete(f"/api/v1/cities/delhi/incidents/{iid}")
        r404 = await client.delete(f"/api/v1/cities/delhi/incidents/{uuid.uuid4()}")
        wrong_city = await client.delete(f"/api/v1/cities/kolkata/incidents/{iid}")
    assert r1.status_code == 200
    assert r1.json()["incident"]["status"] == "CANCELLED"
    assert r2.status_code == 200
    assert r404.status_code == 404
    assert wrong_city.status_code == 404
    cmds = await _commands(redis)
    assert [c["type"] for c in cmds] == ["start_dispatch", "cancel_dispatch"]
    assert cmds[1]["dispatch_id"] == created["vehicle"]["dispatch_id"]
    # the unit stays busy until the worker confirms (persister frees it)
    unit = await scalar(
        sessionmaker, "SELECT status FROM units WHERE id = :u", u=created["vehicle"]["id"]
    )
    assert unit == "DISPATCHED"


async def test_rate_limit_and_api_key(test_settings: Settings, redis: Redis) -> None:
    from pydantic import SecretStr  # noqa: PLC0415

    cfg = test_settings.model_copy(update={"rate_limit_per_min": 2, "api_key": SecretStr("k")})
    body = {"type": "BOGUS", "location": [28.61, 77.21]}  # 422s still count
    async with api(cfg, redis) as (_, client):
        no_key = await client.post("/api/v1/cities/delhi/incidents", json=body)
        codes = [
            (
                await client.post(
                    "/api/v1/cities/delhi/incidents", json=body, headers={"X-API-Key": "k"}
                )
            ).status_code
            for _ in range(3)
        ]
        limited = await client.post(
            "/api/v1/cities/delhi/incidents", json=body, headers={"X-API-Key": "k"}
        )
        reads = await client.get("/api/v1/cities")
    assert no_key.status_code == 401
    assert codes == [422, 422, 429]
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert reads.status_code == 200  # reads are neither keyed nor limited
