"""Migration 0005 (stage 5 fix-2 B5, B6) on a database holding 0004 data: the contested
count of every claim is backfilled from its preemption_conflict events (a claim queued
and later served used to look uncontested), stations.speciality is added; downgrade,
upgrade again and no drift against the ORM. Synthetic rows on a throw-away database."""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from divert.cli import alembic_config

from .conftest import database_url_for, recreate_database
from .test_migration_0004 import _all, _drift, _exec, _stage_a_rows

pytestmark = pytest.mark.integration


async def _conflict_event(eng: AsyncEngine, event: str, claim: str, dispatch: object) -> None:
    await _exec(
        eng,
        "INSERT INTO incident_events (event_id, incident_id, dispatch_id, type, payload, ts) "
        "SELECT :e, incident_id, id, 'preemption_conflict', "
        "jsonb_build_object('claim_event_id', CAST(:c AS text), 'winner', 'PCR-1'), now() "
        "FROM dispatches WHERE id = :d",
        e=event,
        c=claim,
        d=dispatch,
    )


async def test_0005_backfills_contested_claims_and_round_trips() -> None:
    url = database_url_for("divert_test_m0005")
    await recreate_database(url)
    cfg = alembic_config(url)
    await asyncio.to_thread(command.upgrade, cfg, "0003_position_elapsed")
    eng = create_async_engine(url)
    try:
        ids = await _stage_a_rows(eng)  # clm:a served (PREEMPTED), clm:b lost (CONFLICT)
        await asyncio.to_thread(command.upgrade, cfg, "0004_trip_legs")
        # clm:c was queued twice and then served: its row says PREEMPTED
        await _exec(
            eng,
            "INSERT INTO signal_preemptions (event_id, dispatch_id, signal_id, city_id, state, "
            "claimed_at, released_at, outcome) VALUES ('clm:c', :d, 'J1', 'delhi', 'PREEMPTED', "
            "now(), now(), 'CLEARED')",
            d=ids["on_scene"],
        )
        await _conflict_event(eng, "cfl:c:1", "clm:c", ids["on_scene"])
        await _conflict_event(eng, "cfl:c:2", "clm:c", ids["on_scene"])

        await asyncio.to_thread(command.upgrade, cfg, "0005_contested_claims_speciality")
        got = {
            r.event_id: (r.state, r.conflicts)
            for r in await _all(eng, "SELECT event_id, state, conflicts FROM signal_preemptions")
        }
        assert got == {
            "clm:a": ("PREEMPTED", 0),  # never queued
            "clm:b": ("CONFLICT", 1),  # still queued at release (no conflict event kept)
            "clm:c": ("PREEMPTED", 2),  # queued twice, then served: contested is kept
        }
        cols = await _all(
            eng,
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'stations' AND column_name = 'speciality'",
        )
        assert [(c.column_name, c.data_type) for c in cols] == [("speciality", "text")]
        with pytest.raises(Exception, match="ck_signal_preemptions_conflicts"):
            await _exec(eng, "UPDATE signal_preemptions SET conflicts = -1")

        await asyncio.to_thread(command.downgrade, cfg, "0004_trip_legs")
        cols = await _all(
            eng,
            "SELECT column_name FROM information_schema.columns WHERE "
            "(table_name, column_name) IN (('stations', 'speciality'), "
            "('signal_preemptions', 'conflicts'))",
        )
        assert cols == []
        await asyncio.to_thread(command.upgrade, cfg, "head")
        async with eng.connect() as conn:
            assert await conn.run_sync(_drift) == []
    finally:
        await eng.dispose()
