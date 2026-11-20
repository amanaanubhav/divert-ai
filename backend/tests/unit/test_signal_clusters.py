"""Movements through signal clusters (design §3, stage 5 fix F2).

A junction of the snapshot is often several OSM signal nodes merged within 40 m (a dual
carriageway crossing), and a route turns across 2-4 OSRM intersections there. The movement
is taken across the whole *junction span* - first ``in`` to last ``out`` - not at one node.

The OSRM responses in ``tests/data/osrm`` are real answers of the public OSRM demo
(router.project-osrm.org, fetched 2026-11-20 with ``overview=full geometries=geojson
steps=true annotations=distance,duration,speed,nodes``) for routes the runtime verifier
found misclassified; the junctions and their member node ids come from the committed
snapshots.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from divert.cities.loader import load_snapshot
from divert.domain.enums import Lane, Movement
from divert.domain.geo import destination_point
from divert.domain.models import CorridorCandidate, Signal
from divert.domain.movement import junction_movement, junction_span
from divert.domain.route import CorridorSignal, Intersection, Route
from divert.domain.signal_plan import JunctionGeometry
from divert.services.corridor import build_corridor
from divert.services.routing import parse_osrm_routes
from tests.support import city, route_through

DATA = Path(__file__).parents[1] / "data" / "osrm"


def osrm(name: str) -> Route:
    body: dict[str, Any] = json.loads((DATA / name).read_text(encoding="utf-8"))
    return parse_osrm_routes(body)[0]


def signal(city_id: str, signal_id: str) -> Signal:
    return next(s for s in load_snapshot(city_id).signals if s.id == signal_id)


def corridor_entry(route: Route, sig: Signal, *, near_m: float | None = None) -> CorridorSignal:
    """The corridor entry of ``sig`` (the pass nearest ``near_m`` along the route)."""
    entries = [c for c in build_corridor(route, [CorridorCandidate(sig, 0.0)]) if c.signal_id]
    if near_m is not None:
        entries.sort(key=lambda c: abs(c.along_m - near_m))
    return entries[0]


# ------------------------------------------------------------------ parsing node ids


def test_osrm_node_ids_line_up_with_the_route_points() -> None:
    route = osrm("delhi_J308719791_via.json")  # a via route: two OSRM legs
    assert len(route.node_ids) == len(route.points)
    assert route.node_ids[0] == route.node_ids[-1] == 0  # snapped ends: no node there
    members = set(signal("delhi", "J308719791").osm_node_ids)
    on_route = [i for i, n in enumerate(route.node_ids) if n in members]
    assert len(on_route) >= 3, "the route crosses several nodes of the cluster"
    for i in on_route:  # a member vertex is that node's own position (OSRM intersections)
        assert any(it.location == route.points[i] for it in route.intersections)
    assert Route.from_json(route.to_json()).node_ids == ()  # consumed at planning time


def test_no_nodes_annotation_means_no_node_ids() -> None:
    body = json.loads((DATA / "delhi_J1817741097.json").read_text(encoding="utf-8"))
    for leg in body["routes"][0]["legs"]:
        del leg["annotation"]["nodes"]
    assert parse_osrm_routes(body)[0].node_ids == ()


# --------------------------------------------------------- the verifier's junctions


@pytest.mark.parametrize(
    ("fixture", "city_id", "signal_id", "near_m", "movement", "approach", "exit_"),
    [
        # Delhi, ITO-area dual carriageway: 315 -> 285 -> 30 over three nodes (was STRAIGHT
        # 30 -> 30 from the last node alone)
        ("delhi_J308719791_via.json", "delhi", "J308719791", 5831.3, Movement.RIGHT, 315, 30),
        # Delhi, Gurugram border: 210 -> 285 -> 285 (was STRAIGHT 285 -> 285); the bend
        # 165 -> 210 just before (16.7 m from the centre, not a member node) is not in it
        ("delhi_J1817741097.json", "delhi", "J1817741097", 379.0, Movement.RIGHT, 210, 285),
        # Bengaluru: 120 -> 225 -> 210; the entry node is 15.0 m from the centre, found as
        # a cluster member (was STRAIGHT 225 -> 210)
        ("bengaluru_J428853845.json", "bengaluru", "J428853845", 574.9, Movement.RIGHT, 120, 210),
        # Kolkata: 240 -> 195 -> 270 is a slight right (was LEFT 240 -> 195, kerb lane)
        ("kolkata_J6150923997.json", "kolkata", "J6150923997", 144.5,
         Movement.SLIGHT_RIGHT, 240, 270),
    ],
)  # fmt: skip
def test_movement_spans_the_whole_signal_cluster(
    fixture: str,
    city_id: str,
    signal_id: str,
    near_m: float,
    movement: Movement,
    approach: float,
    exit_: float,
) -> None:
    route = osrm(fixture)
    sig = signal(city_id, signal_id)
    c = corridor_entry(route, sig, near_m=near_m)
    assert c.along_m == pytest.approx(near_m, abs=1.0)
    assert c.movement is movement
    assert c.approach_bearing == pytest.approx(approach)
    assert c.exit_bearing == pytest.approx(exit_)
    adv = c.advice
    assert adv is not None
    # every one of these turns right or goes on: the median-side lane, never the kerb lane
    assert adv.lane_index == adv.lanes_total
    assert c.lane is (Lane.CENTRE if adv.lanes_total == 1 else Lane.RIGHT)


@pytest.mark.parametrize(
    ("fixture", "city_id", "signal_id", "near_m", "cached", "phase"),
    [
        # the geometry each junction had in Redis during the verifier's run (osrm source)
        ("delhi_J308719791_via.json", "delhi", "J308719791", 5831.3, (210, 300, 30, 105), 1),
        ("delhi_J1817741097.json", "delhi", "J1817741097", 379.0, (195, 285, 15, 105), 0),
        ("bengaluru_J428853845.json", "bengaluru", "J428853845", 574.9, (225, 300, 30, 120), 3),
    ],
)  # fmt: skip
def test_the_cluster_approach_selects_the_vehicles_own_phase(
    fixture: str,
    city_id: str,
    signal_id: str,
    near_m: float,
    cached: tuple[float, ...],
    phase: int,
) -> None:
    c = corridor_entry(osrm(fixture), signal(city_id, signal_id), near_m=near_m)
    geometry = JunctionGeometry(len(cached), cached, "osrm")
    assert geometry.phase_for(c.approach_bearing) == phase
    # a junction first seen on this pass gets phases from the span's node with the most
    # roads (the entry on ties): the vehicle's approach is one of them, or next to one
    own = JunctionGeometry.from_osrm(c.junction_bearings)
    assert own is not None
    assert (
        min(abs(((h - c.approach_bearing + 180) % 360) - 180) for h in own.approach_bearings) < 46
    )


def test_kolkata_through_movement_gets_the_emergency_through_lane() -> None:
    c = corridor_entry(
        osrm("kolkata_J6150923997.json"), signal("kolkata", "J6150923997"), near_m=144.5
    )
    assert c.advice is not None
    assert (c.advice.lane_index, c.advice.lanes_total, c.lane) == (2, 2, Lane.RIGHT)


# ------------------------------------------------------------------ span mechanics

J = city().center


def _crossing(*, member_ids: dict[str, int]) -> tuple[Route, list[Intersection]]:
    """North 200 m to node n1 (10 m south of J), right to n2 (10 m east of n1, 14 m from
    J), on to n3 (25 m further, 36 m from J), then 200 m east: a dual-carriageway crossing
    seen as three OSRM intersections. ``member_ids`` puts OSM ids on n1 / n2 / n3 / far
    (a vertex 150 m past n3)."""
    a = destination_point(J, 180.0, 200.0)
    n1 = destination_point(J, 180.0, 10.0)
    n2 = destination_point(n1, 90.0, 10.0)
    n3 = destination_point(n2, 90.0, 25.0)
    far = destination_point(n3, 90.0, 150.0)
    end = destination_point(n3, 90.0, 200.0)
    route = route_through([a, n1, n2, n3, far, end], step_m=5.0)
    at = {
        name: route.points.index(p)
        for name, p in (("n1", n1), ("n2", n2), ("n3", n3), ("far", far))
    }
    inters = [
        Intersection(route.cumulative[at["n1"]], n1, (0.0, 90.0, 180.0, 270.0), 2, 1),
        Intersection(route.cumulative[at["n2"]], n2, (0.0, 40.0, 180.0, 270.0), 3, 1),
        Intersection(route.cumulative[at["n3"]], n3, (0.0, 120.0, 270.0), 2, 1),
    ]
    ids = [0] * len(route.points)
    for name, node in member_ids.items():
        ids[at[name]] = node
    built = Route.build(route.points, route.segment_speeds_mps, intersections=inters)
    return dataclasses.replace(built, node_ids=tuple(ids)), inters


def test_span_takes_the_first_in_and_the_last_out() -> None:
    route, inters = _crossing(member_ids={})
    along = inters[0].along_m + 5.0
    assert junction_span(route, along, J) == inters[:2]  # n3 is 36 m from J: not in it
    m = junction_movement(route, along, J, 0.0)
    assert (m.approach_bearing, m.exit_bearing, m.movement) == (0.0, 40.0, Movement.SLIGHT_RIGHT)
    assert m.legs == 4
    assert m.bearings == (0.0, 90.0, 180.0, 270.0)  # most roads, ties: the entry node


def test_member_nodes_beyond_15_m_extend_the_span() -> None:
    route, inters = _crossing(member_ids={"n1": 11, "n2": 12, "n3": 13})
    along = inters[0].along_m + 5.0
    m = junction_movement(route, along, J, 0.0, members={11, 12, 13})
    assert (m.approach_bearing, m.exit_bearing, m.movement) == (0.0, 120.0, Movement.RIGHT)
    other = junction_movement(route, along, J, 0.0, members={99})  # another junction's ids
    assert other.exit_bearing == 40.0


def test_member_nodes_of_another_pass_are_ignored() -> None:
    route, inters = _crossing(member_ids={"n1": 11, "n2": 12, "far": 14})
    m = junction_movement(route, inters[0].along_m + 5.0, J, 0.0, members={11, 12, 14})
    assert m.exit_bearing == 40.0  # the member 185 m along is a different pass
