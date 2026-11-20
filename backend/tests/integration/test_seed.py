"""`divert seed` is idempotent and faithful to the committed OSM snapshots."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from divert.cities.loader import load_snapshot
from divert.config import Settings
from divert.db.models import City, Signal, Station, Unit
from divert.db.seed import SeedReport, seed_city
from divert.domain.enums import UnitStatus

pytestmark = pytest.mark.integration


async def _count(session: AsyncSession, model: type, city_id: str) -> int:
    return int(
        await session.scalar(
            sa.select(sa.func.count()).select_from(model).where(model.city_id == city_id)  # type: ignore[attr-defined]
        )
        or 0
    )


async def test_initial_seed_matches_snapshots(
    seeded_db: list[SeedReport], session: AsyncSession, test_settings: Settings
) -> None:
    assert {r.city_id for r in seeded_db} == set(test_settings.cities)
    for report in seeded_db:
        snap = load_snapshot(report.city_id)
        assert report.stations == len(snap.stations)
        assert report.signals == len(snap.signals)
        assert await _count(session, Station, report.city_id) == len(snap.stations)
        assert await _count(session, Signal, report.city_id) == len(snap.signals)
        assert (
            await _count(session, Unit, report.city_id)
            == len(snap.stations) * test_settings.units_per_station
        )
        city = await session.get(City, report.city_id)
        assert city is not None
        assert len(city.hourly_congestion) == 24
        assert city.data_meta["licence"].startswith("ODbL")


async def test_reseed_is_idempotent_and_keeps_live_unit_status(
    session: AsyncSession, test_settings: Settings
) -> None:
    city_id = test_settings.cities[0]
    snap = load_snapshot(city_id)
    unit_id = await session.scalar(
        sa.select(Unit.id).where(Unit.city_id == city_id).order_by(Unit.id).limit(1)
    )
    await session.execute(
        sa.update(Unit).where(Unit.id == unit_id).values(status=UnitStatus.OUT_OF_SERVICE)
    )
    before = {m.__name__: await _count(session, m, city_id) for m in (Station, Signal, Unit)}

    first = await seed_city(session, snap, test_settings.units_per_station)
    second = await seed_city(session, snap, test_settings.units_per_station)

    after = {m.__name__: await _count(session, m, city_id) for m in (Station, Signal, Unit)}
    assert before == after
    assert first.units_created == second.units_created == 0
    assert first.stations_deactivated == first.signals_deactivated == 0
    status = await session.scalar(sa.select(Unit.status).where(Unit.id == unit_id))
    assert status is UnitStatus.OUT_OF_SERVICE  # a re-seed never resets live state


async def test_vanished_station_is_deactivated_not_deleted(
    session: AsyncSession, test_settings: Settings
) -> None:
    city_id = test_settings.cities[0]
    snap = load_snapshot(city_id)
    dropped = snap.stations[0]
    trimmed = type(snap)(
        city=snap.city, stations=snap.stations[1:], signals=snap.signals, meta=snap.meta
    )
    report = await seed_city(session, trimmed, test_settings.units_per_station)
    assert report.stations_deactivated == 1
    row = await session.get(Station, dropped.id)
    assert row is not None
    assert row.active is False
    statuses = set(
        await session.scalars(sa.select(Unit.status).where(Unit.station_id == dropped.id))
    )
    assert statuses == {UnitStatus.OUT_OF_SERVICE}
    # and it comes back when the snapshot has it again
    await seed_city(session, snap, test_settings.units_per_station)
    await session.refresh(row)
    assert row.active is True
