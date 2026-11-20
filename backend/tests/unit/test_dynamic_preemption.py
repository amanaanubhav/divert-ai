"""The engine with dynamic pre-emption (design §4): dynamic claim lead, compatible and
incompatible claimants, the priority window, speed advisories, release past the box,
recovery, max-hold TIMEOUT and re-claim, multi-pass claims, free lefts, destinations in
the box, the obey-the-signal motion of non-emergency vehicles, and the live JSON."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from divert.domain.enums import (
    IncidentType,
    JunctionState,
    Lane,
    Movement,
    ServiceType,
    SignalState,
)
from divert.domain.geo import LatLng, destination_point, project_onto_polyline
from divert.domain.models import CorridorCandidate, IncidentZone, Signal
from divert.domain.route import CorridorSignal, Route
from divert.domain.signal_plan import PhasePlan
from divert.services.corridor import build_corridor
from divert.services.eta import EtaQuery, KinematicEtaModel
from divert.services.preemption import PreemptionRules
from divert.services.simulation import SimConfig, SimulationEngine, Vehicle, VehicleStatus
from divert.services.traffic import ModelledTraffic
from divert.store.events import event_id
from tests.support import Clock, city, route_through, start_cmd

DELHI = city("delhi")
C = DELHI.center
J = destination_point(C, 90.0, 1500.0)


def corridor(route: Route, signals: dict[str, LatLng]) -> list[CorridorSignal]:
    cands = [CorridorCandidate(Signal(s, "delhi", None, p, (1,)), 0.0) for s, p in signals.items()]
    return build_corridor(route, cands)


def make_engine(
    rules: PreemptionRules | None = None, scene_time_s: float = 5.0
) -> SimulationEngine:
    return SimulationEngine(
        {"delhi": DELHI},
        KinematicEtaModel(),
        SimConfig(
            scene_time_s=scene_time_s, position_sample_s=5.0, rules=rules or PreemptionRules()
        ),
    )


def run(
    eng: SimulationEngine,
    clock: Clock,
    *,
    until: Callable[[], bool],
    max_ticks: int = 4000,
    each: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for _ in range(max_ticks):
        if until():
            return events
        now = clock.advance(0.5)
        for b in eng.tick(dt_sim=0.5, sim=now, wall=now).values():
            events += b.history
            if each is not None:
                each({"signals": dict(b.signal_sets), "dels": set(b.signal_dels), "tick": b.tick})
    raise AssertionError("did not finish")


def through(
    approach: float, *, before: float = 600.0, after: float = 300.0, turn: float = 0.0
) -> Route:
    return route_through(
        [
            destination_point(J, (approach + 180.0) % 360.0, before),
            J,
            destination_point(J, (approach + turn) % 360.0, after),
        ],
        step_m=10.0,
        speed=12.0,
    )


def start(
    eng: SimulationEngine, clock: Clock, route: Route, cor: Sequence[CorridorSignal], **kw: Any
) -> Vehicle:
    emergency = kw.pop("emergency", True)
    cmd = start_cmd(route, cor, **kw)
    cmd["emergency"] = emergency
    v = eng.start_dispatch(cmd, stream_id=None, wall=clock.now, sim=clock.now)
    assert v is not None
    return v


def of(events: list[dict[str, Any]], type_: str, **match: Any) -> list[dict[str, Any]]:
    return [
        e for e in events if e["type"] == type_ and all(e.get(k) == v for k, v in match.items())
    ]


# ----------------------------------------------------------------------- claim lead


def test_claim_happens_at_the_dynamic_lead_and_records_its_parts(clock: Clock) -> None:
    eng = make_engine()
    r = through(0.0, before=1500.0)
    cor = corridor(r, {"J": J})
    v = start(eng, clock, r, cor)
    etas: list[tuple[float, float]] = []  # (ETA to J, along) per tick before the claim

    def watch(_: dict[str, Any]) -> None:
        if "J" not in v.claims and v.status is VehicleStatus.EN_ROUTE:
            etas.append((v.signal_etas.get("J", 0.0), v.along_m))

    events = run(eng, clock, until=lambda: v.status is VehicleStatus.ON_SCENE, each=watch)
    (claimed,) = of(events, "preemption_claimed")
    lead = claimed["required_lead_s"]
    assert lead == pytest.approx(claimed["transition_s"] + claimed["queue_clear_s"] + 5.0, abs=0.15)
    prev_eta, prev_along = etas[-1]  # the last tick before the claim
    by_distance = cor[0].along_m - prev_along <= 150.0 + 12.0
    assert claimed["eta_s"] <= lead + 0.6 or by_distance  # claimed once within the lead ...
    assert prev_eta > lead - 1.5 or by_distance  # ... and not before (no fixed 45 s / 600 m)
    assert claimed["eta_s"] <= 90.0
    assert claimed["phase_state_at_claim"] in {"green_extend", "red_truncate"}
    assert claimed["movement"] == "STRAIGHT"
    assert claimed["lane"] == "R"
    assert claimed["lanes_total"] == 2
    (green,) = of(events, "preempt_green")
    (rel,) = of(events, "preemption_released")
    assert rel["outcome"] == "CLEARED"
    assert rel["reason"] == "passed"
    assert rel["crossed_at"] is not None
    assert events.index(claimed) < events.index(green) < events.index(rel)
    if claimed["phase_state_at_claim"] == "red_truncate":
        assert of(events, "preempt_transition")


def test_green_extension_needs_less_lead_than_red_truncation(clock: Clock) -> None:
    eng = make_engine()
    r = through(0.0)
    (c,) = corridor(r, {"J": J})
    v = start(eng, clock, r, [c])
    ctl, phase = eng.junctions.phase_of("delhi", c, clock.now.timestamp())
    plan = ctl.plan
    t = clock.now.timestamp()
    start_green = plan.next_green_at(phase, t)
    green_t = start_green + 20.0  # late in the vehicle's green: its queue has discharged
    red_t = start_green + plan.slot_s + 1.0  # 1 s into the next phase's green
    sim_green = datetime.fromtimestamp(green_t, UTC)
    sim_red = datetime.fromtimestamp(red_t, UTC)
    extend = eng._lead(v, c, ctl, phase, sim_green)
    truncate = eng._lead(v, c, ctl, phase, sim_red)
    assert extend.transition_s == 0.0
    assert extend.green_now
    assert truncate.transition_s == pytest.approx(9.0 + 5.0)  # rest of min green + clearance
    assert truncate.required_s > extend.required_s
    # at the start of its green the queue built over the red still has to clear
    early = eng._lead(v, c, ctl, phase, datetime.fromtimestamp(start_green + 1.0, UTC))
    assert early.green_now
    assert early.queue_clear_s > extend.queue_clear_s + 15.0


# ----------------------------------------------------------------------- claimants


def test_same_approach_claimants_are_compatible(clock: Clock) -> None:
    eng = make_engine()
    r1 = through(0.0, before=600.0)
    r2 = through(0.0, before=760.0)
    v1 = start(eng, clock, r1, corridor(r1, {"J": J}), vehicle_id="AMB-1")
    v2 = start(
        eng, clock, r2, corridor(r2, {"J": J}), vehicle_id="PCR-1", service=ServiceType.POLICE
    )
    both: list[dict[str, Any]] = []

    def watch(batch: dict[str, Any]) -> None:
        sig = batch["signals"].get("J")
        if sig and len(sig["claimants"]) == 2:
            both.append(sig)

    run(
        eng,
        clock,
        until=lambda: v1.status is VehicleStatus.ON_SCENE and v2.status is VehicleStatus.ON_SCENE,
        each=watch,
    )
    assert both, "the two never held J together"
    for sig in both:
        assert sig["state"] in {"TRANSITIONING", "PREEMPTED"}  # never QUEUED: same phase
        assert {c["state"] for c in sig["claimants"]} == {"PREEMPTED"}
    assert v1.preempted_passed == v2.preempted_passed == 1


def conflict_pair(
    eng: SimulationEngine, clock: Clock, *, amb_before: float
) -> tuple[Vehicle, Vehicle, CorridorSignal]:
    pr = through(270.0 + 180.0, before=400.0)  # police from the west, heading east
    ar = through(180.0, before=amb_before)  # ambulance from the north, heading south
    pcor, acor = corridor(pr, {"J": J}), corridor(ar, {"J": J})
    pcr = start(eng, clock, pr, pcor, vehicle_id="PCR-1", service=ServiceType.POLICE)
    amb = start(eng, clock, ar, acor, vehicle_id="AMB-1")
    return pcr, amb, acor[0]


def test_incompatible_claimants_are_sequenced_and_the_loser_is_advised(clock: Clock) -> None:
    eng = make_engine()
    _pcr, amb, aj = conflict_pair(eng, clock, amb_before=700.0)  # ambulance > 10 s behind
    rules = eng.config.rules
    line = rules.stop_line_m(aj.along_m, aj.legs)
    queued: list[dict[str, Any]] = []
    advisories: list[float] = []
    violations: list[float] = []

    def watch(batch: dict[str, Any]) -> None:
        sig = batch["signals"].get("J")
        claim = amb.claims.get("J")
        if sig and sig["state"] == "QUEUED":
            queued.append(sig)
        if claim is not None and claim.state is SignalState.CONFLICT:
            if amb.speed_advisory_mps is not None:
                advisories.append(amb.speed_advisory_mps)
            if amb.along_m > line + 0.31:
                violations.append(amb.along_m)

    events = run(eng, clock, until=lambda: amb.status is VehicleStatus.ON_SCENE, each=watch)
    conflicts = of(events, "preemption_conflict")
    assert [e["vehicle_id"] for e in conflicts] == ["AMB-1"]
    assert conflicts[0]["winner"] == "PCR-1"
    assert conflicts[0]["expected_green_at"] is not None
    assert queued, "the junction never showed QUEUED"
    q = queued[0]
    who = {c["vehicle_id"]: c for c in q["claimants"]}
    assert who["AMB-1"]["phase_state"] == "QUEUED"
    assert who["AMB-1"]["queue_pos"] == 1
    assert who["PCR-1"]["phase_state"] == "PREEMPTED"
    assert (who["AMB-1"]["rank"], who["PCR-1"]["rank"]) == (1, 3)
    assert who["AMB-1"]["movement"] == "STRAIGHT"
    assert all(a == 0.0 or a >= 2.0 for a in advisories)  # (it arrives after its green)
    assert not violations, "the queued ambulance entered the junction before its green"
    assert amb.preempted_passed == 1  # it got the junction after the police car


def test_priority_window_gives_the_ambulance_the_junction(clock: Clock) -> None:
    eng = make_engine()
    pcr, amb, _ = conflict_pair(eng, clock, amb_before=480.0)  # a few seconds behind
    pr_entry = pcr.corridor[0]
    line = eng.config.rules.stop_line_m(pr_entry.along_m, pr_entry.legs)
    advisories: list[float] = []
    violations: list[float] = []

    def watch(_: dict[str, Any]) -> None:
        claim = pcr.claims.get("J")
        if claim is not None and claim.state is SignalState.CONFLICT:
            if pcr.speed_advisory_mps is not None:
                advisories.append(pcr.speed_advisory_mps)
            if pcr.along_m > line + 0.31:
                violations.append(pcr.along_m)

    done = lambda: amb.status is VehicleStatus.ON_SCENE and pcr.status is VehicleStatus.ON_SCENE  # noqa: E731
    events = run(eng, clock, until=done, each=watch)
    conflicts = of(events, "preemption_conflict")
    assert conflicts
    assert all(e["vehicle_id"] == "PCR-1" for e in conflicts)
    assert all(e["winner"] == "AMB-1" for e in conflicts)
    # the police car, earlier but lower priority, is told to slow down (or stop at the line)
    assert advisories, "the queued police car got no speed advisory"
    assert all(a == 0.0 or a >= 2.0 for a in advisories)
    assert not violations, "the queued police car entered the junction before its green"
    assert pcr.preempted_passed == 1


# ----------------------------------------------------------------------- release


@pytest.mark.parametrize(("legs", "box"), [(None, 20.0), (5, 30.0)])
def test_release_past_the_box_then_recovering_then_normal(
    clock: Clock, legs: int | None, box: float
) -> None:
    eng = make_engine()
    r = through(0.0)
    (c,) = corridor(r, {"J": J})
    c = CorridorSignal(**{**{f: getattr(c, f) for f in c.__dataclass_fields__}, "legs": legs})
    v = start(eng, clock, r, [c])
    at_release: list[float] = []
    states: list[str] = []
    gone: list[bool] = []

    def watch(batch: dict[str, Any]) -> None:
        sig = batch["signals"].get("J")
        if sig:
            states.append(sig["state"])
        if "J" in batch["dels"]:
            gone.append(True)

    def released() -> bool:
        if "J" not in v.claims and "J" in v.passed and not at_release:
            at_release.append(v.along_m)
        return bool(gone) or (v.status is not VehicleStatus.EN_ROUTE and bool(gone))

    run(eng, clock, until=released, each=watch)
    assert c.along_m + box < at_release[0] <= c.along_m + box + 8.0  # one tick at <= 16 m/s
    assert "RECOVERING" in states
    assert states.index("RECOVERING") > max(
        i for i, s in enumerate(states) if s in {"PREEMPTED", "TRANSITIONING"}
    )
    assert states[-1] == "RECOVERING"  # ... and then it is deleted (NORMAL)


class FrozenEngine(SimulationEngine):
    """A vehicle in ``frozen`` does not move (broken down / blocked) but still claims."""

    frozen: set[str]

    def _advance(self, v: Vehicle, dt: float, sim: datetime, wall: datetime) -> None:
        if v.vehicle_id not in self.frozen:
            super()._advance(v, dt, sim, wall)
            return
        v.speed_mps = 0.0
        v.elapsed_s += dt
        self._refresh_eta(v, sim)
        self._track_claims(v, dt, wall)
        self._update_claims(v, wall, sim)
        self.batch(v.city_id).vehicle_sets[v.vehicle_id] = v.live_json()


def test_max_hold_times_out_and_the_vehicle_reclaims_later(clock: Clock) -> None:
    eng = FrozenEngine(
        {"delhi": DELHI}, KinematicEtaModel(), SimConfig(scene_time_s=5.0, rules=PreemptionRules())
    )
    eng.frozen = set()
    r = through(0.0, before=600.0)
    cor = corridor(r, {"J": J})
    v = start(eng, clock, r, cor)
    run(eng, clock, until=lambda: cor[0].along_m - v.along_m < 110.0)
    eng.frozen = {v.vehicle_id}  # it stops 100 m short of the junction
    run(
        eng,
        clock,
        until=lambda: "J" in v.claims and v.claims["J"].green_at is not None,
        max_ticks=200,
    )
    states: list[str] = []
    events = run(
        eng, clock, until=lambda: v.attempts.get("J", 0) >= 1, max_ticks=400,
        each=lambda b: states.append(b["signals"]["J"]["state"]) if "J" in b["signals"] else None,
    )  # fmt: skip
    (timeout,) = of(events, "preempt_timeout")
    assert timeout["hold_s"] > 60.0
    (rel,) = of(events, "preemption_released")
    assert (rel["outcome"], rel["reason"]) == ("TIMEOUT", "timeout")
    assert "J" not in v.claims
    assert "J" not in v.passed
    assert states[-1] == "RECOVERING"
    # no re-claim during the cool-down (cross traffic gets the junction back first) ...
    t_out = clock.now.timestamp()
    events = run(eng, clock, until=lambda: "J" in v.claims, max_ticks=200)
    (again,) = of(events, "preemption_claimed")
    assert again["attempt"] == 1
    assert again["event_id"] == event_id("clm", v.dispatch_id, "J", 0, 1)
    assert datetime.fromisoformat(again["claimed_at"]).timestamp() >= t_out + 29.5
    # ... and once it moves again it crosses on its new claim
    eng.frozen = set()
    events = run(eng, clock, until=lambda: v.status is VehicleStatus.ON_SCENE)
    assert [e["outcome"] for e in of(events, "preemption_released")] == ["CLEARED"]


def test_starvation_is_reported(clock: Clock) -> None:
    eng = FrozenEngine(
        {"delhi": DELHI}, KinematicEtaModel(),
        SimConfig(scene_time_s=5.0, rules=PreemptionRules(max_hold_s=1000.0)),
    )  # fmt: skip
    eng.frozen = set()
    r = through(0.0, before=300.0)
    v = start(eng, clock, r, corridor(r, {"J": J}))
    run(eng, clock, until=lambda: "J" in v.claims)
    eng.frozen = {v.vehicle_id}
    run(eng, clock, until=lambda: eng.starved.get("delhi", 0) > 0, max_ticks=400)
    assert eng.starved["delhi"] == 1


# ----------------------------------------------------------------------- special cases


def test_a_junction_crossed_twice_is_claimed_once_per_pass(clock: Clock) -> None:
    eng = make_engine()
    n = destination_point(J, 0.0, 400.0)
    back = destination_point(J, 90.0, 60.0)
    r = route_through(
        [destination_point(J, 180.0, 400.0), J, n, destination_point(n, 90.0, 60.0), back, J,
         destination_point(J, 270.0, 300.0)],
        step_m=10.0,
    )  # fmt: skip
    cor = corridor(r, {"J": J})
    assert [c.key for c in cor] == ["J", "J#1"]
    v = start(eng, clock, r, cor)
    events = run(eng, clock, until=lambda: v.status is VehicleStatus.ON_SCENE)
    claims = of(events, "preemption_claimed")
    assert [e["pass_no"] for e in claims] == [0, 1]
    assert [e["event_id"] for e in claims] == [
        event_id("clm", v.dispatch_id, "J"),
        event_id("clm", v.dispatch_id, "J", 1, 0),
    ]
    assert {e["outcome"] for e in of(events, "preemption_released")} == {"CLEARED"}
    assert v.preempted_passed == 2


def test_free_left_is_never_claimed(clock: Clock) -> None:
    eng = make_engine()
    r = through(0.0)
    (c,) = corridor(r, {"J": J})
    free = CorridorSignal(
        **{
            **{f: getattr(c, f) for f in c.__dataclass_fields__},
            "free_left": True,
            "movement": Movement.LEFT,
        }
    )
    v = start(eng, clock, r, [free])
    events = run(eng, clock, until=lambda: v.status is VehicleStatus.ON_SCENE)
    assert not of(events, "preemption_claimed")
    assert "J" in v.passed


@pytest.mark.parametrize(
    ("end_before_centre", "claimed"), [(15.0, False), (5.0, True), (0.0, True)]
)
def test_destination_inside_the_junction_box(
    clock: Clock, end_before_centre: float, claimed: bool
) -> None:
    eng = make_engine()
    start_pt = destination_point(J, 180.0, 600.0)
    r = route_through(
        [start_pt, destination_point(J, 180.0, end_before_centre)]
        if end_before_centre
        else [start_pt, J],
        step_m=10.0,
    )
    proj = project_onto_polyline(J, r.points, r.cumulative)
    c = CorridorSignal(
        "J",
        J,
        proj.along_m + end_before_centre if end_before_centre else proj.along_m,
        0.0,
        Lane.RIGHT,
        0.0,
    )
    v = start(eng, clock, r, [c])
    events = run(eng, clock, until=lambda: v.status is VehicleStatus.ON_SCENE)
    assert bool(of(events, "preemption_claimed")) is claimed
    if claimed:  # released on arrival (it never got past the box)
        assert of(events, "preemption_released")[0]["reason"] == "arrived"


# ----------------------------------------------------------------------- non-emergency


RED_BASE_T = 1_795_000_000.0  # 2026-11-18 11:06 UTC: the hour all red-light runs happen in


class AlwaysGreen:
    """A signal view with every junction green: the free (unsignalled) travel time."""

    def is_green(self, c: CorridorSignal, t: float) -> bool:
        del c, t
        return True


def red_on_arrival(c: CorridorSignal, eta_s: float, phase: int = 0) -> datetime:
    """A start time at which ``phase`` of J is ~40 s into its red when the vehicle arrives."""
    plan = PhasePlan.for_junction(c.signal_id, c.legs)
    base = RED_BASE_T
    green = plan.next_green_at(phase, base)
    return datetime.fromtimestamp(green + plan.green_s + 40.0 - eta_s, UTC)


def test_return_vehicle_stops_on_red_and_goes_on_green() -> None:
    r = through(0.0, before=500.0)
    (c,) = corridor(r, {"J": J})
    eng = make_engine()
    # the ETA at the hour the run happens in (congestion depends on the hour of day)
    at = datetime.fromtimestamp(RED_BASE_T, UTC)
    q = EtaQuery(
        r, ServiceType.AMBULANCE, DELHI, [c], 0.0, 0.0, at, emergency=False, signals=AlwaysGreen()
    )
    to_j = KinematicEtaModel().estimate(q).to_signals_s["J"]
    clock = Clock(red_on_arrival(c, to_j))
    v = start(eng, clock, r, [c], emergency=False)
    line = eng.config.rules.stop_line_m(c.along_m, c.legs)
    ctl, phase = eng.junctions.phase_of("delhi", c, clock.now.timestamp())
    waited: list[float] = []
    crossed_on: list[bool] = []

    def watch(_: dict[str, Any]) -> None:
        t = clock.now.timestamp()
        if v.speed_mps == 0.0 and abs(v.along_m - line) < 0.5:
            waited.append(t)
            assert not ctl.is_green_for(phase, t - 0.5)  # (moves off one tick after)
        if v.along_m > line + 0.3 and not crossed_on:
            crossed_on.append(ctl.is_green_for(phase, t) or ctl.is_green_for(phase, t - 0.5))

    events = run(eng, clock, until=lambda: v.status is VehicleStatus.ON_SCENE, each=watch)
    assert len(waited) * 0.5 > 20.0, "it should have waited at the red stop line"
    assert crossed_on == [True], "it crossed the stop line on red"
    assert not v.claims
    assert not of(events, "preemption_claimed")
    assert v.preempted_passed == 0


def test_junction_preempted_for_another_approach_counts_as_red() -> None:
    r_amb = through(180.0, before=500.0)  # ambulance from the north
    r_car = through(90.0, before=500.0)  # non-emergency vehicle from the west
    eng = make_engine()
    (c_car,) = corridor(r_car, {"J": J})
    at = datetime.fromtimestamp(RED_BASE_T, UTC)  # the hour the run happens in
    q = EtaQuery(
        r_car, ServiceType.POLICE, DELHI, [c_car], 0.0, 0.0, at, emergency=False,
        signals=AlwaysGreen(),
    )  # fmt: skip
    to_j = KinematicEtaModel().estimate(q).to_signals_s["J"]
    ctl_plan = PhasePlan.for_junction("J", None)
    base = RED_BASE_T
    # the car's plan green is due as it arrives: only the pre-emption makes it wait
    clock = Clock(datetime.fromtimestamp(ctl_plan.next_green_at(1, base) + 5.0 - to_j, UTC))
    amb = start(eng, clock, r_amb, corridor(r_amb, {"J": J}), vehicle_id="AMB-1")
    eng.junctions.phase_of("delhi", corridor(r_amb, {"J": J})[0], clock.now.timestamp())  # phase 0
    car = start(
        eng, clock, r_car, [c_car], vehicle_id="PCR-9", service=ServiceType.POLICE, emergency=False
    )
    line = eng.config.rules.stop_line_m(c_car.along_m, None)
    bad: list[float] = []
    held: list[bool] = []

    def watch(batch: dict[str, Any]) -> None:
        ctl = eng.junctions.find("delhi", "J")
        if ctl is not None and ctl.served_phase is not None and ctl.served_phase != 1:
            held.append(True)
            if car.along_m > line + 0.31:
                bad.append(car.along_m)

    run(eng, clock, until=lambda: car.status is VehicleStatus.ON_SCENE, each=watch)
    assert held, "the ambulance never held the junction"
    assert not bad, "the car entered a junction pre-empted for another approach"
    assert amb.preempted_passed == 1


def test_back_to_back_junctions_stop_before_the_first_box(clock: Clock) -> None:
    eng = make_engine()
    r = through(0.0, before=600.0)
    j2 = destination_point(J, 0.0, 12.0)
    cor = corridor(r, {"J": J, "J2": j2})
    v = start(eng, clock, r, cor, emergency=False)
    rules = eng.config.rules
    line1 = rules.stop_line_m(cor[0].along_m, None)
    line2 = rules.stop_line_m(cor[1].along_m, None)
    assert line1 < line2 < cor[0].along_m + rules.box_m(None)
    assert eng._shift_back(v, 1, line2) == pytest.approx(line1)
    v.along_m = line1 + 2.0  # already inside J's box: wait at J2's line
    assert eng._shift_back(v, 1, line2) == pytest.approx(line2)


# ----------------------------------------------------------------------- ETA and JSON


def test_eta_within_10pct_with_incident_zones_and_class_congestion(clock: Clock) -> None:
    eng = make_engine(scene_time_s=100_000.0)
    pts = [C, destination_point(C, 90.0, 3000.0)]
    r = route_through(pts, step_m=50.0)
    speeds = [
        16.7 if i % 6 < 3 else 7.0 for i in range(len(r.segment_speeds_mps))
    ]  # arterial/local
    r = Route.build(r.points, speeds)
    fire_at = destination_point(C, 90.0, 1500.0)
    other = route_through(
        [destination_point(fire_at, 0.0, 400.0), destination_point(fire_at, 0.0, 120.0)]
    )
    cmd = start_cmd(other, vehicle_id="FIRE-1", service=ServiceType.FIRE_ENGINE)
    cmd["incident_type"] = "FIRE"
    cmd["destination"] = [fire_at.lat, fire_at.lng]
    eng.start_dispatch(cmd, stream_id=None, wall=clock.now, sim=clock.now)
    zone = IncidentZone(cmd["incident_id"], IncidentType.FIRE, fire_at)
    model = KinematicEtaModel()
    q = EtaQuery(
        r, ServiceType.AMBULANCE, DELHI, [], 0.0, 0.0, clock.now, traffic=ModelledTraffic([zone])
    )
    predicted = model.estimate(q).to_destination_s
    no_zone = model.estimate(
        EtaQuery(r, ServiceType.AMBULANCE, DELHI, [], 0.0, 0.0, clock.now)
    ).to_destination_s
    assert predicted > no_zone * 1.1  # the fire zone matters on this route
    v = start(eng, clock, r, [], predicted_eta_s=predicted)
    run(eng, clock, until=lambda: v.status is VehicleStatus.ON_SCENE)
    assert v.travel_s == pytest.approx(predicted, rel=0.10)


def test_tick_and_signal_json_carry_the_new_fields(clock: Clock) -> None:
    eng = make_engine()
    r = through(0.0)
    v = start(eng, clock, r, corridor(r, {"J": J}))
    seen: list[dict[str, Any]] = []
    run(
        eng,
        clock,
        until=lambda: bool(seen),
        each=lambda b: seen.append(b) if "J" in b["signals"] else None,
    )
    b = seen[0]
    veh = b["tick"]["vehicles"][0]
    nm = veh["next_movement"]
    assert set(nm) >= {
        "signal_id",
        "movement",
        "lane_index",
        "lanes_total",
        "side",
        "text",
        "distance_m",
        "free_left",
    }
    assert nm["signal_id"] == "J"
    assert nm["side"] in {"L", "C", "R"}
    assert "speed_advisory_mps" in veh
    sig = b["signals"]["J"]
    assert sig["state"] in {s.value for s in JunctionState} - {"NORMAL"}
    assert set(sig["phase_info"]) >= {"transition_s_left", "expected_green_at", "hold_s"}
    assert sig["lane"] in {"L", "R", "C"}
    (cl,) = sig["claimants"]
    assert set(cl) >= {"vehicle_id", "service", "eta_s", "state", "movement", "rank"}
    assert cl["state"] in {"PREEMPTED", "CONFLICT"}
    live = v.live_json()
    assert live["next_movement"] == v.next_movement()
    assert live["emergency"] is True
    assert live["rank"] == 1
