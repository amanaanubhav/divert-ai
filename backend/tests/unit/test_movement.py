"""Turn classification at a junction (design §3): thresholds, OSRM bearings and
modifiers, roundabouts, U-turns, free lefts, leg counts and degenerate geometry."""

from __future__ import annotations

import logging

import pytest

from divert.domain.enums import Movement
from divert.domain.geo import (
    LatLng,
    bearing_delta_deg,
    destination_point,
    haversine_m,
    project_onto_polyline,
)
from divert.domain.movement import (
    approach_speed_mps,
    chord_bearing,
    class_distance,
    classify_turn,
    junction_movement,
    movement_from_modifier,
)
from divert.domain.route import Intersection, Maneuver, Route
from tests.support import city, route_through

J = city().center


def turn_route(turn_deg: float, *, approach: float = 0.0, leg_m: float = 200.0) -> Route:
    """Straight in on ``approach``, through J, out at ``approach + turn_deg``."""
    a = destination_point(J, (approach + 180.0) % 360.0, leg_m)
    b = destination_point(J, (approach + turn_deg) % 360.0, leg_m)
    return route_through([a, J, b], step_m=10.0)


def along_of(route: Route, p: LatLng) -> float:
    return project_onto_polyline(p, route.points, route.cumulative).along_m


@pytest.mark.parametrize(
    ("delta", "movement"),
    [
        (0.0, Movement.STRAIGHT),
        (20.0, Movement.STRAIGHT),
        (-20.0, Movement.STRAIGHT),
        (20.01, Movement.SLIGHT_RIGHT),
        (-20.01, Movement.SLIGHT_LEFT),
        (45.0, Movement.SLIGHT_RIGHT),
        (-45.0, Movement.SLIGHT_LEFT),
        (45.01, Movement.RIGHT),
        (-45.01, Movement.LEFT),
        (135.0, Movement.RIGHT),
        (-135.0, Movement.LEFT),
        (135.01, Movement.SHARP_RIGHT),
        (-135.01, Movement.SHARP_LEFT),
        (169.99, Movement.SHARP_RIGHT),
        (-169.99, Movement.SHARP_LEFT),
        (170.0, Movement.U_TURN),
        (-170.0, Movement.U_TURN),
        (180.0, Movement.U_TURN),
        (-180.0, Movement.U_TURN),
    ],
)
def test_turn_thresholds_at_every_boundary(delta: float, movement: Movement) -> None:
    assert classify_turn(delta) is movement


def test_turn_classes_cover_the_whole_circle_symmetrically() -> None:
    mirror = {
        Movement.SLIGHT_LEFT: Movement.SLIGHT_RIGHT,
        Movement.LEFT: Movement.RIGHT,
        Movement.SHARP_LEFT: Movement.SHARP_RIGHT,
    }
    for deg in range(-180, 181):
        m = classify_turn(float(deg))
        assert classify_turn(float(-deg)) is mirror.get(
            m, {v: k for k, v in mirror.items()}.get(m, m)
        )
        if deg < -20 and m is not Movement.U_TURN:
            assert m.name.endswith("LEFT")  # negative = left


@pytest.mark.parametrize(
    ("a", "b", "gap"),
    [
        (Movement.STRAIGHT, Movement.STRAIGHT, 0),
        (Movement.STRAIGHT, Movement.SLIGHT_LEFT, 1),
        (Movement.SLIGHT_LEFT, Movement.SLIGHT_RIGHT, 2),
        (Movement.LEFT, Movement.SHARP_LEFT, 1),
        (Movement.SHARP_RIGHT, Movement.U_TURN, 1),
        (Movement.U_TURN, Movement.SHARP_LEFT, 1),
        (Movement.U_TURN, Movement.STRAIGHT, 4),
        (Movement.ROUNDABOUT, Movement.LEFT, 99),
    ],
)
def test_class_distance(a: Movement, b: Movement, gap: int) -> None:
    assert class_distance(a, b) == gap == class_distance(b, a)


def test_osrm_modifiers_map_to_movements() -> None:
    assert movement_from_modifier("sharp left") is Movement.SHARP_LEFT
    assert movement_from_modifier("uturn") is Movement.U_TURN
    assert movement_from_modifier(None) is None
    assert movement_from_modifier("weird") is None


@pytest.mark.parametrize(
    ("turn", "movement"),
    [
        (0.0, Movement.STRAIGHT),
        (30.0, Movement.SLIGHT_RIGHT),
        (-30.0, Movement.SLIGHT_LEFT),
        (90.0, Movement.RIGHT),
        (-90.0, Movement.LEFT),
        (150.0, Movement.SHARP_RIGHT),
        (-150.0, Movement.SHARP_LEFT),
    ],
)
def test_geometry_movement_from_the_route(turn: float, movement: Movement) -> None:
    r = turn_route(turn, approach=70.0)
    m = junction_movement(r, along_of(r, J), J, 0.0)
    assert m.movement is movement
    assert m.turn_deg == pytest.approx(turn, abs=1.0)
    assert m.approach_bearing == pytest.approx(70.0, abs=1.0)
    assert m.legs is None  # no OSRM intersection: leg count unknown
    assert not m.free_left


def test_u_turn_geometry() -> None:
    out = destination_point(J, 180.0, 300.0)
    r = route_through([out, J, destination_point(J, 90.0, 4.0), destination_point(out, 90, 4.0)])
    m = junction_movement(r, along_of(r, J), J, 0.0)
    assert m.movement is Movement.U_TURN


def test_osrm_intersection_bearings_and_leg_count_win_over_the_chords() -> None:
    r = turn_route(10.0)  # the chords say straight on (10 deg)
    along = along_of(r, J)
    # OSRM: arrive on the road at 180 (heading north), leave on the road at 90 -> right
    inter = Intersection(along, J, (0.0, 90.0, 180.0, 270.0, 315.0), in_idx=2, out_idx=1)
    r = Route.build(r.points, r.segment_speeds_mps, intersections=[inter])
    m = junction_movement(r, along, J, 0.0)
    assert m.movement is Movement.RIGHT
    assert m.legs == 5
    assert m.bearings == (0.0, 90.0, 180.0, 270.0, 315.0)
    # OSRM's arrive point lists a single road: not a junction, the leg count stays unknown
    arrive = Intersection(along, J, (180.0,), in_idx=0)
    r1 = Route.build(r.points, r.segment_speeds_mps, intersections=[arrive])
    assert junction_movement(r1, along, J, 0.0).legs is None
    # an intersection more than 15 m away is another junction
    far = Intersection(along, destination_point(J, 0.0, 20.0), (0.0, 90.0, 180.0), 2, 1)
    r2 = Route.build(r.points, r.segment_speeds_mps, intersections=[far])
    assert junction_movement(r2, along, J, 0.0).legs is None


def _with_maneuver(r: Route, along: float, type_: str, modifier: str | None, **kw: object) -> Route:
    man = Maneuver(along, type_, modifier, J, **kw)  # type: ignore[arg-type]
    return Route.build(r.points, r.segment_speeds_mps, maneuvers=[man])


def test_osrm_within_one_class_is_used() -> None:
    r = turn_route(40.0)  # geometry: SLIGHT_RIGHT
    along = along_of(r, J)
    m = junction_movement(_with_maneuver(r, along, "turn", "right"), along, J, 0.0)
    assert m.movement is Movement.RIGHT
    assert m.geometry_movement is Movement.SLIGHT_RIGHT


def test_osrm_disagreeing_by_more_than_one_class_loses_and_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    r = turn_route(-90.0)  # geometry: LEFT
    along = along_of(r, J)
    with caplog.at_level(logging.DEBUG, logger="divert.domain.movement"):
        m = junction_movement(_with_maneuver(r, along, "turn", "right"), along, J, 0.0)
    assert m.movement is Movement.LEFT
    assert m.osrm_modifier == "right"
    assert any("geometry wins" in rec.getMessage() for rec in caplog.records)


def test_roundabout_takes_the_osrm_exit() -> None:
    r = turn_route(90.0)
    along = along_of(r, J)
    m = junction_movement(_with_maneuver(r, along, "roundabout", "right", exit=3), along, J, 0.0)
    assert m.movement is Movement.ROUNDABOUT
    assert m.roundabout_exit == 3
    assert m.turn_deg == pytest.approx(90.0, abs=1.0)  # the exit side
    # a signal at the exit of a rotary: the exit number comes from the entry step
    entry = Maneuver(along - 60.0, "rotary", "straight", J, exit=2)
    leave = Maneuver(along, "exit rotary", "right", J)
    r2 = Route.build(r.points, r.segment_speeds_mps, maneuvers=[entry, leave])
    m2 = junction_movement(r2, along + 5.0, J, 0.0)
    assert (m2.movement, m2.roundabout_exit) == (Movement.ROUNDABOUT, 2)


def test_free_left_slip_road_is_detected() -> None:
    # the route leaves the approach 40 m before the junction centre and joins the cross
    # road 25 m past it on the left - it never comes closer than ~15 m to the centre
    approach = 0.0
    start = destination_point(J, 180.0, 250.0)
    leave = destination_point(J, 180.0, 40.0)
    join = destination_point(J, 270.0, 25.0)
    end = destination_point(J, 270.0, 250.0)
    r = route_through([start, leave, join, end], step_m=5.0)
    proj = project_onto_polyline(J, r.points, r.cumulative)
    assert proj.offset_m > 12.0
    m = junction_movement(r, proj.along_m, J, proj.offset_m)
    assert m.movement in (Movement.LEFT, Movement.SLIGHT_LEFT)
    assert m.free_left
    assert abs(bearing_delta_deg(m.approach_bearing, approach)) < 60.0


def test_left_through_the_junction_centre_is_not_free() -> None:
    r = turn_route(-90.0)
    m = junction_movement(r, along_of(r, J), J, 0.0)
    assert m.movement is Movement.LEFT
    assert not m.free_left
    # offset alone is not enough: the turn must complete >= 10 m before the junction
    m2 = junction_movement(r, along_of(r, J), J, 20.0)
    assert not m2.free_left


def test_route_ending_at_the_junction_and_starting_at_it() -> None:
    a = destination_point(J, 180.0, 200.0)
    ends = route_through([a, J])
    m = junction_movement(ends, ends.length_m, J, 0.0)
    assert m.exit_bearing is None
    assert m.movement is Movement.STRAIGHT
    starts = route_through([J, destination_point(J, 45.0, 200.0)])
    m2 = junction_movement(starts, 0.0, J, 0.0)
    assert m2.approach_bearing == pytest.approx(45.0, abs=1.0)


def test_zero_length_segments_and_short_chords() -> None:
    a = destination_point(J, 180.0, 100.0)
    pts = [a, a, J, J, destination_point(J, 90.0, 100.0)]
    r = Route.build(pts, [10.0] * 4)  # duplicates merged: no zero-length segment left
    assert len(r.points) == 3
    assert chord_bearing(r, 50.0, 50.4) is None  # < 1 m: no meaningful bearing
    m = junction_movement(r, along_of(r, J), J, 0.0)
    assert m.movement is Movement.RIGHT


def test_approach_speed_is_the_fastest_segment_just_before() -> None:
    a = destination_point(J, 180.0, 100.0)
    r = route_through([a, J, destination_point(J, 0.0, 100.0)], step_m=10.0)
    speeds = [14.0] * len(r.segment_speeds_mps)
    j = next(i for i, p in enumerate(r.points) if haversine_m(p, J) < 0.5)
    speeds[j - 1] = 3.0  # OSRM folds the turn penalty into the last segment
    r = Route.build(r.points, speeds)
    assert approach_speed_mps(r, r.cumulative[j]) == pytest.approx(14.0)
    assert approach_speed_mps(r, 0.0) == pytest.approx(14.0)
