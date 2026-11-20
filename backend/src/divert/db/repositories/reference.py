"""Reference-data queries: cities, stations, signals, units.

Spatial work stays in PostGIS on GiST-indexed ``geography`` columns:

* :meth:`ReferenceRepository.nearest_stations_with_free_units` - index-assisted KNN
  (``ORDER BY geom <-> point``) restricted to stations that have an AVAILABLE unit;
* :meth:`ReferenceRepository.corridor_candidates` - ``ST_DWithin(signal, route, m)``;
* :meth:`ReferenceRepository.lock_free_unit` - ``FOR UPDATE SKIP LOCKED`` so two
  concurrent dispatches can never take the same vehicle.

Everything returns immutable domain objects (no ORM instances escape).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.ext.asyncio import AsyncSession

from divert.db.geo_sql import geog_line, geog_point, lat_of, lng_of
from divert.db.models import City as CityRow
from divert.db.models import Signal as SignalRow
from divert.db.models import Station as StationRow
from divert.db.models import Unit as UnitRow
from divert.domain.enums import ServiceType, UnitStatus
from divert.domain.geo import BBox, LatLng
from divert.domain.models import (
    City,
    CorridorCandidate,
    Signal,
    Station,
    StationAvailability,
    StationCandidate,
    TrafficProfile,
    Unit,
)


@dataclass(frozen=True, slots=True)
class CityReference:
    """Everything the API/worker cache per city (reloaded on demand)."""

    city: City
    stations: tuple[Station, ...]
    signals: tuple[Signal, ...]


def _station_cols() -> tuple[Any, ...]:
    return (
        StationRow.id,
        StationRow.city_id,
        StationRow.service,
        StationRow.name,
        lat_of(StationRow.geom).label("lat"),
        lng_of(StationRow.geom).label("lng"),
        StationRow.osm_type,
        StationRow.osm_id,
    )


def _station(row: Any) -> Station:
    return Station(
        id=row.id,
        city_id=row.city_id,
        service=ServiceType(row.service),
        name=row.name,
        pos=LatLng(row.lat, row.lng),
        osm_ref=f"{row.osm_type}/{row.osm_id}",
    )


def _signal_cols() -> tuple[Any, ...]:
    return (
        SignalRow.id,
        SignalRow.city_id,
        SignalRow.name,
        lat_of(SignalRow.geom).label("lat"),
        lng_of(SignalRow.geom).label("lng"),
        SignalRow.osm_node_ids,
    )


def _signal(row: Any) -> Signal:
    return Signal(
        id=row.id,
        city_id=row.city_id,
        name=row.name,
        pos=LatLng(row.lat, row.lng),
        osm_node_ids=tuple(row.osm_node_ids),
    )


def _unit(row: Any) -> Unit:
    return Unit(
        id=row.id,
        station_id=row.station_id,
        city_id=row.city_id,
        service=ServiceType(row.service),
        status=UnitStatus(row.status),
    )


class ReferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ cities
    async def list_cities(self) -> list[City]:
        rows = await self.session.execute(
            sa.select(
                CityRow,
                lat_of(CityRow.center).label("lat"),
                lng_of(CityRow.center).label("lng"),
            ).order_by(CityRow.id)
        )
        return [self._city(r.City, r.lat, r.lng) for r in rows]

    async def get_city(self, city_id: str) -> City | None:
        row = (
            await self.session.execute(
                sa.select(
                    CityRow,
                    lat_of(CityRow.center).label("lat"),
                    lng_of(CityRow.center).label("lng"),
                ).where(CityRow.id == city_id)
            )
        ).first()
        return None if row is None else self._city(row.City, row.lat, row.lng)

    @staticmethod
    def _city(c: CityRow, lat: float, lng: float) -> City:
        return City(
            id=c.id,
            name=c.name,
            timezone=c.timezone,
            center=LatLng(lat, lng),
            bbox=BBox(c.bbox_south, c.bbox_west, c.bbox_north, c.bbox_east),
            zoom=c.zoom,
            traffic=TrafficProfile(tuple(c.hourly_congestion), c.avg_junction_delay_s),
        )

    async def load_city_reference(self, city_id: str) -> CityReference | None:
        city = await self.get_city(city_id)
        if city is None:
            return None
        return CityReference(
            city=city,
            stations=tuple(await self.list_stations(city_id)),
            signals=tuple(await self.list_signals(city_id)),
        )

    # ---------------------------------------------------------------- stations
    async def list_stations(
        self, city_id: str, service: ServiceType | None = None
    ) -> list[Station]:
        q = sa.select(*_station_cols()).where(StationRow.city_id == city_id, StationRow.active)
        if service is not None:
            q = q.where(StationRow.service == service)
        rows = await self.session.execute(q.order_by(StationRow.id))
        return [_station(r) for r in rows]

    async def station_availability(self, city_id: str) -> list[StationAvailability]:
        total = sa.func.count(UnitRow.id)
        free = sa.func.count(UnitRow.id).filter(UnitRow.status == UnitStatus.AVAILABLE)
        rows = await self.session.execute(
            sa.select(*_station_cols(), total.label("total"), free.label("free"))
            .select_from(StationRow)
            .outerjoin(UnitRow, UnitRow.station_id == StationRow.id)
            .where(StationRow.city_id == city_id, StationRow.active)
            .group_by(StationRow.id)
            .order_by(StationRow.id)
        )
        return [StationAvailability(_station(r), int(r.total), int(r.free)) for r in rows]

    async def nearest_stations_with_free_units(
        self, city_id: str, service: ServiceType, point: LatLng, limit: int = 3
    ) -> list[StationCandidate]:
        """The ``limit`` nearest active stations of ``service`` that have a free unit.

        Index-assisted KNN on the geography GiST index; ``distance_m`` is the exact
        geodesic (spheroid) distance. Availability here is a hint - the dispatch
        transaction re-checks with :meth:`lock_free_unit`.
        """
        pt = geog_point(point)
        free = (
            sa.select(sa.func.count())
            .where(UnitRow.station_id == StationRow.id, UnitRow.status == UnitStatus.AVAILABLE)
            .correlate(StationRow)
            .scalar_subquery()
        )
        has_free = sa.exists().where(
            UnitRow.station_id == StationRow.id, UnitRow.status == UnitStatus.AVAILABLE
        )
        rows = await self.session.execute(
            sa.select(
                *_station_cols(),
                free.label("free"),
                sa.func.ST_Distance(StationRow.geom, pt).label("distance_m"),
            )
            .where(
                StationRow.city_id == city_id,
                StationRow.service == service,
                StationRow.active,
                has_free,
            )
            .order_by(StationRow.geom.op("<->")(pt))
            .limit(limit)
        )
        return [StationCandidate(_station(r), int(r.free), float(r.distance_m)) for r in rows]

    # ----------------------------------------------------------------- signals
    async def list_signals(self, city_id: str) -> list[Signal]:
        rows = await self.session.execute(
            sa.select(*_signal_cols())
            .where(SignalRow.city_id == city_id, SignalRow.active)
            .order_by(SignalRow.id)
        )
        return [_signal(r) for r in rows]

    async def corridor_candidates(
        self, city_id: str, route: Sequence[LatLng], within_m: float
    ) -> list[CorridorCandidate]:
        """Active signals within ``within_m`` metres of the route line (ST_DWithin on
        the GiST index), roughly ordered along the route.

        Precise along-route distances and lane advisories are computed by the caller
        with :func:`divert.domain.geo.project_onto_polyline` and the OSRM maneuvers.
        """
        line = geog_line(route)
        rows = await self.session.execute(
            sa.select(
                *_signal_cols(),
                sa.func.ST_Distance(SignalRow.geom, line).label("offset_m"),
                sa.func.ST_LineLocatePoint(
                    sa.cast(line, Geometry), sa.cast(SignalRow.geom, Geometry)
                ).label("frac"),
            )
            .where(
                SignalRow.city_id == city_id,
                SignalRow.active,
                sa.func.ST_DWithin(SignalRow.geom, line, within_m),
            )
            .order_by(sa.text("frac"), SignalRow.id)
        )
        return [CorridorCandidate(_signal(r), float(r.offset_m)) for r in rows]

    # ------------------------------------------------------------------- units
    async def list_units(self, city_id: str, station_id: str | None = None) -> list[Unit]:
        q = sa.select(
            UnitRow.id, UnitRow.station_id, UnitRow.city_id, UnitRow.service, UnitRow.status
        ).where(UnitRow.city_id == city_id)
        if station_id is not None:
            q = q.where(UnitRow.station_id == station_id)
        return [_unit(r) for r in await self.session.execute(q.order_by(UnitRow.id))]

    async def lock_free_unit(self, station_id: str) -> Unit | None:
        """Row-lock one AVAILABLE unit of the station, skipping units other open
        transactions have locked. Must run inside the dispatch transaction; the lock
        is held until it commits or rolls back."""
        row = (
            await self.session.execute(
                sa.select(
                    UnitRow.id,
                    UnitRow.station_id,
                    UnitRow.city_id,
                    UnitRow.service,
                    UnitRow.status,
                )
                .where(UnitRow.station_id == station_id, UnitRow.status == UnitStatus.AVAILABLE)
                .order_by(UnitRow.ordinal)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).first()
        return None if row is None else _unit(row)
