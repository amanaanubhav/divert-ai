"""Geo math against closed-form values and an independent formula."""

from __future__ import annotations

import math
import random

import pytest

from divert.domain.geo import (
    EARTH_RADIUS_M,
    BBox,
    LatLng,
    bearing_at_distance,
    bearing_delta_deg,
    cumulative_distances,
    destination_point,
    haversine_m,
    initial_bearing_deg,
    interpolate_by_distance,
    polyline_length_m,
    project_onto_polyline,
)

ONE_DEG_M = EARTH_RADIUS_M * math.pi / 180.0  # 111 195.08 m on this sphere


def _vincenty_sphere(a: LatLng, b: LatLng) -> float:
    """Great-circle distance via the Vincenty special case (independent of haversine)."""
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dl = math.radians(b.lng - a.lng)
    num = math.hypot(
        math.cos(p2) * math.sin(dl),
        math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl),
    )
    den = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return EARTH_RADIUS_M * math.atan2(num, den)


def test_radius_is_iugg_mean() -> None:
    assert EARTH_RADIUS_M == 6_371_008.8


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (LatLng(0, 0), LatLng(1, 0), ONE_DEG_M),  # 1 deg of meridian
        (LatLng(0, 0), LatLng(0, 1), ONE_DEG_M),  # 1 deg of equator
        (LatLng(0, 0), LatLng(0, 180), math.pi * EARTH_RADIUS_M),  # antipodal
        (LatLng(-90, 0), LatLng(90, 0), math.pi * EARTH_RADIUS_M),  # pole to pole
        # along a parallel the arc is NOT cos(lat)*dlng*R; at 60N, 1 deg of longitude:
        (LatLng(60, 0), LatLng(60, 1), 2 * EARTH_RADIUS_M * math.asin(
            math.cos(math.radians(60)) * math.sin(math.radians(0.5)))),
    ],
)  # fmt: skip
def test_haversine_known_distances(a: LatLng, b: LatLng, expected: float) -> None:
    assert haversine_m(a, b) == pytest.approx(expected, rel=1e-12, abs=1e-6)


def test_haversine_matches_independent_formula_on_city_scale_pairs() -> None:
    rng = random.Random(7)
    for _ in range(500):
        a = LatLng(rng.uniform(8, 35), rng.uniform(68, 97))  # India
        b = destination_point(a, rng.uniform(0, 360), rng.uniform(1, 50_000))
        assert haversine_m(a, b) == pytest.approx(_vincenty_sphere(a, b), rel=1e-9, abs=1e-6)
        assert haversine_m(a, b) == pytest.approx(haversine_m(b, a), abs=1e-9)


def test_destination_point_roundtrip() -> None:
    origin = LatLng(28.6139, 77.2090)
    for bearing in range(0, 360, 15):
        for dist in (1.0, 37.5, 600.0, 12_345.0):
            p = destination_point(origin, bearing, dist)
            assert haversine_m(origin, p) == pytest.approx(dist, rel=1e-9, abs=1e-6)
            assert initial_bearing_deg(origin, p) == pytest.approx(bearing % 360, abs=1e-6) or (
                bearing == 0 and initial_bearing_deg(origin, p) == pytest.approx(360, abs=1e-6)
            )


@pytest.mark.parametrize(
    ("b", "expected"),
    [(LatLng(1, 0), 0.0), (LatLng(0, 1), 90.0), (LatLng(-1, 0), 180.0), (LatLng(0, -1), 270.0)],
)
def test_initial_bearing_cardinals(b: LatLng, expected: float) -> None:
    assert initial_bearing_deg(LatLng(0, 0), b) == pytest.approx(expected, abs=1e-9)


def test_bearing_delta_signed() -> None:
    assert bearing_delta_deg(350, 10) == pytest.approx(20)
    assert bearing_delta_deg(10, 350) == pytest.approx(-20)
    assert bearing_delta_deg(0, 180) == pytest.approx(180)
    assert bearing_delta_deg(90, 0) == pytest.approx(-90)  # left turn is negative


def _line() -> list[LatLng]:
    a = LatLng(22.5726, 88.3639)
    b = destination_point(a, 90, 1000)  # 1 km east
    c = destination_point(b, 0, 500)  # then 500 m north
    return [a, b, c]


def test_cumulative_and_length() -> None:
    pts = _line()
    cum = cumulative_distances(pts)
    assert cum[0] == 0.0
    assert cum[1] == pytest.approx(1000, abs=1e-6)
    assert cum[2] == pytest.approx(1500, abs=1e-6)
    assert polyline_length_m(pts) == pytest.approx(1500, abs=1e-6)
    assert polyline_length_m([]) == 0.0


@pytest.mark.parametrize("d", [0.0, 1.0, 250.0, 999.0, 1000.0, 1200.0, 1500.0])
def test_interpolate_by_distance_is_consistent_with_cumulative(d: float) -> None:
    pts = _line()
    cum = cumulative_distances(pts)
    p, seg = interpolate_by_distance(pts, cum, d)
    # the interpolated point is d metres along: distance to the segment start matches
    assert cum[seg] + haversine_m(pts[seg], p) == pytest.approx(d, abs=0.05)


def test_interpolate_clamps_and_rejects_empty() -> None:
    pts = _line()
    cum = cumulative_distances(pts)
    assert interpolate_by_distance(pts, cum, -5)[0] == pts[0]
    assert interpolate_by_distance(pts, cum, 1e9)[0] == pts[-1]
    with pytest.raises(ValueError, match="empty"):
        interpolate_by_distance([], [], 1.0)


def test_bearing_at_distance() -> None:
    pts = _line()
    cum = cumulative_distances(pts)
    assert bearing_at_distance(pts, cum, 500) == pytest.approx(90, abs=0.01)
    assert bearing_at_distance(pts, cum, 1200) == pytest.approx(0, abs=0.01) or (
        bearing_at_distance(pts, cum, 1200) == pytest.approx(360, abs=0.01)
    )


@pytest.mark.parametrize(("along", "offset"), [(100, 0), (400, 25), (900, 29.9), (1300, 12)])
def test_projection_recovers_along_and_offset(along: float, offset: float) -> None:
    pts = _line()
    cum = cumulative_distances(pts)
    on_line, seg = interpolate_by_distance(pts, cum, along)
    heading = initial_bearing_deg(pts[seg], pts[seg + 1])
    query = destination_point(on_line, heading + 90, offset)  # step off to the right
    proj = project_onto_polyline(query, pts, cum)
    assert proj.along_m == pytest.approx(along, abs=0.5)
    assert proj.offset_m == pytest.approx(offset, abs=0.1)
    assert proj.segment == seg


def test_projection_before_start_clamps_to_start() -> None:
    pts = _line()
    behind = destination_point(pts[0], 270, 80)
    proj = project_onto_polyline(behind, pts)
    assert proj.along_m == 0.0
    assert proj.offset_m == pytest.approx(80, abs=0.01)


def test_bbox_contains() -> None:
    box = BBox(22.45, 88.25, 22.65, 88.50)
    assert box.contains(LatLng(22.5726, 88.3639))
    assert not box.contains(LatLng(22.70, 88.36))
    assert box.contains(LatLng(22.45, 88.25))  # inclusive edges
