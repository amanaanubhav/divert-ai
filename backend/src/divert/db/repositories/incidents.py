"""Incident / dispatch writes made by the API, and the history read models.

Writes (``create_dispatch``, ``cancel_incident``) run inside the caller's transaction;
reads return plain dicts shaped for the API (coordinates always ``[lat, lng]``).
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.ext.asyncio import AsyncSession

from divert.db.geo_sql import lat_of, linestring_wkt, lng_of, point_wkt
from divert.db.models import (
    Dispatch,
    DispatchLeg,
    Incident,
    IncidentEvent,
    Outbox,
    SignalPreemption,
    Unit,
    VehiclePosition,
)
from divert.db.models import Station as StationRow
from divert.domain.enums import (
    ACTIVE_INCIDENT_STATUSES,
    TERMINAL_INCIDENT_STATUSES,
    DestinationKind,
    DispatchStatus,
    IncidentStatus,
    IncidentType,
    LegKind,
    ServiceType,
    UnitStatus,
)
from divert.domain.geo import LatLng
from divert.domain.models import IncidentZone


def _iso(ts: datetime | None) -> str | None:
    return None if ts is None else ts.isoformat()


def _val(x: Any) -> Any:
    """An enum column's plain value (None stays None)."""
    return getattr(x, "value", x)


def route_points_sql(col: Any) -> sa.ColumnElement[str]:
    """GeoJSON of a geography LineString (9 decimals ~ 0.1 mm)."""
    return sa.func.ST_AsGeoJSON(sa.cast(col, Geometry), 9)


def geojson_to_latlng(raw: str | None) -> list[list[float]]:
    if not raw:
        return []
    coords = json.loads(raw)["coordinates"]
    return [[float(lat), float(lng)] for lng, lat in coords]


# ------------------------------------------------------------------------ cursors


def encode_cursor(reported_at: datetime, incident_id: uuid.UUID) -> str:
    raw = json.dumps([reported_at.isoformat(), str(incident_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        pad = "=" * (-len(cursor) % 4)
        ts, iid = json.loads(base64.urlsafe_b64decode(cursor + pad))
        return datetime.fromisoformat(ts), uuid.UUID(iid)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc


# ------------------------------------------------------------------------- writes


@dataclass(frozen=True, slots=True)
class NewDispatch:
    incident_id: uuid.UUID
    dispatch_id: uuid.UUID
    city_id: str
    incident_type: IncidentType
    reported_type: str
    location: LatLng
    description: str | None
    unit_id: str
    station_id: str
    service: ServiceType
    route_points: Sequence[LatLng]
    segment_speeds_mps: Sequence[float]
    distance_m: float
    predicted_eta_s: float
    eta_model: str
    corridor: list[dict[str, Any]]
    now: datetime
    command: dict[str, Any]
    route_choice: dict[str, Any] | None = None  # kept in the "dispatched" event payload


class IncidentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_dispatch(self, d: NewDispatch) -> int:
        """Insert incident + dispatch + events + outbox and mark the (locked) unit busy.
        Returns the outbox id. Must run in the transaction that locked the unit."""
        s = self.session
        await s.execute(
            sa.insert(Incident).values(
                id=d.incident_id,
                city_id=d.city_id,
                type=d.incident_type,
                reported_type=d.reported_type[:32],
                geom=point_wkt(d.location),
                description=d.description,
                status=IncidentStatus.DISPATCHED,
                reported_at=d.now,
                dispatched_at=d.now,
            )
        )
        await s.execute(
            sa.insert(Dispatch).values(
                id=d.dispatch_id,
                incident_id=d.incident_id,
                unit_id=d.unit_id,
                station_id=d.station_id,
                city_id=d.city_id,
                service=d.service,
                route=linestring_wkt(d.route_points),
                distance_m=d.distance_m,
                predicted_eta_s=d.predicted_eta_s,
                corridor=d.corridor,
                segment_speeds_mps=list(d.segment_speeds_mps),
                eta_model=d.eta_model,
                dispatched_at=d.now,
                status=DispatchStatus.DISPATCHED,
            )
        )
        # the RESPONSE leg (seq 0): the dispatch's own route (design §5)
        await s.execute(
            sa.insert(DispatchLeg).values(
                dispatch_id=d.dispatch_id,
                seq=0,
                kind=LegKind.RESPONSE,
                emergency=True,
                origin=point_wkt(d.route_points[0]),
                destination=point_wkt(d.location),
                destination_kind=DestinationKind.SCENE,
                destination_station_id=None,
                route=linestring_wkt(d.route_points),
                distance_m=d.distance_m,
                predicted_eta_s=d.predicted_eta_s,
                segment_speeds_mps=list(d.segment_speeds_mps),
                corridor=d.corridor,
                route_choice=d.route_choice,
                eta_model=d.eta_model,
                planned_at=d.now,
                started_at=d.now,
                started_trip_s=0.0,
            )
        )
        updated = await s.execute(
            sa.update(Unit)
            .where(Unit.id == d.unit_id, Unit.status == UnitStatus.AVAILABLE)
            .values(
                status=UnitStatus.DISPATCHED,
                current_dispatch_id=d.dispatch_id,
                status_changed_at=d.now,
            )
        )
        if updated.rowcount != 1:  # type: ignore[attr-defined]
            raise RuntimeError(f"unit {d.unit_id} was not AVAILABLE inside its own lock")
        await s.execute(
            sa.insert(IncidentEvent),
            [
                {
                    "event_id": f"rep:{d.incident_id.hex}",
                    "incident_id": d.incident_id,
                    "dispatch_id": None,
                    "type": "reported",
                    "payload": {
                        "reported_type": d.reported_type,
                        "type": d.incident_type.value,
                        "location": [d.location.lat, d.location.lng],
                        "description": d.description,
                    },
                    "ts": d.now,
                },
                {
                    "event_id": f"dsp:{d.dispatch_id.hex}",
                    "incident_id": d.incident_id,
                    "dispatch_id": d.dispatch_id,
                    "type": "dispatched",
                    "payload": {
                        "unit_id": d.unit_id,
                        "station_id": d.station_id,
                        "service": d.service.value,
                        "distance_m": round(d.distance_m, 1),
                        "predicted_eta_s": round(d.predicted_eta_s, 1),
                        "corridor_signals": len(d.corridor),
                        "route_choice": d.route_choice,
                    },
                    "ts": d.now,
                },
            ],
        )
        outbox_id = await s.scalar(
            sa.insert(Outbox).values(topic="commands", payload=d.command).returning(Outbox.id)
        )
        assert outbox_id is not None
        return int(outbox_id)

    async def active_incident_zones(self, city_id: str) -> list[IncidentZone]:
        """Active incidents of the city (crowding zones for route choice, design §2)."""
        rows = await self.session.execute(
            sa.select(
                Incident.id,
                Incident.type,
                lat_of(Incident.geom).label("lat"),
                lng_of(Incident.geom).label("lng"),
            ).where(
                Incident.city_id == city_id,
                Incident.status.in_(sorted(ACTIVE_INCIDENT_STATUSES)),
            )
        )
        return [IncidentZone(str(r.id), IncidentType(r.type), LatLng(r.lat, r.lng)) for r in rows]

    async def lock_incident(self, city_id: str, incident_id: uuid.UUID) -> Incident | None:
        return (
            await self.session.execute(
                sa.select(Incident)
                .where(Incident.id == incident_id, Incident.city_id == city_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def active_dispatch(self, incident_id: uuid.UUID) -> Dispatch | None:
        return (
            await self.session.execute(
                sa.select(Dispatch).where(
                    Dispatch.incident_id == incident_id, Dispatch.ended_at.is_(None)
                )
            )
        ).scalar_one_or_none()

    async def mark_cancelled(
        self,
        incident: Incident,
        dispatch: Dispatch | None,
        now: datetime,
        command: dict[str, Any] | None,
    ) -> int | None:
        previous = IncidentStatus(incident.status)
        incident.status = IncidentStatus.CANCELLED
        incident.cancelled_at = now
        self.session.add(
            IncidentEvent(
                event_id=f"cxr:{incident.id.hex}",
                incident_id=incident.id,
                dispatch_id=dispatch.id if dispatch else None,
                type="cancel_requested",
                payload={"previous_status": previous.value},
                ts=now,
            )
        )
        outbox_id: int | None = None
        if dispatch is not None and command is not None:
            outbox_id = await self.session.scalar(
                sa.insert(Outbox).values(topic="commands", payload=command).returning(Outbox.id)
            )
        await self.session.flush()
        return outbox_id

    # ------------------------------------------------------------------ reads
    @staticmethod
    def _incident_cols() -> tuple[Any, ...]:
        return (
            Incident.id,
            Incident.city_id,
            Incident.type,
            Incident.reported_type,
            Incident.status,
            Incident.description,
            lat_of(Incident.geom).label("lat"),
            lng_of(Incident.geom).label("lng"),
            Incident.reported_at,
            Incident.dispatched_at,
            Incident.en_route_at,
            Incident.on_scene_at,
            Incident.resolved_at,
            Incident.cancelled_at,
            Incident.failed_at,
            Incident.failure_reason,
            Incident.hospital_station_id,
            Incident.transport_started_at,
            Incident.at_hospital_at,
        )

    @staticmethod
    def incident_json(r: Any) -> dict[str, Any]:
        return {
            "id": str(r.id),
            "city": r.city_id,
            "type": IncidentType(r.type).value,
            "reported_type": r.reported_type,
            "status": IncidentStatus(r.status).value,
            "description": r.description,
            "location": [r.lat, r.lng],
            "reported_at": _iso(r.reported_at),
            "dispatched_at": _iso(r.dispatched_at),
            "en_route_at": _iso(r.en_route_at),
            "on_scene_at": _iso(r.on_scene_at),
            "resolved_at": _iso(r.resolved_at),
            "cancelled_at": _iso(r.cancelled_at),
            "failed_at": _iso(r.failed_at),
            "failure_reason": r.failure_reason,
            "hospital_station_id": r.hospital_station_id,
            "transport_started_at": _iso(r.transport_started_at),
            "at_hospital_at": _iso(r.at_hospital_at),
        }

    async def list_incidents(
        self,
        city_id: str,
        *,
        statuses: Sequence[IncidentStatus] = (),
        types: Sequence[IncidentType] = (),
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Newest first; keyset pagination on (reported_at, id) DESC."""
        q = sa.select(*self._incident_cols()).where(Incident.city_id == city_id)
        if statuses:
            q = q.where(Incident.status.in_(list(statuses)))
        if types:
            q = q.where(Incident.type.in_(list(types)))
        if start is not None:
            q = q.where(Incident.reported_at >= start)
        if end is not None:
            q = q.where(Incident.reported_at < end)
        if cursor:
            ts, iid = decode_cursor(cursor)
            q = q.where(sa.tuple_(Incident.reported_at, Incident.id) < sa.tuple_(ts, iid))
        rows = (
            await self.session.execute(
                q.order_by(Incident.reported_at.desc(), Incident.id.desc()).limit(limit + 1)
            )
        ).all()
        items = [self.incident_json(r) for r in rows[:limit]]
        nxt = (
            encode_cursor(rows[limit - 1].reported_at, rows[limit - 1].id)
            if len(rows) > limit
            else None
        )
        return items, nxt

    async def get_incident(self, incident_id: uuid.UUID) -> dict[str, Any] | None:
        row = (
            await self.session.execute(
                sa.select(*self._incident_cols()).where(Incident.id == incident_id)
            )
        ).first()
        return None if row is None else self.incident_json(row)

    async def incident_detail(self, incident_id: uuid.UUID) -> dict[str, Any] | None:
        incident = await self.get_incident(incident_id)
        if incident is None:
            return None
        drows = (
            await self.session.execute(
                sa.select(
                    Dispatch.id,
                    Dispatch.unit_id,
                    Dispatch.station_id,
                    StationRow.name.label("station_name"),
                    lat_of(StationRow.geom).label("st_lat"),
                    lng_of(StationRow.geom).label("st_lng"),
                    Dispatch.service,
                    Dispatch.status,
                    route_points_sql(Dispatch.route).label("route"),
                    Dispatch.distance_m,
                    Dispatch.predicted_eta_s,
                    Dispatch.actual_travel_s,
                    Dispatch.est_time_saved_s,
                    Dispatch.corridor,
                    Dispatch.eta_model,
                    Dispatch.dispatched_at,
                    Dispatch.arrived_at,
                    Dispatch.ended_at,
                    Dispatch.end_reason,
                )
                .join(StationRow, StationRow.id == Dispatch.station_id)
                .where(Dispatch.incident_id == incident_id)
                .order_by(Dispatch.dispatched_at)
            )
        ).all()
        prow = (
            await self.session.execute(
                sa.select(
                    SignalPreemption.event_id,
                    SignalPreemption.dispatch_id,
                    SignalPreemption.signal_id,
                    SignalPreemption.state,
                    SignalPreemption.lane,
                    SignalPreemption.approach_bearing,
                    SignalPreemption.eta_at_claim_s,
                    SignalPreemption.claimed_at,
                    SignalPreemption.released_at,
                    SignalPreemption.leg_seq,
                    SignalPreemption.movement,
                    SignalPreemption.lane_index,
                    SignalPreemption.lanes_total,
                    SignalPreemption.free_left,
                    SignalPreemption.required_lead_s,
                    SignalPreemption.transition_s,
                    SignalPreemption.queue_clear_s,
                    SignalPreemption.green_at,
                    SignalPreemption.crossed_at,
                    SignalPreemption.waited_s,
                    SignalPreemption.outcome,
                    SignalPreemption.phase_state_at_claim,
                    SignalPreemption.conflicts,
                )
                .join(Dispatch, Dispatch.id == SignalPreemption.dispatch_id)
                .where(Dispatch.incident_id == incident_id)
                .order_by(SignalPreemption.claimed_at, SignalPreemption.id)
            )
        ).all()
        erows = (
            await self.session.execute(
                sa.select(
                    IncidentEvent.event_id,
                    IncidentEvent.dispatch_id,
                    IncidentEvent.type,
                    IncidentEvent.payload,
                    IncidentEvent.ts,
                )
                .where(IncidentEvent.incident_id == incident_id)
                .order_by(IncidentEvent.ts, IncidentEvent.id)
            )
        ).all()
        choices = {
            str(r.dispatch_id): r.payload.get("route_choice")
            for r in erows
            if r.type == "dispatched" and r.dispatch_id is not None and r.payload
        }
        incident["dispatches"] = [
            {
                "id": str(r.id),
                "vehicle_id": r.unit_id,
                "service": ServiceType(r.service).value,
                "status": DispatchStatus(r.status).value,
                "station": {
                    "id": r.station_id,
                    "name": r.station_name,
                    "location": [r.st_lat, r.st_lng],
                },
                "route": geojson_to_latlng(r.route),
                "distance_m": r.distance_m,
                "predicted_eta_s": r.predicted_eta_s,
                "actual_travel_s": r.actual_travel_s,
                "est_time_saved_s": r.est_time_saved_s,
                "eta_model": r.eta_model,
                "corridor": r.corridor,
                "dispatched_at": _iso(r.dispatched_at),
                "arrived_at": _iso(r.arrived_at),
                "ended_at": _iso(r.ended_at),
                "end_reason": r.end_reason,
                "route_choice": choices.get(str(r.id)),
            }
            for r in drows
        ]
        incident["preemptions"] = [
            {
                "event_id": r.event_id,
                "dispatch_id": str(r.dispatch_id),
                "signal_id": r.signal_id,
                "state": r.state.value if hasattr(r.state, "value") else r.state,
                "lane": r.lane.value if hasattr(r.lane, "value") else r.lane,
                "approach_bearing": r.approach_bearing,
                "eta_at_claim_s": r.eta_at_claim_s,
                "claimed_at": _iso(r.claimed_at),
                "released_at": _iso(r.released_at),
                "leg_seq": r.leg_seq,
                "movement": _val(r.movement),
                "lane_index": r.lane_index,
                "lanes_total": r.lanes_total,
                "free_left": r.free_left,
                "required_lead_s": r.required_lead_s,
                "transition_s": r.transition_s,
                "queue_clear_s": r.queue_clear_s,
                "green_at": _iso(r.green_at),
                "crossed_at": _iso(r.crossed_at),
                "waited_s": r.waited_s,
                "outcome": _val(r.outcome),
                "phase_state_at_claim": _val(r.phase_state_at_claim),
                # state is the final one; a claim queued behind another phase stays
                # "contested" after it is served (waited_s: its wait at the stop line)
                "conflicts": r.conflicts,
                "contested": r.conflicts > 0 or _val(r.state) == "CONFLICT",
            }
            for r in prow
        ]
        incident["timeline"] = [
            {
                "event_id": r.event_id,
                "dispatch_id": None if r.dispatch_id is None else str(r.dispatch_id),
                "type": r.type,
                "ts": _iso(r.ts),
                "payload": r.payload,
            }
            for r in erows
        ]
        return incident

    async def track(self, incident_id: uuid.UUID, max_points: int) -> list[dict[str, Any]]:
        """Sampled positions of the incident's dispatches (evenly thinned to max_points)."""
        base = (
            sa.select(
                VehiclePosition.dispatch_id,
                VehiclePosition.ts,
                lat_of(VehiclePosition.geom).label("lat"),
                lng_of(VehiclePosition.geom).label("lng"),
                VehiclePosition.speed_mps,
                VehiclePosition.along_m,
                VehiclePosition.leg_seq,
                sa.func.row_number().over(order_by=VehiclePosition.ts).label("rn"),
                sa.func.count().over().label("n"),
            )
            .join(Dispatch, Dispatch.id == VehiclePosition.dispatch_id)
            .where(Dispatch.incident_id == incident_id)
            .subquery()
        )
        stride = sa.cast(
            sa.func.greatest(1, sa.func.ceil(base.c.n / float(max_points))), sa.BigInteger
        )
        rows = await self.session.execute(
            sa.select(base)
            .where(sa.or_(sa.func.mod(base.c.rn - 1, stride) == 0, base.c.rn == base.c.n))
            .order_by(base.c.ts)
        )
        return [
            {
                "dispatch_id": str(r.dispatch_id),
                "ts": _iso(r.ts),
                "pos": [r.lat, r.lng],
                "speed_mps": r.speed_mps,
                "along_m": r.along_m,
                "leg_seq": r.leg_seq,
            }
            for r in rows
        ]


def is_terminal(status: IncidentStatus) -> bool:
    return status in TERMINAL_INCIDENT_STATUSES
