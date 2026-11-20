"""End to end on real PostGIS + Redis: API -> outbox -> worker -> history -> persister,
plus the outbox relay, persister idempotency/DLQ/retention and rebuild-from-PG."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.config import Settings
from divert.persister import Persister
from divert.services.outbox import OutboxRelay
from divert.store import redis_store as keys
from divert.store.live import read_vehicles
from divert.store.streams import FIELD
from divert.worker import Worker
from tests.integration.helpers import (
    Clock,
    api,
    drain_persister,
    new_worker,
    not_an_ed,
    rows,
    scalar,
    signal_near_center,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


def _settings(base: Settings, **kw: Any) -> Settings:
    defaults: dict[str, Any] = {
        "scene_time_s": 20.0,
        "hospital_handover_s": 20.0,
        "leader_lock_ttl_ms": 60_000,
        "persister_retry_idle_ms": 100,
        "persister_max_retries": 2,
    }
    return base.model_copy(update={**defaults, **kw})


async def _dispatch(client: Any, city: str, type_: str) -> dict[str, Any]:
    where = signal_near_center(city)
    r = await client.post(
        f"/api/v1/cities/{city}/incidents", json={"type": type_, "location": [where.lat, where.lng]}
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


async def _pg_status(sm: async_sessionmaker[AsyncSession], did: str) -> tuple[str, str, str | None]:
    r = (
        await rows(
            sm,
            "SELECT d.status, u.status AS unit_status, u.current_dispatch_id "
            "FROM dispatches d JOIN units u ON u.id = d.unit_id WHERE d.id = :d",
            d=did,
        )
    )[0]
    current = None if r.current_dispatch_id is None else str(r.current_dispatch_id)
    return str(r.status), str(r.unit_status), current


async def drive_until_back(
    worker: Worker,
    persister: Persister,
    clock: Clock,
    sm: async_sessionmaker[AsyncSession],
    did: str,
    *,
    max_ticks: int = 40_000,
) -> list[str]:
    """Drive commands + ticks + persister until the dispatch is COMPLETED in PG; returns
    every unit status seen meanwhile (the unit must stay busy until the very end)."""
    seen: list[str] = []
    ticks = 0
    while True:
        status, unit_status, current = await _pg_status(sm, did)
        if status in ("COMPLETED", "FAILED"):
            return seen
        assert current == did, f"unit freed before the trip ended ({status}, {unit_status})"
        seen.append(unit_status)
        for _ in range(50):
            clock.advance(0.5)
            await worker.lock.renew()
            await worker.process_commands()
            await worker.tick_once()
            await asyncio.sleep(0)  # the LegPlanner's tasks run on this loop
            ticks += 1
        await drain_persister(persister)
        assert ticks < max_ticks, f"trip did not end (dispatch {status})"


async def test_full_lifecycle_is_persisted(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """MEDICAL: RESPONSE -> scene -> TRANSPORT to a hospital -> handover -> RETURN -> back in
    service; every leg, the hospital and the pre-emptions persisted, unit freed at the end."""
    cfg = _settings(test_settings)
    clock = Clock()
    worker = new_worker(cfg, redis, sessionmaker, clock, "w-e2e")
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-e2e")
    await persister.setup()
    async with api(cfg, redis) as (_, client):
        created = await _dispatch(client, "delhi", "MEDICAL")
        clock.now = datetime.now(UTC)  # simulated time starts after the dispatch
        assert await worker.try_lead()
        iid, did = created["incident"]["id"], created["vehicle"]["dispatch_id"]
        # its own hospital is not an ED: the patient goes elsewhere, then a real RETURN leg
        async with not_an_ed(sessionmaker, created["vehicle"]["station"]["id"]):
            seen = await drive_until_back(worker, persister, clock, sessionmaker, did)
        assert set(seen) <= {"DISPATCHED", "ON_SCENE", "TRANSPORTING", "AT_HOSPITAL", "RETURNING"}
        assert "TRANSPORTING" in seen or "AT_HOSPITAL" in seen

        inc = (await rows(sessionmaker, "SELECT * FROM incidents WHERE id = :i", i=iid))[0]
        assert inc.status == "RESOLVED"
        assert inc.dispatched_at <= inc.en_route_at <= inc.on_scene_at
        assert inc.on_scene_at <= inc.transport_started_at <= inc.at_hospital_at
        assert inc.at_hospital_at <= inc.resolved_at
        hospital = (
            await rows(
                sessionmaker,
                "SELECT id, service, emergency, healthcare FROM stations WHERE id = :h",
                h=inc.hospital_station_id,
            )
        )[0]
        assert hospital.service == "AMBULANCE"
        assert hospital.emergency != "no"
        assert hospital.healthcare not in ("dentist", "blood_bank", "alternative", "doctor")
        d = (await rows(sessionmaker, "SELECT * FROM dispatches WHERE id = :d", d=did))[0]
        assert (d.status, d.end_reason) == ("COMPLETED", "CLEARED")
        assert d.actual_travel_s == pytest.approx(d.predicted_eta_s, rel=0.10)
        assert d.est_time_saved_s is not None
        unit = (
            await rows(
                sessionmaker,
                "SELECT status, current_dispatch_id FROM units WHERE id = :u",
                u=created["vehicle"]["id"],
            )
        )[0]
        assert (unit.status, unit.current_dispatch_id) == ("AVAILABLE", None)

        legs = await rows(
            sessionmaker,
            "SELECT seq, kind, emergency, destination_kind, destination_station_id, "
            "predicted_eta_s, actual_travel_s, end_reason, route_choice, hospital_choice, "
            "ST_Distance(origin, LAG(destination) OVER (ORDER BY seq)) AS gap_m "
            "FROM dispatch_legs WHERE dispatch_id = :d ORDER BY seq",
            d=did,
        )
        assert [(lg.seq, lg.kind, lg.emergency) for lg in legs] == [
            (0, "RESPONSE", True), (1, "TRANSPORT", True), (2, "RETURN", False),
        ]  # fmt: skip
        assert [lg.end_reason for lg in legs] == ["ARRIVED"] * 3
        assert [lg.destination_kind for lg in legs] == ["SCENE", "HOSPITAL", "STATION"]
        assert legs[1].destination_station_id == inc.hospital_station_id
        assert legs[2].destination_station_id == d.station_id
        assert all(lg.gap_m < 1.0 for lg in legs[1:]), "a leg starts where the last one ended"
        assert legs[1].hospital_choice["chosen"] == inc.hospital_station_id
        assert all(lg.route_choice for lg in legs)
        for lg in legs:  # the +-10 % predicted-vs-actual guarantee, on every leg kind
            assert lg.actual_travel_s == pytest.approx(lg.predicted_eta_s, rel=0.10), lg.kind

        pre = await rows(
            sessionmaker, "SELECT * FROM signal_preemptions WHERE dispatch_id = :d", d=did
        )
        assert pre, "the junction at the scene was pre-empted"
        assert all(p.released_at is not None and p.outcome for p in pre)
        assert {p.leg_seq for p in pre} <= {0, 1}, "the RETURN leg never claims"
        assert all(p.movement and p.lane and p.lanes_total for p in pre)
        n_pos = await scalar(
            sessionmaker, "SELECT count(*) FROM vehicle_positions WHERE dispatch_id = :d", d=did
        )
        assert n_pos >= int(d.actual_travel_s // cfg.position_sample_s)
        pos_legs = await rows(
            sessionmaker,
            "SELECT DISTINCT leg_seq FROM vehicle_positions WHERE dispatch_id = :d ORDER BY 1",
            d=did,
        )
        assert [r.leg_seq for r in pos_legs] == [0, 1, 2]
        timeline = [
            r.type
            for r in await rows(
                sessionmaker,
                "SELECT type FROM incident_events WHERE incident_id = :i ORDER BY ts, id",
                i=iid,
            )
        ]
        assert timeline[:3] == ["reported", "dispatched", "en_route"]
        order = [
            "arrived", "transport_started", "arrived_hospital", "handover_complete",
            "returning", "back_in_service",
        ]  # fmt: skip
        assert [timeline.index(t) for t in order] == sorted(timeline.index(t) for t in order)
        assert "cleared" not in timeline  # MEDICAL resolves at the handover
        assert timeline.count("leg_started") == 2  # TRANSPORT + RETURN (RESPONSE = en_route)
        assert timeline.count("leg_ended") == 3

        # read APIs over the history
        detail = (await client.get(f"/api/v1/incidents/{iid}")).json()
        assert detail["status"] == "RESOLVED"
        assert detail["dispatches"][0]["route"][0] == pytest.approx(created["vehicle"]["route"][0])
        assert detail["dispatches"][0]["status"] == "COMPLETED"
        assert [lg["kind"] for lg in detail["legs"]] == ["RESPONSE", "TRANSPORT", "RETURN"]
        assert all(lg["route_choice"] for lg in detail["legs"])
        assert all(abs(lg["eta_error_pct"]) <= 10.0 for lg in detail["legs"])
        assert detail["hospital"]["id"] == inc.hospital_station_id
        assert detail["transport_started_at"]
        assert detail["at_hospital_at"]
        assert len(detail["preemptions"]) == len(pre)
        assert [e["type"] for e in detail["timeline"]] == timeline
        track = (
            await client.get(f"/api/v1/incidents/{iid}/track", params={"max_points": 5})
        ).json()
        assert 2 <= len(track["points"]) <= 6
        assert track["points"][0]["ts"] < track["points"][-1]["ts"]
        summary = (await client.get("/api/v1/cities/delhi/analytics/summary")).json()
        assert summary["incidents"]["total"] == 1
        assert summary["incidents"]["by_status"]["RESOLVED"] == 1
        rt = summary["response_time_s"]["MEDICAL"]
        assert rt["count"] == 1
        assert rt["p50_s"] == pytest.approx(d.actual_travel_s, abs=5.0)
        assert summary["preemptions"]["total"] == len(pre)
        assert summary["est_time_saved_s"]["total"] == pytest.approx(d.est_time_saved_s)
        assert summary["transport_time_s"]["count"] == 1
        assert summary["call_to_hospital_s"]["count"] == 1
        assert summary["handover_to_back_in_service_s"]["count"] == 1
        assert summary["hospitals"][0]["station_id"] == inc.hospital_station_id
        assert {k: v["count"] for k, v in summary["legs"].items()} == {
            "RESPONSE": 1, "TRANSPORT": 1, "RETURN": 1,
        }  # fmt: skip
        assert summary["dispatches"]["by_status"]["COMPLETED"] == 1

        # replaying every history event changes nothing (idempotent persister)
        before = await rows(
            sessionmaker,
            "SELECT (SELECT count(*) FROM incident_events), (SELECT count(*) FROM signal_preemptions), (SELECT count(*) FROM vehicle_positions), (SELECT count(*) FROM dispatch_legs)",
        )
        for _, fields in await redis.xrange(keys.STREAM_HISTORY):
            await redis.xadd(keys.STREAM_HISTORY, {FIELD: fields[FIELD]})
        await drain_persister(persister)
        after = await rows(
            sessionmaker,
            "SELECT (SELECT count(*) FROM incident_events), (SELECT count(*) FROM signal_preemptions), (SELECT count(*) FROM vehicle_positions), (SELECT count(*) FROM dispatch_legs)",
        )
        assert before == after
        assert await redis.xlen(keys.STREAM_HISTORY_DLQ) == 0


async def test_cancel_frees_the_unit_after_the_worker_confirms(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Cancel en route: claims released, the vehicle brakes to a stop and drives back from
    there (never teleports); the unit is freed only when that RETURN leg ends."""
    cfg = _settings(test_settings)
    clock = Clock()
    worker = new_worker(cfg, redis, sessionmaker, clock, "w-cxl")
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-cxl")
    await persister.setup()
    async with api(cfg, redis) as (_, client):
        created = await _dispatch(client, "bengaluru", "CRIME")
        did = created["vehicle"]["dispatch_id"]
        assert await worker.try_lead()
        for _ in range(10):
            clock.advance(0.5)
            await worker.process_commands()
            await worker.tick_once()
        live = {v["id"]: v for v in await read_vehicles(redis, "bengaluru")}
        at_cancel = live[created["vehicle"]["id"]]
        assert at_cancel["speed_mps"] > 0
        r = await client.delete(f"/api/v1/cities/bengaluru/incidents/{created['incident']['id']}")
        assert r.status_code == 200
        assert r.json()["incident"]["status"] == "CANCELLED"
        again = await client.delete(
            f"/api/v1/cities/bengaluru/incidents/{created['incident']['id']}"
        )
        assert again.status_code == 200  # idempotent
        await drive_until_back(worker, persister, clock, sessionmaker, did)
    unit_id = created["vehicle"]["id"]
    assert (
        await scalar(sessionmaker, "SELECT status FROM units WHERE id = :u", u=unit_id)
        == "AVAILABLE"
    )
    d = (
        await rows(sessionmaker, "SELECT status, end_reason FROM dispatches WHERE id = :d", d=did)
    )[0]
    assert (d.status, d.end_reason) == ("COMPLETED", "CANCELLED")
    legs = await rows(
        sessionmaker,
        "SELECT seq, kind, end_reason, emergency, actual_travel_s, predicted_eta_s, "
        "ST_Distance(origin, ST_GeogFromText(:p)) AS from_cancel_m "
        "FROM dispatch_legs WHERE dispatch_id = :d ORDER BY seq",
        d=did,
        p=f"SRID=4326;POINT({at_cancel['pos'][1]} {at_cancel['pos'][0]})",
    )
    assert [(lg.seq, lg.kind, lg.end_reason) for lg in legs] == [
        (0, "RESPONSE", "CANCELLED"), (1, "RETURN", "ARRIVED"),
    ]  # fmt: skip
    assert not legs[1].emergency
    # the RETURN starts where the vehicle braked to a stop: a few metres on, not the station
    assert legs[1].from_cancel_m < 60.0
    assert legs[1].actual_travel_s == pytest.approx(legs[1].predicted_eta_s, rel=0.10)
    pre = await rows(
        sessionmaker, "SELECT outcome, released_at FROM signal_preemptions WHERE dispatch_id = :d",
        d=did,
    )  # fmt: skip
    assert all(p.released_at is not None for p in pre)
    assert await read_vehicles(redis, "bengaluru") == []
    assert (
        await scalar(
            sessionmaker, "SELECT status FROM incidents WHERE id = :i", i=created["incident"]["id"]
        )
        == "CANCELLED"
    )


async def test_outbox_relay_delivers_commands_when_redis_blipped(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Fast path fails (Redis unreachable at commit time) -> relay sends it later."""
    from pydantic import SecretStr  # noqa: PLC0415

    from divert.store.redis_store import create_redis  # noqa: PLC0415

    dead = create_redis(
        test_settings.model_copy(
            update={"redis_url": SecretStr("redis://127.0.0.1:1/0"), "redis_socket_timeout_s": 0.2}
        )
    )
    where = signal_near_center("delhi")
    try:
        async with api(test_settings, dead) as (_, client):
            r = await client.post(
                "/api/v1/cities/delhi/incidents",
                json={"type": "MEDICAL", "location": [where.lat, where.lng]},
            )
        assert r.status_code == 201, r.text  # the dispatch is durable in PG regardless
    finally:
        await dead.aclose()
    assert await scalar(sessionmaker, "SELECT count(*) FROM outbox WHERE sent_at IS NULL") == 1
    assert await redis.exists(keys.STREAM_COMMANDS) == 0
    relay = OutboxRelay(sessionmaker, redis, min_age_s=0.0)
    assert await relay.relay_once() == 1
    assert await relay.relay_once() == 0
    (entry,) = await redis.xrange(keys.STREAM_COMMANDS)
    cmd = json.loads(entry[1][FIELD])
    assert cmd["dispatch_id"] == r.json()["vehicle"]["dispatch_id"]
    assert await scalar(sessionmaker, "SELECT count(*) FROM outbox WHERE sent_at IS NULL") == 0
    assert await relay.backlog() == 0


async def test_concurrent_relays_send_each_row_once(
    redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as s, s.begin():
        for i in range(30):
            await s.execute(
                sa.text(
                    "INSERT INTO outbox (topic, payload) VALUES ('commands', CAST(:p AS jsonb))"
                ),
                {"p": json.dumps({"type": "cancel_dispatch", "v": 1, "city": "delhi", "n": i})},
            )
    relays = [OutboxRelay(sessionmaker, redis, min_age_s=0.0, batch=7) for _ in range(3)]
    for _ in range(6):
        await asyncio.gather(*(r.relay_once() for r in relays))
    ids = [json.loads(f[FIELD])["outbox_id"] for _, f in await redis.xrange(keys.STREAM_COMMANDS)]
    assert sorted(ids) == sorted(set(ids))
    assert len(ids) == 30


async def test_persister_dead_letters_poison_and_retries_failures(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    cfg = _settings(test_settings)
    p = Persister(cfg, redis, sessionmaker, instance_id="p-dlq")
    await p.setup()
    now = datetime.now(UTC).isoformat()
    ghost = str(uuid.uuid4())
    bad_ts = {
        "type": "en_route",
        "v": 1,
        "event_id": "enr:x",
        "ts": "yesterday",
        "city": "delhi",
        "incident_id": ghost,
        "dispatch_id": ghost,
    }
    fk_fail = {
        "type": "en_route",
        "v": 1,
        "event_id": "enr:ghost",
        "ts": now,
        "city": "delhi",
        "incident_id": ghost,
        "dispatch_id": ghost,
    }
    for ev in (bad_ts, fk_fail):
        await redis.xadd(keys.STREAM_HISTORY, {FIELD: json.dumps(ev)})
    await p.process_once()  # bad_ts -> DLQ at once; fk_fail -> pending (retry)
    assert await redis.xlen(keys.STREAM_HISTORY_DLQ) == 1
    for _ in range(5):
        await asyncio.sleep(0.15)
        await p.process_once()
    dlq = [json.loads(f[FIELD]) for _, f in await redis.xrange(keys.STREAM_HISTORY_DLQ)]
    assert len(dlq) == 2
    assert "bad event" in dlq[0]["reason"]
    assert dlq[1]["deliveries"] >= cfg.persister_max_retries
    assert (await redis.xpending(keys.STREAM_HISTORY, keys.GROUP_PERSISTER))["pending"] == 0
    assert await scalar(sessionmaker, "SELECT count(*) FROM incident_events") == 0


async def test_retention_prunes_old_positions_and_relayed_outbox(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    cfg = _settings(test_settings)
    async with api(cfg, redis) as (_, client):
        created = await _dispatch(client, "delhi", "FIRE")
    did = created["vehicle"]["dispatch_id"]
    old = datetime.now(UTC) - timedelta(days=cfg.position_retention_days + 1)
    async with sessionmaker() as s, s.begin():
        for i in range(3):
            await s.execute(
                sa.text(
                    "INSERT INTO vehicle_positions (dispatch_id, ts, geom, speed_mps, along_m) VALUES (:d, :ts, ST_GeogFromText('SRID=4326;POINT(77.2 28.6)'), 1, 0)"
                ),
                {"d": did, "ts": old + timedelta(seconds=i)},
            )
        await s.execute(
            sa.text(
                "INSERT INTO vehicle_positions (dispatch_id, ts, geom, speed_mps, along_m) VALUES (:d, now(), ST_GeogFromText('SRID=4326;POINT(77.2 28.6)'), 1, 0)"
            ),
            {"d": did},
        )
        await s.execute(sa.text("UPDATE outbox SET sent_at = now() - interval '30 days'"))
    p = Persister(cfg, redis, sessionmaker, instance_id="p-ret")
    assert await p.retention_once() == (3, 1)
    assert await scalar(sessionmaker, "SELECT count(*) FROM vehicle_positions") == 1


async def test_new_leader_rebuilds_from_postgres_when_redis_lost_live_state(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    cfg = _settings(test_settings)
    clock = Clock()
    w1 = new_worker(cfg, redis, sessionmaker, clock, "w-old")
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-rb")
    await persister.setup()
    async with api(cfg, redis) as (_, client):
        created = await _dispatch(client, "kolkata", "MEDICAL")
    did = created["vehicle"]["dispatch_id"]
    assert await w1.try_lead()
    for _ in range(40):  # 20 s of driving, 4+ positions persisted
        clock.advance(0.5)
        await w1.process_commands()
        await w1.tick_once()
    await drain_persister(persister)
    last_along = await scalar(
        sessionmaker,
        "SELECT along_m FROM vehicle_positions WHERE dispatch_id = :d ORDER BY ts DESC LIMIT 1",
        d=did,
    )
    assert last_along > 0

    # Redis loses every live key (e.g. restarted without AOF) and w1 is gone
    async for k in redis.scan_iter(match="divert:{kolkata}:*"):
        await redis.delete(k)
    await redis.delete(keys.LEADER_LOCK)
    w2 = new_worker(cfg, redis, sessionmaker, clock, "w-new")
    assert await w2.try_lead()
    assert w2.engine is not None
    v = w2.engine.vehicles["kolkata"][created["vehicle"]["id"]]
    assert v.dispatch_id == did
    assert v.along_m == pytest.approx(last_along, abs=0.01)  # resumed where PG last saw it

    await drive_until_back(w2, persister, clock, sessionmaker, did)
    assert (
        await scalar(
            sessionmaker, "SELECT status FROM incidents WHERE id = :i", i=created["incident"]["id"]
        )
        == "RESOLVED"
    )
    assert (
        await scalar(
            sessionmaker, "SELECT status FROM units WHERE id = :u", u=created["vehicle"]["id"]
        )
        == "AVAILABLE"
    )
