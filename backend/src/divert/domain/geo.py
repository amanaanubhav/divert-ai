"""Spherical geodesy used everywhere in DIVERT.

All distances are great-circle (haversine) metres on a sphere of radius
``EARTH_RADIUS_M`` = 6 371 008.8 m (the IUGG mean Earth radius). At city scale the
spherical error versus the WGS84 ellipsoid is < 0.5 %, far below routing noise.

Coordinates are always ``LatLng(lat, lng)`` in decimal degrees (note the order:
latitude first, the same as the public API's ``[lat, lng]`` arrays).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

EARTH_RADIUS_M = 6_371_008.8


class LatLng(NamedTuple):
    lat: float
    lng: float


@dataclass(frozen=True, slots=True)
class BBox:
    south: float
    west: float
    north: float
    east: float

    def contains(self, p: LatLng) -> bool:
        return self.south <= p.lat <= self.north and self.west <= p.lng <= self.east


def haversine_m(a: LatLng, b: LatLng) -> float:
    """Great-circle distance in metres."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def initial_bearing_deg(a: LatLng, b: LatLng) -> float:
    """Initial great-circle bearing from a to b, degrees clockwise from north in [0, 360)."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlng = math.radians(b.lng - a.lng)
    y = math.sin(dlng) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination_point(origin: LatLng, bearing_deg: float, distance_m: float) -> LatLng:
    """Point reached travelling ``distance_m`` along a great circle at ``bearing_deg``."""
    delta = distance_m / EARTH_RADIUS_M
    theta = math.radians(bearing_deg)
    lat1, lng1 = math.radians(origin.lat), math.radians(origin.lng)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(delta) + math.cos(lat1) * math.sin(delta) * math.cos(theta)
    )
    lng2 = lng1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(lat1),
        math.cos(delta) - math.sin(lat1) * math.sin(lat2),
    )
    return LatLng(math.degrees(lat2), (math.degrees(lng2) + 540.0) % 360.0 - 180.0)


def bearing_delta_deg(from_deg: float, to_deg: float) -> float:
    """Signed smallest turn from one bearing to another, in (-180, 180]. Positive = right."""
    d = (to_deg - from_deg) % 360.0
    return d - 360.0 if d > 180.0 else d


def cumulative_distances(points: Sequence[LatLng]) -> list[float]:
    """``out[i]`` = haversine length of the polyline from points[0] to points[i]."""
    out = [0.0] * len(points)
    for i in range(1, len(points)):
        out[i] = out[i - 1] + haversine_m(points[i - 1], points[i])
    return out


def polyline_length_m(points: Sequence[LatLng]) -> float:
    return cumulative_distances(points)[-1] if points else 0.0


def interpolate_by_distance(
    points: Sequence[LatLng], cumulative: Sequence[float], distance_m: float
) -> tuple[LatLng, int]:
    """Position ``distance_m`` along the polyline (clamped to its ends).

    Returns (position, segment index i such that the point lies on points[i]..points[i+1]).
    ``cumulative`` must come from :func:`cumulative_distances` for the same points.
    """
    if not points:
        raise ValueError("empty polyline")
    if len(points) == 1 or distance_m <= 0.0:
        return points[0], 0
    total = cumulative[-1]
    if distance_m >= total:
        return points[-1], len(points) - 2
    i = bisect.bisect_right(cumulative, distance_m) - 1
    i = max(0, min(i, len(points) - 2))
    seg = cumulative[i + 1] - cumulative[i]
    f = 0.0 if seg <= 0.0 else (distance_m - cumulative[i]) / seg
    a, b = points[i], points[i + 1]
    return LatLng(a.lat + (b.lat - a.lat) * f, a.lng + (b.lng - a.lng) * f), i


def bearing_at_distance(
    points: Sequence[LatLng], cumulative: Sequence[float], distance_m: float
) -> float:
    """Travel bearing of the segment that contains ``distance_m`` (skips zero-length ones)."""
    if len(points) < 2:
        return 0.0
    _, i = interpolate_by_distance(points, cumulative, distance_m)
    j = i
    while j < len(points) - 2 and cumulative[j + 1] - cumulative[j] <= 0.0:
        j += 1
    return initial_bearing_deg(points[j], points[j + 1])


@dataclass(frozen=True, slots=True)
class Projection:
    """Closest point of a polyline to a query point."""

    point: LatLng  # the projected point on the polyline
    along_m: float  # distance from the polyline start to ``point`` (haversine, cumulative)
    offset_m: float  # haversine distance from the query point to ``point``
    segment: int  # index i of the segment points[i]..points[i+1]
    fraction: float  # position within that segment, 0..1


def _project_on_segment(p: LatLng, a: LatLng, b: LatLng) -> tuple[float, LatLng]:
    """Fraction t in [0,1] of the closest point on segment ab, using a local
    equirectangular frame centred on a (exact enough for segments < a few km)."""
    k = math.cos(math.radians((a.lat + b.lat) / 2.0))
    bx, by = (b.lng - a.lng) * k, b.lat - a.lat
    px, py = (p.lng - a.lng) * k, p.lat - a.lat
    seg2 = bx * bx + by * by
    t = 0.0 if seg2 == 0.0 else max(0.0, min(1.0, (px * bx + py * by) / seg2))
    return t, LatLng(a.lat + (b.lat - a.lat) * t, a.lng + (b.lng - a.lng) * t)


def project_onto_polyline(
    p: LatLng, points: Sequence[LatLng], cumulative: Sequence[float] | None = None
) -> Projection:
    """Project ``p`` onto the polyline; ties go to the earliest segment.

    ``along_m`` is measured with the same cumulative haversine distances as the
    simulation, so it is directly comparable with a vehicle's ``along_m``.
    """
    if not points:
        raise ValueError("empty polyline")
    cum = list(cumulative) if cumulative is not None else cumulative_distances(points)
    if len(points) == 1:
        return Projection(points[0], 0.0, haversine_m(p, points[0]), 0, 0.0)
    best: Projection | None = None
    for i in range(len(points) - 1):
        t, q = _project_on_segment(p, points[i], points[i + 1])
        off = haversine_m(p, q)
        if best is None or off < best.offset_m - 1e-9:
            along = cum[i] + t * (cum[i + 1] - cum[i])
            best = Projection(q, along, off, i, t)
    assert best is not None
    return best
