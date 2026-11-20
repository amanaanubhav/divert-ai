"""Lane model (design §3): road class and lanes from the free-flow speed, the target lane
for each movement, lane-change look-ahead, and the advice text.

OSRM returns no lane data for DIVERT's cities and the OSM snapshot has no ``lanes`` /
``turn:lanes`` tags, so lane counts are *estimated* from the road class (an assumption,
see docs/simulation-model.md). India drives on the left: lanes are numbered from the
kerb, lane 1 is the leftmost and lane n is next to the median.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from divert.domain.enums import Lane, Movement, RoadClass

KMH: Final = 1000.0 / 3600.0

# Road class from the OSRM free-flow speed (design §2 table; OSRM speeds reflect the OSM
# highway class). Assumptions, documented in docs/simulation-model.md.
EXPRESSWAY_MIN_MPS: Final = 75.0 * KMH
ARTERIAL_MIN_MPS: Final = 50.0 * KMH
ARTERIAL_WIDE_MIN_MPS: Final = 60.0 * KMH  # arterials this fast get 3 lanes, else 2
COLLECTOR_MIN_MPS: Final = 32.0 * KMH
LANES_BY_CLASS: Final[dict[RoadClass, int]] = {
    RoadClass.EXPRESSWAY: 3,
    RoadClass.ARTERIAL: 2,  # 3 when >= ARTERIAL_WIDE_MIN_MPS
    RoadClass.COLLECTOR: 2,
    RoadClass.LOCAL: 1,
}

# Lane-change planning (assumptions): moving k lanes needs k x max(30 m, v x (3 s to find
# a gap + 2 s to move across)); the advice is never given later than 150 m before.
LANE_CHANGE_MIN_M: Final = 30.0
LANE_CHANGE_GAP_S: Final = 3.0
LANE_CHANGE_MOVE_S: Final = 2.0
ADVISE_MIN_M: Final = 150.0
START_LANE: Final = 1  # a unit leaves its station (or the kerb at a scene) in lane 1

LEFT_MOVEMENTS: Final = frozenset({Movement.LEFT, Movement.SLIGHT_LEFT, Movement.SHARP_LEFT})
RIGHT_MOVEMENTS: Final = frozenset(
    {Movement.RIGHT, Movement.SHARP_RIGHT, Movement.SLIGHT_RIGHT, Movement.U_TURN}
)


def road_class(free_flow_mps: float) -> RoadClass:
    if free_flow_mps >= EXPRESSWAY_MIN_MPS:
        return RoadClass.EXPRESSWAY
    if free_flow_mps >= ARTERIAL_MIN_MPS:
        return RoadClass.ARTERIAL
    if free_flow_mps >= COLLECTOR_MIN_MPS:
        return RoadClass.COLLECTOR
    return RoadClass.LOCAL


def lanes_per_direction(free_flow_mps: float) -> int:
    cls = road_class(free_flow_mps)
    if cls is RoadClass.ARTERIAL and free_flow_mps >= ARTERIAL_WIDE_MIN_MPS:
        return 3
    return LANES_BY_CLASS[cls]


def lane_change_distance_m(lanes_to_cross: int, speed_mps: float) -> float:
    per_lane = max(LANE_CHANGE_MIN_M, speed_mps * (LANE_CHANGE_GAP_S + LANE_CHANGE_MOVE_S))
    return abs(lanes_to_cross) * per_lane


def _clamp(lane: int, lanes: int) -> int:
    return min(max(1, lane), max(1, lanes))


def target_lane(
    movement: Movement,
    lanes: int,
    *,
    emergency: bool,
    free_left: bool = False,
    roundabout_exit: int | None = None,
    turn_deg: float = 0.0,
) -> int:
    """Lane to be in at the junction (design §3 table), 1 = kerb, ``lanes`` = median."""
    n = max(1, lanes)
    if free_left or movement in LEFT_MOVEMENTS:
        return 1
    if movement in RIGHT_MOVEMENTS:
        return n
    if movement is Movement.ROUNDABOUT:
        if roundabout_exit is None:  # exit unknown: the exit side decides
            return 1 if turn_deg < -45.0 else n
        return 1 if roundabout_exit <= 1 else n
    # STRAIGHT: emergency vehicles take the rightmost through lane (traffic yields left);
    # everyone else keeps to the middle lane
    return n if emergency else math.ceil(n / 2)


def side_for(lane_index: int, lanes_total: int, movement: Movement, *, free_left: bool) -> Lane:
    if lanes_total <= 1:  # one lane: where to position in it
        if free_left or movement in LEFT_MOVEMENTS:
            return Lane.LEFT
        if movement in RIGHT_MOVEMENTS:
            return Lane.RIGHT
        return Lane.CENTRE
    if lane_index <= 1:
        return Lane.LEFT
    if lane_index >= lanes_total:
        return Lane.RIGHT
    return Lane.CENTRE


@dataclass(frozen=True, slots=True)
class LaneInput:
    """One junction on the route, in route order."""

    along_m: float
    movement: Movement
    approach_speed_mps: float  # OSRM free-flow speed of the approach
    free_left: bool = False
    roundabout_exit: int | None = None
    turn_deg: float = 0.0

    @property
    def lanes(self) -> int:
        return lanes_per_direction(self.approach_speed_mps)


@dataclass(frozen=True, slots=True)
class LaneAdvice:
    movement: Movement
    lane_index: int
    lanes_total: int
    side: Lane
    advise_at_m: float
    free_left: bool
    text: str
    road_class: RoadClass = RoadClass.COLLECTOR
    lookahead: bool = False  # lane chosen to suit the next junction

    def to_json(self) -> dict[str, Any]:
        return {
            "movement": self.movement.value,
            "lane_index": self.lane_index,
            "lanes_total": self.lanes_total,
            "side": self.side.value,
            "advise_at_m": round(self.advise_at_m, 1),
            "free_left": self.free_left,
            "text": self.text,
            "road_class": self.road_class.value,
            "lookahead": self.lookahead,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> LaneAdvice:
        return cls(
            movement=Movement(d.get("movement", Movement.STRAIGHT.value)),
            lane_index=int(d.get("lane_index", 1)),
            lanes_total=int(d.get("lanes_total", 1)),
            side=Lane(d.get("side", Lane.RIGHT.value)),
            advise_at_m=float(d.get("advise_at_m", 0.0)),
            free_left=bool(d.get("free_left", False)),
            text=str(d.get("text", "")),
            road_class=RoadClass(d.get("road_class", RoadClass.COLLECTOR.value)),
            lookahead=bool(d.get("lookahead", False)),
        )


_WHAT: Final[dict[Movement, str]] = {
    Movement.LEFT: "left turn",
    Movement.SLIGHT_LEFT: "bear left",
    Movement.SHARP_LEFT: "sharp left turn",
    Movement.RIGHT: "protected right turn",
    Movement.SLIGHT_RIGHT: "bear right",
    Movement.SHARP_RIGHT: "protected sharp right turn",
    Movement.U_TURN: "protected U-turn",
}


def _lane_name(lane: int, lanes: int) -> str:
    if lanes <= 1:
        return "Single lane"
    if lane <= 1:
        return "Left lane"
    if lane >= lanes:
        return "Right lane"
    if lanes % 2 == 1 and lane == (lanes + 1) // 2:
        return "Centre lane"
    return f"Lane {lane}"


def _roundabout_text(exit_no: int | None) -> str:
    if exit_no is None:
        return "roundabout"
    if exit_no <= 1:
        return "roundabout, first exit"
    return f"roundabout exit {exit_no}: inner lane, move out after exit {exit_no - 1}"


def _what(j: LaneInput, lanes: int, *, emergency: bool) -> str:
    if j.free_left:
        return "free left, bypasses the signal"
    if j.movement is Movement.ROUNDABOUT:
        return _roundabout_text(j.roundabout_exit)
    if j.movement is not Movement.STRAIGHT:
        return _WHAT[j.movement]
    if not emergency:
        return "straight on"
    return "centre, traffic pulls left" if lanes <= 1 else "straight on, traffic yields left"


def _short(j: LaneInput) -> str:
    if j.free_left:
        return "free left"
    if j.movement is Movement.ROUNDABOUT:
        return "roundabout"
    if j.movement is Movement.STRAIGHT:
        return "straight on"
    return _WHAT[j.movement].replace("protected ", "")


def plan_lanes(
    junctions: Sequence[LaneInput], *, emergency: bool, start_along_m: float = 0.0
) -> list[LaneAdvice]:
    """Lane advice for every junction of a leg (``junctions`` in route order).

    Look-ahead: when the lane change needed for junction i+1 does not fit between i and
    i+1, a straight-on movement at i already uses junction i+1's lane (straight at J1, left
    80 m later at J2 -> lane 1 through J1). Turns keep their own lane.
    """
    n = len(junctions)
    natural = [
        target_lane(
            j.movement,
            j.lanes,
            emergency=emergency,
            free_left=j.free_left,
            roundabout_exit=j.roundabout_exit,
            turn_deg=j.turn_deg,
        )
        for j in junctions
    ]
    final = list(natural)
    lookahead = [False] * n
    for i in range(n - 2, -1, -1):
        cur, nxt = junctions[i], junctions[i + 1]
        if cur.movement is not Movement.STRAIGHT or cur.free_left:
            continue
        nxt_lane = final[i + 1]
        needed = lane_change_distance_m(
            _clamp(natural[i], nxt.lanes) - nxt_lane, nxt.approach_speed_mps
        )
        if nxt.along_m - cur.along_m < needed:
            final[i] = _clamp(nxt_lane, cur.lanes)
            lookahead[i] = final[i] != natural[i]
    out: list[LaneAdvice] = []
    for i, j in enumerate(junctions):
        lanes = j.lanes
        lane = _clamp(final[i], lanes)
        prev_lane = _clamp(final[i - 1] if i else START_LANE, lanes)
        floor = junctions[i - 1].along_m if i else start_along_m
        required = lane_change_distance_m(lane - prev_lane, j.approach_speed_mps)
        advise_at = max(floor, 0.0, j.along_m - max(required, ADVISE_MIN_M))
        text = (
            f"{_lane_name(lane, lanes)} ({lane} of {lanes}): {_what(j, lanes, emergency=emergency)}"
        )
        if lookahead[i]:
            nxt = junctions[i + 1]
            text += f", for the {_short(nxt)} {nxt.along_m - j.along_m:.0f} m after"
        out.append(
            LaneAdvice(
                movement=j.movement,
                lane_index=lane,
                lanes_total=lanes,
                side=side_for(lane, lanes, j.movement, free_left=j.free_left),
                advise_at_m=advise_at,
                free_left=j.free_left,
                text=text,
                road_class=road_class(j.approach_speed_mps),
                lookahead=lookahead[i],
            )
        )
    return out
