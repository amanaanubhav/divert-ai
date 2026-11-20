"""Simulation engine behaviour (pure, no Redis)."""

from __future__ import annotations

import itertools
import uuid
from typing import Any

import pytest

from divert.domain.enums import ServiceType, SignalState
from divert.domain.geo import destination_point
from divert.services.simulation import CommandError, Vehicle, VehicleStatus
from divert.store.events import event_id
from tests.support import (
    Clock,
    city,
    corridor_at,
    engine,
    fulfil_plans,
    route_through,
    start_cmd,
    straight_route,
)


def run(
    eng: Any, clock: Clock, ticks: int, dt: float = 0.5, *, plans: bool = True
) -> list[dict[str, Any]]:
    """Tick; ``plans``: answer every leg request at once with a stub leg (unit tests)."""
    events: list[dict[str, Any]] = []
    for _ in range(ticks):
        now = clock.advance(dt)
        batches = eng.tick(dt_sim=dt, sim=now, wall=now)
        for b in batches.values():
            events += b.history
        if plans and fulfil_plans(eng, batches, now):
            for b in eng.drain().values():
                events += b.history
    return events


def run_until_empty(eng: Any, clock: Clock, max_ticks: int = 20_000) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for _ in range(max_ticks):
        events += run(eng, clock, 1)
        if eng.active_count() == 0:
            return events
    raise AssertionError("vehicles did not finish")


def test_start_is_idempotent_and_finished_dispatches_never_restart(clock: Clock) -> None:
    eng = engine(scene_time_s=1.0)
    route = straight_route(city().center, n=5)
    cmd = start_cmd(route)
    assert eng.start_dispatch(cmd, stream_id="1-0", wall=clock.now, sim=clock.now) is not None
    assert eng.start_dispatch(cmd, stream_id="2-0", wall=clock.now, sim=clock.now) is None
    batch = eng.drain()["delhi"]
    assert batch.acks == ["1-0", "2-0"]  # duplicates are acked, not re-applied
    run_until_empty(eng, clock)  # RESPONSE, TRANSPORT, RETURN
    assert eng.active_count() == 0
    assert eng.is_finished("delhi", cmd["dispatch_id"])
    assert eng.start_dispatch(cmd, stream_id="3-0", wall=clock.now, sim=clock.now) is None
    assert eng.active_count() == 0


def test_invalid_command_raises_command_error(clock: Clock) -> None:
    eng = engine()
    with pytest.raises(CommandError):
        eng.start_dispatch(
            {"city": "delhi", "dispatch_id": "x"}, stream_id=None, wall=clock.now, sim=clock.now
        )
    with pytest.raises(CommandError):
        eng.start_dispatch({"city": "atlantis"}, stream_id=None, wall=clock.now, sim=clock.now)


def test_cancel_before_start_reports_cancelled_and_blocks_late_start(clock: Clock) -> None:
    eng = engine()
    cmd = start_cmd(straight_route(city().center, n=5))
    cancel = {
        "city": "delhi",
        "dispatch_id": cmd["dispatch_id"],
        "incident_id": cmd["incident_id"],
        "vehicle_id": "AMB-1",
    }
    assert eng.cancel_dispatch(cancel, stream_id="1-0", wall=clock.now) is False
    b = eng.drain()["delhi"]
    # never left its station: back in service at once (the unit is freed, no RETURN leg)
    assert [e["type"] for e in b.history] == ["cancelled", "back_in_service"]
    assert b.history[1]["end_reason"] == "CANCELLED"
    assert eng.start_dispatch(cmd, stream_id="2-0", wall=clock.now, sim=clock.now) is None


def test_lifecycle_events_and_deterministic_ids(clock: Clock) -> None:
    eng = engine(scene_time_s=10.0)
    route = straight_route(city().center, n=40)
    cmd = start_cmd(route, corridor_at(route, [20]))
    eng.start_dispatch(cmd, stream_id=None, wall=clock.now, sim=clock.now)
    events = eng.drain()["delhi"].history + run_until_empty(eng, clock)
    types = [e["type"] for e in events]
    assert types[0] == "en_route"
    # the MEDICAL chain (design §1): scene -> hospital -> handover -> back in service
    chain = [
        "arrived", "leg_planned", "transport_started", "arrived_hospital", "leg_planned",
        "handover_complete", "returning", "back_in_service",
    ]  # fmt: skip
    assert [t for t in types if t in chain] == chain
    assert "cleared" not in types  # MEDICAL resolves at the handover
    assert (
        types.index("preemption_claimed")
        < types.index("preemption_released")
        < types.index("arrived")
    )
    legs = [(e["seq"], e["kind"]) for e in events if e["type"] == "leg_started"]
    assert legs == [(1, "TRANSPORT"), (2, "RETURN")]
    ended = [(e["seq"], e["end_reason"]) for e in events if e["type"] == "leg_ended"]
    assert ended == [(0, "ARRIVED"), (1, "ARRIVED"), (2, "ARRIVED")]
    did = cmd["dispatch_id"]
    by_type = {e["type"]: e["event_id"] for e in events}
    assert by_type["arrived"] == event_id("arr", did)
    assert by_type["preemption_claimed"] == event_id("clm", did, "S20")
    positions = [e for e in events if e["type"] == "vehicle_position" and e["leg_seq"] == 0]
    assert [p["sample"] for p in positions] == list(range(len(positions)))
    ids = [e["event_id"] for e in events if e["type"] == "vehicle_position"]
    assert len(ids) == len(set(ids))  # unique across legs
    elapsed = [p["elapsed_s"] for p in positions]
    assert all(b - a == pytest.approx(5.0, abs=0.51) for a, b in itertools.pairwise(elapsed))
    arrived = next(e for e in events if e["type"] == "arrived")
    assert arrived["est_time_saved_s"] == pytest.approx(city().traffic.avg_junction_delay_s)
    assert all(len(e["event_id"]) <= 64 for e in events)


def test_conflict_lower_eta_wins_and_loser_is_queued(clock: Clock) -> None:
    c = city().center
    j = destination_point(c, 90.0, 500.0)
    near = route_through([destination_point(j, 270.0, 120.0), j, destination_point(j, 90.0, 300.0)])
    far = route_through([destination_point(j, 0.0, 450.0), j, destination_point(j, 180.0, 300.0)])

    def corridor(route: Any, approach: float) -> Any:
        from divert.domain.geo import project_onto_polyline  # noqa: PLC0415

        p = project_onto_polyline(j, route.points, route.cumulative)
        base = corridor_at(route, [1])[0]
        return [type(base)("J", j, p.along_m, p.offset_m, base.lane, approach)]

    eng = engine()
    # the police car is far ahead (ETA gap > the 10 s priority window) -> it wins although
    # the ambulance ranks higher; the two approaches are different phases (incompatible)
    eng.start_dispatch(
        start_cmd(far, corridor(far, 180.0), vehicle_id="AMB-1"),
        stream_id=None,
        wall=clock.now,
        sim=clock.now,
    )
    eng.start_dispatch(
        start_cmd(near, corridor(near, 90.0), vehicle_id="PCR-1", service=ServiceType.POLICE),
        stream_id=None, wall=clock.now, sim=clock.now,
    )  # fmt: skip
    amb, pcr = eng.vehicles["delhi"]["AMB-1"], eng.vehicles["delhi"]["PCR-1"]
    events: list[dict[str, Any]] = []
    for _ in range(200):
        events += run(eng, clock, 1)
        if "J" in amb.claims and "J" in pcr.claims:
            break
    assert amb.claims["J"].state is SignalState.CONFLICT
    assert pcr.claims["J"].state is SignalState.PREEMPTED
    conflicts = [e for e in events if e["type"] == "preemption_conflict"]
    assert [e["vehicle_id"] for e in conflicts] == ["AMB-1"]
    assert conflicts[0]["winner"] == "PCR-1"
    b = eng.tick(dt_sim=0.5, sim=clock.advance(0.5), wall=clock.now)["delhi"]
    assert b.signal_sets["J"]["state"] in {"TRANSITIONING", "QUEUED"}
    assert b.signal_sets["J"]["vehicle_id"] == "PCR-1"
    # the loser is queued behind the winner: its expected green is after the winner's
    amb_green = amb.claims["J"].expected_green_at
    pcr_green = pcr.claims["J"].green_at
    assert amb_green is not None
    assert pcr_green is not None
    assert amb_green > pcr_green
    # and it is not given the pre-empted junction speed
    prof = eng._motion_profile(amb, clock.now)
    assert prof.allowed_speed(amb.corridor[0].along_m) <= 15 / 3.6 + 1e-9


def test_vehicle_restore_round_trip(clock: Clock) -> None:
    eng = engine()
    route = straight_route(city().center, n=40)
    eng.start_dispatch(
        start_cmd(route, corridor_at(route, [10, 30])),
        stream_id=None,
        wall=clock.now,
        sim=clock.now,
    )
    run(eng, clock, 60)
    v = next(iter(eng.vehicles["delhi"].values()))
    assert v.claims
    from divert.services.simulation import Claim  # noqa: PLC0415

    claims = {sid: Claim.from_json(c.to_json(v)) for sid, c in v.claims.items()}
    clone = Vehicle.restore(v.live_json(), v.route_json(), claims)
    assert clone.along_m == pytest.approx(v.along_m, abs=0.01)
    assert clone.speed_mps == pytest.approx(v.speed_mps, abs=0.01)
    assert clone.samples == v.samples
    assert clone.passed == v.passed
    assert set(clone.claims) == set(v.claims)
    assert clone.status is VehicleStatus.EN_ROUTE
    assert clone.dispatch_id == v.dispatch_id
    assert uuid.UUID(clone.incident_id)
