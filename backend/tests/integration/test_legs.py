"""Trip legs end to end on real PostGIS + Redis (design section 7): the real worker, its
LegPlanner (routing with a straight-line stub router, hospitals / corridors from PostGIS),
the persister and the API.

* FIRE / CRIME: RESPONSE then RETURN;
* cancel in every phase (409 during TRANSPORT / AT_HOSPITAL, idempotent once RETURNING);
* a leg planned while Redis is unreachable is delivered by the outbox relay, applied once;
* leader failover while a vehicle waits for its plan, and on the TRANSPORT leg;
* OSRM failure during TRANSPORT planning: retries, the vehicle waits in place;
* a patient taken to the unit's own hospital: no RETURN leg is needed - also when the
  hospital is mapped 40 m off the road, so every route to it ends at its road point.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.config import Settings
from divert.domain.geo import LatLng, haversine_m
from divert.persister import Persister
from divert.services.outbox import OutboxRelay
from divert.services.planner import LegPlanner
from divert.services.simulation import SimulationEngine
from divert.store import redis_store as keys
from divert.store.redis_store import create_redis
from divert.worker import EngineView, Worker
from tests.integration.helpers import (
    Clock,
    SnappingRouter,
    StubRouter,
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
        "scene_time_s": 20.0,  # deprecated fallback: every service
        "hospital_handover_s": 20.0,
        "leader_lock_ttl_ms": 60_000,
        "persister_retry_idle_ms": 100,
        "persister_max_retries": 2,
        "leg_plan_backoff_s": 0.05,  # real seconds: keep the retries quick in tests
        "leg_plan_backoff_max_s": 0.2,
    }
    return base.model_copy(update={**defaults, **kw})


async def _dispatch(client: Any, city: str, type_: str) -> dict[str, Any]:
    where = signal_near_center(city)
    r = await client.post(
        f"/api/v1/cities/{city}/incidents", json={"type": type_, "location": [where.lat, where.lng]}
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


async def _status(sm: async_sessionmaker[AsyncSession], table: str, id_: str) -> str:
    return str(await scalar(sm, f"SELECT status FROM {table} WHERE id = :i", i=id_))


async def drive(
    worker: Worker,
    persister: Persister,
    clock: Clock,
    *,
    until: Callable[[], Awaitable[bool]],
    every: int = 10,
    max_ticks: int = 40_000,
) -> int:
    """Commands + ticks + (every ``every`` ticks) the persister, until ``until()``."""
    ticks = 0
    while not await until():
        for _ in range(every):
            clock.advance(0.5)
            await worker.lock.renew()
            await worker.process_commands()
            await worker.tick_once()
            await asyncio.sleep(0)  # the LegPlanner's tasks run on this loop
            ticks += 1
        await drain_persister(persister)
        assert ticks < max_ticks, "the simulation did not get there"
    return ticks


def completed(sm: async_sessionmaker[AsyncSession], *dids: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        statuses = [await _status(sm, "dispatches", d) for d in dids]
        return all(st == "COMPLETED" for st in statuses)

    return check


def incident_is(
    sm: async_sessionmaker[AsyncSession], iid: str, status: str
) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return await _status(sm, "incidents", iid) == status

    return check


async def legs_of(sm: async_sessionmaker[AsyncSession], did: str) -> list[Any]:
    return await rows(
        sm,
        "SELECT seq, kind, emergency, end_reason, started_at, destination_station_id, "
        "predicted_eta_s, actual_travel_s, hospital_choice, "
        "ST_Distance(origin, LAG(destination) OVER (ORDER BY seq)) AS gap_m "
        "FROM dispatch_legs WHERE dispatch_id = :d ORDER BY seq",
        d=did,
    )


async def timeline(sm: async_sessionmaker[AsyncSession], iid: str) -> list[Any]:
    return await rows(
        sm,
        "SELECT type, payload FROM incident_events WHERE incident_id = :i ORDER BY ts, id",
        i=iid,
    )


async def live(redis: Redis, city: str, vehicle_id: str) -> dict[str, Any]:
    raw = await redis.hget(keys.vehicles(city), vehicle_id)
    return {} if raw is None else dict(json.loads(raw))


def _at(raw: Sequence[float]) -> LatLng:
    return LatLng(float(raw[0]), float(raw[1]))


class BlackHolePlanner:
    """A LegPlanner that never answers (its leader dies before planning anything)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def submit(self, messages: Sequence[dict[str, Any]]) -> None:
        self.requests.extend(m for m in messages if m.get("type") == "plan_request")

    async def close(self) -> None:
        return None


def dead_redis(cfg: Settings) -> Redis:
    return create_redis(
        cfg.model_copy(
            update={"redis_url": SecretStr("redis://127.0.0.1:1/0"), "redis_socket_timeout_s": 0.2}
        )
    )


def planner_without_redis(dead: Redis) -> Callable[[Worker, SimulationEngine], LegPlanner]:
    """The real LegPlanner whose Redis is unreachable: it plans into PostgreSQL (leg +
    outbox row) but cannot XADD the command."""

    def make(w: Worker, engine: SimulationEngine) -> LegPlanner:
        return LegPlanner(
            settings=w.settings,
            sessionmaker=w.sessionmaker,
            redis=dead,
            router=StubRouter(),
            view=EngineView(w, engine),
            outbox=OutboxRelay(w.sessionmaker, dead),
        )

    return make


# --------------------------------------------------------------------- FIRE / CRIME


async def test_fire_and_crime_respond_then_return(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    cfg = _settings(test_settings)
    clock = Clock()
    worker = new_worker(cfg, redis, sessionmaker, clock, "w-fc")
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-fc")
    await persister.setup()
    async with api(cfg, redis) as (_, client):
        fire = await _dispatch(client, "delhi", "FIRE")
        crime = await _dispatch(client, "bengaluru", "CRIME")
        clock.now = datetime.now(UTC)
        assert await worker.try_lead()
        dids = [fire["vehicle"]["dispatch_id"], crime["vehicle"]["dispatch_id"]]
        await drive(worker, persister, clock, until=completed(sessionmaker, *dids))

    for created in (fire, crime):
        iid, did = created["incident"]["id"], created["vehicle"]["dispatch_id"]
        legs = await legs_of(sessionmaker, did)
        assert [(lg.seq, lg.kind, lg.emergency, lg.end_reason) for lg in legs] == [
            (0, "RESPONSE", True, "ARRIVED"), (1, "RETURN", False, "ARRIVED"),
        ]  # fmt: skip
        assert legs[1].destination_station_id == created["vehicle"]["station"]["id"]
        assert legs[1].gap_m < 1.0, "the RETURN starts at the scene"
        for lg in legs:
            assert lg.actual_travel_s == pytest.approx(lg.predicted_eta_s, rel=0.10), lg.kind
        assert await _status(sessionmaker, "incidents", iid) == "RESOLVED"
        d = (await rows(sessionmaker, "SELECT * FROM dispatches WHERE id = :d", d=did))[0]
        assert (d.status, d.end_reason) == ("COMPLETED", "CLEARED")
        assert await _status(sessionmaker, "units", created["vehicle"]["id"]) == "AVAILABLE"
        types = [e.type for e in await timeline(sessionmaker, iid)]
        order = ["arrived", "cleared", "returning", "back_in_service"]
        assert [types.index(t) for t in order] == sorted(types.index(t) for t in order)
        assert "transport_started" not in types
        pre = await rows(
            sessionmaker, "SELECT leg_seq, released_at FROM signal_preemptions WHERE "
            "dispatch_id = :d", d=did,
        )  # fmt: skip
        assert all(p.leg_seq == 0 and p.released_at is not None for p in pre)
    assert await redis.hlen(keys.vehicles("delhi")) == 0
    assert await redis.hlen(keys.vehicles("bengaluru")) == 0


# --------------------------------------------------------------------------- cancel


async def test_cancel_refused_with_a_patient_on_board_and_idempotent_when_returning(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    cfg = _settings(test_settings)
    clock = Clock()
    worker = new_worker(cfg, redis, sessionmaker, clock, "w-cx")
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-cx")
    await persister.setup()
    async with api(cfg, redis) as (_, client):
        created = await _dispatch(client, "delhi", "MEDICAL")
        iid, did = created["incident"]["id"], created["vehicle"]["dispatch_id"]
        url = f"/api/v1/cities/delhi/incidents/{iid}"
        clock.now = datetime.now(UTC)
        assert await worker.try_lead()
        async with not_an_ed(sessionmaker, created["vehicle"]["station"]["id"]):
            for phase in ("TRANSPORTING", "AT_HOSPITAL"):
                await drive(
                    worker, persister, clock, until=incident_is(sessionmaker, iid, phase), every=4
                )
                r = await client.delete(url)
                assert r.status_code == 409, r.text
                assert r.json()["error"]["code"] == "cannot_cancel_transport"
                assert r.json()["error"]["details"]["status"] == phase
                assert await _status(sessionmaker, "incidents", iid) == phase
            # handover done: the incident is RESOLVED, the unit drives back (RETURNING)
            await drive(
                worker, persister, clock, until=incident_is(sessionmaker, iid, "RESOLVED"), every=4
            )
            assert await _status(sessionmaker, "units", created["vehicle"]["id"]) == "RETURNING"
            r = await client.delete(url)
            assert r.status_code == 200, r.text
            assert r.json()["incident"]["status"] == "RESOLVED"  # nothing changed
            await drive(worker, persister, clock, until=completed(sessionmaker, did))
    types = [e.type for e in await timeline(sessionmaker, iid)]
    assert "cancel_requested" not in types
    assert "cancelled" not in types
    d = await rows(sessionmaker, "SELECT status, end_reason FROM dispatches WHERE id = :d", d=did)
    assert (d[0].status, d[0].end_reason) == ("COMPLETED", "CLEARED")
    legs = await legs_of(sessionmaker, did)
    assert [lg.kind for lg in legs] == ["RESPONSE", "TRANSPORT", "RETURN"]
    assert await scalar(sessionmaker, "SELECT count(*) FROM outbox WHERE payload->>'type' = "
                        "'cancel_dispatch'") == 0  # fmt: skip


async def test_cancel_on_scene_drives_back_from_the_scene(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """FIRE and MEDICAL (TRANSPORT pending) cancelled while on scene: RETURN from the scene,
    incident CANCELLED, never cleared / transported; the unit is freed at the station."""
    cfg = _settings(test_settings)
    clock = Clock()
    worker = new_worker(cfg, redis, sessionmaker, clock, "w-cs")
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-cs")
    await persister.setup()
    async with api(cfg, redis) as (_, client):
        fire = await _dispatch(client, "bengaluru", "FIRE")
        medical = await _dispatch(client, "kolkata", "MEDICAL")
        clock.now = datetime.now(UTC)
        assert await worker.try_lead()
        waiting = {fire["incident"]["id"]: "bengaluru", medical["incident"]["id"]: "kolkata"}

        async def cancel_each_on_scene() -> bool:
            """Cancel every incident the moment PostgreSQL shows it ON_SCENE."""
            for iid, city in list(waiting.items()):
                if await _status(sessionmaker, "incidents", iid) == "ON_SCENE":
                    r = await client.delete(f"/api/v1/cities/{city}/incidents/{iid}")
                    assert r.status_code == 200, r.text
                    assert r.json()["incident"]["status"] == "CANCELLED"
                    del waiting[iid]
            return not waiting

        await drive(worker, persister, clock, until=cancel_each_on_scene, every=4)
        dids = [fire["vehicle"]["dispatch_id"], medical["vehicle"]["dispatch_id"]]
        await drive(worker, persister, clock, until=completed(sessionmaker, *dids))
    for created in (fire, medical):
        iid, did = created["incident"]["id"], created["vehicle"]["dispatch_id"]
        assert await _status(sessionmaker, "incidents", iid) == "CANCELLED"
        d = (await rows(sessionmaker, "SELECT end_reason FROM dispatches WHERE id = :d", d=did))[0]
        assert d.end_reason == "CANCELLED"
        assert await _status(sessionmaker, "units", created["vehicle"]["id"]) == "AVAILABLE"
        legs = await legs_of(sessionmaker, did)
        driven = [lg for lg in legs if lg.started_at is not None]
        assert next((lg.kind, lg.end_reason) for lg in driven) == ("RESPONSE", "ARRIVED")
        # never transported; a TRANSPORT already planned is superseded, never started
        assert all(lg.end_reason == "SUPERSEDED" for lg in legs if lg.kind == "TRANSPORT")
        types = [e.type for e in await timeline(sessionmaker, iid)]
        assert "cleared" not in types
        assert "transport_started" not in types
        assert "back_in_service" in types
        if len(driven) > 1:  # (no RETURN leg when the scene is at the unit's own station)
            assert (driven[1].kind, driven[1].end_reason) == ("RETURN", "ARRIVED")
            ret = next(lg for lg in legs if lg.kind == "RETURN")
            scene = await rows(
                sessionmaker,
                "SELECT ST_Distance(l.origin, i.geom) AS m FROM dispatch_legs l JOIN dispatches d "
                "ON d.id = l.dispatch_id JOIN incidents i ON i.id = d.incident_id "
                "WHERE l.dispatch_id = :d AND l.seq = :s",
                d=did,
                s=ret.seq,
            )
            assert scene[0].m < 1.0, "the RETURN starts at the scene"


# ------------------------------------------------------------ outbox / Redis down


async def test_leg_plan_outbox_recovery_with_redis_down(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The planner commits the leg + its outbox row but cannot XADD (Redis unreachable for
    it): the vehicle waits in place ("planning route") until the API's outbox relay
    delivers the row; the leader applies it exactly once."""
    cfg = _settings(test_settings)
    clock = Clock()
    dead = dead_redis(cfg)
    worker = Worker(
        cfg, redis, sessionmaker, instance_id="w-ob", wall_clock=clock,
        planner_factory=planner_without_redis(dead),
    )  # fmt: skip
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-ob")
    await persister.setup()
    try:
        async with api(cfg, redis) as (_, client):
            created = await _dispatch(client, "delhi", "FIRE")
            iid, did, vid = (
                created["incident"]["id"],
                created["vehicle"]["dispatch_id"],
                created["vehicle"]["id"],
            )
            clock.now = datetime.now(UTC)
            assert await worker.try_lead()

            async def planned() -> bool:
                n = await scalar(
                    sessionmaker,
                    "SELECT count(*) FROM dispatch_legs WHERE dispatch_id = :d AND seq = 1",
                    d=did,
                )
                return bool(n)

            await drive(worker, persister, clock, until=planned, every=4)
            box = await rows(
                sessionmaker,
                "SELECT id, sent_at FROM outbox WHERE payload->>'type' = 'leg_planned' "
                "AND payload->>'dispatch_id' = :d",
                d=did,
            )
            assert len(box) == 1
            assert box[0].sent_at is None, "Redis is down for the planner: not delivered"

            # the dwell ends; the vehicle waits at the scene for the plan
            await drive(worker, persister, clock, until=incident_is(sessionmaker, iid, "RESOLVED"))
            for _ in range(60):
                clock.advance(0.5)
                await worker.process_commands()
                await worker.tick_once()
                await asyncio.sleep(0.005)
            v = await live(redis, "delhi", vid)
            assert v["planning"] is True
            assert v["status"] == "ON_SCENE"
            assert v["speed_mps"] == 0.0
            assert haversine_m(_at(v["pos"]), _at(created["incident"]["location"])) < 1.0

            relay = OutboxRelay(sessionmaker, redis, min_age_s=0.0)
            assert await relay.relay_once() >= 1  # Redis is fine for the API: delivered
            await drive(worker, persister, clock, until=completed(sessionmaker, did))
        types = [e.type for e in await timeline(sessionmaker, iid)]
        assert types.count("leg_planned") == 1
        assert types.count("leg_started") == 1  # the RETURN leg, applied once
        legs = await legs_of(sessionmaker, did)
        assert [(lg.kind, lg.end_reason) for lg in legs] == [
            ("RESPONSE", "ARRIVED"), ("RETURN", "ARRIVED"),
        ]  # fmt: skip
        sent = await scalar(sessionmaker, "SELECT sent_at FROM outbox WHERE id = :i", i=box[0].id)
        assert sent is not None
        for _ in range(100):  # the planner's own retry loop stops (the relay sent it)
            if worker.planner is not None and not getattr(worker.planner, "busy", 0):
                break
            await asyncio.sleep(0.05)
        assert worker.planner is not None
        assert getattr(worker.planner, "busy", 0) == 0
    finally:
        await worker.shutdown()
        await dead.aclose()


# ------------------------------------------------------------------------- failover


async def test_failover_while_waiting_for_a_plan(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """FIRE: the old leader dies before planning the RETURN (the vehicle waits at the
    scene); MEDICAL: the old leader planned the TRANSPORT into PostgreSQL but died before
    sending it. The new leader re-issues both requests; the second is answered from the
    PostgreSQL row (same hospital, no second plan)."""
    cfg = _settings(test_settings)
    clock = Clock()
    dead = dead_redis(cfg)
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-fo")
    await persister.setup()
    w1 = Worker(
        cfg, redis, sessionmaker, instance_id="w-fo1", wall_clock=clock,
        planner_factory=planner_without_redis(dead),
    )  # fmt: skip
    try:
        async with api(cfg, redis) as (_, client):
            medical = await _dispatch(client, "kolkata", "MEDICAL")
            clock.now = datetime.now(UTC)
            assert await w1.try_lead()
            mdid = medical["vehicle"]["dispatch_id"]

            async def transport_planned() -> bool:
                n = await scalar(
                    sessionmaker,
                    "SELECT count(*) FROM dispatch_legs WHERE dispatch_id = :d AND seq = 1",
                    d=mdid,
                )
                return bool(n)

            await drive(w1, persister, clock, until=transport_planned, every=4)
            stored_hospital = await scalar(
                sessionmaker,
                "SELECT destination_station_id FROM dispatch_legs WHERE dispatch_id = :d "
                "AND seq = 1",
                d=mdid,
            )
            await w1.shutdown()  # (its planner never delivered the command)

            # a second leader term whose planner never answers: FIRE waits for its RETURN
            w2 = Worker(
                cfg, redis, sessionmaker, instance_id="w-fo2", wall_clock=clock,
                planner_factory=lambda _w, _e: BlackHolePlanner(),
            )  # fmt: skip
            assert await w2.try_lead()
            fire = await _dispatch(client, "delhi", "FIRE")
            fiid, fdid = fire["incident"]["id"], fire["vehicle"]["dispatch_id"]
            await drive(
                w2, persister, clock, until=incident_is(sessionmaker, fiid, "RESOLVED"), every=4
            )
            v = await live(redis, "delhi", fire["vehicle"]["id"])
            assert v["pending"]["kind"] == "RETURN"
            assert v["status"] == "ON_SCENE"
            # w2 dies without a goodbye (no lock release): the lock key expires
            await redis.delete(keys.LEADER_LOCK)

            w3 = new_worker(cfg, redis, sessionmaker, clock, "w-fo3")
            assert await w3.try_lead()
            await drive(w3, persister, clock, until=completed(sessionmaker, mdid, fdid))
        mlegs = await legs_of(sessionmaker, mdid)
        assert [lg.kind for lg in mlegs][:2] == ["RESPONSE", "TRANSPORT"]
        assert mlegs[1].destination_station_id == stored_hospital  # the PG plan, re-sent
        assert sum(1 for lg in mlegs if lg.kind == "TRANSPORT") == 1
        flegs = await legs_of(sessionmaker, fdid)
        assert [(lg.kind, lg.end_reason) for lg in flegs] == [
            ("RESPONSE", "ARRIVED"), ("RETURN", "ARRIVED"),
        ]  # fmt: skip
        for did, iid in ((mdid, medical["incident"]["id"]), (fdid, fiid)):
            types = [e.type for e in await timeline(sessionmaker, iid)]
            assert types.count("back_in_service") == 1
            assert len([t for t in types if t == "leg_started"]) == len(
                [lg for lg in await legs_of(sessionmaker, did) if lg.seq > 0]
            )
    finally:
        await dead.aclose()


async def test_failover_on_transport_rebuilds_from_postgres(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The leader dies on the TRANSPORT leg AND Redis loses the live state: the new leader
    rebuilds the vehicle on leg 1 from PostgreSQL; every claim is released in the end."""
    cfg = _settings(test_settings)
    clock = Clock()
    w1 = new_worker(cfg, redis, sessionmaker, clock, "w-tr1")
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-tr")
    await persister.setup()
    async with api(cfg, redis) as (_, client):
        created = await _dispatch(client, "delhi", "MEDICAL")
        iid, did, vid = (
            created["incident"]["id"],
            created["vehicle"]["dispatch_id"],
            created["vehicle"]["id"],
        )
        clock.now = datetime.now(UTC)
        assert await w1.try_lead()
        async with not_an_ed(sessionmaker, created["vehicle"]["station"]["id"]):
            await drive(
                w1, persister, clock, until=incident_is(sessionmaker, iid, "TRANSPORTING"), every=4
            )
            for _ in range(30):  # some way along the TRANSPORT leg, positions persisted
                clock.advance(0.5)
                await w1.process_commands()
                await w1.tick_once()
            await drain_persister(persister)
            along = await scalar(
                sessionmaker,
                "SELECT along_m FROM vehicle_positions WHERE dispatch_id = :d AND leg_seq = 1 "
                "ORDER BY ts DESC LIMIT 1",
                d=did,
            )
            assert along is not None
            assert along > 0
            # Redis loses every live key of the city and w1 is gone
            async for k in redis.scan_iter(match="divert:{delhi}:*"):
                await redis.delete(k)
            await redis.delete(keys.LEADER_LOCK)
            w2 = new_worker(cfg, redis, sessionmaker, clock, "w-tr2")
            assert await w2.try_lead()
            assert w2.engine is not None
            v = w2.engine.vehicles["delhi"][vid]
            assert (v.leg_seq, v.leg_kind.value, v.phase.value) == (1, "TRANSPORT", "TRANSPORTING")
            assert v.along_m == pytest.approx(along, abs=0.01)
            await drive(w2, persister, clock, until=completed(sessionmaker, did))
    legs = await legs_of(sessionmaker, did)
    assert [(lg.kind, lg.end_reason) for lg in legs] == [
        ("RESPONSE", "ARRIVED"), ("TRANSPORT", "ARRIVED"), ("RETURN", "ARRIVED"),
    ]  # fmt: skip
    assert await _status(sessionmaker, "incidents", iid) == "RESOLVED"
    assert await _status(sessionmaker, "units", vid) == "AVAILABLE"
    pre = await rows(
        sessionmaker, "SELECT released_at FROM signal_preemptions WHERE dispatch_id = :d", d=did
    )
    assert all(p.released_at is not None for p in pre), "every claim released"
    leftovers = [k async for k in redis.scan_iter(match="divert:{delhi}:*claims:*")]
    assert leftovers == []
    types = [e.type for e in await timeline(sessionmaker, iid)]
    assert types.count("transport_started") == 1
    assert types.count("handover_complete") == 1


# ---------------------------------------------------------------- OSRM failure


async def test_osrm_failure_during_transport_planning_retries_without_teleport(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    cfg = _settings(test_settings)
    clock = Clock()
    router = StubRouter(fail=True)  # the planner's OSRM is down
    worker = new_worker(cfg, redis, sessionmaker, clock, "w-osrm", router=router)
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-osrm")
    await persister.setup()
    async with api(cfg, redis) as (_, client):  # (the API's own router works)
        created = await _dispatch(client, "bengaluru", "MEDICAL")
        iid, did, vid = (
            created["incident"]["id"],
            created["vehicle"]["dispatch_id"],
            created["vehicle"]["id"],
        )
        clock.now = datetime.now(UTC)
        assert await worker.try_lead()

        async def failed_three_times() -> bool:
            n = await scalar(
                sessionmaker,
                "SELECT count(*) FROM incident_events WHERE incident_id = :i "
                "AND type = 'leg_plan_failed'",
                i=iid,
            )
            return int(n) >= 3

        await drive(worker, persister, clock, until=failed_three_times, every=4)
        for _ in range(60):  # past the 20 s dwell: still at the scene, planning
            clock.advance(0.5)
            await worker.process_commands()
            await worker.tick_once()
            await asyncio.sleep(0.005)
        v = await live(redis, "bengaluru", vid)
        assert v["status"] == "ON_SCENE"
        assert v["phase"] == "ON_SCENE"
        assert v["planning"] is True
        assert v["plan_error"]
        assert "routing" in v["plan_error"]
        assert v["pending"]["kind"] == "TRANSPORT"
        assert haversine_m(_at(v["pos"]), _at(created["incident"]["location"])) < 1.0
        assert await scalar(
            sessionmaker, "SELECT count(*) FROM dispatch_legs WHERE dispatch_id = :d", d=did
        ) == 1  # nothing planned, no straight-line fallback  # fmt: skip

        router.fail = False  # OSRM is back: the next retry plans the TRANSPORT
        await drive(worker, persister, clock, until=completed(sessionmaker, did))
    failures = [e.payload for e in await timeline(sessionmaker, iid) if e.type == "leg_plan_failed"]
    assert [f["attempt"] for f in failures] == list(range(1, len(failures) + 1))
    assert all(f["kind"] == "TRANSPORT" for f in failures)
    legs = await legs_of(sessionmaker, did)
    assert [lg.kind for lg in legs][:2] == ["RESPONSE", "TRANSPORT"]
    assert legs[1].gap_m < 1.0, "TRANSPORT starts at the scene (no teleport)"
    assert legs[1].end_reason == "ARRIVED"


# ------------------------------------------------------------- home hospital


async def test_patient_taken_to_the_units_own_hospital_needs_no_return_leg(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Delhi centre: the nearest ambulance is based at the best hospital for the patient,
    so after the handover the unit is already home - back in service, no RETURN leg."""
    cfg = _settings(test_settings)
    clock = Clock()
    worker = new_worker(cfg, redis, sessionmaker, clock, "w-home")
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-home")
    await persister.setup()
    async with api(cfg, redis) as (_, client):
        created = await _dispatch(client, "delhi", "MEDICAL")
        iid, did = created["incident"]["id"], created["vehicle"]["dispatch_id"]
        clock.now = datetime.now(UTC)
        assert await worker.try_lead()
        await drive(worker, persister, clock, until=completed(sessionmaker, did))
    legs = await legs_of(sessionmaker, did)
    assert legs[1].destination_station_id == created["vehicle"]["station"]["id"], (
        "precondition: the patient went to the unit's own hospital"
    )
    assert [(lg.kind, lg.end_reason) for lg in legs] == [
        ("RESPONSE", "ARRIVED"), ("TRANSPORT", "ARRIVED"),
    ]  # fmt: skip
    types = [e.type for e in await timeline(sessionmaker, iid)]
    order = ["handover_complete", "returning", "back_in_service"]
    assert [types.index(t) for t in order] == sorted(types.index(t) for t in order)
    d = await rows(sessionmaker, "SELECT status, end_reason FROM dispatches WHERE id = :d", d=did)
    assert (d[0].status, d[0].end_reason) == ("COMPLETED", "CLEARED")
    assert await _status(sessionmaker, "units", created["vehicle"]["id"]) == "AVAILABLE"
    async with api(cfg, redis) as (_, client):
        summary = (await client.get("/api/v1/cities/delhi/analytics/summary")).json()
    assert summary["handover_to_back_in_service_s"]["count"] == 1
    assert summary["legs"]["RETURN"]["count"] == 0


async def test_own_hospital_mapped_off_the_road_needs_no_return_leg(
    test_settings: Settings, redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Stage 5 fix F1: the hospital's OSM point is 40 m from the road point OSRM snaps it
    to (real case: Vimhans, 35.8 m). The TRANSPORT ends at that road point - where the
    unit's RESPONSE started - so the unit is home: no RETURN, no endless planning."""
    cfg = _settings(test_settings)
    clock = Clock()
    router = SnappingRouter("delhi", off_road_m=40.0)
    worker = new_worker(cfg, redis, sessionmaker, clock, "w-offroad", router=router)
    persister = Persister(cfg, redis, sessionmaker, instance_id="p-offroad")
    await persister.setup()
    async with api(cfg, redis, router=router) as (_, client):
        created = await _dispatch(client, "delhi", "MEDICAL")
        iid, did = created["incident"]["id"], created["vehicle"]["dispatch_id"]
        clock.now = datetime.now(UTC)
        assert await worker.try_lead()
        await drive(worker, persister, clock, until=completed(sessionmaker, did), max_ticks=6000)
    station = created["vehicle"]["station"]["id"]
    off = await rows(
        sessionmaker,
        "SELECT l.seq, l.destination_station_id, "
        "ST_Distance(ST_StartPoint(l.route::geometry)::geography, s.geom) AS start_off_m, "
        "ST_Distance(ST_EndPoint(l.route::geometry)::geography, s.geom) AS end_off_m "
        "FROM dispatch_legs l JOIN stations s ON s.id = :s "
        "WHERE l.dispatch_id = :d ORDER BY l.seq",
        s=station,
        d=did,
    )
    assert [r.seq for r in off] == [0, 1], "RESPONSE and TRANSPORT only: no RETURN leg"
    assert off[1].destination_station_id == station, "precondition: its own hospital"
    assert off[0].start_off_m == pytest.approx(40.0, abs=0.5)  # RESPONSE from the road point
    assert off[1].end_off_m == pytest.approx(40.0, abs=0.5)  # TRANSPORT to the road point
    types = [e.type for e in await timeline(sessionmaker, iid)]
    assert "leg_plan_failed" not in types
    order = ["handover_complete", "returning", "back_in_service"]
    assert [types.index(t) for t in order] == sorted(types.index(t) for t in order)
    assert await _status(sessionmaker, "units", created["vehicle"]["id"]) == "AVAILABLE"
    assert router.same_point == 0  # never even asked: the road point is known
