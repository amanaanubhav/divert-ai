"""OSRM client: request shape, parsing, retries, cache, errors; lane advisories."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from redis.asyncio import Redis

from divert.domain.enums import Lane
from divert.domain.geo import LatLng
from divert.domain.models import CorridorCandidate, Signal
from divert.domain.route import Maneuver, lane_advisory
from divert.services.dispatch import build_corridor
from divert.services.routing import OsrmRouter, RoutingError, parse_osrm_route
from tests.support import city, straight_route

BASE = "http://osrm.test"


def osrm_body(n: int = 10, *, left_turn_at: int | None = 5) -> dict[str, Any]:
    """An OSRM-shaped response for a stub straight route (with one duplicate point)."""
    r = straight_route(city().center, bearing=0.0, n=n, step_m=100.0, speed=10.0)
    coords = [[p.lng, p.lat] for p in r.points]
    coords.insert(3, coords[3])  # OSRM sometimes repeats a vertex
    dists = [100.0] * n
    dists.insert(3, 0.0)
    steps = [
        {"distance": 500.0, "maneuver": {"type": "depart", "location": coords[0]}},
        {
            "distance": 500.0,
            "maneuver": {
                "type": "turn",
                "modifier": "left" if left_turn_at else "straight",
                "location": coords[(left_turn_at or 5) + 1],
            },
        },
        {"distance": 0.0, "maneuver": {"type": "arrive", "location": coords[-1]}},
    ]
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": 100.0 * n,
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
        ],
    }


def test_parse_merges_duplicates_and_locates_maneuvers() -> None:
    route = parse_osrm_route(osrm_body(10))
    assert len(route.points) == 11  # duplicate merged
    assert len(route.segment_speeds_mps) == 10
    assert route.length_m == pytest.approx(1000.0, rel=1e-6)
    turn = next(m for m in route.maneuvers if m.type == "turn")
    assert turn.modifier == "left"
    assert turn.along_m == pytest.approx(500.0, abs=1.0)
    assert lane_advisory(500.0, route.maneuvers) is Lane.LEFT
    assert lane_advisory(200.0, route.maneuvers) is Lane.RIGHT  # straight on


@pytest.mark.parametrize(
    ("modifier", "lane"),
    [
        ("left", Lane.LEFT), ("slight left", Lane.LEFT), ("sharp left", Lane.LEFT),
        ("straight", Lane.RIGHT), ("right", Lane.RIGHT), ("sharp right", Lane.RIGHT),
        ("uturn", Lane.RIGHT), (None, Lane.RIGHT),
    ],
)  # fmt: skip
def test_lane_advisory_left_hand_traffic(modifier: str | None, lane: Lane) -> None:
    m = Maneuver(along_m=100.0, type="turn", modifier=modifier, location=LatLng(0, 0))
    assert lane_advisory(110.0, [m]) is lane
    assert lane_advisory(200.0, [m]) is Lane.RIGHT  # too far from the junction


def test_corridor_projection_lane_and_bearing() -> None:
    route = parse_osrm_route(osrm_body(10))
    sig = Signal("J1", "delhi", None, route.points[5], (1,))
    corridor = build_corridor(route, [CorridorCandidate(sig, 0.0)])
    assert corridor[0].along_m == pytest.approx(500.0, abs=0.5)
    assert corridor[0].lane is Lane.LEFT
    assert corridor[0].approach_bearing == pytest.approx(0.0, abs=0.5)  # heading north


@pytest.mark.parametrize("code", ["NoRoute", "InvalidQuery"])
def test_parse_rejects_non_ok(code: str) -> None:
    with pytest.raises(RoutingError):
        parse_osrm_route({"code": code, "message": "x"})


@respx.mock
async def test_request_shape_retry_and_cache(fake_redis: Redis) -> None:
    route = respx.get(url__startswith=f"{BASE}/route/v1/driving/").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=osrm_body())]
    )
    router = OsrmRouter(BASE, retries=2, cache=fake_redis, backoff_s=0.0)
    a, b = LatLng(28.6, 77.2), LatLng(28.61, 77.21)
    r1 = await router.route(a, b)
    assert route.call_count == 2  # one retry after the 503
    req = route.calls.last.request
    assert req.url.path == "/route/v1/driving/77.200000,28.600000;77.210000,28.610000"
    params = dict(req.url.params)
    assert params["overview"] == "full"
    assert params["geometries"] == "geojson"
    assert params["steps"] == "true"
    assert params["annotations"] == "distance,duration,speed"
    r2 = await router.route(a, b)  # served from the Redis cache
    assert route.call_count == 2
    assert r2.points == r1.points
    await router.aclose()


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
