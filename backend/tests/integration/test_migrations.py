"""Alembic migration against real PostGIS: schema shape, indexes, no ORM drift, reversibility."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from divert.cli import alembic_config, run_migrations
from divert.db import models  # noqa: F401
from divert.db.base import Base
from divert.migrations.filters import include_object

from .conftest import database_url_for, recreate_database

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "cities", "stations", "units", "signals", "incidents", "dispatches",
    "signal_preemptions", "incident_events", "vehicle_positions", "outbox", "dispatch_legs",
}  # fmt: skip


async def _scalar_rows(engine: AsyncEngine, sql: str) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(sa.text(sql))).all())


async def test_head_has_all_tables_and_postgis(engine: AsyncEngine) -> None:
    rows = await _scalar_rows(engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    assert {r[0] for r in rows} >= EXPECTED_TABLES
    (version,) = (await _scalar_rows(engine, "SELECT postgis_lib_version()"))[0]
    assert str(version).startswith("3.")
    (rev,) = (await _scalar_rows(engine, "SELECT version_num FROM alembic_version"))[0]
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    head = ScriptDirectory.from_config(alembic_config("postgresql://unused")).get_current_head()
    assert rev == head == "0005_contested_claims_speciality"


async def test_spatial_and_time_indexes(engine: AsyncEngine) -> None:
    rows = await _scalar_rows(
        engine, "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
    )
    defs = dict(rows)
    for idx in ("ix_signals_geom", "ix_stations_geom", "ix_incidents_geom", "ix_dispatches_route"):
        assert "USING gist" in defs[idx], idx
    assert "USING brin" in defs["ix_vehicle_positions_ts_brin"]
    assert "WHERE" in defs["ix_units_free_by_station"]
    assert "'AVAILABLE'" in defs["ix_units_free_by_station"]
    assert "UNIQUE" in defs["uq_dispatches_active_unit"]
    assert "WHERE (ended_at IS NULL)" in defs["uq_dispatches_active_unit"]
    assert "WHERE (sent_at IS NULL)" in defs["ix_outbox_unsent"]
    assert "WHERE (sent_at IS NOT NULL)" in defs["ix_outbox_sent_at"]
    assert "(city_id, dispatched_at)" in defs["ix_dispatches_city_dispatched"]


async def test_geography_column_types(engine: AsyncEngine) -> None:
    rows = await _scalar_rows(
        engine,
        "SELECT f_table_name, f_geography_column, type, srid FROM geography_columns "
        "WHERE f_table_schema = 'public' ORDER BY 1, 2",
    )
    got = {(t, c): (typ, srid) for t, c, typ, srid in rows}
    assert got[("signals", "geom")] == ("Point", 4326)
    assert got[("stations", "geom")] == ("Point", 4326)
    assert got[("incidents", "geom")] == ("Point", 4326)
    assert got[("vehicle_positions", "geom")] == ("Point", 4326)
    assert got[("cities", "center")] == ("Point", 4326)
    assert got[("dispatches", "route")] == ("LineString", 4326)


async def test_models_match_migration(engine: AsyncEngine) -> None:
    def diff(conn: Connection) -> list[Any]:
        ctx = MigrationContext.configure(
            conn, opts={"include_object": include_object, "compare_type": True}
        )
        return list(compare_metadata(ctx, Base.metadata))

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []


async def test_downgrade_to_base_and_upgrade_again() -> None:
    url = database_url_for("divert_test_migrations")
    await recreate_database(url)
    await asyncio.to_thread(run_migrations, url)
    await asyncio.to_thread(command.downgrade, alembic_config(url), "base")
    eng = create_async_engine(url)
    try:
        rows = await _scalar_rows(
            eng, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        assert EXPECTED_TABLES.isdisjoint({r[0] for r in rows})
        await asyncio.to_thread(run_migrations, url)
        rows = await _scalar_rows(
            eng, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        assert {r[0] for r in rows} >= EXPECTED_TABLES
    finally:
        await eng.dispose()
