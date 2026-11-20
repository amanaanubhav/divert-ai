"""dispatch_legs: written by the API (RESPONSE, in the dispatch transaction) and by the
LegPlanner (TRANSPORT / RETURN, in ONE transaction with the outbox row carrying the
``leg_planned`` command); read back by the planner (re-issued requests), the worker
(rebuild from PostgreSQL) and the API (incident detail)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from divert.db.geo_sql import lat_of, linestring_wkt, lng_of, point_wkt
from divert.db.models import DispatchLeg, Outbox
from divert.db.models import Station as StationRow
from divert.db.repositories.incidents import geojson_to_latlng, route_points_sql
from divert.domain.enums import DestinationKind, LegEndReason, LegKind
from divert.domain.geo import LatLng
from divert.store.events import OUTBOX_TOPIC_COMMANDS


@dataclass(frozen=True, slots=True)
class NewLeg:
    dispatch_id: uuid.UUID
    seq: int
    kind: LegKind
    origin: LatLng
    destination: LatLng
    destination_kind: DestinationKind
    destination_station_id: str | None
    route_points: Sequence[LatLng]
    segment_speeds_mps: Sequence[float]
    distance_m: float
    predicted_eta_s: float
    corridor: list[dict[str, Any]]
    route_choice: dict[str, Any] | None
    hospital_choice: dict[str, Any] | None
    eta_model: str
    planned_at: datetime
    started_at: datetime | None = None  # RESPONSE: starts with the dispatch

    def values(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "seq": self.seq,
            "kind": self.kind,
            "emergency": self.kind is not LegKind.RETURN,
            "origin": point_wkt(self.origin),
            "destination": point_wkt(self.destination),
            "destination_kind": self.destination_kind,
            "destination_station_id": self.destination_station_id,
            "route": linestring_wkt(self.route_points),
            "distance_m": self.distance_m,
            "predicted_eta_s": self.predicted_eta_s,
            "segment_speeds_mps": list(self.segment_speeds_mps),
            "corridor": self.corridor,
            "route_choice": self.route_choice,
            "hospital_choice": self.hospital_choice,
            "eta_model": self.eta_model,
            "planned_at": self.planned_at,
            "started_at": self.started_at,
            "started_trip_s": 0.0 if self.started_at is not None else None,
        }


@dataclass(frozen=True, slots=True)
class LegRow:
    dispatch_id: str
    seq: int
    kind: LegKind
    emergency: bool
    origin: LatLng
    destination: LatLng
    destination_kind: DestinationKind
    destination_station_id: str | None
    destination_name: str | None
    route_points: list[list[float]]
    segment_speeds_mps: list[float]
    distance_m: float
    predicted_eta_s: float
    corridor: list[dict[str, Any]]
    route_choice: dict[str, Any] | None
    hospital_choice: dict[str, Any] | None
    eta_model: str | None
    planned_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    actual_travel_s: float | None
    end_reason: LegEndReason | None
    reroutes: int
    started_trip_s: float | None
    ended_trip_s: float | None

    def to_json(self) -> dict[str, Any]:
        """The incident-detail ``legs[]`` item."""
        return {
            "seq": self.seq,
            "kind": self.kind.value,
            "emergency": self.emergency,
            "origin": [self.origin.lat, self.origin.lng],
            "destination": {
                "kind": self.destination_kind.value,
                "station_id": self.destination_station_id,
                "name": self.destination_name,
                "location": [self.destination.lat, self.destination.lng],
            },
            "route": self.route_points,
            "distance_m": round(self.distance_m, 1),
            "predicted_eta_s": round(self.predicted_eta_s, 1),
            "actual_travel_s": None
            if self.actual_travel_s is None
            else round(self.actual_travel_s, 1),
            "eta_error_pct": eta_error_pct(self.predicted_eta_s, self.actual_travel_s),
            "corridor": self.corridor,
            "route_choice": self.route_choice,
            "hospital_choice": self.hospital_choice,
            "eta_model": self.eta_model,
            "planned_at": _iso(self.planned_at),
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "end_reason": None if self.end_reason is None else self.end_reason.value,
            "reroutes": self.reroutes,
            "started_trip_s": self.started_trip_s,
            "ended_trip_s": self.ended_trip_s,
        }


def eta_error_pct(predicted: float, actual: float | None) -> float | None:
    """(actual - predicted) / actual in percent (the +-10 % guarantee is on this)."""
    if actual is None or actual <= 0:
        return None
    return round(100.0 * (actual - predicted) / actual, 1)


def _iso(ts: datetime | None) -> str | None:
    return None if ts is None else ts.isoformat()


def _cols() -> tuple[Any, ...]:
    return (
        DispatchLeg.dispatch_id,
        DispatchLeg.seq,
        DispatchLeg.kind,
        DispatchLeg.emergency,
        lat_of(DispatchLeg.origin).label("o_lat"),
        lng_of(DispatchLeg.origin).label("o_lng"),
        lat_of(DispatchLeg.destination).label("d_lat"),
        lng_of(DispatchLeg.destination).label("d_lng"),
        DispatchLeg.destination_kind,
        DispatchLeg.destination_station_id,
        StationRow.name.label("destination_name"),
        route_points_sql(DispatchLeg.route).label("route"),
        DispatchLeg.segment_speeds_mps,
        DispatchLeg.distance_m,
        DispatchLeg.predicted_eta_s,
        DispatchLeg.corridor,
        DispatchLeg.route_choice,
        DispatchLeg.hospital_choice,
        DispatchLeg.eta_model,
        DispatchLeg.planned_at,
        DispatchLeg.started_at,
        DispatchLeg.ended_at,
        DispatchLeg.actual_travel_s,
        DispatchLeg.end_reason,
        DispatchLeg.reroutes,
        DispatchLeg.started_trip_s,
        DispatchLeg.ended_trip_s,
    )


def _row(r: Any) -> LegRow:
    return LegRow(
        dispatch_id=str(r.dispatch_id),
        seq=int(r.seq),
        kind=LegKind(r.kind),
        emergency=bool(r.emergency),
        origin=LatLng(r.o_lat, r.o_lng),
        destination=LatLng(r.d_lat, r.d_lng),
        destination_kind=DestinationKind(r.destination_kind),
        destination_station_id=r.destination_station_id,
        destination_name=r.destination_name,
        route_points=geojson_to_latlng(r.route),
        segment_speeds_mps=list(r.segment_speeds_mps or []),
        distance_m=float(r.distance_m),
        predicted_eta_s=float(r.predicted_eta_s),
        corridor=list(r.corridor or []),
        route_choice=r.route_choice,
        hospital_choice=r.hospital_choice,
        eta_model=r.eta_model,
        planned_at=r.planned_at,
        started_at=r.started_at,
        ended_at=r.ended_at,
        actual_travel_s=r.actual_travel_s,
        end_reason=None if r.end_reason is None else LegEndReason(r.end_reason),
        reroutes=int(r.reroutes or 0),
        started_trip_s=r.started_trip_s,
        ended_trip_s=r.ended_trip_s,
    )


def _select() -> sa.Select[Any]:
    return sa.select(*_cols()).outerjoin(
        StationRow, StationRow.id == DispatchLeg.destination_station_id
    )


class LegRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_leg(self, leg: NewLeg) -> bool:
        """Insert the leg; False when (dispatch_id, seq) already exists (idempotent)."""
        res = await self.session.scalar(
            pg_insert(DispatchLeg)
            .values(**leg.values())
            .on_conflict_do_nothing(index_elements=["dispatch_id", "seq"])
            .returning(DispatchLeg.id)
        )
        return res is not None

    async def insert_leg_with_command(self, leg: NewLeg, command: dict[str, Any]) -> int | None:
        """The planner's write: the leg and the outbox row of its ``leg_planned`` command,
        in the caller's transaction. Returns the outbox id, or None when the leg already
        existed (another planner won the race - the caller re-sends the stored one)."""
        if not await self.insert_leg(leg):
            return None
        return await self.add_command(command)

    async def add_command(self, command: dict[str, Any]) -> int:
        outbox_id = await self.session.scalar(
            sa.insert(Outbox)
            .values(topic=OUTBOX_TOPIC_COMMANDS, payload=command)
            .returning(Outbox.id)
        )
        assert outbox_id is not None
        return int(outbox_id)

    async def get_leg(self, dispatch_id: uuid.UUID, seq: int) -> LegRow | None:
        row = (
            await self.session.execute(
                _select().where(DispatchLeg.dispatch_id == dispatch_id, DispatchLeg.seq == seq)
            )
        ).first()
        return None if row is None else _row(row)

    async def legs_of(self, dispatch_ids: Sequence[uuid.UUID]) -> list[LegRow]:
        if not dispatch_ids:
            return []
        rows = await self.session.execute(
            _select()
            .where(DispatchLeg.dispatch_id.in_(list(dispatch_ids)))
            .order_by(DispatchLeg.dispatch_id, DispatchLeg.seq)
        )
        return [_row(r) for r in rows]
