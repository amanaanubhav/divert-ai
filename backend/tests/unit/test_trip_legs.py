"""Trip legs in the simulation engine (design sections 1, 2, 4.5): the leg chain, waiting
for a plan, cancel in every phase, the home-hospital case, re-routing, the +-10 % ETA
guarantee on every leg kind, and claim release on leg change / re-route / failover."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from redis.asyncio import Redis

from divert.domain.enums import IncidentType, LegKind, ServiceType, UnitStatus
from divert.domain.geo import destination_point, haversine_m, interpolate_by_distance
from divert.services.legs import route_replaced_command
from divert.services.simulation import Claim, SimulationEngine, Vehicle, VehicleStatus
from divert.store import redis_store as keys
from divert.store.events import event_id
from tests.support import (
    Clock,
    city,
    corridor_at,
    engine,
    route_through,
    start_cmd,
    straight_route,
    stub_leg_command,
)
from tests.unit.conftest import UnitWorker, claim_keys, history

C = city("delhi").center
Answer = Callable[[dict[str, Any]], dict[str, Any] | None]


@dataclass
class Out:
    history: list[dict[str, Any]] = field(default_factory=list)
    publish: list[dict[str, Any]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def of(self, type_: str, **match: Any) -> list[dict[str, Any]]:
        return [
            e for e in self.history
            if e["type"] == type_ and all(e.get(k) == v for k, v in match.items())
        ]  # fmt: skip

    def types(self) -> list[str]:
        return [e["type"] for e in self.history]


def stub_answer(corridor_every: int | None = 10) -> Answer:
    def answer(req: dict[str, Any]) -> dict[str, Any] | None:
        return stub_leg_command(req, corridor_every=corridor_every)

    return answer


def step(
    eng: SimulationEngine,
    clock: Clock,
    out: Out,
    *,
    ticks: int = 1,
    answer: Answer | None = None,
    dt: float = 0.5,
) -> None:
    """Tick; collect everything; answer plan requests with ``answer`` (None: keep them)."""
    for _ in range(ticks):
        now = clock.advance(dt)
        batches = list(eng.tick(dt_sim=dt, sim=now, wall=now).values())
        for b in batches:
            out.requests += b.plan_requests
            if answer is None:
                continue
            for req in b.plan_requests:
                if req["type"] == "plan_request" and (cmd := answer(req)) is not None:
                    eng.apply_leg_planned(cmd, stream_id=None, wall=now)
        batches += list(eng.drain().values())
        for b in batches:
            out.history += b.history
            out.publish += b.publish


def until(
    eng: SimulationEngine,
    clock: Clock,
    out: Out,
    done: Callable[[], bool],
    *,
    answer: Answer | None = None,
    max_ticks: int = 20_000,
) -> None:
    for _ in range(max_ticks):
        if done():
            return
        step(eng, clock, out, answer=answer)
    raise AssertionError("did not get there")


def start(
    eng: SimulationEngine,
    clock: Clock,
    route: Any,
    corridor: Any = (),
    out: Out | None = None,
    **kw: Any,
) -> Vehicle:
    itype = kw.pop("incident_type", "MEDICAL")
    cmd = start_cmd(route, corridor, **kw)
    cmd["incident_type"] = itype
    v = eng.start_dispatch(cmd, stream_id=None, wall=clock.now, sim=clock.now)
    assert v is not None
    if "predicted_eta_s" not in kw:  # the API's prediction: the model at dispatch
        v.predicted_eta_s = v.eta_s
    if out is not None:
        for b in eng.drain().values():
            out.history += b.history
            out.publish += b.publish
    return v


def drained(eng: SimulationEngine, clock: Clock, out: Out, answer: Answer | None = None) -> None:
    """Collect what a command applied outside a tick produced; answer its plan requests."""
    for b in eng.drain().values():
        out.history += b.history
        out.publish += b.publish
        out.requests += b.plan_requests
        for req in b.plan_requests:
            if answer is not None and req["type"] == "plan_request":
                cmd = answer(req)
                if cmd is not None:
                    eng.apply_leg_planned(cmd, stream_id=None, wall=clock.now)


def gone(eng: SimulationEngine) -> Callable[[], bool]:
    return lambda: eng.active_count() == 0


# ------------------------------------------------------------------------- the chain


def test_medical_chain_runs_every_leg_and_frees_the_unit_last(clock: Clock) -> None:
    eng = engine(scene_time_s=20.0, hospital_handover_s=20.0)
    route = straight_route(C, n=30)
    v = start(eng, clock, route, corridor_at(route, [10, 20]))
    out = Out()
    phases: list[UnitStatus] = []
    for _ in range(20_000):
        if eng.active_count() == 0:
            break
        phases.append(v.phase)
        step(eng, clock, out, answer=stub_answer())
    order = [
        "arrived", "transport_started", "arrived_hospital", "handover_complete",
        "returning", "back_in_service",
    ]  # fmt: skip
    types = out.types()
    assert [types.index(t) for t in order] == sorted(types.index(t) for t in order)
    assert "cleared" not in types
    assert list(dict.fromkeys(phases)) == [
        UnitStatus.DISPATCHED, UnitStatus.ON_SCENE, UnitStatus.TRANSPORTING,
        UnitStatus.AT_HOSPITAL, UnitStatus.RETURNING,
    ]  # fmt: skip
    ended = out.of("leg_ended")
    assert [(e["seq"], e["kind"], e["end_reason"]) for e in ended] == [
        (0, "RESPONSE", "ARRIVED"), (1, "TRANSPORT", "ARRIVED"), (2, "RETURN", "ARRIVED"),
    ]  # fmt: skip
    for e in ended:  # the +-10 % guarantee on every leg kind
        assert e["travel_s"] == pytest.approx(e["predicted_eta_s"], rel=0.10), e["kind"]
    assert [e["seq"] for e in out.of("leg_started")] == [1, 2]
    assert {e["leg_seq"] for e in out.of("vehicle_position")} == {0, 1, 2}
    claimed = out.of("preemption_claimed")
    assert {e["leg_seq"] for e in claimed} == {0, 1}, "RESPONSE + TRANSPORT claim; RETURN never"
    released = {e["claim_event_id"] for e in out.of("preemption_released")}
    assert {e["event_id"] for e in claimed} == released
    changed = [m for m in out.publish if m["type"] == "leg_changed"]
    assert [(m["leg"]["kind"], m["phase"]) for m in changed] == [
        ("TRANSPORT", "TRANSPORTING"), ("RETURN", "RETURNING"),
    ]  # fmt: skip
    assert changed[0]["hospital"]["id"] == "H-stub"
    assert changed[0]["destination_info"]["kind"] == "HOSPITAL"
    removed = [m for m in out.publish if m["type"] == "vehicle_removed"]
    assert [m["reason"] for m in removed] == ["back_in_service"]
    assert out.of("back_in_service")[0]["end_reason"] == "CLEARED"
    ids = [e["event_id"] for e in out.history]
    assert len(ids) == len(set(ids))


def test_transport_started_records_the_real_time_on_scene(clock: Clock) -> None:
    """Stage 5 fix-2 B4: scene_s = arrival -> TRANSPORT departure (the 20 s dwell plus the
    30 s the unit waited for its plan), not 0."""
    eng = engine(scene_time_s=20.0, hospital_handover_s=20.0)
    v = start(eng, clock, straight_route(C, n=10))
    out = Out()
    until(eng, clock, out, lambda: v.phase is UnitStatus.ON_SCENE)
    step(eng, clock, out, ticks=100)  # 50 s on scene, nobody answers the plan request
    assert v.stopped_s == pytest.approx(50.0, abs=0.01)
    (req,) = eng.pending_plan_requests()
    assert eng.apply_leg_planned(stub_leg_command(req), stream_id=None, wall=clock.now)
    drained(eng, clock, out)
    (ts,) = out.of("transport_started")
    assert ts["scene_s"] == pytest.approx(50.0, abs=0.01)
    assert v.stopped_s == 0.0  # driving the TRANSPORT leg


def test_fire_responds_then_returns(clock: Clock) -> None:
    eng = engine(scene_time_s=20.0)
    route = straight_route(C, n=20)
    start(eng, clock, route, incident_type="FIRE", service=ServiceType.FIRE_ENGINE,
          vehicle_id="FIRE-1")  # fmt: skip
    out = Out()
    until(eng, clock, out, gone(eng), answer=stub_answer())
    types = out.types()
    assert [types.index(t) for t in ("arrived", "cleared", "returning", "back_in_service")] == (
        sorted(types.index(t) for t in ("arrived", "cleared", "returning", "back_in_service"))
    )
    assert "transport_started" not in types
    assert [(e["seq"], e["kind"]) for e in out.of("leg_ended")] == [(0, "RESPONSE"), (1, "RETURN")]
    (req,) = [r for r in out.requests if r["type"] == "plan_request"]
    assert (req["kind"], req["seq"]) == ("RETURN", 1)


def test_vehicle_waits_in_place_until_its_plan_arrives(clock: Clock) -> None:
    eng = engine(scene_time_s=10.0)
    route = straight_route(C, n=10)
    v = start(eng, clock, route)
    out = Out()
    until(eng, clock, out, lambda: v.phase is UnitStatus.ON_SCENE)
    step(eng, clock, out, ticks=200)  # dwell over long ago, nobody answers
    assert v.planning
    assert v.status is VehicleStatus.ON_SCENE
    assert v.speed_mps == 0.0
    assert haversine_m(v.pos, route.destination) < 0.5  # never teleported
    assert v.live_json()["planning"] is True
    assert v.tick_json()["next_movement"] is None
    (req,) = eng.pending_plan_requests()  # what a new leader would re-issue
    assert (req["kind"], req["seq"]) == ("TRANSPORT", 1)
    wrong = stub_leg_command({**req, "seq": 7})
    assert not eng.apply_leg_planned(wrong, stream_id=None, wall=clock.now)
    cmd = stub_leg_command(req)
    assert eng.apply_leg_planned(cmd, stream_id=None, wall=clock.now)
    assert v.moving  # started at once: it was waiting
    assert (v.leg_seq, v.leg_kind, v.phase) == (1, LegKind.TRANSPORT, UnitStatus.TRANSPORTING)
    assert not eng.apply_leg_planned(cmd, stream_id=None, wall=clock.now)  # idempotent
    doc = v.tick_json()
    assert doc["leg"] == {"seq": 1, "kind": "TRANSPORT", "emergency": True}
    assert doc["destination_info"]["kind"] == "HOSPITAL"
    assert doc["destination"] == [v.destination.lat, v.destination.lng]  # v1 type kept
    assert doc["phase"] == "TRANSPORTING"


# --------------------------------------------------------------------------- cancel


def test_cancel_en_route_brakes_and_returns_from_where_it_stopped(clock: Clock) -> None:
    eng = engine(scene_time_s=10.0)
    route = straight_route(C, n=40)
    v = start(eng, clock, route, corridor_at(route, [6, 9, 30]))
    out = Out()
    # (well away from its station: a cancel within 30 m of home needs no RETURN leg)
    until(eng, clock, out, lambda: bool(v.claims) and v.along_m > 150.0)
    held = set(v.claims)
    along, speed = v.along_m, v.speed_mps
    assert eng.cancel_dispatch(
        {"city": "delhi", "dispatch_id": v.dispatch_id}, stream_id=None, wall=clock.now
    )
    drained(eng, clock, out)  # the RETURN request is answered once the vehicle stopped
    assert not v.claims
    rel = {e["signal_id"]: e["outcome"] for e in out.of("preemption_released")}
    assert {s: rel[s] for s in held} == dict.fromkeys(held, "CANCELLED")
    assert v.halt_at_m == pytest.approx(along + speed**2 / (2 * 3.0))
    assert v.phase is UnitStatus.RETURNING
    assert not v.emergency  # it never claims again
    (req,) = [r for r in out.requests if r["type"] == "plan_request"]
    halt, _ = interpolate_by_distance(route.points, route.cumulative, v.halt_at_m)
    assert req["kind"] == "RETURN"
    assert haversine_m(LatLngOf(req["origin"]), halt) < 0.01
    until(eng, clock, out, lambda: not v.moving)  # brakes to a stop at the halt point
    assert v.along_m == pytest.approx(along + speed**2 / 6.0, abs=1.0)
    assert eng.apply_leg_planned(stub_leg_command(req), stream_id=None, wall=clock.now)
    until(eng, clock, out, gone(eng), answer=stub_answer())
    assert [(e["kind"], e["end_reason"]) for e in out.of("leg_ended")] == [
        ("RESPONSE", "CANCELLED"), ("RETURN", "ARRIVED"),
    ]  # fmt: skip
    assert out.of("back_in_service")[0]["end_reason"] == "CANCELLED"
    assert not out.of("preemption_claimed", leg_seq=1)


def LatLngOf(raw: Any) -> Any:  # noqa: N802 - reads like a constructor
    from divert.domain.geo import LatLng  # noqa: PLC0415

    return LatLng(float(raw[0]), float(raw[1]))


def test_cancel_on_scene_supersedes_the_pending_transport(clock: Clock) -> None:
    eng = engine(scene_time_s=30.0)
    route = straight_route(C, n=10)
    v = start(eng, clock, route)
    out = Out()
    until(eng, clock, out, lambda: v.phase is UnitStatus.ON_SCENE)
    assert v.pending is not None
    assert v.pending.kind is LegKind.TRANSPORT
    assert eng.cancel_dispatch(
        {"city": "delhi", "dispatch_id": v.dispatch_id}, stream_id=None, wall=clock.now
    )
    before = len(out.requests)
    drained(eng, clock, out, answer=stub_answer())
    reqs = out.requests[before:]
    assert [r["type"] for r in reqs] == ["plan_cancel", "plan_request"]
    assert (reqs[0]["seq"], reqs[1]["seq"], reqs[1]["kind"]) == (1, 2, "RETURN")
    (sup,) = out.of("leg_ended", seq=1)
    assert (sup["end_reason"], sup["started"]) == ("SUPERSEDED", False)
    until(eng, clock, out, gone(eng), answer=stub_answer())
    types = out.types()
    assert "transport_started" not in types
    assert "cleared" not in types
    assert out.of("back_in_service")[0]["end_reason"] == "CANCELLED"


def test_cancel_with_a_patient_on_board_or_resolved_is_rejected(clock: Clock) -> None:
    eng = engine(scene_time_s=5.0, hospital_handover_s=20.0)
    v = start(eng, clock, straight_route(C, n=10))
    out = Out()
    cancel = {"city": "delhi", "dispatch_id": v.dispatch_id}
    for phase, status in (
        (UnitStatus.TRANSPORTING, "TRANSPORTING"),
        (UnitStatus.AT_HOSPITAL, "AT_HOSPITAL"),
        (UnitStatus.RETURNING, "RESOLVED"),
    ):
        until(eng, clock, out, lambda p=phase: v.phase is p, answer=stub_answer())
        assert not eng.cancel_dispatch(cancel, stream_id=None, wall=clock.now)
        b = eng.drain()
        rejected = [e for x in b.values() for e in x.history if e["type"] == "cancel_rejected"]
        assert [e["status"] for e in rejected] == [status]
        assert not v.cancelled
    until(eng, clock, out, gone(eng), answer=stub_answer())
    assert out.of("back_in_service")[0]["end_reason"] == "CLEARED"


# ------------------------------------------------------------------- home hospital


def test_patient_taken_to_the_units_own_hospital_needs_no_return_leg(clock: Clock) -> None:
    eng = engine(scene_time_s=5.0, hospital_handover_s=10.0)
    route = straight_route(C, n=12)
    start(eng, clock, route)

    def to_home_hospital(req: dict[str, Any]) -> dict[str, Any]:
        assert req["kind"] == "TRANSPORT", "no RETURN is requested from home"
        cmd = stub_leg_command({**req, "kind": "RETURN"})  # a route to the home station
        return {**cmd, "kind": "TRANSPORT", "emergency": True}

    out = Out()
    until(eng, clock, out, gone(eng), answer=to_home_hospital)
    assert [r["kind"] for r in out.requests if r["type"] == "plan_request"] == ["TRANSPORT"]
    assert [(e["seq"], e["kind"]) for e in out.of("leg_ended")] == [
        (0, "RESPONSE"), (1, "TRANSPORT"),
    ]  # fmt: skip
    types = out.types()
    order = ["handover_complete", "returning", "back_in_service"]
    assert [types.index(t) for t in order] == sorted(types.index(t) for t in order)


# A station mapped off the road (stage 5 fix F1): real Vimhans gap between the OSM point
# and the road point OSRM snaps it to (35.8 m > AT_HOME_M).
OFF_ROAD_M = 35.8


def _off_road_station(route: Any) -> list[float]:
    p = destination_point(route.origin, 270.0, OFF_ROAD_M)
    return [p.lat, p.lng]


def _to_road_point(road: Any) -> Answer:
    """TRANSPORT to the unit's own hospital: the route ends at the hospital's road point
    (where OSRM snaps it), as every route to it does - not at its OSM point."""

    def answer(req: dict[str, Any]) -> dict[str, Any] | None:
        if req["kind"] != "TRANSPORT":
            return None
        cmd = stub_leg_command({**req, "kind": "RETURN", "home": [road.lat, road.lng]})
        return {**cmd, "kind": "TRANSPORT", "emergency": True}

    return answer


def test_a_station_mapped_off_the_road_is_home_at_its_road_point(clock: Clock) -> None:
    eng = engine(scene_time_s=5.0, hospital_handover_s=10.0)
    route = straight_route(C, n=12)
    cmd = start_cmd(route)
    cmd["station_pos"] = _off_road_station(route)
    v = eng.start_dispatch(cmd, stream_id=None, wall=clock.now, sim=clock.now)
    assert v is not None
    assert v.home_road == route.origin  # the RESPONSE route starts at the road point
    assert haversine_m(route.origin, v.home or route.origin) > 30.0
    out = Out()
    until(eng, clock, out, gone(eng), answer=_to_road_point(route.origin))
    assert [r["kind"] for r in out.requests if r["type"] == "plan_request"] == ["TRANSPORT"]
    assert [(e["seq"], e["kind"]) for e in out.of("leg_ended")] == [
        (0, "RESPONSE"), (1, "TRANSPORT"),
    ]  # fmt: skip
    assert out.of("leg_plan_failed") == []
    assert out.of("back_in_service")[0]["end_reason"] == "CLEARED"


def test_return_found_to_have_no_length_brings_the_unit_back_in_service(clock: Clock) -> None:
    """A vehicle whose road point is unknown (restored from a live document written before
    it was kept) asks for the RETURN; the planner finds origin and station snap to the
    same road point and says so: no leg, back in service at the end of the handover."""
    eng = engine(scene_time_s=5.0, hospital_handover_s=10.0)
    route = straight_route(C, n=12)
    cmd = start_cmd(route)
    cmd["station_pos"] = _off_road_station(route)
    v = eng.start_dispatch(cmd, stream_id=None, wall=clock.now, sim=clock.now)
    assert v is not None
    v.home_road = None
    out = Out()
    until(eng, clock, out, lambda: v.pending is not None and v.pending.kind is LegKind.RETURN,
          answer=_to_road_point(route.origin))  # fmt: skip
    p = v.pending
    assert p is not None
    assert not eng.leg_not_needed(v.dispatch_id, p.seq + 1)  # not the pending request
    assert not eng.leg_not_needed("no-such-dispatch", p.seq)
    assert eng.leg_not_needed(v.dispatch_id, p.seq)
    assert v.pending is None
    assert v.home_road == v.pos
    assert v.live_json()["home_road"] == [round(v.pos.lat, 7), round(v.pos.lng, 7)]
    assert not eng.leg_not_needed(v.dispatch_id, p.seq)  # applied once
    until(eng, clock, out, gone(eng))
    assert [(e["seq"], e["kind"]) for e in out.of("leg_ended")] == [
        (0, "RESPONSE"), (1, "TRANSPORT"),
    ]  # fmt: skip
    types = out.types()
    order = ["handover_complete", "returning", "back_in_service"]
    assert [types.index(t) for t in order] == sorted(types.index(t) for t in order)


def test_a_transport_is_never_dropped_as_not_needed(clock: Clock) -> None:
    eng = engine(scene_time_s=5.0)
    route = straight_route(C, n=6)
    v = start(eng, clock, route)
    out = Out()
    until(eng, clock, out, lambda: v.pending is not None)
    assert v.pending is not None
    assert v.pending.kind is LegKind.TRANSPORT
    assert not eng.leg_not_needed(v.dispatch_id, v.pending.seq)
    assert v.pending is not None


# live-document fields stage B added (a stage-A leader never wrote them)
STAGE_B_ONLY = frozenset({
    "phase", "leg", "destination_info", "planning", "incident_location", "home", "home_road",
    "dwell_s", "dwell_target_s", "next_seq", "pending", "plan_error", "halt_at_m",
    "cancelled", "released", "trip_s", "leg_started_trip_s", "hospital", "reroutes",
    "reroute_after_t", "zones_seen",
})  # fmt: skip


@pytest.mark.parametrize(
    ("service", "itype", "next_kind"),
    [(ServiceType.FIRE_ENGINE, "FIRE", "RETURN"), (ServiceType.AMBULANCE, "MEDICAL", "TRANSPORT")],
)
def test_a_stage_a_unit_on_scene_finishes_its_dwell_and_gets_its_next_leg(
    clock: Clock, service: ServiceType, itype: str, next_kind: str
) -> None:
    """Stage 5 fix-2 B3: a unit ON_SCENE in a stage-A live document (no dwell target, no
    pending leg) used to clear at once and never get a leg (stuck forever). It now dwells
    for the rest of the service's scene time, then drives its next leg and is freed."""
    old = engine(scene_time_s=600.0)
    v = start(old, clock, straight_route(C, n=10), service=service, incident_type=itype)
    until(old, clock, Out(), lambda: not v.moving)
    live = {k: x for k, x in v.live_json().items() if k not in STAGE_B_ONLY}
    live["scene_s"] = 30.0  # 30 s into its 600 s on scene when the stack was upgraded

    eng = engine(scene_time_s=600.0, hospital_handover_s=20.0)
    nv = eng.restore_live(live, {"route": v.route.to_json(), "corridor": []}, {})
    assert (nv.phase, nv.dwell_s, nv.dwell_target_s) == (UnitStatus.ON_SCENE, 30.0, 600.0)
    assert nv.trip_s == pytest.approx(v.elapsed_s + 30.0, abs=0.01)
    (req,) = eng.pending_plan_requests()  # what the new leader hands to the planner
    assert (req["kind"], req["seq"]) == (next_kind, 1)
    assert req["start_at"] - req["requested_t"] == pytest.approx(570.0)
    out = Out()
    step(eng, clock, out, ticks=1138)  # 569 s: still on scene, nothing cleared yet
    assert nv.phase is UnitStatus.ON_SCENE
    assert not nv.released
    assert eng.apply_leg_planned(stub_leg_command(req), stream_id=None, wall=clock.now)
    until(eng, clock, out, gone(eng), answer=stub_answer())
    types = out.types()
    assert types[-1] == "back_in_service"
    assert (out.of("leg_started")[0]["kind"], out.of("leg_started")[0]["seq"]) == (next_kind, 1)
    if next_kind == "TRANSPORT":
        assert out.of("transport_started")[0]["scene_s"] == pytest.approx(600.0, abs=0.6)
    else:
        assert out.of("cleared")[0]["scene_s"] == pytest.approx(600.0)


def test_the_road_point_survives_a_restore(clock: Clock) -> None:
    eng = engine()
    route = straight_route(C, n=6)
    cmd = start_cmd(route)
    cmd["station_pos"] = _off_road_station(route)
    v = eng.start_dispatch(cmd, stream_id=None, wall=clock.now, sim=clock.now)
    assert v is not None
    back = Vehicle.restore(v.live_json(), v.route_json(), {})
    assert back.home_road is not None
    assert haversine_m(back.home_road, route.origin) < 0.01
    legacy = {k: val for k, val in v.live_json().items() if k != "home_road"}
    assert Vehicle.restore(legacy, v.route_json(), {}).home_road == route.origin  # RESPONSE
    on_transport = {**legacy, "leg": {"seq": 1, "kind": "TRANSPORT", "emergency": True}}
    assert Vehicle.restore(on_transport, v.route_json(), {}).home_road is None  # unknown


# ------------------------------------------------------------------------ re-route


def _replace(v: Vehicle, at_m: float, via_bearing: float, no: int) -> dict[str, Any]:
    origin, _ = interpolate_by_distance(v.route.points, v.route.cumulative, at_m)
    detour = destination_point(origin, via_bearing, 400.0)
    new = route_through([origin, detour, v.destination])
    return route_replaced_command(
        city_id="delhi", dispatch_id=v.dispatch_id, vehicle_id=v.vehicle_id, seq=v.leg_seq,
        reroute_no=no, origin_along_m=at_m, route=new, corridor=corridor_at(new, [8], "R"),
        saved_s=90.0, old_remaining_s=300.0, new_remaining_s=210.0, reason="test",
        route_choice=None,
    )  # fmt: skip


def test_route_replaced_releases_claims_off_the_new_path(clock: Clock) -> None:
    eng = engine(scene_time_s=5.0)
    route = straight_route(C, bearing=90.0, n=60)  # 3 km east
    v = start(eng, clock, route, corridor_at(route, [12, 40]))
    out = Out()
    until(eng, clock, out, lambda: "S12" in v.claims)
    old_len = v.route.length_m
    cmd = _replace(v, v.along_m + 20.0, 0.0, 1)
    assert eng.apply_route_replaced(cmd, stream_id=None, wall=clock.now)
    assert not eng.apply_route_replaced(cmd, stream_id=None, wall=clock.now)  # idempotent
    b = eng.drain()
    hist = [e for x in b.values() for e in x.history]
    pub = [m for x in b.values() for m in x.publish]
    assert "S12" not in v.claims  # beyond the re-route point, not on the new path
    rel = [e for e in hist if e["type"] == "preemption_released"]
    assert [(e["signal_id"], e["outcome"]) for e in rel] == [("S12", "REROUTED")]
    (rr,) = [e for e in hist if e["type"] == "rerouted"]
    assert (rr["reroute_no"], rr["seq"]) == (1, 0)
    assert [m["type"] for m in pub] == ["route_replaced"]
    assert v.reroutes == 1
    assert v.route.length_m != pytest.approx(old_len)
    assert [c.signal_id for c in v.corridor] == ["R8"]
    late = _replace(v, v.along_m - 1.0, 0.0, 2)  # a re-route point already behind it
    assert not eng.apply_route_replaced(late, stream_id=None, wall=clock.now)
    until(eng, clock, out, gone(eng), answer=stub_answer())
    assert out.of("leg_ended", seq=0)[0]["reroutes"] == 1


def test_a_junction_reclaimed_after_a_reroute_gets_its_own_event_ids(clock: Clock) -> None:
    """Stage 5 fix-2 B1: a junction dropped by a re-route and crossed again by the new
    path is claimed again; that second claim and its CLEARED release must not reuse the
    first claim's ids (the persister would drop them as duplicates)."""
    eng = engine(scene_time_s=5.0)
    route = straight_route(C, bearing=90.0, n=60)  # 3 km east, junction S20 at 1 km
    v = start(eng, clock, route, corridor_at(route, [20]))
    out = Out()
    until(eng, clock, out, lambda: "S20" in v.claims and v.claims["S20"].conflicts >= 0)
    at_m = v.along_m + 120.0
    assert at_m < route.cumulative[20]  # S20 lies beyond the re-route point: released
    origin, _ = interpolate_by_distance(route.points, route.cumulative, at_m)
    new = route_through([origin, route.points[20], route.destination])
    i20 = min(range(len(new.points)), key=lambda i: haversine_m(new.points[i], route.points[20]))
    (c,) = corridor_at(new, [i20])
    same = type(c)("S20", c.pos, c.along_m, c.offset_m, c.lane, c.approach_bearing)
    cmd = route_replaced_command(
        city_id="delhi", dispatch_id=v.dispatch_id, vehicle_id=v.vehicle_id, seq=0,
        reroute_no=1, origin_along_m=at_m, route=new, corridor=[same], saved_s=90.0,
        old_remaining_s=300.0, new_remaining_s=210.0, reason="test", route_choice=None,
    )  # fmt: skip
    assert eng.apply_route_replaced(cmd, stream_id=None, wall=clock.now)
    drained(eng, clock, out)
    assert "S20" not in v.claims
    until(eng, clock, out, lambda: "S20" in v.claims)
    held = v.claims["S20"]  # the re-claim on the new path, as a new leader restores it
    assert Claim.from_json(json.loads(json.dumps(held.to_json(v)))).reroute_no == 1
    until(eng, clock, out, gone(eng), answer=stub_answer(corridor_every=None))
    claimed = out.of("preemption_claimed", signal_id="S20")
    released = out.of("preemption_released", signal_id="S20")
    assert [(e["reroute_no"], e["leg_seq"]) for e in claimed] == [(0, 0), (1, 0)]
    assert [e["outcome"] for e in released] == ["REROUTED", "CLEARED"]
    assert claimed[0]["event_id"] == event_id("clm", v.dispatch_id, "S20")  # unchanged
    assert claimed[1]["event_id"] == event_id("clm", v.dispatch_id, "S20", "r1")
    assert [e["claim_event_id"] for e in released] == [e["event_id"] for e in claimed]
    ids = [e["event_id"] for e in out.history]
    assert len(ids) == len(set(ids)), "every event id unique: nothing de-duplicated"


def test_a_new_fire_zone_ahead_triggers_a_reroute_request(clock: Clock) -> None:
    eng = engine(scene_time_s=600.0)
    route = straight_route(C, bearing=90.0, n=60)
    v = start(eng, clock, route)
    out = Out()
    step(eng, clock, out, ticks=10)
    ahead = route.points[40]  # a fire 2 km ahead, on the route
    start(eng, clock, route_through([destination_point(ahead, 0.0, 300.0), ahead]),
          incident_type="FIRE", service=ServiceType.FIRE_ENGINE, vehicle_id="FIRE-1")  # fmt: skip
    step(eng, clock, out, ticks=2)
    reqs = [r for r in out.requests if r["type"] == "reroute_request"]
    assert [(r["vehicle_id"], r["reroute_no"], r["kind"]) for r in reqs] == [
        ("AMB-1", 1, "RESPONSE"),
    ]  # fmt: skip
    assert reqs[0]["reason"] == "fire incident on the route"
    assert reqs[0]["origin_along_m"] > v.along_m
    eng.reroute_answered(v.dispatch_id, 0, 1)  # the planner kept the route
    assert v.reroute_pending is None


@pytest.mark.parametrize("fire_during_dwell", [True, False])
def test_a_fire_zone_created_during_the_dwell_reroutes_the_planned_transport(
    clock: Clock, fire_during_dwell: bool
) -> None:
    """Stage 5 fix-3: the TRANSPORT route is chosen at the start of the scene dwell; a FIRE
    zone that appears on it during the dwell is new to that route, so the leg's start
    checks it (design §2 trigger (a)). A zone the planner already knew is not re-checked."""
    eng = engine(scene_time_s=120.0)
    v = start(eng, clock, straight_route(C, bearing=90.0, n=10))
    out = Out()
    scene = v.incident_pos
    assert scene is not None
    fire = destination_point(scene, 0.0, 500.0)  # on the stub TRANSPORT route (800 m north)

    def dispatch_fire() -> None:
        fire_route = route_through([destination_point(fire, 90.0, 300.0), fire])
        start(eng, clock, fire_route, incident_type="FIRE", service=ServiceType.FIRE_ENGINE,
              vehicle_id="FIRE-1")  # fmt: skip

    if not fire_during_dwell:
        dispatch_fire()  # known when the TRANSPORT is planned
    until(eng, clock, out, lambda: v.ready is not None, answer=stub_answer(None))
    assert v.leg_kind is LegKind.RESPONSE
    assert v.dwell_s < v.dwell_target_s
    if fire_during_dwell:
        dispatch_fire()  # after the TRANSPORT was planned, before it starts
    until(eng, clock, out, lambda: v.leg_kind is LegKind.TRANSPORT, answer=stub_answer(None))
    step(eng, clock, out, ticks=4, answer=stub_answer(None))
    reqs = [
        r for r in out.requests if r["type"] == "reroute_request" and r["vehicle_id"] == "AMB-1"
    ]
    if not fire_during_dwell:
        assert reqs == []
        return
    assert [(r["kind"], r["seq"], r["reroute_no"], r["reason"]) for r in reqs] == [
        ("TRANSPORT", 1, 1, "fire incident on the route"),
    ]  # fmt: skip
    eng.reroute_answered(v.dispatch_id, 1, 1)  # kept: the hysteresis is the planner's
    assert v.reroute_pending is None
    assert v.reroutes == 0


def test_running_late_triggers_a_reroute_but_never_more_than_two(clock: Clock) -> None:
    eng = engine(scene_time_s=5.0)
    route = straight_route(C, bearing=90.0, n=80)  # 4 km
    v = start(eng, clock, route, predicted_eta_s=30.0)  # an over-optimistic plan
    out = Out()
    until(eng, clock, out, lambda: any(r["type"] == "reroute_request" for r in out.requests))
    (req,) = [r for r in out.requests if r["type"] == "reroute_request"]
    assert req["reason"] == "running late"
    assert (
        eng.apply_route_replaced(
            _replace(v, req["origin_along_m"], 10.0, 1), stream_id=None, wall=clock.now
        )
        or v.along_m > req["origin_along_m"] - 5.0
    )
    v.reroutes = 2  # the cap (design: at most 2 re-routes per leg)
    v.reroute_pending, v.reroute_after_t = None, 0.0
    before = len(out.requests)
    step(eng, clock, out, ticks=300)
    assert not [r for r in out.requests[before:] if r["type"] == "reroute_request"]


def test_non_emergency_legs_never_reroute(clock: Clock) -> None:
    eng = engine()
    route = straight_route(C, bearing=90.0, n=80)
    cmd = start_cmd(route, predicted_eta_s=10.0)
    cmd["emergency"], cmd["leg_kind"] = False, "RETURN"
    v = eng.start_dispatch(cmd, stream_id=None, wall=clock.now, sim=clock.now)
    assert v is not None
    out = Out()
    step(eng, clock, out, ticks=400)
    assert not [r for r in out.requests if r["type"] == "reroute_request"]


# ------------------------------------------------- regression: failover on TRANSPORT


async def test_failover_mid_transport_releases_every_claim(
    make_worker: Callable[..., UnitWorker], fake_redis: Redis, clock: Clock
) -> None:
    w1, w2 = make_worker("w1"), make_worker("w2")
    assert await w1.try_lead()
    w1.stub().corridor_every = 6  # TRANSPORT / RETURN legs cross stub junctions
    route = straight_route(C, n=20)
    cmd = start_cmd(route, corridor_at(route, [8, 16]))
    cmd["incident_type"] = IncidentType.MEDICAL.value
    await w1.send(cmd)

    def transporting_with_claims() -> bool:
        assert w1.engine is not None
        v = w1.engine.vehicles.get("delhi", {}).get("AMB-1")
        return v is not None and v.phase is UnitStatus.TRANSPORTING and bool(v.claims)

    await w1.run_until(clock, transporting_with_claims)
    await fake_redis.delete(keys.LEADER_LOCK)  # w1 dies mid-TRANSPORT, holding claims
    assert await w2.try_lead()
    w2.stub().corridor_every = 6
    assert w2.engine is not None
    restored = w2.engine.vehicles["delhi"]["AMB-1"]
    assert restored.leg_kind is LegKind.TRANSPORT
    assert restored.claims  # resumed with the claims it held
    await w2.run_until(clock, lambda: w2.engine is not None and not w2.engine.active_count())
    assert await claim_keys(fake_redis) == []
    events = await history(fake_redis)
    claimed = {e["event_id"] for e in events if e["type"] == "preemption_claimed"}
    released = {e["claim_event_id"] for e in events if e["type"] == "preemption_released"}
    assert claimed
    assert claimed == released
    assert any(e["type"] == "preemption_claimed" and e["leg_seq"] == 1 for e in events)
    assert [e["type"] for e in events].count("back_in_service") == 1


# ------------------------------------------- Stage A gap: junction geometry in Redis


async def test_junction_geometry_is_cached_in_redis_and_seeded_on_take_over(
    make_worker: Callable[..., UnitWorker], fake_redis: Redis, clock: Clock
) -> None:
    w1, w2 = make_worker("w1"), make_worker("w2")
    assert await w1.try_lead()
    route = straight_route(C, bearing=30.0, n=20)
    await w1.send(start_cmd(route, corridor_at(route, [8, 16])))
    await w1.run_until(clock, lambda: bool(w1.engine and w1.engine.junctions.city("delhi")))
    cached = await fake_redis.hgetall(keys.junctions("delhi"))
    assert set(cached) >= {"S8"}
    assert w1.engine is not None
    first = w1.engine.junctions.known_geometry("delhi", "S8")
    assert first is not None
    doc = json.loads(cached["S8"])
    assert doc["legs"] == first.legs
    assert doc["approach_bearings"] == [round(b, 2) for b in first.approach_bearings]

    await fake_redis.delete(keys.LEADER_LOCK)  # w1 dies
    assert await w2.try_lead()
    assert w2.engine is not None
    seeded = w2.engine.junctions.known_geometry("delhi", "S8")
    assert seeded is not None
    assert seeded.legs == first.legs
    assert seeded.approach_bearings == pytest.approx(first.approach_bearings, abs=0.01)
    # a later approach from another direction maps onto the SAME phases (no re-derivation)
    other = corridor_at(straight_route(C, bearing=120.0, n=4), [2])[0]
    moved = type(other)("S8", other.pos, other.along_m, 0.0, other.lane, 120.0)
    assert w2.engine.junctions.geometry("delhi", moved) == seeded
