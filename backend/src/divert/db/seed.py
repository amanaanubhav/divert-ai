"""Idempotent reference-data seed from the committed OSM snapshots.

* cities: upsert by id;
* stations: upsert by OSM natural key (osm_type, osm_id); stations that vanished from
  the snapshot are deactivated (never deleted: history references them) and their
  AVAILABLE units are taken OUT_OF_SERVICE;
* signals: upsert by junction id (``J<smallest member OSM node id>``), vanished ones
  deactivated;
* units: ``units_per_station`` per active station, inserted only when missing - an
  existing unit's live status is never reset by a re-seed.

Running it twice yields identical row counts (integration-tested).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from divert.cities.loader import CitySnapshot
from divert.db.geo_sql import point_wkt
from divert.db.models import City, Signal, Station, Unit
from divert.domain.enums import CALLSIGN_PREFIX, UnitStatus

log = logging.getLogger(__name__)
CHUNK = 1000


@dataclass(frozen=True, slots=True)
class SeedReport:
    city_id: str
    stations: int
    signals: int
    units_total: int
    units_created: int
    stations_deactivated: int
    signals_deactivated: int


def _chunks[T](items: Sequence[T], size: int = CHUNK) -> Iterator[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def callsign(prefix: str, station_id: str, ordinal: int) -> str:
    return f"{prefix}-{station_id}-{ordinal}"


async def seed_city(
    session: AsyncSession, snap: CitySnapshot, units_per_station: int
) -> SeedReport:
    """Upsert one city's reference data. Runs inside the caller's transaction."""
    c = snap.city
    city_row: dict[str, Any] = {
        "id": c.id,
        "name": c.name,
        "timezone": c.timezone,
        "center": point_wkt(c.center),
        "bbox_south": c.bbox.south,
        "bbox_west": c.bbox.west,
        "bbox_north": c.bbox.north,
        "bbox_east": c.bbox.east,
        "zoom": c.zoom,
        "hourly_congestion": list(c.traffic.hourly_congestion),
        "avg_junction_delay_s": c.traffic.avg_junction_delay_s,
        "data_meta": snap.meta,
    }
    stmt = insert(City).values(city_row)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[City.id],
            set_={k: stmt.excluded[k] for k in city_row if k != "id"}
            | {"updated_at": sa.func.now()},
        )
    )

    # ---- stations (natural key = OSM element)
    station_rows = []
    for s in snap.stations:
        osm_type, _, osm_id = s.osm_ref.partition("/")
        station_rows.append(
            {
                "id": s.id,
                "city_id": c.id,
                "service": s.service,
                "name": s.name,
                "osm_type": osm_type,
                "osm_id": int(osm_id),
                "geom": point_wkt(s.pos),
                "active": True,
                "emergency": s.emergency,
                "healthcare": s.healthcare,
            }
        )
    for st_chunk in _chunks(station_rows):
        ins = insert(Station).values(list(st_chunk))
        await session.execute(
            ins.on_conflict_do_update(
                index_elements=[Station.osm_type, Station.osm_id],
                set_={
                    "city_id": ins.excluded.city_id,
                    "service": ins.excluded.service,
                    "name": ins.excluded.name,
                    "geom": ins.excluded.geom,
                    "active": sa.true(),
                    "emergency": ins.excluded.emergency,
                    "healthcare": ins.excluded.healthcare,
                    "updated_at": sa.func.now(),
                },
            )
        )
    station_ids = [r["id"] for r in station_rows]
    gone_stations = await session.execute(
        sa.update(Station)
        .where(Station.city_id == c.id, Station.active, Station.id.not_in(station_ids))
        .values(active=False, updated_at=sa.func.now())
        .returning(Station.id)
    )
    deactivated = [r[0] for r in gone_stations]
    if deactivated:
        await session.execute(
            sa.update(Unit)
            .where(Unit.station_id.in_(deactivated), Unit.status == UnitStatus.AVAILABLE)
            .values(status=UnitStatus.OUT_OF_SERVICE, status_changed_at=sa.func.now())
        )

    # ---- signals (natural key = junction id derived from OSM node ids)
    signal_rows = [
        {
            "id": s.id,
            "city_id": c.id,
            "name": s.name,
            "geom": point_wkt(s.pos),
            "osm_node_ids": list(s.osm_node_ids),
            "active": True,
        }
        for s in snap.signals
    ]
    for sig_chunk in _chunks(signal_rows):
        ins = insert(Signal).values(list(sig_chunk))
        await session.execute(
            ins.on_conflict_do_update(
                index_elements=[Signal.id],
                set_={
                    "city_id": ins.excluded.city_id,
                    "name": ins.excluded.name,
                    "geom": ins.excluded.geom,
                    "osm_node_ids": ins.excluded.osm_node_ids,
                    "active": sa.true(),
                    "updated_at": sa.func.now(),
                },
            )
        )
    signal_ids = [r["id"] for r in signal_rows]
    gone_signals = await session.execute(
        sa.update(Signal)
        .where(Signal.city_id == c.id, Signal.active, Signal.id.not_in(signal_ids))
        .values(active=False, updated_at=sa.func.now())
        .returning(Signal.id)
    )
    signals_deactivated = len(gone_signals.all())

    # ---- units: insert missing only (never touch a live unit's status)
    # Station ids may differ from the snapshot when an OSM element kept its natural key
    # but changed amenity, so read the stored ids back.
    stored = await session.execute(
        sa.select(Station.id, Station.service).where(Station.city_id == c.id, Station.active)
    )
    unit_rows = [
        {
            "id": callsign(CALLSIGN_PREFIX[service], station_id, n),
            "station_id": station_id,
            "city_id": c.id,
            "service": service,
            "ordinal": n,
        }
        for station_id, service in stored.all()
        for n in range(1, units_per_station + 1)
    ]
    created = 0
    for unit_chunk in _chunks(unit_rows):
        res = await session.execute(
            insert(Unit)
            .values(list(unit_chunk))
            .on_conflict_do_nothing(index_elements=[Unit.station_id, Unit.ordinal])
            .returning(Unit.id)
        )
        created += len(res.all())
    units_total = await session.scalar(
        sa.select(sa.func.count()).select_from(Unit).where(Unit.city_id == c.id)
    )
    report = SeedReport(
        city_id=c.id,
        stations=len(station_rows),
        signals=len(signal_rows),
        units_total=int(units_total or 0),
        units_created=created,
        stations_deactivated=len(deactivated),
        signals_deactivated=signals_deactivated,
    )
    log.info("seeded %s", c.id, extra={"seed": report.__repr__()})
    return report
