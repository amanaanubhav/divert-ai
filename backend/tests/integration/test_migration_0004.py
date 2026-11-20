"""Migration 0004_trip_legs on a database holding stage-A (revision 0003) data:
upgrade (backfill), downgrade (status mapping keeps the old CHECKs valid), upgrade again,
and no drift against the ORM. Rows are synthetic history on a throw-away database."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from divert.cli import alembic_config
from divert.db import models  # noqa: F401
from divert.db.base import Base
from divert.migrations.filters import include_object

from .conftest import database_url_for, recreate_database

pytestmark = pytest.mark.integration

ROUTE = "SRID=4326;LINESTRING(77.20 28.60, 77.21 28.61)"
SCENE = "SRID=4326;POINT(77.21 28.61)"

# (dispatch status story, incident status, unit status, arrived?, ended?, end_reason)
STORIES: dict[str, tuple[str, str | None, bool, bool, str | None]] = {
    "en_route": ("EN_ROUTE", "DISPATCHED", False, False, None),
    "on_scene": ("ON_SCENE", "ON_SCENE", True, False, None),
    "cleared": ("RESOLVED", None, True, True, "CLEARED"),
    "cancelled": ("CANCELLED", None, False, True, "CANCELLED"),
    "failed": ("FAILED", None, False, True, "FAILED"),
}


async def _exec(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text(sql), params)


async def _all(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(sa.text(sql), params)).all())


async def _stage_a_rows(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Reference rows + one dispatch per story, in the 0003 schema (no new columns)."""
    await _exec(
        engine,
        "INSERT INTO cities (id, name, timezone, center, bbox_south, bbox_west, bbox_north, "
        "bbox_east, zoom, hourly_congestion, avg_junction_delay_s) VALUES ('delhi', 'Delhi', "
        "'Asia/Kolkata', ST_GeogFromText('SRID=4326;POINT(77.2 28.6)'), 28.4, 76.8, 28.9, 77.4, "
        "12, array_fill(1.0::double precision, ARRAY[24]), 20)",
    )
    await _exec(
        engine,
        "INSERT INTO stations (id, city_id, service, name, osm_type, osm_id, geom) VALUES "
        "('H1', 'delhi', 'AMBULANCE', 'Hospital', 'node', 1, ST_GeogFromText(:p))",
        p="SRID=4326;POINT(77.20 28.60)",
    )
    await _exec(
        engine,
        "INSERT INTO signals (id, city_id, geom, osm_node_ids) VALUES "
        "('J1', 'delhi', ST_GeogFromText(:p), ARRAY[10]::bigint[])",
        p=SCENE,
    )
    ids: dict[str, uuid.UUID] = {}
    for n, (story, (inc_status, unit_status, arrived, ended, reason)) in enumerate(STORIES.items()):
        iid, did, unit = uuid.uuid4(), uuid.uuid4(), f"AMB-{n}"
        ids[story] = did
        await _exec(
            engine,
            "INSERT INTO units (id, station_id, city_id, service, ordinal) "
            "VALUES (:u, 'H1', 'delhi', 'AMBULANCE', :o)",
            u=unit,
            o=n + 1,
        )
        await _exec(
            engine,
            "INSERT INTO incidents (id, city_id, type, reported_type, geom, status) "
            "VALUES (:i, 'delhi', 'MEDICAL', 'MEDICAL', ST_GeogFromText(:p), :s)",
            i=iid,
            p=SCENE,
            s=inc_status,
        )
        await _exec(
            engine,
            "INSERT INTO dispatches (id, incident_id, unit_id, station_id, city_id, service, "
            "route, distance_m, predicted_eta_s, actual_travel_s, segment_speeds_mps, eta_model, "
            "arrived_at, ended_at, end_reason) VALUES (:d, :i, :u, 'H1', 'delhi', 'AMBULANCE', "
            "ST_GeogFromText(:r), 1400, 120, :a, ARRAY[12.0]::double precision[], 'kinematic-v2', "
            "CASE WHEN :arr THEN now() END, CASE WHEN :end THEN now() END, :why)",
            d=did,
            i=iid,
            u=unit,
            r=ROUTE,
            a=118.0 if arrived else None,
            arr=arrived,
            end=ended,
            why=reason,
        )
        if unit_status is not None:
            await _exec(
                engine,
                "UPDATE units SET status = :s, current_dispatch_id = :d WHERE id = :u",
                s=unit_status,
                d=did,
                u=unit,
            )
        await _exec(
            engine,
            "INSERT INTO incident_events (event_id, incident_id, dispatch_id, type, payload, ts) "
            "VALUES (:e, :i, :d, 'dispatched', "
            "jsonb_build_object('route_choice', jsonb_build_object('chosen', 'A')), now())",
            e=f"dsp:{did.hex}",
            i=iid,
            d=did,
        )
        await _exec(
            engine,
            "INSERT INTO vehicle_positions (dispatch_id, ts, geom, speed_mps, along_m, elapsed_s) "
            "VALUES (:d, now(), ST_GeogFromText(:p), 10, 100, 10)",
            d=did,
            p=SCENE,
        )
    # a stage A claim: lane 'C' could not be stored (NULL); its release payload has it all
    did = ids["cleared"]
    await _exec(
        engine,
        "INSERT INTO signal_preemptions (event_id, dispatch_id, signal_id, city_id, state, lane, "
        "claimed_at, released_at) VALUES ('clm:a', :d, 'J1', 'delhi', 'PREEMPTED', NULL, "
        "now(), now())",
        d=did,
    )
    await _exec(
        engine,
        "INSERT INTO incident_events (event_id, incident_id, dispatch_id, type, payload, ts) "
        "SELECT 'rel:a', incident_id, id, 'preemption_released', jsonb_build_object("
        "'claim_event_id', 'clm:a', 'lane', 'C', 'movement', 'STRAIGHT', 'lane_index', 2, "
        "'lanes_total', 3, 'free_left', false, 'required_lead_s', 21.5, 'transition_s', 7.0, "
        "'queue_clear_s', 9.5, 'reason', 'passed', 'outcome', 'CLEARED', "
        "'phase_state_at_claim', 'red_truncate', 'waited_s', 0.0, 'leg_seq', 0) , now() "
        "FROM dispatches WHERE id = :d",
        d=did,
    )
    # an older claim with only a release reason
    await _exec(
        engine,
        "INSERT INTO signal_preemptions (event_id, dispatch_id, signal_id, city_id, state, lane, "
        "claimed_at, released_at) VALUES ('clm:b', :d, 'J1', 'delhi', 'CONFLICT', 'R', "
        "now(), now())",
        d=ids["cancelled"],
    )
    await _exec(
        engine,
        "INSERT INTO incident_events (event_id, incident_id, dispatch_id, type, payload, ts) "
        "SELECT 'rel:b', incident_id, id, 'preemption_released', "
        "jsonb_build_object('claim_event_id', 'clm:b', 'reason', 'cancelled'), now() "
        "FROM dispatches WHERE id = :d",
        d=ids["cancelled"],
    )
    return ids


def _drift(conn: Connection) -> list[Any]:
    ctx = MigrationContext.configure(
        conn, opts={"include_object": include_object, "compare_type": True}
    )
    return list(compare_metadata(ctx, Base.metadata))


async def test_0004_backfills_stage_a_rows_and_round_trips() -> None:
    url = database_url_for("divert_test_m0004")
    await recreate_database(url)
    cfg = alembic_config(url)
    await asyncio.to_thread(command.upgrade, cfg, "0003_position_elapsed")
    eng = create_async_engine(url)
    try:
        ids = await _stage_a_rows(eng)

        # ---------------------------------------------------------------- upgrade
        await asyncio.to_thread(command.upgrade, cfg, "0004_trip_legs")
        by_id = {
            r.id: r
            for r in await _all(
                eng,
                "SELECT d.id, d.status, l.seq, l.kind, l.emergency, l.destination_kind, "
                "l.started_at IS NOT NULL AS started, l.end_reason, l.actual_travel_s, "
                "l.route_choice, ST_Distance(l.origin, ST_StartPoint(d.route::geometry)::geography)"
                " AS origin_gap_m FROM dispatches d JOIN dispatch_legs l ON l.dispatch_id = d.id",
            )
        }
        assert len(by_id) == len(STORIES), "every existing dispatch got its RESPONSE leg"
        expect = {
            "en_route": ("DISPATCHED", None),
            "on_scene": ("ON_SCENE", "ARRIVED"),
            "cleared": ("COMPLETED", "ARRIVED"),
            "cancelled": ("COMPLETED", "CANCELLED"),
            "failed": ("FAILED", "FAILED"),
        }
        for story, did in ids.items():
            r = by_id[did]
            assert (r.status, r.end_reason) == expect[story], story
            assert (r.seq, r.kind, r.emergency, r.destination_kind, r.started) == (
                0, "RESPONSE", True, "SCENE", True,
            )  # fmt: skip
            assert r.origin_gap_m < 0.01
            assert r.route_choice == {"chosen": "A"}
        assert by_id[ids["on_scene"]].actual_travel_s == 118.0
        pre = {
            r.event_id: r
            for r in await _all(
                eng,
                "SELECT event_id, lane, movement, lane_index, lanes_total, required_lead_s, "
                "outcome, phase_state_at_claim, leg_seq FROM signal_preemptions",
            )
        }
        a = pre["clm:a"]
        assert (a.lane, a.movement, a.lane_index, a.lanes_total) == ("C", "STRAIGHT", 2, 3)
        assert (a.required_lead_s, a.outcome, a.phase_state_at_claim, a.leg_seq) == (
            21.5, "CLEARED", "red_truncate", 0,
        )  # fmt: skip
        assert (pre["clm:b"].lane, pre["clm:b"].outcome) == ("R", "CANCELLED")
        positions = await _all(eng, "SELECT DISTINCT leg_seq FROM vehicle_positions")
        assert [r.leg_seq for r in positions] == [0]

        # ------------------------------------------ new statuses, then downgrade
        await _exec(
            eng,
            "UPDATE units SET status = 'RETURNING' WHERE current_dispatch_id = :d",
            d=ids["on_scene"],
        )
        await _exec(
            eng,
            "UPDATE incidents SET status = 'TRANSPORTING' WHERE id = "
            "(SELECT incident_id FROM dispatches WHERE id = :d)",
            d=ids["en_route"],
        )
        await asyncio.to_thread(command.downgrade, cfg, "0003_position_elapsed")
        units = await _all(eng, "SELECT status, current_dispatch_id FROM units ORDER BY id")
        assert {u.status for u in units} <= {"AVAILABLE", "DISPATCHED", "ON_SCENE"}
        assert [u.status for u in units if u.current_dispatch_id == ids["on_scene"]] == ["ON_SCENE"]
        statuses = await _all(eng, "SELECT DISTINCT status FROM incidents")
        assert "TRANSPORTING" not in {s.status for s in statuses}
        lanes = await _all(eng, "SELECT event_id, lane FROM signal_preemptions ORDER BY 1")
        assert [(r.event_id, r.lane) for r in lanes] == [("clm:a", None), ("clm:b", "R")]
        tables = await _all(eng, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        assert "dispatch_legs" not in {t.tablename for t in tables}

        # --------------------------------------------------- upgrade again, no drift
        await asyncio.to_thread(command.upgrade, cfg, "head")
        n_legs = await _all(eng, "SELECT count(*) AS n FROM dispatch_legs")
        assert n_legs[0].n == len(STORIES)
        async with eng.connect() as conn:
            assert await conn.run_sync(_drift) == []
    finally:
        await eng.dispose()
