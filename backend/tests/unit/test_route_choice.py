"""Crowd-aware route choice (design §2): junction delays, the 1.6x filter, costs under the
traffic model, incident zones, contention, tie-breaks and the recorded explanation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from divert.domain.enums import IncidentType, Lane, Movement, ServiceType
from divert.domain.geo import LatLng, destination_point
from divert.domain.models import IncidentZone
from divert.domain.route import CorridorSignal, Route
from divert.domain.signal_plan import PhasePlan
from divert.services.eta import KinematicEtaModel, expected_signal_wait_s
from divert.services.route_choice import (
    CANDIDATE_MAX_POINTS,
    Candidate,
    candidate_polyline,
    choose,
    junction_delay_s,
    make_candidate,
    score_route,
    turn_delay_s,
    within_length,
)
from divert.services.traffic import ModelledTraffic
from tests.support import city, route_through

PEAK = datetime(2026, 11, 17, 12, 30, tzinfo=UTC)  # 18:00 IST
ORIGIN = city().center
D = destination_point(ORIGIN, 90.0, 2000.0)


def two_routes(speed_a: float = 12.0, speed_b: float = 12.0) -> tuple[Route, Route]:
    """A: straight ORIGIN -> D. B: via a point 300 m north (about 10 % longer)."""
    a = route_through([ORIGIN, D], step_m=50.0, speed=speed_a)
    via = destination_point(destination_point(ORIGIN, 90.0, 1000.0), 0.0, 300.0)
    b = route_through([ORIGIN, via, D], step_m=50.0, speed=speed_b)
    return a, b


def sig(
    sid: str, along: float, movement: Movement, *, free_left: bool = False, **kw: object
) -> CorridorSignal:
    return CorridorSignal(
        sid, LatLng(28.6, 77.2), along, 0.0, Lane.RIGHT, 0.0,
        movement=movement, free_left=free_left, **kw,  # type: ignore[arg-type]
    )  # fmt: skip


@pytest.mark.parametrize(
    ("movement", "delay"),
    [
        (Movement.STRAIGHT, 0.0), (Movement.SLIGHT_LEFT, 1.0), (Movement.SLIGHT_RIGHT, 1.0),
        (Movement.LEFT, 3.0), (Movement.RIGHT, 6.0), (Movement.SHARP_LEFT, 8.0),
        (Movement.SHARP_RIGHT, 8.0), (Movement.U_TURN, 12.0),
    ],
)  # fmt: skip
def test_turn_delays(movement: Movement, delay: float) -> None:
    assert turn_delay_s(sig("J", 100.0, movement)) == delay


def test_special_junction_delays() -> None:
    assert turn_delay_s(sig("J", 1.0, Movement.LEFT, free_left=True)) == 0.0
    assert turn_delay_s(sig("J", 1.0, Movement.ROUNDABOUT, roundabout_exit=3)) == 4.0 + 6.0
    right = sig("J", 1.0, Movement.RIGHT, legs=4)
    assert junction_delay_s(right, emergency=True) == 6.0
    # a RETURN leg's signal waits are in its replayed ETA, not added again here
    assert junction_delay_s(right, emergency=False) == 6.0
    assert expected_signal_wait_s(right) == pytest.approx(
        PhasePlan.for_junction("J", 4).expected_wait_s()
    )
    free = sig("J", 1.0, Movement.LEFT, free_left=True)
    assert junction_delay_s(free, emergency=False) == 0.0  # bypasses the signal


def test_candidates_longer_than_1_6x_are_dropped() -> None:
    a, _ = two_routes()
    long = route_through([ORIGIN, destination_point(ORIGIN, 0.0, 1500.0), D], step_m=100.0)
    assert long.length_m > 1.6 * a.length_m
    assert within_length([a, long]) == [0]
    assert within_length([long, a]) == [1]
    assert within_length([]) == []


def score(routes: list[Route], traffic: ModelledTraffic, **kw: object) -> list[object]:
    model = KinematicEtaModel()
    return [
        score_route(
            i, r, (), eta_model=model, city=city(), service=ServiceType.AMBULANCE,
            at=PEAK, traffic=traffic, **kw,  # type: ignore[arg-type]
        )
        for i, r in enumerate(routes)
    ]  # fmt: skip


def test_less_congested_alternative_wins_at_peak() -> None:
    # A: arterial (54 km/h) takes the full evening drop; B: collector (47 km/h) only 70 %
    a, b = two_routes(speed_a=15.0, speed_b=13.0)
    ca, cb = score([a, b], ModelledTraffic())
    assert ca.base_s < cb.base_s  # free flow: A is faster ...  # type: ignore[attr-defined]
    choice = choose([ca, cb])  # type: ignore[list-item]
    assert choice.chosen.label == "B"  # ... at 18:00 B is
    assert ca.crowd_index > cb.crowd_index  # type: ignore[attr-defined]
    assert choice.reason.startswith("Route B:")
    assert "faster than A" in choice.reason
    assert "congested" in choice.reason


def test_incident_free_alternative_wins() -> None:
    a, b = two_routes()
    fire = IncidentZone("fire-9", IncidentType.FIRE, destination_point(ORIGIN, 90.0, 1000.0))
    ca, cb = score([a, b], ModelledTraffic([fire]))
    assert [z.incident_id for z in ca.zones] == ["fire-9"]  # type: ignore[attr-defined]
    assert cb.zones == ()  # type: ignore[attr-defined]
    choice = choose([ca, cb])  # type: ignore[list-item]
    assert choice.chosen.label == "B"
    assert "fire incident on the way" in choice.reason
    # without the fire, the shorter A wins
    plain = choose(score([a, b], ModelledTraffic()))  # type: ignore[arg-type]
    assert plain.chosen.label == "A"


def test_contention_adds_the_conflict_delay() -> None:
    a, _ = two_routes()
    j = sig("J1", 1000.0, Movement.STRAIGHT)
    model = KinematicEtaModel()
    free = score_route(0, a, [j], eta_model=model, city=city(), service=ServiceType.AMBULANCE,
                       at=PEAK, traffic=ModelledTraffic())  # fmt: skip
    eta_j = free.eta_s * 0.5  # roughly mid-route
    busy = score_route(0, a, [j], eta_model=model, city=city(), service=ServiceType.AMBULANCE,
                       at=PEAK, traffic=ModelledTraffic(),
                       others=[("FIRE-1", "J1", eta_j)])  # fmt: skip
    assert busy.contention == ("J1",)
    assert busy.cost_s == pytest.approx(free.cost_s + 10.0)


def cand(i: int, eta: float, corridor: list[CorridorSignal], route: Route | None = None) -> object:
    r = route or two_routes()[0]
    return make_candidate(
        i,
        r,
        corridor,
        eta_s=eta,
        base_s=eta * 0.8,
        factors=[1.0] * len(r.segment_speeds_mps),
        emergency=True,
    )


def test_within_5_percent_fewer_right_turns_then_shorter() -> None:
    rights = [sig("J1", 100.0, Movement.RIGHT), sig("J2", 900.0, Movement.U_TURN)]
    straight = [sig("J3", 100.0, Movement.STRAIGHT)]
    a = cand(0, 300.0, rights)  # cheapest, but 2 signalised right / U turns (+18 s delay)
    b = cand(1, 320.0, straight)  # 2 % dearer in total cost
    choice = choose([a, b])  # type: ignore[list-item]
    assert choice.chosen.label == "B"
    assert choice.tie_break == "fewer right turns"
    assert "within 5% of A" in choice.reason
    # more than 5 % apart: cost decides
    c = cand(1, 400.0, straight)
    assert choose([a, c]).chosen.label == "A"  # type: ignore[list-item]
    # equal turns and contention: the shorter one
    _, longer = two_routes()
    d = cand(0, 300.0, straight)
    e = cand(1, 299.0, straight, route=longer)
    pick = choose([d, e])  # type: ignore[list-item]
    assert pick.chosen.label == "A"
    assert pick.tie_break == "shorter"


def test_route_choice_json_records_every_candidate() -> None:
    a, b = two_routes()
    fire = IncidentZone("fire-9", IncidentType.FIRE, destination_point(ORIGIN, 90.0, 1000.0))
    choice = choose(score([a, b], ModelledTraffic([fire])))  # type: ignore[arg-type]
    doc = choice.to_json()
    assert doc["chosen"] == "B"
    assert doc["reason"] == choice.reason
    assert [c["label"] for c in doc["candidates"]] == ["A", "B"]
    first = doc["candidates"][0]
    assert set(first) >= {
        "distance_m", "base_time_s", "congested_time_s", "junction_delay_s", "penalties_s",
        "cost_s", "crowd_index", "right_turns", "contention_junctions", "incident_zones",
        "source", "signals",
    }  # fmt: skip
    assert first["incident_zones"] == [{"incident_id": "fire-9", "type": "FIRE"}]
    assert 0.0 <= first["crowd_index"] <= 1.0
    assert first["congested_time_s"] > first["base_time_s"]
    only = choose([choice.candidates[0]])
    assert only.reason == "Route A: the only route"


# ------------------------------------------------ candidate geometry (fix-2 B7)


def test_every_candidate_carries_a_simplified_bounded_polyline() -> None:
    a, b = two_routes()
    cands = [
        make_candidate(i, r, [], eta_s=100.0, base_s=90.0, factors=[1.0] * (len(r.points) - 1),
                       emergency=True)
        for i, r in enumerate((a, b))
    ]  # fmt: skip
    doc = choose(cands).to_json()
    lines = {c["label"]: c["route"] for c in doc["candidates"]}
    assert lines["A"] == [
        [round(ORIGIN.lat, 5), round(ORIGIN.lng, 5)],
        [round(D.lat, 5), round(D.lng, 5)],
    ]
    assert len(lines["B"]) == 3  # straight legs: only the ends and the corner remain
    via = b.points[len(b.points) // 2]
    assert min(abs(p[0] - via.lat) + abs(p[1] - via.lng) for p in lines["B"]) < 2e-4


def test_a_long_winding_candidate_is_capped() -> None:
    pts = [ORIGIN]
    for i in range(1000):  # 10 km of 10 m zig-zags 30 m wide: nothing within 10 m
        p = destination_point(pts[0], 90.0, 10.0 * (i + 1))
        pts.append(destination_point(p, 0.0, 30.0) if i % 2 else p)
    route = Route.build(pts, [12.0] * (len(pts) - 1))
    line = candidate_polyline(route)
    assert 2 <= len(line) <= CANDIDATE_MAX_POINTS
    assert line[0] == [round(ORIGIN.lat, 5), round(ORIGIN.lng, 5)]


# ------------------------------------------------ "why this route" wording (fix-3)


def _cand(label: str, route: Route, eta_s: float, junction_s: float, right_turns: int) -> Candidate:
    return Candidate(
        label, "osrm", route, (), eta_s=eta_s, base_s=eta_s, junction_delay_s=junction_s,
        penalties_s=0.0, crowd_index=0.0, right_turns=right_turns,
    )  # fmt: skip


def test_reason_says_faster_only_when_the_travel_time_is_lower() -> None:
    a, b = two_routes()
    # B drives 24 s faster and has one right turn fewer: "faster" by the travel time
    choice = choose([_cand("A", a, 240.0, 6.0, 1), _cand("B", b, 216.0, 0.0, 0)])
    assert choice.chosen.label == "B"
    assert choice.reason == "Route B: 0.4 min faster than A (A: 1 extra right turn)"


def test_reason_says_cheaper_overall_when_not_faster() -> None:
    a, b = two_routes()
    # the live case: B 3:37, A 3:36 - B only wins on A's extra right turn (6 s)
    choice = choose([_cand("A", a, 216.0, 6.0, 1), _cand("B", b, 217.0, 0.0, 0)])
    assert choice.chosen.label == "B"
    assert choice.tie_break is None
    assert "faster" not in choice.reason
    assert choice.reason == (
        "Route B: preferred over A, 0.1 min cheaper overall on turn delays, same travel time"
        " (A: 1 extra right turn)"
    )
    # clearly slower but still cheaper (A has 40 s of turn delays): says so
    slow = choose([_cand("A", a, 200.0, 40.0, 0), _cand("B", b, 215.0, 0.0, 0)])
    assert slow.chosen.label == "B"
    assert slow.reason == (
        "Route B: preferred over A, 0.4 min cheaper overall on turn delays, though 0.2 min slower"
    )
