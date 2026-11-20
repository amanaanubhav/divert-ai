"""Lane model (design §3): road class, lanes, movement -> lane for 1-3 lanes, look-ahead,
advise-at distances, text and JSON."""

from __future__ import annotations

import pytest

from divert.domain.enums import Lane, Movement, RoadClass
from divert.domain.lanes import (
    KMH,
    LaneAdvice,
    LaneInput,
    lane_change_distance_m,
    lanes_per_direction,
    plan_lanes,
    road_class,
    side_for,
    target_lane,
)

# OSRM free-flow speeds that give 1, 2 and 3 lanes per direction
SPEED_FOR_LANES = {1: 25 * KMH, 2: 40 * KMH, 3: 65 * KMH}


@pytest.mark.parametrize(
    ("kmh", "cls", "lanes"),
    [
        (90.0, RoadClass.EXPRESSWAY, 3),
        (75.0, RoadClass.EXPRESSWAY, 3),
        (74.9, RoadClass.ARTERIAL, 3),
        (60.0, RoadClass.ARTERIAL, 3),
        (59.9, RoadClass.ARTERIAL, 2),
        (50.0, RoadClass.ARTERIAL, 2),
        (49.9, RoadClass.COLLECTOR, 2),
        (32.0, RoadClass.COLLECTOR, 2),
        (31.9, RoadClass.LOCAL, 1),
        (0.0, RoadClass.LOCAL, 1),
    ],
)
def test_road_class_and_lanes_from_free_flow_speed(kmh: float, cls: RoadClass, lanes: int) -> None:
    assert road_class(kmh * KMH) is cls
    assert lanes_per_direction(kmh * KMH) == lanes


# (movement, free_left, roundabout exit, emergency) -> target lane as a function of n
CASES: list[tuple[Movement, bool, int | None, bool, str]] = [
    (Movement.LEFT, False, None, True, "1"),
    (Movement.SLIGHT_LEFT, False, None, True, "1"),
    (Movement.SHARP_LEFT, False, None, True, "1"),
    (Movement.LEFT, True, None, True, "1"),  # free left
    (Movement.STRAIGHT, False, None, True, "n"),  # emergency: rightmost through lane
    (Movement.STRAIGHT, False, None, False, "mid"),  # non-emergency: middle lane
    (Movement.RIGHT, False, None, True, "n"),
    (Movement.SHARP_RIGHT, False, None, True, "n"),
    (Movement.SLIGHT_RIGHT, False, None, True, "n"),
    (Movement.U_TURN, False, None, True, "n"),
    (Movement.ROUNDABOUT, False, 1, True, "1"),
    (Movement.ROUNDABOUT, False, 2, True, "n"),
    (Movement.ROUNDABOUT, False, 4, True, "n"),
]


@pytest.mark.parametrize("lanes", [1, 2, 3])
@pytest.mark.parametrize(("movement", "free_left", "exit_no", "emergency", "want"), CASES)
def test_target_lane_for_every_movement_and_lane_count(
    movement: Movement, free_left: bool, exit_no: int | None, emergency: bool, want: str, lanes: int
) -> None:
    expected = {"1": 1, "n": lanes, "mid": (lanes + 1) // 2}[want]
    got = target_lane(
        movement, lanes, emergency=emergency, free_left=free_left, roundabout_exit=exit_no
    )
    assert got == expected
    adv = plan_lanes(
        [
            LaneInput(
                400.0,
                movement,
                SPEED_FOR_LANES[lanes],
                free_left=free_left,
                roundabout_exit=exit_no,
            )
        ],
        emergency=emergency,
    )[0]
    assert (adv.lane_index, adv.lanes_total) == (expected, lanes)
    assert adv.free_left is free_left
    assert adv.text.startswith(
        {1: "Single lane", 2: "Left lane" if expected == 1 else "Right lane"}.get(
            lanes, {1: "Left lane", 2: "Centre lane", 3: "Right lane"}[expected]
        )
    )
    assert f"({expected} of {lanes})" in adv.text


@pytest.mark.parametrize(
    ("lane", "lanes", "movement", "side"),
    [
        (1, 3, Movement.LEFT, Lane.LEFT),
        (2, 3, Movement.STRAIGHT, Lane.CENTRE),
        (3, 3, Movement.STRAIGHT, Lane.RIGHT),
        (1, 2, Movement.STRAIGHT, Lane.LEFT),
        (2, 2, Movement.RIGHT, Lane.RIGHT),
        (1, 1, Movement.STRAIGHT, Lane.CENTRE),  # "centre, traffic pulls left"
        (1, 1, Movement.LEFT, Lane.LEFT),
        (1, 1, Movement.RIGHT, Lane.RIGHT),
        (1, 1, Movement.U_TURN, Lane.RIGHT),
    ],
)
def test_side_is_l_c_r(lane: int, lanes: int, movement: Movement, side: Lane) -> None:
    assert side_for(lane, lanes, movement, free_left=False) is side


def test_unknown_roundabout_exit_uses_the_exit_side() -> None:
    assert target_lane(Movement.ROUNDABOUT, 3, emergency=True, turn_deg=-90.0) == 1
    assert target_lane(Movement.ROUNDABOUT, 3, emergency=True, turn_deg=60.0) == 3


def test_texts() -> None:
    right = plan_lanes([LaneInput(500.0, Movement.RIGHT, SPEED_FOR_LANES[3])], emergency=True)[0]
    assert right.text == "Right lane (3 of 3): protected right turn"
    one = plan_lanes([LaneInput(500.0, Movement.STRAIGHT, SPEED_FOR_LANES[1])], emergency=True)[0]
    assert one.text == "Single lane (1 of 1): centre, traffic pulls left"
    assert one.side is Lane.CENTRE
    rb = plan_lanes(
        [LaneInput(500.0, Movement.ROUNDABOUT, SPEED_FOR_LANES[2], roundabout_exit=3)],
        emergency=True,
    )[0]
    assert "move out after exit 2" in rb.text
    free = plan_lanes(
        [LaneInput(500.0, Movement.LEFT, SPEED_FOR_LANES[2], free_left=True)], emergency=True
    )[0]
    assert "free left" in free.text


def test_lane_change_distance() -> None:
    assert lane_change_distance_m(0, 20.0) == 0.0
    assert lane_change_distance_m(1, 2.0) == 30.0  # slow: the 30 m floor
    assert lane_change_distance_m(2, 15.0) == pytest.approx(2 * 15.0 * 5.0)
    assert lane_change_distance_m(-2, 15.0) == pytest.approx(150.0)


def test_lookahead_straight_then_left_80_m_later_uses_lane_1() -> None:
    v = SPEED_FOR_LANES[3]
    j1 = LaneInput(500.0, Movement.STRAIGHT, v)
    j2 = LaneInput(580.0, Movement.LEFT, v)
    a1, a2 = plan_lanes([j1, j2], emergency=True)
    assert (a1.lane_index, a2.lane_index) == (1, 1)
    assert a1.lookahead
    assert not a2.lookahead
    assert "left turn 80 m after" in a1.text
    # with room to change lanes in between, J1 keeps its own lane
    b1, b2 = plan_lanes([j1, LaneInput(1500.0, Movement.LEFT, v)], emergency=True)
    assert (b1.lane_index, b2.lane_index) == (3, 1)
    assert not b1.lookahead


def test_lookahead_chains_and_turns_keep_their_lane() -> None:
    v = SPEED_FOR_LANES[3]
    # straight, straight, left - each 60 m apart: the left lane propagates back
    adv = plan_lanes(
        [
            LaneInput(300.0, Movement.STRAIGHT, v),
            LaneInput(360.0, Movement.STRAIGHT, v),
            LaneInput(420.0, Movement.LEFT, v),
        ],
        emergency=True,
    )
    assert [a.lane_index for a in adv] == [1, 1, 1]
    # a right turn needs the median lane whatever comes next
    adv2 = plan_lanes(
        [LaneInput(300.0, Movement.RIGHT, v), LaneInput(350.0, Movement.LEFT, v)], emergency=True
    )
    assert [a.lane_index for a in adv2] == [3, 1]


def test_advise_at_is_150_m_or_the_lane_change_distance_clipped_to_the_previous_junction() -> None:
    v = 65 * KMH  # 18.1 m/s -> 90 m per lane
    adv = plan_lanes(
        [
            LaneInput(1000.0, Movement.STRAIGHT, v),  # from lane 1 to 3: 181 m
            LaneInput(1100.0, Movement.STRAIGHT, v),
            LaneInput(3000.0, Movement.LEFT, v),  # from lane 3 to 1: 181 m
        ],
        emergency=True,
    )
    assert adv[0].advise_at_m == pytest.approx(1000.0 - 2 * v * 5.0)
    assert adv[1].advise_at_m == pytest.approx(1000.0)  # clipped to the previous junction
    assert adv[2].advise_at_m == pytest.approx(3000.0 - 2 * v * 5.0)
    near_start = plan_lanes([LaneInput(60.0, Movement.RIGHT, v)], emergency=True)[0]
    assert near_start.advise_at_m == 0.0


def test_advice_json_round_trip() -> None:
    a = plan_lanes([LaneInput(500.0, Movement.U_TURN, SPEED_FOR_LANES[2])], emergency=True)[0]
    assert LaneAdvice.from_json(a.to_json()) == LaneAdvice.from_json(a.to_json())
    back = LaneAdvice.from_json(a.to_json())
    assert (back.movement, back.lane_index, back.lanes_total, back.side, back.text) == (
        a.movement,
        a.lane_index,
        a.lanes_total,
        a.side,
        a.text,
    )
    assert back.road_class is RoadClass.COLLECTOR
