"""Turn classification at a junction (design §3).

Bearings: the approach bearing is the chord over the 30 m of route before the junction's
along-position and the exit bearing the chord over the 30 m after it. If an OSRM
intersection lies within 15 m of the junction, its ``bearings[in]`` (reversed) and
``bearings[out]`` are used instead, and its bearing count is the junction's leg count.

Signal clusters: a junction of the snapshot is often several OSM signal nodes merged
(single linkage, 40 m) - a dual-carriageway crossing is 2-4 OSRM intersections, and the
route turns across them, not at one. The route's *junction span* on this pass runs from
the first to the last of: its vertices that are member nodes of the cluster (OSRM's
``nodes`` annotation) and the OSRM intersections within 15 m of the junction. The
approach is the first span intersection's ``in`` (reversed), the exit the last one's
``out``; the leg count and road bearings come from the span intersection with the most
roads (ties: the first, where the vehicle enters). Without node ids the span is the
intersections within 15 m.

Turn angle ``delta = normalise(exit - approach)`` in (-180, 180]; negative = left (bearings
run clockwise). Thresholds on ``|delta|``: <= 20 STRAIGHT, <= 45 SLIGHT, <= 135 LEFT/RIGHT,
< 170 SHARP, >= 170 U_TURN.

OSRM corroborates: the step maneuver near the junction (within 40 m along the route) is
compared with the geometry. Within one class, OSRM's modifier is used (it sees the real
road layout); more than one class apart, geometry wins and a debug line is logged. A
``roundabout`` / ``rotary`` maneuver makes the movement ROUNDABOUT with OSRM's exit.

Free left: a LEFT / SLIGHT_LEFT whose route passes more than 12 m from the junction
centre and is already turning left 10 m before the junction's along-position (heading
more than 15 deg left of the approach further back) is a slip road that bypasses the
signal; it is never claimed (design §3; see the design doc's Deviations for why the test
looks at the turn being under way rather than completed).
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

from divert.domain.enums import Movement
from divert.domain.geo import (
    LatLng,
    bearing_at_distance,
    bearing_delta_deg,
    haversine_m,
    initial_bearing_deg,
    interpolate_by_distance,
)
from divert.domain.route import Intersection, Maneuver, Route

log = logging.getLogger(__name__)

STRAIGHT_MAX_DEG: Final = 20.0
SLIGHT_MAX_DEG: Final = 45.0
TURN_MAX_DEG: Final = 135.0
U_TURN_MIN_DEG: Final = 170.0

BEARING_WINDOW_M: Final = 30.0  # approach / exit chords
INTERSECTION_SNAP_M: Final = 15.0  # an OSRM intersection this close is the junction
INTERSECTION_ALONG_M: Final = 50.0  # ...and on this pass of the route (multi-pass)
# cluster member nodes of this pass: within half the corridor's minimum gap between two
# passes (corridor.MIN_REPASS_M = 200 m) of the junction's along-position
CLUSTER_ALONG_M: Final = 100.0
SPAN_EPS_M: Final = 0.5  # intersections sit on vertices; float slack only
MANEUVER_WINDOW_M: Final = 40.0  # a step maneuver this close (along the route) belongs here
MIN_CHORD_M: Final = 1.0  # shorter chords have no meaningful bearing
MIN_JUNCTION_LEGS: Final = 2  # OSRM's depart / arrive intersections list one road only
APPROACH_SPEED_WINDOW_M: Final = 30.0  # free-flow speed of the approach (road class)

FREE_LEFT_MIN_OFFSET_M: Final = 12.0
FREE_LEFT_LEAD_M: Final = 10.0
# heading change that counts as "already turning" (assumption: above digitisation noise)
TURN_UNDER_WAY_DEG: Final = 15.0

ROUNDABOUT_TYPES: Final = frozenset({"roundabout", "rotary"})
ROUNDABOUT_EXIT_TYPES: Final = frozenset({"exit roundabout", "exit rotary"})
IGNORED_MANEUVERS: Final = frozenset({"depart", "arrive"})

MODIFIER_MOVEMENT: Final[dict[str, Movement]] = {
    "straight": Movement.STRAIGHT,
    "slight left": Movement.SLIGHT_LEFT,
    "left": Movement.LEFT,
    "sharp left": Movement.SHARP_LEFT,
    "slight right": Movement.SLIGHT_RIGHT,
    "right": Movement.RIGHT,
    "sharp right": Movement.SHARP_RIGHT,
    "uturn": Movement.U_TURN,
}

# signed class scale for "how many classes apart" (U_TURN sits beyond both SHARPs)
_ORDINAL: Final[dict[Movement, int]] = {
    Movement.SHARP_LEFT: -3,
    Movement.LEFT: -2,
    Movement.SLIGHT_LEFT: -1,
    Movement.STRAIGHT: 0,
    Movement.SLIGHT_RIGHT: 1,
    Movement.RIGHT: 2,
    Movement.SHARP_RIGHT: 3,
}
_U_TURN_ORDINAL: Final = 4
_FAR: Final = 99


def classify_turn(delta_deg: float) -> Movement:
    """Movement for a signed turn angle in degrees (negative = left)."""
    a = abs(delta_deg)
    if a <= STRAIGHT_MAX_DEG:
        return Movement.STRAIGHT
    if a >= U_TURN_MIN_DEG:
        return Movement.U_TURN
    left = delta_deg < 0
    if a <= SLIGHT_MAX_DEG:
        return Movement.SLIGHT_LEFT if left else Movement.SLIGHT_RIGHT
    if a <= TURN_MAX_DEG:
        return Movement.LEFT if left else Movement.RIGHT
    return Movement.SHARP_LEFT if left else Movement.SHARP_RIGHT


def class_distance(a: Movement, b: Movement) -> int:
    """How many classes apart two movements are (ROUNDABOUT only matches itself)."""
    if a is b:
        return 0
    if Movement.ROUNDABOUT in (a, b):
        return _FAR
    if Movement.U_TURN in (a, b):
        other = b if a is Movement.U_TURN else a
        return _U_TURN_ORDINAL - abs(_ORDINAL[other])
    return abs(_ORDINAL[a] - _ORDINAL[b])


def movement_from_modifier(modifier: str | None) -> Movement | None:
    return None if modifier is None else MODIFIER_MOVEMENT.get(modifier)


# ------------------------------------------------------------------------- geometry


def _point(route: Route, along_m: float) -> LatLng:
    p, _ = interpolate_by_distance(route.points, route.cumulative, along_m)
    return p


def chord_bearing(route: Route, from_m: float, to_m: float) -> float | None:
    """Bearing of the straight line between two along-positions (None if too short)."""
    a = _point(route, max(0.0, from_m))
    b = _point(route, min(route.length_m, to_m))
    if haversine_m(a, b) < MIN_CHORD_M:
        return None
    return initial_bearing_deg(a, b)


def geometry_bearings(route: Route, along_m: float) -> tuple[float, float | None]:
    """(approach, exit) chords; exit is None when the route ends at the junction."""
    approach = chord_bearing(route, along_m - BEARING_WINDOW_M, along_m)
    if approach is None:  # the route starts at the junction
        approach = bearing_at_distance(route.points, route.cumulative, max(0.0, along_m))
    exit_ = chord_bearing(route, along_m, along_m + BEARING_WINDOW_M)
    return approach, exit_


def approach_speed_mps(route: Route, along_m: float) -> float:
    """Free-flow speed of the approach: the highest OSRM segment speed in the 30 m before
    the junction (OSRM folds turn penalties into the last segments, which only ever
    lowers them, so the maximum is the road's own speed)."""
    cum, speeds = route.cumulative, route.segment_speeds_mps
    lo = along_m - APPROACH_SPEED_WINDOW_M
    first = max(bisect.bisect_right(cum, lo) - 1, 0)
    last = min(bisect.bisect_left(cum, along_m), len(speeds))
    window = [speeds[i] for i in range(first, last) if cum[i] < along_m and cum[i + 1] > lo]
    if window:
        return max(window)
    i = min(max(bisect.bisect_right(cum, along_m) - 1, 0), len(speeds) - 1)
    return speeds[i]


def _junction_roads(route: Route) -> list[Intersection]:
    # depart / arrive points list one road only: not junctions
    return [it for it in route.intersections if it.legs >= MIN_JUNCTION_LEGS]


def member_alongs(route: Route, along_m: float, members: Collection[int]) -> list[float]:
    """Along-positions of this pass's vertices that are member nodes of the junction."""
    if not members or not route.node_ids:
        return []
    cum = route.cumulative
    return [
        cum[i]
        for i, node in enumerate(route.node_ids)
        if node in members and abs(cum[i] - along_m) <= CLUSTER_ALONG_M
    ]


def junction_span(
    route: Route, along_m: float, pos: LatLng, members: Collection[int] = ()
) -> list[Intersection]:
    """The OSRM intersections the route crosses inside the junction on this pass, in
    route order (see module doc); empty when OSRM saw none there."""
    roads = _junction_roads(route)
    near = [
        it.along_m
        for it in roads
        if abs(it.along_m - along_m) <= INTERSECTION_ALONG_M
        and haversine_m(it.location, pos) <= INTERSECTION_SNAP_M
    ]
    marks = near + member_alongs(route, along_m, members)
    if not marks:
        return []
    lo, hi = min(marks) - SPAN_EPS_M, max(marks) + SPAN_EPS_M
    return [it for it in roads if lo <= it.along_m <= hi]


def nearest_maneuver(route: Route, along_m: float) -> Maneuver | None:
    best: Maneuver | None = None
    for m in route.maneuvers:
        if m.type in IGNORED_MANEUVERS:
            continue
        gap = abs(m.along_m - along_m)
        if gap <= MANEUVER_WINDOW_M and (best is None or gap < abs(best.along_m - along_m)):
            best = m
    return best


def _roundabout_exit(route: Route, m: Maneuver) -> int | None:
    if m.type in ROUNDABOUT_TYPES:
        return m.exit
    # an "exit roundabout" step: the exit number is on the step that entered it
    entered = [x for x in route.maneuvers if x.type in ROUNDABOUT_TYPES and x.along_m <= m.along_m]
    return entered[-1].exit if entered else None


def turning_left_before(route: Route, along_m: float) -> bool:
    """Is the route already turning left FREE_LEFT_LEAD_M before the junction?

    Compares the heading of the route at ``along_m - 10 m`` with the approach heading
    further back (the chord over [along - 60 m, along - 30 m]).
    """
    far = chord_bearing(route, along_m - 2 * BEARING_WINDOW_M, along_m - BEARING_WINDOW_M)
    if far is None or along_m - FREE_LEFT_LEAD_M <= 0.0:
        return False
    here = bearing_at_distance(route.points, route.cumulative, along_m - FREE_LEFT_LEAD_M)
    return bearing_delta_deg(far, here) < -TURN_UNDER_WAY_DEG


@dataclass(frozen=True, slots=True)
class JunctionMovement:
    movement: Movement
    turn_deg: float
    approach_bearing: float
    exit_bearing: float | None
    legs: int | None  # from the OSRM intersection; None = unknown
    bearings: tuple[float, ...]  # OSRM road bearings at the junction (may be empty)
    roundabout_exit: int | None = None
    free_left: bool = False
    osrm_modifier: str | None = None
    geometry_movement: Movement = Movement.STRAIGHT


def span_bearings(
    span: Collection[Intersection], approach: float, exit_: float | None
) -> tuple[float, float | None]:
    """(approach, exit) across a junction span: the first ``in`` and the last ``out``
    OSRM knows of (the geometry chords where it knows neither)."""
    ins = [it.approach_bearing for it in span if it.approach_bearing is not None]
    outs = [it.exit_bearing for it in span if it.exit_bearing is not None]
    return (ins[0] if ins else approach), (outs[-1] if outs else exit_)


def junction_movement(
    route: Route,
    along_m: float,
    pos: LatLng,
    offset_m: float,
    *,
    members: Collection[int] = (),
) -> JunctionMovement:
    """The movement at one junction of a route (see module doc); ``members`` are the OSM
    node ids of the junction's signal cluster."""
    span = junction_span(route, along_m, pos, members)
    approach, exit_ = span_bearings(span, *geometry_bearings(route, along_m))
    # leg count / road bearings: the span node with the most roads (ties: the entry)
    inter = max(span, key=lambda it: it.legs) if span else None
    delta = 0.0 if exit_ is None else bearing_delta_deg(approach, exit_)
    geometry = classify_turn(delta)
    movement = geometry
    rb_exit: int | None = None
    man = nearest_maneuver(route, along_m)
    modifier = None if man is None else man.modifier
    if man is not None and (man.type in ROUNDABOUT_TYPES or man.type in ROUNDABOUT_EXIT_TYPES):
        movement = Movement.ROUNDABOUT
        rb_exit = _roundabout_exit(route, man)
    elif exit_ is not None and (osrm := movement_from_modifier(modifier)) is not None:
        gap = class_distance(geometry, osrm)
        if gap == 1:
            movement = osrm
        elif gap > 1:
            log.debug(
                "OSRM modifier disagrees with the geometry; geometry wins",
                extra={
                    "along_m": round(along_m, 1),
                    "osrm": osrm.value,
                    "geometry": geometry.value,
                },
            )
    free_left = False
    if movement in (Movement.LEFT, Movement.SLIGHT_LEFT) and exit_ is not None:
        free_left = offset_m > FREE_LEFT_MIN_OFFSET_M and turning_left_before(route, along_m)
    return JunctionMovement(
        movement=movement,
        turn_deg=delta,
        approach_bearing=approach,
        exit_bearing=exit_,
        legs=None if inter is None else inter.legs,
        bearings=() if inter is None else inter.bearings,
        roundabout_exit=rb_exit,
        free_left=free_left,
        osrm_modifier=modifier,
        geometry_movement=geometry,
    )
