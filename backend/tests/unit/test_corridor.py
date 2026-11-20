"""Corridor building (design §3, §4.2): every pass through a junction, movements and lane
advice per pass, keys, JSON round trip and the stored-corridor backward compatibility."""

from __future__ import annotations

import pytest

from divert.domain.enums import Lane, Movement
from divert.domain.geo import LatLng, destination_point, haversine_m
from divert.domain.models import CorridorCandidate, Signal
from divert.domain.route import CorridorSignal, corridor_key
from divert.services.corridor import build_corridor, project_passes
from tests.support import city, route_through

J = city().center


def loop_route() -> object:
    """South -> J -> 400 m north -> around a block -> back south through J -> 300 m east."""
    n = destination_point(J, 0.0, 400.0)
    ne = destination_point(n, 90.0, 60.0)
    back = destination_point(J, 90.0, 60.0)
    return route_through(
        [destination_point(J, 180.0, 300.0), J, n, ne, back, J, destination_point(J, 270.0, 300.0)],
        step_m=10.0,
    )


def test_a_route_through_the_same_junction_twice_has_two_passes() -> None:
    route = loop_route()
    passes = project_passes(J, route, 30.0)  # type: ignore[arg-type]
    assert len(passes) == 2
    assert passes[0].offset_m < 1.0
    assert passes[1].offset_m < 1.0
    assert passes[1].along_m - passes[0].along_m > 800.0
    corridor = build_corridor(route, [CorridorCandidate(Signal("J", "delhi", None, J, (1,)), 0.0)])  # type: ignore[arg-type]
    assert [(c.signal_id, c.pass_no, c.key) for c in corridor] == [("J", 0, "J"), ("J", 1, "J#1")]
    assert corridor[0].movement is Movement.STRAIGHT  # northbound, straight through
    assert corridor[1].movement is Movement.STRAIGHT  # back from the east, on to the west
    assert corridor_key("J", 2) == "J#2"


def test_wiggling_out_of_the_buffer_is_still_one_pass() -> None:
    # runs 20 m east of the junction, swings out to 40 m for a moment, comes back
    def at(east: float, north: float) -> LatLng:
        return destination_point(destination_point(J, 0.0, north), 90.0, east)

    route = route_through(
        [at(20, -200), at(20, -15), at(40, 0), at(20, 15), at(20, 200)], step_m=2.0
    )
    # it does leave the 30 m buffer in between
    assert haversine_m(at(20, -15), J) < 30.0 < haversine_m(at(40, 0), J)
    assert len(project_passes(J, route, 30.0)) == 1


def test_corridor_entries_carry_movement_and_lane_advice() -> None:
    a = destination_point(J, 180.0, 500.0)
    j2 = destination_point(J, 0.0, 400.0)
    route = route_through([a, J, j2, destination_point(j2, 270.0, 400.0)], step_m=10.0)
    corridor = build_corridor(
        route,
        [
            CorridorCandidate(Signal("J2", "delhi", "Second", j2, (2,)), 0.0),
            CorridorCandidate(Signal("J1", "delhi", "First", J, (1,)), 0.0),
        ],
    )
    assert [c.signal_id for c in corridor] == ["J1", "J2"]  # route order
    j1c, j2c = corridor
    assert (j1c.movement, j2c.movement) == (Movement.STRAIGHT, Movement.LEFT)
    assert j2c.advice is not None
    assert j2c.advice.lane_index == 1
    assert j2c.lane is Lane.LEFT
    assert j1c.name == "First"
    back = CorridorSignal.from_json(j2c.to_json())
    assert back.to_json() == j2c.to_json()
    assert back.advice is not None
    assert back.advice.text == j2c.advice.text


def test_corridor_json_written_before_stage_5a_still_reads() -> None:
    old = {
        "signal_id": "J1", "name": None, "pos": [28.6, 77.2], "along_m": 120.5,
        "offset_m": 3.2, "lane": "L", "approach_bearing": 91.0,
    }  # fmt: skip
    c = CorridorSignal.from_json(old)
    assert (c.pass_no, c.movement, c.free_left, c.legs, c.advice) == (
        0,
        Movement.STRAIGHT,
        False,
        None,
        None,
    )
    assert c.lane is Lane.LEFT
    assert c.key == "J1"
    assert c.pos == LatLng(28.6, 77.2)
    assert c.along_m == pytest.approx(120.5)
