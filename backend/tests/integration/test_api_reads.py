"""Read API, ops endpoints and the WebSocket stream (real PG + Redis)."""

from __future__ import annotations

import json
import time

import pytest
import redis as sync_redis
from redis.asyncio import Redis
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from divert.cities.loader import load_snapshot
from divert.config import Settings
from divert.main import create_app
from divert.store import redis_store as keys
from tests.integration.helpers import StubRouter, api, signal_near_center

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


async def test_cities_and_city_detail(test_settings: Settings, redis: Redis) -> None:
    async with api(test_settings, redis) as (_, client):
        cities = (await client.get("/api/v1/cities")).json()["cities"]
        detail = (await client.get("/api/v1/cities/kolkata")).json()
        missing = await client.get("/api/v1/cities/mumbai")
    assert [c["id"] for c in cities] == ["bengaluru", "delhi", "kolkata"]
    snap = load_snapshot("kolkata")
    assert detail["center"] == pytest.approx([snap.city.center.lat, snap.city.center.lng])
    assert len(detail["stations"]) == len(snap.stations)
    assert len(detail["signals"]) == len(snap.signals)
    assert all(s["free_units"] == s["total_units"] == 2 for s in detail["stations"])
    assert len(detail["traffic"]["hourly_congestion"]) == 24
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "city_not_found"


async def test_history_list_keyset_pagination_and_filters(
    test_settings: Settings, redis: Redis
) -> None:
    where = signal_near_center("delhi")
    loc = [where.lat, where.lng]
    async with api(test_settings, redis) as (_, client):
        ids = []
        for t in ("MEDICAL", "POLICE", "FIRE"):
            r = await client.post(
                "/api/v1/cities/delhi/incidents", json={"type": t, "location": loc}
            )
            ids.append(r.json()["incident"]["id"])
        await client.delete(f"/api/v1/cities/delhi/incidents/{ids[0]}")
        p1 = (await client.get("/api/v1/cities/delhi/incidents", params={"limit": 2})).json()
        p2 = (
            await client.get(
                "/api/v1/cities/delhi/incidents", params={"limit": 2, "cursor": p1["next_cursor"]}
            )
        ).json()
        crime = (
            await client.get("/api/v1/cities/delhi/incidents", params={"type": "police"})
        ).json()
        cancelled = (
            await client.get(
                "/api/v1/cities/delhi/incidents", params={"status": "CANCELLED,RESOLVED"}
            )
        ).json()
        future = (
            await client.get(
                "/api/v1/cities/delhi/incidents", params={"from": "2100-01-01T00:00:00Z"}
            )
        ).json()
        bad = await client.get("/api/v1/cities/delhi/incidents", params={"status": "NOPE"})
        bad_cursor = await client.get("/api/v1/cities/delhi/incidents", params={"cursor": "%%%"})
        other_city = (await client.get("/api/v1/cities/kolkata/incidents")).json()
    assert [i["id"] for i in p1["items"]] == ids[::-1][:2]  # newest first
    assert [i["id"] for i in p2["items"]] == [ids[0]]
    assert p2["next_cursor"] is None
    assert [i["type"] for i in crime["items"]] == ["CRIME"]
    assert [i["id"] for i in cancelled["items"]] == [ids[0]]
    assert future["items"] == []
    assert bad.status_code == 422
    assert bad_cursor.status_code == 422
    assert other_city["items"] == []


async def test_live_endpoints_read_worker_state(test_settings: Settings, redis: Redis) -> None:
    await redis.hset(
        keys.vehicles("delhi"),
        "AMB-x",
        json.dumps({"id": "AMB-x", "status": "EN_ROUTE", "pos": [28.6, 77.2]}),
    )
    await redis.set(
        keys.vehicle_route("delhi", "AMB-x"),
        json.dumps({"route": {"points": [[28.6, 77.2], [28.61, 77.21]]}, "corridor": []}),
    )
    await redis.hset(
        keys.active_signals("delhi"), "J1", json.dumps({"signal_id": "J1", "state": "PREEMPTED"})
    )
    async with api(test_settings, redis) as (_, client):
        vehicles = (
            await client.get("/api/v1/cities/delhi/vehicles", params={"include_route": True})
        ).json()
        signals = (await client.get("/api/v1/cities/delhi/signals/active")).json()
        empty = (await client.get("/api/v1/cities/kolkata/vehicles")).json()
    assert [v["id"] for v in vehicles["vehicles"]] == ["AMB-x"]
    assert vehicles["vehicles"][0]["route"] == [[28.6, 77.2], [28.61, 77.21]]
    assert signals["signals"] == [{"signal_id": "J1", "state": "PREEMPTED"}]
    assert empty["vehicles"] == []


async def test_readyz_metrics_healthz(test_settings: Settings, redis: Redis) -> None:
    async with api(test_settings, redis) as (_, client):
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        r1 = (await client.get("/readyz")).json()
        await redis.hset(
            keys.WORKER_HEARTBEAT,
            mapping={"ts": time.time(), "instance": "w1", "tick": 5, "vehicles": 0},
        )
        r2 = await client.get("/readyz")
        metrics = (await client.get("/metrics")).text
    assert r1["ready"] is True
    assert r1["checks"]["worker"]["status"] == "missing"
    assert r1["degraded"] is True
    assert r2.status_code == 200
    body = r2.json()
    assert body["checks"] == {**body["checks"], "postgres": "ok", "redis": "ok"}
    assert body["checks"]["worker"]["status"] == "ok"
    assert body["degraded"] is False
    for name in (
        "divert_outbox_backlog", "divert_http_requests_total", "divert_worker_heartbeat_age_seconds",
        "divert_dispatches_total", "divert_osrm_request_duration_seconds", "divert_tick_duration_seconds",
        "divert_persister_lag_seconds", "divert_active_vehicles", "divert_preemptions_total",
    ):  # fmt: skip
        assert name in metrics, name


def test_websocket_snapshot_then_live_messages(test_settings: Settings, redis: Redis) -> None:
    # (sync test: TestClient runs the app in its own loop; `redis` only flushed the db)
    r = sync_redis.Redis.from_url(test_settings.redis_url.get_secret_value(), decode_responses=True)
    r.hset(keys.vehicles("delhi"), "AMB-x", json.dumps({"id": "AMB-x", "status": "EN_ROUTE"}))
    r.set(
        keys.vehicle_route("delhi", "AMB-x"),
        json.dumps({"route": {"points": [[1, 2], [3, 4]]}, "corridor": [{"signal_id": "J"}]}),
    )
    r.hset(keys.active_signals("delhi"), "J", json.dumps({"signal_id": "J", "state": "PREEMPTED"}))
    r.hset(keys.city_tick("delhi"), mapping={"tick": 42, "sim_ts": "2026-11-17T00:00:00+00:00"})
    app = create_app(test_settings, router=StubRouter(), background=False)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/cities/delhi/stream") as ws:
                snap = ws.receive_json()
                assert (snap["type"], snap["v"], snap["tick"]) == ("snapshot", 1, 42)
                assert snap["vehicles"][0]["route"] == [[1, 2], [3, 4]]
                assert snap["vehicles"][0]["corridor"] == [{"signal_id": "J"}]
                assert snap["signals"] == [{"signal_id": "J", "state": "PREEMPTED"}]
                r.publish(
                    keys.events_channel("delhi"), json.dumps({"type": "tick", "v": 1, "tick": 43})
                )
                r.publish(
                    keys.events_channel("kolkata"), json.dumps({"type": "tick", "v": 1, "tick": 99})
                )
                r.publish(
                    keys.events_channel("delhi"),
                    json.dumps({"type": "vehicle_removed", "v": 1, "vehicle_id": "AMB-x"}),
                )
                assert ws.receive_json() == {"type": "tick", "v": 1, "tick": 43}
                assert ws.receive_json()["type"] == "vehicle_removed"
                ws.send_json({"type": "ping"})
                assert ws.receive_json() == {"type": "pong", "v": 1}
            with client.websocket_connect("/api/v1/cities/atlantis/stream") as ws:
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_json()
                assert closed.value.code == 4404
    finally:
        r.flushdb()
        r.close()
