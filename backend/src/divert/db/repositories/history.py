"""History writes (persister), worker rebuild reads and retention.

Every write is idempotent so a redelivered stream message is harmless:

* incident_events / signal_preemptions are keyed by the event's deterministic id
  (``ON CONFLICT (event_id) DO NOTHING`` / ``DO UPDATE``); an event's other effects run
  only when its timeline row was inserted (a duplicate changes nothing);
* vehicle_positions is keyed by (dispatch_id, ts); dispatch_legs by (dispatch_id, seq),
  their timestamps are ``COALESCE``-d (first write wins);
* incident, unit and dispatch statuses only move forward and never leave a terminal
  status (except ``cancel_rejected``, which restores what an API cancel overwrote);
* a unit is changed only while it still points at the dispatch of the event, and freed
  only by ``back_in_service`` / ``failed`` (design §1: freed when the RETURN leg ends).
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

from divert.db.geo_sql import lat_of, linestring_wkt, lng_of, point_wkt
from divert.db.models import (
    Dispatch,
    DispatchLeg,
    Incident,
    IncidentEvent,
    Outbox,
    SignalPreemption,
    Station,
    Unit,
    VehiclePosition,
)
from divert.db.repositories.legs import LegRepository, LegRow
from divert.domain.enums import (
    NON_TERMINAL_INCIDENT_STATUSES,
    DispatchStatus,
    IncidentStatus,
    Lane,
    LegEndReason,
    LegKind,
    Movement,
    PhaseStateAtClaim,
    PreemptOutcome,
    SignalState,
    UnitStatus,
    dispatch_statuses_before,
)
from divert.domain.geo import LatLng
from divert.store.events import HistoryEventType

_BEFORE_SCENE: Final = (IncidentStatus.OPEN, IncidentStatus.DISPATCHED, IncidentStatus.EN_ROUTE)
_FORWARD_FROM: Final[dict[IncidentStatus, tuple[IncidentStatus, ...]]] = {
    IncidentStatus.EN_ROUTE: (IncidentStatus.OPEN, IncidentStatus.DISPATCHED),
    IncidentStatus.ON_SCENE: _BEFORE_SCENE,
    IncidentStatus.TRANSPORTING: (*_BEFORE_SCENE, IncidentStatus.ON_SCENE),
    IncidentStatus.AT_HOSPITAL: (
        *_BEFORE_SCENE,
        IncidentStatus.ON_SCENE,
        IncidentStatus.TRANSPORTING,
    ),
    IncidentStatus.RESOLVED: tuple(sorted(NON_TERMINAL_INCIDENT_STATUSES)),
    IncidentStatus.CANCELLED: tuple(sorted(NON_TERMINAL_INCIDENT_STATUSES)),
    IncidentStatus.FAILED: tuple(sorted(NON_TERMINAL_INCIDENT_STATUSES)),
}
_TS_COLUMN: Final = {
    IncidentStatus.EN_ROUTE: "en_route_at",
    IncidentStatus.ON_SCENE: "on_scene_at",
    IncidentStatus.TRANSPORTING: "transport_started_at",
    IncidentStatus.AT_HOSPITAL: "at_hospital_at",
    IncidentStatus.RESOLVED: "resolved_at",
    IncidentStatus.CANCELLED: "cancelled_at",
    IncidentStatus.FAILED: "failed_at",
}
_UNIT_FROM: Final[dict[UnitStatus, tuple[UnitStatus, ...]]] = {
    UnitStatus.ON_SCENE: (UnitStatus.DISPATCHED,),
    UnitStatus.TRANSPORTING: (UnitStatus.DISPATCHED, UnitStatus.ON_SCENE),
    UnitStatus.AT_HOSPITAL: (UnitStatus.DISPATCHED, UnitStatus.ON_SCENE, UnitStatus.TRANSPORTING),
    UnitStatus.RETURNING: (
        UnitStatus.DISPATCHED,
        UnitStatus.ON_SCENE,
        UnitStatus.TRANSPORTING,
        UnitStatus.AT_HOSPITAL,
    ),
}
_ENVELOPE: Final = frozenset({"type", "v", "event_id", "ts", "city", "incident_id", "dispatch_id"})
_PREEMPTION_EVENTS: Final = frozenset(
    {
        HistoryEventType.PREEMPTION_CLAIMED,
        HistoryEventType.PREEMPTION_CONFLICT,
        HistoryEventType.PREEMPTION_RELEASED,
        HistoryEventType.PREEMPT_GREEN,
    }
)
TIMELINE_ONLY: Final = frozenset(
    {
        HistoryEventType.PREEMPT_TRANSITION,
        HistoryEventType.PREEMPT_TIMEOUT,
        HistoryEventType.LEG_PLAN_FAILED,
    }
)
_RESTORABLE: Final = frozenset(
    {IncidentStatus.TRANSPORTING, IncidentStatus.AT_HOSPITAL, IncidentStatus.RESOLVED}
)


def _conflicts(kind: HistoryEventType, ev: Mapping[str, Any]) -> int:
    """Times the claim was queued so far (events written before stage 5 fix-2 carry no
    count: a conflict event means at least one)."""
    floor = 1 if kind is HistoryEventType.PREEMPTION_CONFLICT else 0
    return max(floor, int(ev.get("conflicts") or 0))


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


def _opt_ts(value: Any) -> datetime | None:
    return None if value is None else _ts(value)


def _uuid(value: Any, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise BadEvent(f"bad {what} {value!r}") from exc


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _opt_enum[E](cls: type[E], value: Any) -> E | None:
    return None if value is None else cls(value)  # type: ignore[call-arg]


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
        try:
            await self._effects(kind, ev, incident_id, dispatch_id, ts)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, BadEvent):
                raise
            raise BadEvent(f"bad {kind.value} event: {exc!r}") from exc
        return True

    async def _effects(  # noqa: PLR0912 - one branch per event type
        self,
        kind: HistoryEventType,
        ev: Mapping[str, Any],
        incident_id: uuid.UUID,
        dispatch_id: uuid.UUID,
        ts: datetime,
    ) -> None:
        if kind in TIMELINE_ONLY:
            return
        if kind in _PREEMPTION_EVENTS:
            await self._preemption(kind, ev, dispatch_id, ts)
        elif kind is HistoryEventType.EN_ROUTE:
            await self._advance(incident_id, IncidentStatus.EN_ROUTE, ts)
        elif kind is HistoryEventType.ARRIVED:
            await self._arrived(ev, incident_id, dispatch_id, ts)
        elif kind is HistoryEventType.CLEARED or kind is HistoryEventType.HANDOVER_COMPLETE:
            await self._advance(incident_id, IncidentStatus.RESOLVED, ts)
        elif kind is HistoryEventType.CANCELLED:
            await self._advance(incident_id, IncidentStatus.CANCELLED, ts)
        elif kind is HistoryEventType.FAILED:
            await self._failed(ev, incident_id, dispatch_id, ts)
        elif kind is HistoryEventType.LEG_PLANNED:
            pass  # the planner wrote the row with its outbox command; timeline only
        elif kind is HistoryEventType.LEG_STARTED:
            await self._leg_started(ev, dispatch_id, ts)
        elif kind is HistoryEventType.LEG_ENDED:
            await self._leg_ended(ev, dispatch_id, ts)
        elif kind is HistoryEventType.REROUTED:
            await self._rerouted(ev, dispatch_id)
        elif kind is HistoryEventType.TRANSPORT_STARTED:
            await self._transport_started(ev, incident_id, dispatch_id, ts)
        elif kind is HistoryEventType.ARRIVED_HOSPITAL:
            await self._advance(incident_id, IncidentStatus.AT_HOSPITAL, ts)
            await self._unit(dispatch_id, UnitStatus.AT_HOSPITAL, ts)
            await self._dispatch_status(dispatch_id, DispatchStatus.AT_HOSPITAL)
        elif kind is HistoryEventType.RETURNING:
            await self._unit(dispatch_id, UnitStatus.RETURNING, ts)
            await self._dispatch_status(dispatch_id, DispatchStatus.RETURNING)
        elif kind is HistoryEventType.BACK_IN_SERVICE:
            await self._back_in_service(ev, dispatch_id, ts)
        elif kind is HistoryEventType.CANCEL_REJECTED:
            await self._cancel_rejected(ev, incident_id, ts)
        else:  # pragma: no cover - every type is handled above
            raise BadEvent(f"no handler for {kind.value}")

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
            leg_seq = int(ev.get("leg_seq", 0))
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
                leg_seq=leg_seq,
            )
            .on_conflict_do_nothing(index_elements=["dispatch_id", "ts"])
            .returning(VehiclePosition.ts)
        )
        return res is not None

    # ------------------------------------------------------------------ statuses
    async def _advance(
        self,
        incident_id: uuid.UUID,
        status: IncidentStatus,
        ts: datetime,
        *,
        failure_reason: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> bool:
        col = getattr(Incident, _TS_COLUMN[status])
        values: dict[str, Any] = {"status": status, _TS_COLUMN[status]: sa.func.coalesce(col, ts)}
        if failure_reason is not None:
            values["failure_reason"] = failure_reason[:2000]
        values.update(extra or {})
        res = await self.session.execute(
            sa.update(Incident)
            .where(Incident.id == incident_id, Incident.status.in_(_FORWARD_FROM[status]))
            .values(**values)
        )
        return bool(res.rowcount)  # type: ignore[attr-defined]

    async def _unit(self, dispatch_id: uuid.UUID, status: UnitStatus, ts: datetime) -> None:
        """Move the unit forward, only while it is still on this dispatch."""
        await self.session.execute(
            sa.update(Unit)
            .where(Unit.current_dispatch_id == dispatch_id, Unit.status.in_(_UNIT_FROM[status]))
            .values(status=status, status_changed_at=ts)
        )

    async def _free_unit(self, dispatch_id: uuid.UUID, ts: datetime) -> None:
        await self.session.execute(
            sa.update(Unit)
            .where(Unit.current_dispatch_id == dispatch_id)
            .values(status=UnitStatus.AVAILABLE, current_dispatch_id=None, status_changed_at=ts)
        )

    async def _dispatch_status(self, dispatch_id: uuid.UUID, status: DispatchStatus) -> None:
        await self.session.execute(
            sa.update(Dispatch)
            .where(
                Dispatch.id == dispatch_id, Dispatch.status.in_(dispatch_statuses_before(status))
            )
            .values(status=status)
        )

    # ------------------------------------------------------------------ handlers
    async def _preemption(
        self, kind: HistoryEventType, ev: Mapping[str, Any], dispatch_id: uuid.UUID, ts: datetime
    ) -> None:
        released = kind is HistoryEventType.PREEMPTION_RELEASED
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
                "lane": _opt_enum(Lane, ev.get("lane")),
                "approach_bearing": ev.get("approach_bearing"),
                "eta_at_claim_s": ev.get("eta_at_claim_s"),
                "claimed_at": _ts(ev.get("claimed_at", ev.get("ts"))),
                "released_at": ts if released else None,
                "leg_seq": int(ev.get("leg_seq", 0)),
                "movement": _opt_enum(Movement, ev.get("movement")),
                "lane_index": ev.get("lane_index"),
                "lanes_total": ev.get("lanes_total"),
                "free_left": bool(ev.get("free_left", False)),
                "required_lead_s": _opt_float(ev.get("required_lead_s")),
                "transition_s": _opt_float(ev.get("transition_s")),
                "queue_clear_s": _opt_float(ev.get("queue_clear_s")),
                "phase_state_at_claim": _opt_enum(
                    PhaseStateAtClaim, ev.get("phase_state_at_claim")
                ),
                "green_at": _opt_ts(ev.get("green_at")),
                "crossed_at": _opt_ts(ev.get("crossed_at")) if released else None,
                "waited_s": _opt_float(ev.get("waited_s")) if released else None,
                "outcome": _opt_enum(PreemptOutcome, ev.get("outcome")) if released else None,
                "conflicts": _conflicts(kind, ev),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise BadEvent(f"bad preemption event: {exc!r}") from exc
        if row["state"] is SignalState.NORMAL:
            raise BadEvent("a claim is never NORMAL")
        stmt = pg_insert(SignalPreemption).values(**row)
        x = stmt.excluded
        sp = SignalPreemption
        contested = sa.func.greatest(sp.conflicts, x.conflicts)  # the count only grows
        if kind is HistoryEventType.PREEMPTION_CLAIMED:
            stmt2 = stmt.on_conflict_do_nothing(index_elements=["event_id"])
        elif kind is HistoryEventType.PREEMPTION_CONFLICT:
            stmt2 = stmt.on_conflict_do_update(
                index_elements=["event_id"],
                set_={"state": SignalState.CONFLICT, "conflicts": contested},
            )
        elif kind is HistoryEventType.PREEMPT_GREEN:  # green for the claim: when, and how
            stmt2 = stmt.on_conflict_do_update(
                index_elements=["event_id"],
                set_={
                    "state": SignalState.PREEMPTED,  # served - "conflicts" keeps the queueing
                    "conflicts": contested,
                    "green_at": sa.func.coalesce(sp.green_at, x.green_at),
                    "phase_state_at_claim": sa.func.coalesce(
                        sp.phase_state_at_claim, x.phase_state_at_claim
                    ),
                },
            )
        else:  # released: keep the earliest release; fill what the claim row lacks
            stmt2 = stmt.on_conflict_do_update(
                index_elements=["event_id"],
                set_={
                    "state": x.state,  # the claim's final state (queued or served)
                    "conflicts": contested,
                    "released_at": sa.func.coalesce(sp.released_at, x.released_at),
                    "outcome": sa.func.coalesce(sp.outcome, x.outcome),
                    "crossed_at": sa.func.coalesce(sp.crossed_at, x.crossed_at),
                    "waited_s": sa.func.coalesce(sp.waited_s, x.waited_s),
                    "green_at": sa.func.coalesce(sp.green_at, x.green_at),
                    "phase_state_at_claim": sa.func.coalesce(
                        sp.phase_state_at_claim, x.phase_state_at_claim
                    ),
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
        await self._unit(dispatch_id, UnitStatus.ON_SCENE, ts)
        await self._dispatch_status(dispatch_id, DispatchStatus.ON_SCENE)

    async def _transport_started(
        self, ev: Mapping[str, Any], incident_id: uuid.UUID, dispatch_id: uuid.UUID, ts: datetime
    ) -> None:
        hospital = ev.get("hospital_station_id")
        known = hospital is not None and bool(
            await self.session.scalar(sa.select(sa.exists().where(Station.id == str(hospital))))
        )
        await self._advance(
            incident_id,
            IncidentStatus.TRANSPORTING,
            ts,
            extra={"hospital_station_id": str(hospital)} if known else None,
        )
        await self._unit(dispatch_id, UnitStatus.TRANSPORTING, ts)
        await self._dispatch_status(dispatch_id, DispatchStatus.TRANSPORTING)

    async def _failed(
        self, ev: Mapping[str, Any], incident_id: uuid.UUID, dispatch_id: uuid.UUID, ts: datetime
    ) -> None:
        await self.session.execute(
            sa.update(Dispatch)
            .where(Dispatch.id == dispatch_id)
            .values(
                ended_at=sa.func.coalesce(Dispatch.ended_at, ts),
                end_reason=sa.func.coalesce(Dispatch.end_reason, "FAILED"),
            )
        )
        await self._dispatch_status(dispatch_id, DispatchStatus.FAILED)
        reason = str(ev["reason"]) if ev.get("reason") else None
        await self._advance(incident_id, IncidentStatus.FAILED, ts, failure_reason=reason)
        await self._free_unit(dispatch_id, ts)

    async def _back_in_service(
        self, ev: Mapping[str, Any], dispatch_id: uuid.UUID, ts: datetime
    ) -> None:
        end_reason = str(ev.get("end_reason") or "CLEARED")
        if end_reason not in ("CLEARED", "CANCELLED"):
            raise BadEvent(f"bad back_in_service end_reason {end_reason!r}")
        await self.session.execute(
            sa.update(Dispatch)
            .where(Dispatch.id == dispatch_id)
            .values(
                ended_at=sa.func.coalesce(Dispatch.ended_at, ts),
                end_reason=sa.func.coalesce(Dispatch.end_reason, end_reason),
            )
        )
        await self._dispatch_status(dispatch_id, DispatchStatus.COMPLETED)
        await self._free_unit(dispatch_id, ts)

    async def _cancel_rejected(
        self, ev: Mapping[str, Any], incident_id: uuid.UUID, ts: datetime
    ) -> None:
        """The API set CANCELLED but the worker could not cancel: restore its status."""
        status = IncidentStatus(str(ev["status"]))
        if status not in _RESTORABLE:
            raise BadEvent(f"cannot restore incident status {status.value}")
        col = getattr(Incident, _TS_COLUMN[status])
        await self.session.execute(
            sa.update(Incident)
            .where(Incident.id == incident_id, Incident.status == IncidentStatus.CANCELLED)
            .values(
                status=status, cancelled_at=None, **{_TS_COLUMN[status]: sa.func.coalesce(col, ts)}
            )
        )

    # ---------------------------------------------------------------------- legs
    async def _leg_started(
        self, ev: Mapping[str, Any], dispatch_id: uuid.UUID, ts: datetime
    ) -> None:
        seq = int(ev["seq"])
        values: dict[str, Any] = {
            "started_at": sa.func.coalesce(DispatchLeg.started_at, ts),
            "started_trip_s": sa.func.coalesce(
                DispatchLeg.started_trip_s, _opt_float(ev.get("trip_s"))
            ),
        }
        if seq > 0 and ev.get("predicted_eta_s") is not None:
            # the prediction of record is made at departure (design §2 ETA, stage B)
            values["predicted_eta_s"] = float(ev["predicted_eta_s"])
            values["eta_model"] = ev.get("eta_model")
        await self.session.execute(
            sa.update(DispatchLeg)
            .where(
                DispatchLeg.dispatch_id == dispatch_id,
                DispatchLeg.seq == seq,
                DispatchLeg.started_at.is_(None),
            )
            .values(**values)
        )

    async def _leg_ended(self, ev: Mapping[str, Any], dispatch_id: uuid.UUID, ts: datetime) -> None:
        seq = int(ev["seq"])
        reason = LegEndReason(str(ev["end_reason"]))
        travel = _opt_float(ev.get("travel_s")) if ev.get("started", True) else None
        await self.session.execute(
            sa.update(DispatchLeg)
            .where(
                DispatchLeg.dispatch_id == dispatch_id,
                DispatchLeg.seq == seq,
                DispatchLeg.ended_at.is_(None),
            )
            .values(
                ended_at=ts,
                end_reason=reason,
                actual_travel_s=sa.func.coalesce(DispatchLeg.actual_travel_s, travel),
                ended_trip_s=_opt_float(ev.get("trip_s")),
                reroutes=sa.func.greatest(DispatchLeg.reroutes, int(ev.get("reroutes", 0))),
            )
        )

    async def _rerouted(self, ev: Mapping[str, Any], dispatch_id: uuid.UUID) -> None:
        """The leg now follows the spliced route (the vehicle's route since the re-route)."""
        seq, no = int(ev["seq"]), int(ev["reroute_no"])
        route = ev["route"]
        points = [LatLng(float(a), float(b)) for a, b in route["points"]]
        await self.session.execute(
            sa.update(DispatchLeg)
            .where(
                DispatchLeg.dispatch_id == dispatch_id,
                DispatchLeg.seq == seq,
                DispatchLeg.reroutes < no,
            )
            .values(
                route=linestring_wkt(points),
                segment_speeds_mps=[float(s) for s in route["segment_speeds_mps"]],
                distance_m=float(route.get("distance_m", ev.get("distance_m", 0.0))),
                corridor=list(ev.get("corridor") or []),
                reroutes=no,
            )
        )


# --------------------------------------------------------------- worker rebuild


@dataclass(frozen=True, slots=True)
class ActiveDispatch:
    """An open dispatch as PostgreSQL knows it (the worker rebuilds a vehicle from it when
    Redis lost the live state): its legs and the last persisted position."""

    dispatch_id: str
    incident_id: str
    city_id: str
    unit_id: str
    station_id: str
    service: str
    incident_type: str
    incident_status: str
    status: DispatchStatus
    scene: LatLng
    home: LatLng
    predicted_eta_s: float
    dispatched_at: datetime
    arrived_at: datetime | None
    actual_travel_s: float | None
    legs: tuple[LegRow, ...]
    last_leg_seq: int | None
    last_along_m: float | None
    last_ts: datetime | None
    last_elapsed_s: float | None  # simulated seconds into that leg at the last position
    last_pos: LatLng | None

    @property
    def current_leg(self) -> LegRow | None:
        """The last leg that started (RESPONSE until another one starts)."""
        started = [leg for leg in self.legs if leg.started_at is not None]
        return max(started, key=lambda leg: leg.seq) if started else None

    @property
    def home_road(self) -> LatLng | None:
        """The station's road point: where the RESPONSE route starts (OSRM's snap of the
        station; legs ending there are home even when the OSM point is off the road)."""
        response = [leg for leg in self.legs if leg.kind is LegKind.RESPONSE and leg.route_points]
        if not response:
            return None
        lat, lng = min(response, key=lambda leg: leg.seq).route_points[0]
        return LatLng(float(lat), float(lng))


async def active_dispatches(session: AsyncSession, city_ids: Sequence[str]) -> list[ActiveDispatch]:
    """Dispatches the worker should be simulating (ended_at IS NULL)."""
    last = (
        sa.select(
            VehiclePosition.along_m,
            VehiclePosition.ts,
            VehiclePosition.elapsed_s,
            VehiclePosition.leg_seq,
            lat_of(VehiclePosition.geom).label("p_lat"),
            lng_of(VehiclePosition.geom).label("p_lng"),
        )
        .where(VehiclePosition.dispatch_id == Dispatch.id)
        .order_by(VehiclePosition.ts.desc())
        .limit(1)
        .lateral("last")
    )
    rows = (
        await session.execute(
            sa.select(
                Dispatch.id,
                Dispatch.incident_id,
                Dispatch.city_id,
                Dispatch.unit_id,
                Dispatch.station_id,
                Dispatch.service,
                Dispatch.status,
                Incident.type.label("incident_type"),
                Incident.status.label("incident_status"),
                lat_of(Incident.geom).label("lat"),
                lng_of(Incident.geom).label("lng"),
                lat_of(Station.geom).label("st_lat"),
                lng_of(Station.geom).label("st_lng"),
                Dispatch.predicted_eta_s,
                Dispatch.dispatched_at,
                Dispatch.arrived_at,
                Dispatch.actual_travel_s,
                last.c.along_m,
                last.c.ts,
                last.c.elapsed_s,
                last.c.leg_seq,
                last.c.p_lat,
                last.c.p_lng,
            )
            .join(Incident, Incident.id == Dispatch.incident_id)
            .join(Station, Station.id == Dispatch.station_id)
            .outerjoin(last, sa.true())
            .where(Dispatch.ended_at.is_(None), Dispatch.city_id.in_(list(city_ids)))
            .order_by(Dispatch.dispatched_at)
        )
    ).all()
    legs = await LegRepository(session).legs_of([r.id for r in rows])
    by_dispatch: dict[str, list[LegRow]] = {}
    for leg in legs:
        by_dispatch.setdefault(leg.dispatch_id, []).append(leg)
    return [
        ActiveDispatch(
            dispatch_id=str(r.id),
            incident_id=str(r.incident_id),
            city_id=r.city_id,
            unit_id=r.unit_id,
            station_id=r.station_id,
            service=str(getattr(r.service, "value", r.service)),
            incident_type=str(getattr(r.incident_type, "value", r.incident_type)),
            incident_status=str(getattr(r.incident_status, "value", r.incident_status)),
            status=DispatchStatus(r.status),
            scene=LatLng(r.lat, r.lng),
            home=LatLng(r.st_lat, r.st_lng),
            predicted_eta_s=float(r.predicted_eta_s),
            dispatched_at=r.dispatched_at,
            arrived_at=r.arrived_at,
            actual_travel_s=r.actual_travel_s,
            legs=tuple(by_dispatch.get(str(r.id), [])),
            last_leg_seq=r.leg_seq,
            last_along_m=r.along_m,
            last_ts=r.ts,
            last_elapsed_s=r.elapsed_s,
            last_pos=None if r.p_lat is None else LatLng(r.p_lat, r.p_lng),
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
