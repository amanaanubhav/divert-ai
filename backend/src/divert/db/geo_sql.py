"""Helpers to move between domain LatLng values and PostGIS geography expressions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import Geometry
from geoalchemy2.elements import WKTElement

from divert.domain.geo import LatLng


def point_wkt(p: LatLng) -> WKTElement:
    """WKT is lng-lat (x y); our domain is lat-lng."""
    return WKTElement(f"POINT({p.lng!r} {p.lat!r})", srid=4326, extended=False)


def linestring_wkt(points: Sequence[LatLng]) -> WKTElement:
    pts = dedupe_consecutive(points)
    if len(pts) < 2:
        raise ValueError("a route needs at least two distinct points")
    body = ", ".join(f"{p.lng!r} {p.lat!r}" for p in pts)
    return WKTElement(f"LINESTRING({body})", srid=4326, extended=False)


def geog_point(p: LatLng) -> sa.ColumnElement[Any]:
    return sa.func.ST_GeogFromText(f"SRID=4326;POINT({p.lng!r} {p.lat!r})")


def geog_line(points: Sequence[LatLng]) -> sa.ColumnElement[Any]:
    return sa.func.ST_GeogFromText(f"SRID=4326;{linestring_wkt(points).data}")


def lat_of(col: Any) -> sa.ColumnElement[float]:
    return sa.func.ST_Y(sa.cast(col, Geometry)).cast(sa.Double)


def lng_of(col: Any) -> sa.ColumnElement[float]:
    return sa.func.ST_X(sa.cast(col, Geometry)).cast(sa.Double)


def dedupe_consecutive(points: Sequence[LatLng]) -> list[LatLng]:
    out: list[LatLng] = []
    for p in points:
        if not out or out[-1] != p:
            out.append(p)
    return out
