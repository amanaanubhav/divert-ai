"""The prototype's three bugs, ported as regression tests against the new architecture.

1. "POLICE" reports must dispatch a police unit, and an unknown type is rejected
   (422) before anything is created.
2. Signal claims are released on arrival, on an exception while moving, and on cancel.
3. No read-modify-write of shared Redis state: per-vehicle hash fields, per-vehicle
   signal claims, one MULTI/EXEC per city per tick, concurrent dispatches never lose
   each other's updates, and a vehicle leaving a junction never resets it for another
   vehicle of the same type.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from redis.asyncio import Redis

from divert.domain.enums import (
    INCIDENT_TYPE_ALIASES,
    SERVICE_FOR_INCIDENT,
    IncidentType,
    ServiceType,
    normalize_incident_type,
)
from divert.domain.geo import LatLng, destination_point
from divert.services.dispatch import DispatchService, InvalidIncident
from divert.services.eta import EtaEstimate, EtaQuery, KinematicEtaModel
from divert.services.simulation import VehicleStatus
from divert.store import redis_store as keys
from divert.store.events import CommandType, message
from tests.support import Clock, city, corridor_at, route_through, start_cmd, straight_route
from tests.unit.conftest import (
    UnitWorker,
    active_signals,
    claim_keys,
    history,
    live_vehicles,
    unit_settings,
)

# ----------------------------------------------------------------- guarantee 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("POLICE", IncidentType.CRIME),
        ("police", IncidentType.CRIME),
        ("  Police ", IncidentType.CRIME),
        ("CRIME", IncidentType.CRIME),
        ("crime", IncidentType.CRIME),
        ("HEALTH", IncidentType.MEDICAL),
        ("ambulance", IncidentType.MEDICAL),
        ("fire", IncidentType.FIRE),
    ],
)
def test_police_and_other_aliases_normalise(raw: str, expected: IncidentType) -> None:
    assert normalize_incident_type(raw) is expected


def test_police_alias_maps_to_a_police_unit() -> None:
    assert SERVICE_FOR_INCIDENT[normalize_incident_type("POLICE")] is ServiceType.POLICE


class _Explode:
    """Any use of PostgreSQL / OSRM / the reference cache fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"touched {name!r} before validating the incident type")

    def __call__(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("touched a dependency before validating the incident type")


@pytest.mark.parametrize("bad", ["BOGUS", "", "POLICE_X", "ambulance car"])
async def test_unknown_type_is_rejected_before_anything_is_created(
    fake_redis: Redis, bad: str
) -> None:
    svc = DispatchService(
        settings=unit_settings(),
        sessionmaker=_Explode(),  # type: ignore[arg-type]
        redis=fake_redis,
        router=_Explode(),  # type: ignore[arg-type]
        eta_model=KinematicEtaModel(),
        reference=_Explode(),  # type: ignore[arg-type]
        outbox=_Explode(),  # type: ignore[arg-type]
    )
    with pytest.raises(InvalidIncident) as err:
        await svc.create_incident("delhi", bad, LatLng(28.6, 77.2))
    assert err.value.status_code == 422
    assert err.value.details["valid_types"] == sorted(INCIDENT_TYPE_ALIASES)
    assert repr(bad) in err.value.message
    assert await fake_redis.keys("*") == []  # no incident, no command, no vehicle


# ----------------------------------------------------------------- guarantee 2


def _route_ending_next_to_a_signal() -> tuple[Any, Any]:
    delhi = city("delhi")
    route = straight_route(delhi.center, bearing=60.0, n=30, step_m=40.0)
    # junction 10 m before the destination (inside every claim radius at arrival)
    corridor = corridor_at(route, [5, 15, len(route.points) - 1])
    sig = corridor[-1]
    near_end = type(sig)(
        signal_id="SEND", pos=destination_point(route.destination, 240.0, 10.0),
        along_m=route.length_m - 10.0, offset_m=0.0, lane=sig.lane, approach_bearing=60.0,
    )  # fmt: skip
    return route, (*corridor[:2], near_end)


async def test_arrival_next_to_a_signal_releases_every_claim(
    make_worker: Callable[..., UnitWorker], fake_redis: Redis, clock: Clock
) -> None:
    w = make_worker()
    assert await w.try_lead()
    route, corridor = _route_ending_next_to_a_signal()
    await w.send(start_cmd(route, corridor))
    held_at_last_moving_tick: list[str] = []

    def arrived() -> bool:
        assert w.engine is not None
        v = next(iter(w.engine.vehicles.get("delhi", {}).values()), None)
        return v is not None and v.status is VehicleStatus.ON_SCENE

    while not arrived():
        clock.advance(0.5)
        await w.process_commands()
        await w.tick_once()
        live = await live_vehicles(fake_redis)
        if live and live["AMB-1"]["status"] == "EN_ROUTE":
            held_at_last_moving_tick = live["AMB-1"]["claims"]
    assert "SEND" in held_at_last_moving_tick  # it was holding the junction next to the scene

    vehicle = (await live_vehicles(fake_redis))["AMB-1"]
    assert vehicle["status"] == "ON_SCENE"
    assert vehicle["pos"] == pytest.approx([route.destination.lat, route.destination.lng])
    assert vehicle["claims"] == []
    assert await claim_keys(fake_redis) == []
    assert await active_signals(fake_redis) == {}
    released = [e for e in await history(fake_redis) if e["type"] == "preemption_released"]
    assert {e["signal_id"] for e in released} == {"S5", "S15", "SEND"}
    assert {e["reason"] for e in released if e["signal_id"] == "SEND"} == {"arrived"}


class FailingEta(KinematicEtaModel):
    """Raises on its n-th call - i.e. part-way along the route."""

    def __init__(self, fail_on_call: int) -> None:
        super().__init__()
        self.calls = 0
        self.fail_on_call = fail_on_call

    def estimate(self, query: EtaQuery) -> EtaEstimate:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("ETA model crashed")
        return super().estimate(query)


async def test_exception_mid_route_releases_every_claim(
    make_worker: Callable[..., UnitWorker], fake_redis: Redis, clock: Clock
) -> None:
    w = make_worker(eta_model=FailingEta(fail_on_call=60))
    assert await w.try_lead()
    route, corridor = _route_ending_next_to_a_signal()
    cmd = start_cmd(route, corridor)
    await w.send(cmd)
    held: list[int] = []
    for _ in range(80):
        clock.advance(0.5)
        await w.process_commands()
        await w.tick_once()
        held.append(len(await claim_keys(fake_redis)))
    assert max(held) > 0  # it held signals before the crash
    assert await claim_keys(fake_redis) == []
    assert await active_signals(fake_redis) == {}
    assert await live_vehicles(fake_redis) == {}
    failed = [e for e in await history(fake_redis) if e["type"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["dispatch_id"] == cmd["dispatch_id"]
    assert "ETA model crashed" in failed[0]["reason"]


async def test_cancel_releases_every_claim(
    make_worker: Callable[..., UnitWorker], fake_redis: Redis, clock: Clock
) -> None:
    w = make_worker()
    assert await w.try_lead()
    route, corridor = _route_ending_next_to_a_signal()
    cmd = start_cmd(route, corridor)
    await w.send(cmd)
    while not await claim_keys(fake_redis):
        clock.advance(0.5)
        await w.process_commands()
        await w.tick_once()
    for _ in range(5):
        clock.advance(0.5)
        await w.tick_once()
    assert await claim_keys(fake_redis)
    await w.send(
        message(
            CommandType.CANCEL_DISPATCH.value,
            city="delhi",
            dispatch_id=cmd["dispatch_id"],
            incident_id=cmd["incident_id"],
            vehicle_id="AMB-1",
        )
    )
    await w.process_commands()
    clock.advance(0.5)
    await w.tick_once()
    assert await claim_keys(fake_redis) == []
    assert await active_signals(fake_redis) == {}
    assert await live_vehicles(fake_redis) == {}
    types = [e["type"] for e in await history(fake_redis)]
    assert types.count("cancelled") == 1
    assert "arrived" not in types
    # every command was acked in the same transaction as its effects
    pending = await fake_redis.xpending(keys.STREAM_COMMANDS, keys.GROUP_WORKER)
    assert pending["pending"] == 0


# ----------------------------------------------------------------- guarantee 3


def _shared_junction_dispatches() -> list[dict[str, Any]]:
    """Two ambulances through the same junction J (one turns off after it), a fire
    engine and a police car elsewhere - all dispatched at once."""
    c = city("delhi").center
    j = destination_point(c, 90.0, 300.0)
    amb1 = route_through([c, j, destination_point(j, 0.0, 150.0)])
    amb2 = route_through(
        [destination_point(c, 180.0, 200.0), c, j, destination_point(j, 90.0, 800.0)]
    )
    fire = straight_route(destination_point(c, 0.0, 2000.0), bearing=10.0, n=20)
    police = straight_route(destination_point(c, 270.0, 2000.0), bearing=200.0, n=25)

    def junction(route: Any) -> Any:
        from divert.domain.geo import project_onto_polyline  # noqa: PLC0415

        proj = project_onto_polyline(j, route.points, route.cumulative)
        sig = corridor_at(route, [1], prefix="X")[0]
        return type(sig)("J", j, proj.along_m, proj.offset_m, sig.lane, 90.0)

    return [
        start_cmd(amb1, [junction(amb1)], vehicle_id="AMB-1"),
        start_cmd(amb2, [junction(amb2)], vehicle_id="AMB-2"),
        start_cmd(fire, corridor_at(fire, [8, 16], prefix="F"), vehicle_id="FIRE-1",
                  service=ServiceType.FIRE_ENGINE),
        start_cmd(police, corridor_at(police, [5, 20], prefix="P"), vehicle_id="PCR-1",
                  service=ServiceType.POLICE),
    ]  # fmt: skip


async def _run_and_sample(
    w: UnitWorker, fake_redis: Redis, clock: Clock, cmds: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    for cmd in cmds:
        await w.send(cmd)
    samples: list[dict[str, Any]] = []
    for _ in range(4000):
        clock.advance(0.5)
        await w.process_commands()
        await w.tick_once()
        samples.append(
            {
                "vehicles": await live_vehicles(fake_redis),
                "signals": await active_signals(fake_redis),
                "claims": {
                    k: dict(await fake_redis.hgetall(k))
                    for k in await claim_keys(fake_redis)
                    if ":claims:" in k
                },
            }
        )
        if not samples[-1]["vehicles"]:
            break
    return samples


async def test_concurrent_dispatches_never_lose_updates(
    make_worker: Callable[..., UnitWorker], fake_redis: Redis, clock: Clock
) -> None:
    w = make_worker(scene_time_s=5.0)
    assert await w.try_lead()
    cmds = _shared_junction_dispatches()
    samples = await _run_and_sample(w, fake_redis, clock, cmds)
    assert not samples[-1]["vehicles"], "everyone arrived and cleared"

    progress: dict[str, float] = {}
    ended: set[str] = set()
    for s in samples:
        for vid in progress:
            if vid not in ended and vid not in s["vehicles"]:
                ended.add(vid)
        for vid, v in s["vehicles"].items():
            assert vid not in ended, f"{vid} came back after it was removed"
            assert v["along_m"] >= progress.get(vid, 0.0), f"{vid} moved backwards"
            progress[vid] = v["along_m"]
        # every claim field belongs to a vehicle that lists it (per-vehicle ownership)
        for key, holders in s["claims"].items():
            sig = key.rsplit(":", 1)[1]
            for vid in holders:
                assert sig in s["vehicles"][vid]["claims"]
    assert set(progress) == {"AMB-1", "AMB-2", "FIRE-1", "PCR-1"}
    assert any(
        {"AMB-1", "AMB-2"}
        <= {
            json.loads(h)["vehicle_id"]
            for h in s["claims"].get(keys.claims("delhi", "J"), {}).values()
        }
        for s in samples
    ), "the two ambulances never held J at the same time"
    assert await claim_keys(fake_redis) == []
    assert await active_signals(fake_redis) == {}
    events = await history(fake_redis)
    ids = [e["event_id"] for e in events]
    assert len(ids) == len(set(ids)), "history event ids are unique"
    assert sorted(e["vehicle_id"] for e in events if e["type"] == "cleared") == [
        "AMB-1", "AMB-2", "FIRE-1", "PCR-1",
    ]  # fmt: skip


async def test_leaving_vehicle_keeps_junction_preempted_for_same_type_vehicle(
    make_worker: Callable[..., UnitWorker], fake_redis: Redis, clock: Clock
) -> None:
    w = make_worker(scene_time_s=5.0)
    assert await w.try_lead()
    samples = await _run_and_sample(w, fake_redis, clock, _shared_junction_dispatches()[:2])
    handover = [
        s for s in samples
        if "AMB-1" in s["vehicles"] and "J" not in s["vehicles"]["AMB-1"]["claims"]
        and "AMB-2" in s["vehicles"] and "J" in s["vehicles"]["AMB-2"]["claims"]
    ]  # fmt: skip
    assert handover, "AMB-1 never released J while AMB-2 still held it"
    for s in handover:
        sig = s["signals"]["J"]
        assert sig["state"] == "PREEMPTED"
        assert sig["vehicle_id"] == "AMB-2"


async def test_one_multi_exec_per_city_per_tick_and_no_reads(
    make_worker: Callable[..., UnitWorker], fake_redis: Redis, clock: Clock
) -> None:
    w = make_worker()
    assert await w.try_lead()
    delhi_route = straight_route(city("delhi").center, n=30)
    blr_route = straight_route(city("bengaluru").center, n=30)
    await w.send(start_cmd(delhi_route, corridor_at(delhi_route, [10, 20]), vehicle_id="AMB-1"))
    await w.send(
        start_cmd(
            blr_route, corridor_at(blr_route, [10, 20]), city_id="bengaluru", vehicle_id="AMB-7"
        )
    )
    await w.process_commands()

    executed: list[tuple[bool, list[tuple[str, str]]]] = []
    original = fake_redis.pipeline

    def spy(transaction: bool = True, shard_hint: Any = None) -> Any:
        p = original(transaction=transaction, shard_hint=shard_hint)
        real_execute = p.execute

        async def execute(raise_on_error: bool = True) -> Any:
            executed.append(
                (transaction, [(str(c[0][0]).upper(), str(c[0][1])) for c in p.command_stack])
            )
            return await real_execute(raise_on_error)

        p.execute = execute  # type: ignore[method-assign]
        return p

    fake_redis.pipeline = spy  # type: ignore[method-assign]
    # a field another component wrote into the shared hash must survive our ticks
    await fake_redis.hset(keys.vehicles("delhi"), "FOREIGN", '{"id": "FOREIGN"}')
    for _ in range(40):
        executed.clear()
        clock.advance(0.5)
        await w.tick_once()
        tx = [cmds for transactional, cmds in executed if transactional]
        assert len(tx) == 2, "one MULTI/EXEC per city"
        for cmds in tx:
            tags = {k.split(":")[1] for _, k in cmds if k.startswith("divert:{")}
            assert len(tags) == 1, f"a tick transaction spans cities: {tags}"
            names = {n for n, _ in cmds}
            assert not names & {"GET", "HGET", "HGETALL", "MGET", "SMEMBERS", "WATCH"}, names
    assert "FOREIGN" in await fake_redis.hkeys(keys.vehicles("delhi"))
