"""OSRM client: request shape, parsing (alternatives, maneuvers, intersections), retries,
cache (keyed by server), detours, errors; corridor projection with movements."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from redis.asyncio import Redis

from divert.domain.enums import Lane, Movement
from divert.domain.geo import LatLng, destination_point, haversine_m
from divert.domain.models import CorridorCandidate, Signal
from divert.domain.route import Route
from divert.services.dispatch import build_corridor
from divert.services.routing import (
    OsrmRouter,
    RoutingError,
    detour_vias,
    is_duplicate,
    parse_osrm_route,
    parse_osrm_routes,
)
from tests.support import city, route_through, straight_route

BASE = "http://osrm.test"


def osrm_route(
    n: int = 10, *, left_turn_at: int | None = 5, bearing: float = 0.0, step_m: float = 100.0
) -> dict[str, Any]:
    """One OSRM-shaped route: a stub straight line (with one duplicate vertex), a turn
    step with an intersection carrying 4 road bearings, and the arrive step."""
    r = straight_route(city().center, bearing=bearing, n=n, step_m=step_m, speed=10.0)
    coords = [[p.lng, p.lat] for p in r.points]
    coords.insert(3, coords[3])  # OSRM sometimes repeats a vertex
    dists = [step_m] * n
    dists.insert(3, 0.0)
    k = (left_turn_at or 5) + 1  # index in coords (after the inserted duplicate)
    steps = [
        {
            "distance": step_m * (k - 1),
            "name": "Ring Road",
            "maneuver": {"type": "depart", "location": coords[0]},
            "intersections": [{"location": coords[0], "bearings": [0], "out": 0}],
        },
        {
            "distance": step_m * (n - k + 1),
            "name": "Mathura Road",
            "maneuver": {
                "type": "turn",
                "modifier": "left" if left_turn_at else "straight",
                "location": coords[k],
            },
            "intersections": [
                {"location": coords[k], "bearings": [0, 90, 180, 270], "in": 2, "out": 0},
                {"location": coords[k + 2], "bearings": [0, 180, 300], "in": 1, "out": 0},
            ],
        },
        {"distance": 0.0, "maneuver": {"type": "arrive", "location": coords[-1]}},
    ]
    return {
        "distance": step_m * n,
        "duration": 10.0 * n,
        "geometry": {"type": "LineString", "coordinates": coords},
        "legs": [
            {
                "annotation": {
                    "distance": dists,
                    "duration": [d / 10.0 for d in dists],
                    "speed": [10.0 if d else 0.0 for d in dists],
                },
                "steps": steps,
            }
        ],
    }


def osrm_body(
    n: int = 10, *, left_turn_at: int | None = 5, alternatives: int = 1
) -> dict[str, Any]:
    routes = [osrm_route(n, left_turn_at=left_turn_at)]
    for k in range(1, alternatives):
        routes.append(osrm_route(n + k, left_turn_at=None, bearing=10.0 * k))
    return {"code": "Ok", "routes": routes}


def test_parse_merges_duplicates_and_locates_maneuvers_and_intersections() -> None:
    route = parse_osrm_route(osrm_body(10))
    assert len(route.points) == 11  # duplicate merged
    assert len(route.segment_speeds_mps) == 10
    assert route.length_m == pytest.approx(1000.0, rel=1e-6)
    turn = next(m for m in route.maneuvers if m.type == "turn")
    assert turn.modifier == "left"
    assert turn.name == "Mathura Road"
    assert turn.along_m == pytest.approx(500.0, abs=1.0)
    four = next(i for i in route.intersections if i.legs == 4)
    assert four.along_m == pytest.approx(500.0, abs=1.0)
    assert four.approach_bearing == 0.0  # arrived on the road at 180: heading north
    assert four.exit_bearing == 0.0
    assert [i.legs for i in route.intersections] == [1, 4, 3]
    assert route.source == "osrm"


def test_parse_every_alternative() -> None:
    routes = parse_osrm_routes(osrm_body(10, alternatives=3))
    assert [len(r.points) for r in routes] == [11, 12, 13]
    assert parse_osrm_route(osrm_body(10, alternatives=3)).length_m == routes[0].length_m


def test_corridor_projection_movement_lane_and_bearing() -> None:
    route = parse_osrm_route(osrm_body(10))
    sig = Signal("J1", "delhi", None, route.points[5], (1,))
    corridor = build_corridor(route, [CorridorCandidate(sig, 0.0)])
    c = corridor[0]
    assert c.along_m == pytest.approx(500.0, abs=0.5)
    assert c.legs == 4  # from the OSRM intersection
    assert c.junction_bearings == (0.0, 90.0, 180.0, 270.0)
    assert c.approach_bearing == pytest.approx(0.0, abs=0.5)  # heading north
    # geometry is straight; OSRM says "left" - two classes apart: geometry wins
    assert c.movement is Movement.STRAIGHT
    assert c.advice is not None
    assert c.lane is c.advice.side
    assert c.advice.lanes_total == 2  # 36 km/h: COLLECTOR
    assert c.lane is Lane.RIGHT  # emergency, straight on: the rightmost through lane


@pytest.mark.parametrize("code", ["NoRoute", "InvalidQuery"])
def test_parse_rejects_non_ok(code: str) -> None:
    with pytest.raises(RoutingError):
        parse_osrm_route({"code": code, "message": "x"})


@respx.mock
async def test_request_shape_retry_and_cache(fake_redis: Redis) -> None:
    route = respx.get(url__startswith=f"{BASE}/route/v1/driving/").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=osrm_body(alternatives=2))]
    )
    router = OsrmRouter(BASE, retries=2, cache=fake_redis, backoff_s=0.0, alternatives=3)
    a, b = LatLng(28.6, 77.2), LatLng(28.61, 77.21)
    r1 = await router.alternatives(a, b)
    assert route.call_count == 2  # one retry after the 503
    req = route.calls.last.request
    assert req.url.path == "/route/v1/driving/77.200000,28.600000;77.210000,28.610000"
    params = dict(req.url.params)
    assert params["overview"] == "full"
    assert params["geometries"] == "geojson"
    assert params["steps"] == "true"
    assert params["annotations"] == "distance,duration,speed"
    assert params["alternatives"] == "3"
    assert len(r1) == 2
    r2 = await router.alternatives(a, b)  # served from the Redis cache
    assert route.call_count == 2
    assert r2[0].points == r1[0].points
    await router.aclose()


@respx.mock
async def test_cache_key_includes_the_osrm_server(fake_redis: Redis) -> None:
    first = respx.get(url__startswith="http://osrm-a.test/").mock(
        return_value=httpx.Response(200, json=osrm_body(10))
    )
    second = respx.get(url__startswith="http://osrm-b.test/").mock(
        return_value=httpx.Response(200, json=osrm_body(12))
    )
    a, b = LatLng(28.6, 77.2), LatLng(28.61, 77.21)
    ra = OsrmRouter("http://osrm-a.test", cache=fake_redis, backoff_s=0.0)
    rb = OsrmRouter("http://osrm-b.test", cache=fake_redis, backoff_s=0.0)
    assert len((await ra.route(a, b)).points) == 11
    assert len((await rb.route(a, b)).points) == 13  # not the other server's cached route
    assert (first.call_count, second.call_count) == (1, 1)
    await ra.route(a, b)
    assert first.call_count == 1  # its own cache entry still serves
    await ra.aclose()
    await rb.aclose()


@respx.mock
async def test_detours_when_osrm_returns_a_single_route() -> None:
    o = city().center
    d = destination_point(o, 0.0, 3000.0)
    main = route_through([o, d], step_m=100.0)
    calls: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        coords = request.url.path.rsplit("/", 1)[1].split(";")
        calls.append(request.url.params.get("continue_straight", ""))
        pts = [LatLng(float(c.split(",")[1]), float(c.split(",")[0])) for c in coords]
        r = route_through(pts, step_m=100.0)
        n = len(r.points) - 1
        body = {
            "distance": r.length_m,
            "duration": r.length_m / 10.0,
            "geometry": {"type": "LineString", "coordinates": [[p.lng, p.lat] for p in r.points]},
            "legs": [{"annotation": {"distance": [1.0] * n, "speed": [10.0] * n}, "steps": []}],
        }
        return httpx.Response(200, json={"code": "Ok", "routes": [body]})

    respx.get(url__startswith=f"{BASE}/route/v1/driving/").mock(side_effect=reply)
    router = OsrmRouter(BASE, backoff_s=0.0, alternatives=3, detours=2)
    routes = await router.alternatives(o, d)
    assert len(routes) == 3
    assert [r.source for r in routes] == ["osrm", "detour", "detour"]
    assert calls == ["", "true", "true"]  # no U-turn at the via point
    assert routes[1].length_m > main.length_m
    # a short trip gets no detours
    near = destination_point(o, 0.0, 1200.0)
    assert len(await router.alternatives(o, near)) == 1
    await router.aclose()


def test_detour_vias_and_duplicates() -> None:
    o = city().center
    d = destination_point(o, 90.0, 2000.0)
    left, right = detour_vias(o, d, 2)
    assert haversine_m(left, destination_point(o, 90.0, 1000.0)) == pytest.approx(400.0, rel=0.01)
    assert haversine_m(left, right) == pytest.approx(800.0, rel=0.01)
    assert detour_vias(o, d, 0) == []
    a = route_through([o, d], step_m=50.0)
    same = route_through([o, d], step_m=20.0)
    other = route_through([o, left, d], step_m=50.0)
    assert is_duplicate(same, [a])
    assert not is_duplicate(other, [a])


@respx.mock
async def test_unavailable_after_retries() -> None:
    down = respx.get(url__startswith=f"{BASE}/route/v1/driving/7").mock(
        side_effect=httpx.ConnectError("down")
    )
    router = OsrmRouter(BASE, retries=2, backoff_s=0.0)
    with pytest.raises(RoutingError) as err:
        await router.route(LatLng(28.6, 77.2), LatLng(28.61, 77.21))
    assert err.value.kind == "unavailable"
    assert down.call_count == 3
    await router.aclose()


@respx.mock
async def test_no_route_is_not_retried() -> None:
    router = OsrmRouter(BASE, retries=2, backoff_s=0.0)
    nr = respx.get(url__startswith=f"{BASE}/route/v1/driving/").mock(
        return_value=httpx.Response(400, json={"code": "NoRoute", "message": "Impossible route"})
    )
    with pytest.raises(RoutingError) as err2:
        await router.route(LatLng(28.6, 77.2), LatLng(28.61, 77.21))
    assert err2.value.kind == "no_route"
    assert nr.call_count == 1
    await router.aclose()


def test_route_json_round_trip_keeps_geometry_and_speeds() -> None:
    r = parse_osrm_route(osrm_body(10))
    back = Route.from_json(r.to_json())
    assert len(back.points) == len(r.points)
    assert all(haversine_m(a, b) < 0.02 for a, b in zip(back.points, r.points, strict=True))
    assert back.segment_speeds_mps == pytest.approx(r.segment_speeds_mps)
