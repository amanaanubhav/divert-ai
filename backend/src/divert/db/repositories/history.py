"""History writes (persister), worker rebuild reads and retention.

Every write is idempotent so a redelivered stream message is harmless:

* incident_events / signal_preemptions are keyed by the event's deterministic id
  (``ON CONFLICT (event_id) DO NOTHING`` / ``DO UPDATE``);
* vehicle_positions is keyed by (dispatch_id, ts);
* status transitions only move forward and never leave a terminal status;
* a unit is freed only while it still points at the dispatch that ended.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from divert.db.geo_sql import lat_of, lng_of, point_wkt
from divert.db.models import (
    Dispatch,
    Incident,
    IncidentEvent,
    Outbox,
    SignalPreemption,
    Unit,
    VehiclePosition,
)
from divert.db.repositories.incidents import geojson_to_latlng, route_points_sql
from divert.domain.enums import IncidentStatus, Lane, SignalState, UnitStatus
from divert.domain.geo import LatLng
from divert.store.events import HistoryEventType

_NON_TERMINAL: Final = (
    IncidentStatus.OPEN,
    IncidentStatus.DISPATCHED,
    IncidentStatus.EN_ROUTE,
    IncidentStatus.ON_SCENE,
)
_FORWARD_FROM: Final[dict[IncidentStatus, tuple[IncidentStatus, ...]]] = {
    IncidentStatus.EN_ROUTE: (IncidentStatus.OPEN, IncidentStatus.DISPATCHED),
    IncidentStatus.ON_SCENE: (
        IncidentStatus.OPEN,
        IncidentStatus.DISPATCHED,
        IncidentStatus.EN_ROUTE,
    ),
    IncidentStatus.RESOLVED: _NON_TERMINAL,
    IncidentStatus.CANCELLED: _NON_TERMINAL,
    IncidentStatus.FAILED: _NON_TERMINAL,
}
_TS_COLUMN: Final = {
    IncidentStatus.EN_ROUTE: "en_route_at",
    IncidentStatus.ON_SCENE: "on_scene_at",
    IncidentStatus.RESOLVED: "resolved_at",
    IncidentStatus.CANCELLED: "cancelled_at",
    IncidentStatus.FAILED: "failed_at",
}
_ENVELOPE: Final = frozenset({"type", "v", "event_id", "ts", "city", "incident_id", "dispatch_id"})
_END_REASON: Final = {
    HistoryEventType.CLEARED: "CLEARED",
    HistoryEventType.CANCELLED: "CANCELLED",
    HistoryEventType.FAILED: "FAILED",
}
_END_STATUS: Final = {
    HistoryEventType.CLEARED: IncidentStatus.RESOLVED,
    HistoryEventType.CANCELLED: IncidentStatus.CANCELLED,
    HistoryEventType.FAILED: IncidentStatus.FAILED,
}


class BadEvent(ValueError):
    """The event can never be applied (schema violation) - goes straight to the DLQ."""


def _ts(value: Any) -> datetime:
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise BadEvent(f"bad timestamp {value!r}") from exc
    if ts.tzinfo is None:
        raise BadEvent("timestamp without timezone")
    return ts


def _uuid(value: Any, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise BadEvent(f"bad {what} {value!r}") from exc


class HistoryWriter:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def apply(self, ev: Mapping[str, Any]) -> bool:
        """Apply one history event. Returns False when it was a duplicate."""
        try:
            kind = HistoryEventType(ev["type"])
        except (KeyError, ValueError) as exc:
            raise BadEvent(f"unknown event type {ev.get('type')!r}") from exc
        ts = _ts(ev.get("ts"))
        dispatch_id = _uuid(ev.get("dispatch_id"), "dispatch_id")
        if kind is HistoryEventType.VEHICLE_POSITION:
            return await self._position(ev, dispatch_id, ts)
        incident_id = _uuid(ev.get("incident_id"), "incident_id")
        event_id = str(ev.get("event_id") or "")
        if not event_id or len(event_id) > 64:
            raise BadEvent("missing/oversized event_id")
        payload = {k: v for k, v in ev.items() if k not in _ENVELOPE}
        inserted = await self.session.scalar(
            pg_insert(IncidentEvent)
            .values(
                event_id=event_id,
                incident_id=incident_id,
                dispatch_id=dispatch_id if await self._dispatch_exists(dispatch_id) else None,
                type=kind.value,
                payload=payload,
                ts=ts,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(IncidentEvent.id)
        )
        if inserted is None:
            return False  # its effects were committed together with the first copy
        match kind:
            case HistoryEventType.EN_ROUTE:
                await self._advance(incident_id, IncidentStatus.EN_ROUTE, ts)
            case (
                HistoryEventType.PREEMPTION_CLAIMED
                | HistoryEventType.PREEMPTION_CONFLICT
                | HistoryEventType.PREEMPTION_RELEASED
            ):
                await self._preemption(kind, ev, dispatch_id, ts)
            case HistoryEventType.ARRIVED:
                await self._arrived(ev, incident_id, dispatch_id, ts)
            case HistoryEventType.CLEARED | HistoryEventType.CANCELLED | HistoryEventType.FAILED:
                await self._ended(kind, ev, incident_id, dispatch_id, ts)
            case _:  # pragma: no cover - enum is exhaustive above
                pass
        return True

    async def _dispatch_exists(self, dispatch_id: uuid.UUID) -> bool:
        return bool(
            await self.session.scalar(sa.select(sa.exists().where(Dispatch.id == dispatch_id)))
        )

    async def _position(self, ev: Mapping[str, Any], dispatch_id: uuid.UUID, ts: datetime) -> bool:
        try:
            lat, lng = (float(x) for x in ev["pos"])
            speed = float(ev.get("speed_mps", 0.0))
            along = float(ev.get("along_m", 0.0))
            raw_elapsed = ev.get("elapsed_s")
            elapsed = None if raw_elapsed is None else float(raw_elapsed)
        except (KeyError, TypeError, ValueError) as exc:
            raise BadEvent(f"bad vehicle_position: {exc!r}") from exc
        if not await self._dispatch_exists(dispatch_id):
            raise BadEvent(f"unknown dispatch {dispatch_id}")
        res = await self.session.scalar(
            pg_insert(VehiclePosition)
            .values(
                dispatch_id=dispatch_id,
                ts=ts,
                geom=point_wkt(LatLng(lat, lng)),
                speed_mps=speed,
                along_m=along,
                elapsed_s=elapsed,
            )
            .on_conflict_do_nothing(index_elements=["dispatch_id", "ts"])
            .returning(VehiclePosition.ts)
        )
        return res is not None

    async def _advance(
        self,
        incident_id: uuid.UUID,
        status: IncidentStatus,
        ts: datetime,
        *,
        failure_reason: str | None = None,
    ) -> bool:
        col = getattr(Incident, _TS_COLUMN[status])
        values: dict[str, Any] = {"status": status, _TS_COLUMN[status]: sa.func.coalesce(col, ts)}
        if failure_reason is not None:
            values["failure_reason"] = failure_reason[:2000]
        res = await self.session.execute(
            sa.update(Incident)
            .where(Incident.id == incident_id, Incident.status.in_(_FORWARD_FROM[status]))
            .values(**values)
        )
        return bool(res.rowcount)  # type: ignore[attr-defined]

    async def _preemption(
        self, kind: HistoryEventType, ev: Mapping[str, Any], dispatch_id: uuid.UUID, ts: datetime
    ) -> None:
        try:
            state = SignalState(ev["state"])
            row = {
                "event_id": str(ev["claim_event_id"]),
                "dispatch_id": dispatch_id,
                "signal_id": str(ev["signal_id"]),
                "city_id": str(ev["city"]),
                "state": SignalState.CONFLICT
                if kind is HistoryEventType.PREEMPTION_CONFLICT
                else state,
                "lane": Lane(ev["lane"]) if ev.get("lane") else None,
                "approach_bearing": ev.get("approach_bearing"),
                "eta_at_claim_s": ev.get("eta_at_claim_s"),
                "claimed_at": _ts(ev.get("claimed_at", ev.get("ts"))),
                "released_at": ts if kind is HistoryEventType.PREEMPTION_RELEASED else None,
            }
        except (KeyError, ValueError) as exc:
            raise BadEvent(f"bad preemption event: {exc!r}") from exc
        if row["state"] is SignalState.NORMAL:
            raise BadEvent("a claim is never NORMAL")
        stmt = pg_insert(SignalPreemption).values(**row)
        excluded = stmt.excluded
        if kind is HistoryEventType.PREEMPTION_CLAIMED:
            stmt2 = stmt.on_conflict_do_nothing(index_elements=["event_id"])
        elif kind is HistoryEventType.PREEMPTION_CONFLICT:
            stmt2 = stmt.on_conflict_do_update(
                index_elements=["event_id"], set_={"state": SignalState.CONFLICT}
            )
        else:  # released: keep the earliest release; carry the claim data if it is missing
            stmt2 = stmt.on_conflict_do_update(
                index_elements=["event_id"],
                set_={
                    "released_at": sa.func.coalesce(
                        SignalPreemption.released_at, excluded.released_at
                    )
                },
            )
        await self.session.execute(stmt2)

    async def _arrived(
        self, ev: Mapping[str, Any], incident_id: uuid.UUID, dispatch_id: uuid.UUID, ts: datetime
    ) -> None:
        try:
            travel = float(ev["travel_s"])
            saved = float(ev.get("est_time_saved_s", 0.0))
        except (KeyError, TypeError, ValueError) as exc:
            raise BadEvent(f"bad arrived event: {exc!r}") from exc
        await self.session.execute(
            sa.update(Dispatch)
            .where(Dispatch.id == dispatch_id)
            .values(
                arrived_at=sa.func.coalesce(Dispatch.arrived_at, ts),
                actual_travel_s=sa.func.coalesce(Dispatch.actual_travel_s, travel),
                est_time_saved_s=sa.func.coalesce(Dispatch.est_time_saved_s, saved),
            )
        )
        await self._advance(incident_id, IncidentStatus.ON_SCENE, ts)
        await self.session.execute(
            sa.update(Unit)
            .where(Unit.current_dispatch_id == dispatch_id, Unit.status == UnitStatus.DISPATCHED)
            .values(status=UnitStatus.ON_SCENE, status_changed_at=ts)
        )

    async def _ended(
        self,
        kind: HistoryEventType,
        ev: Mapping[str, Any],
        incident_id: uuid.UUID,
        dispatch_id: uuid.UUID,
        ts: datetime,
    ) -> None:
        await self.session.execute(
            sa.update(Dispatch)
            .where(Dispatch.id == dispatch_id)
            .values(
                ended_at=sa.func.coalesce(Dispatch.ended_at, ts),
                end_reason=sa.func.coalesce(Dispatch.end_reason, _END_REASON[kind]),
            )
        )
        reason = str(ev["reason"]) if kind is HistoryEventType.FAILED and ev.get("reason") else None
        await self._advance(incident_id, _END_STATUS[kind], ts, failure_reason=reason)
        # free the unit only if it is still on THIS dispatch
        await self.session.execute(
            sa.update(Unit)
            .where(Unit.current_dispatch_id == dispatch_id)
            .values(status=UnitStatus.AVAILABLE, current_dispatch_id=None, status_changed_at=ts)
        )


# --------------------------------------------------------------- worker rebuild


@dataclass(frozen=True, slots=True)
class ActiveDispatch:
    dispatch_id: str
    incident_id: str
    city_id: str
    unit_id: str
    station_id: str
    service: str
    incident_type: str
    destination: LatLng
    route_points: list[list[float]]
    segment_speeds_mps: list[float]
    corridor: list[dict[str, Any]]
    predicted_eta_s: float
    dispatched_at: datetime
    arrived_at: datetime | None
    actual_travel_s: float | None
    last_along_m: float | None
    last_ts: datetime | None
    last_elapsed_s: float | None  # simulated seconds at the last persisted position


async def active_dispatches(session: AsyncSession, city_ids: Sequence[str]) -> list[ActiveDispatch]:
    """Dispatches the worker should be simulating (ended_at IS NULL)."""
    last = (
        sa.select(VehiclePosition.along_m, VehiclePosition.ts, VehiclePosition.elapsed_s)
        .where(VehiclePosition.dispatch_id == Dispatch.id)
        .order_by(VehiclePosition.ts.desc())
        .limit(1)
        .lateral("last")
    )
    rows = await session.execute(
        sa.select(
            Dispatch.id,
            Dispatch.incident_id,
            Dispatch.city_id,
            Dispatch.unit_id,
            Dispatch.station_id,
            Dispatch.service,
            Incident.type.label("incident_type"),
            lat_of(Incident.geom).label("lat"),
            lng_of(Incident.geom).label("lng"),
            route_points_sql(Dispatch.route).label("route"),
            Dispatch.segment_speeds_mps,
            Dispatch.corridor,
            Dispatch.predicted_eta_s,
            Dispatch.dispatched_at,
            Dispatch.arrived_at,
            Dispatch.actual_travel_s,
            last.c.along_m,
            last.c.ts,
            last.c.elapsed_s,
        )
        .join(Incident, Incident.id == Dispatch.incident_id)
        .outerjoin(last, sa.true())
        .where(Dispatch.ended_at.is_(None), Dispatch.city_id.in_(list(city_ids)))
        .order_by(Dispatch.dispatched_at)
    )
    return [
        ActiveDispatch(
            dispatch_id=str(r.id),
            incident_id=str(r.incident_id),
            city_id=r.city_id,
            unit_id=r.unit_id,
            station_id=r.station_id,
            service=str(getattr(r.service, "value", r.service)),
            incident_type=str(getattr(r.incident_type, "value", r.incident_type)),
            destination=LatLng(r.lat, r.lng),
            route_points=geojson_to_latlng(r.route),
            segment_speeds_mps=list(r.segment_speeds_mps or []),
            corridor=list(r.corridor or []),
            predicted_eta_s=float(r.predicted_eta_s),
            dispatched_at=r.dispatched_at,
            arrived_at=r.arrived_at,
            actual_travel_s=r.actual_travel_s,
            last_along_m=r.along_m,
            last_ts=r.ts,
            last_elapsed_s=r.elapsed_s,
        )
        for r in rows
    ]


async def ended_dispatch_ids(session: AsyncSession, dispatch_ids: Sequence[str]) -> set[str]:
    if not dispatch_ids:
        return set()
    ids = [uuid.UUID(d) for d in dispatch_ids]
    rows = await session.execute(
        sa.select(Dispatch.id).where(Dispatch.id.in_(ids), Dispatch.ended_at.is_not(None))
    )
    return {str(r.id) for r in rows}


# -------------------------------------------------------------------- retention


async def prune_positions(session: AsyncSession, cutoff: datetime, batch: int = 10_000) -> int:
    """Delete one batch of positions older than ``cutoff`` (call until it returns 0)."""
    victims = (
        sa.select(VehiclePosition.dispatch_id, VehiclePosition.ts)
        .where(VehiclePosition.ts < cutoff)
        .limit(batch)
        .subquery()
    )
    res = await session.execute(
        sa.delete(VehiclePosition).where(
            sa.tuple_(VehiclePosition.dispatch_id, VehiclePosition.ts).in_(
                sa.select(victims.c.dispatch_id, victims.c.ts)
            )
        )
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


async def prune_outbox(session: AsyncSession, cutoff: datetime, batch: int = 10_000) -> int:
    victims = (
        sa.select(Outbox.id)
        .where(Outbox.sent_at.is_not(None), Outbox.sent_at < cutoff)
        .limit(batch)
        .scalar_subquery()
    )
    res = await session.execute(sa.delete(Outbox).where(Outbox.id.in_(victims)))
    return int(res.rowcount)  # type: ignore[attr-defined]
