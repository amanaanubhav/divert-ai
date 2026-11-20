"""Dispatch: incident report -> nearest free unit -> route + corridor -> one PG transaction.

1. normalise the type (aliases, any case) and check the point is in the city bbox (422)
   - before anything is created (regression guarantee 1);
2. the N nearest stations of the service with AVAILABLE units (PostGIS KNN);
3. route each with OSRM concurrently (cached), rank by the speed-model ETA;
4. corridor = signals within DIVERT_CORRIDOR_M of the route (ST_DWithin on GiST), then
   precise along-route distance (haversine projection) and the lane advisory;
5. ONE transaction: lock a free unit (FOR UPDATE SKIP LOCKED, next-best station if none),
   insert incident + dispatch + timeline events + the outbox row holding the worker
   command, mark the unit DISPATCHED;
6. after commit, in the background: XADD the command and stamp the outbox row (the relay
   retries on failure), then publish ``incident_updated``. The 201 never waits on Redis.

The congestion hour used for the ETA is the city-local hour of the *simulation* clock
(``divert:sim:clock``, written by the worker leader when DIVERT_SIM_TIME_SCALE != 1), so
the predicted ETA matches what the worker will simulate; wall time when the scale is 1 or
the clock is unavailable.

409 when no unit is free, 503 when OSRM fails - in both cases nothing is written.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.config import Settings
from divert.db.repositories.incidents import IncidentRepository, NewDispatch, is_terminal
from divert.db.repositories.reference import ReferenceRepository
from divert.db.session import transaction
from divert.domain.enums import (
    INCIDENT_TYPE_ALIASES,
    SERVICE_FOR_INCIDENT,
    IncidentStatus,
    IncidentType,
    ServiceType,
    normalize_incident_type,
)
from divert.domain.geo import LatLng, bearing_at_distance, project_onto_polyline
from divert.domain.models import City, CorridorCandidate, StationCandidate
from divert.domain.route import CorridorSignal, Route, lane_advisory
from divert.metrics import DISPATCH_LATENCY, DISPATCHES
from divert.services.eta import EtaModel, EtaQuery
from divert.services.outbox import OutboxRelay
from divert.services.reference import ReferenceCache
from divert.services.routing import RoutingError
from divert.store import redis_store as keys
from divert.store.events import (
    OUTBOX_TOPIC_COMMANDS,
    CommandType,
    StreamMessageType,
    message,
)
from divert.store.guard import RedisGuard
from divert.store.live import read_sim_clock
from divert.store.streams import dumps

log = logging.getLogger(__name__)

APPROACH_LOOKBACK_M = 5.0


class Router(Protocol):
    async def route(self, origin: LatLng, destination: LatLng) -> Route: ...


class DispatchError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class InvalidIncident(DispatchError):
    status_code = 422
    code = "invalid_incident"


class NotFound(DispatchError):
    status_code = 404
    code = "not_found"


class NoUnitAvailable(DispatchError):
    status_code = 409
    code = "no_unit_available"


class InvalidState(DispatchError):
    status_code = 409
    code = "invalid_state"


class RoutingUnavailable(DispatchError):
    status_code = 503
    code = "routing_unavailable"


@dataclass(frozen=True, slots=True)
class Plan:
    candidate: StationCandidate
    route: Route
    corridor: tuple[CorridorSignal, ...]
    eta_s: float


def build_corridor(route: Route, candidates: Sequence[CorridorCandidate]) -> list[CorridorSignal]:
    """Precise along-route position, lane advisory and approach bearing per signal."""
    out: list[CorridorSignal] = []
    for c in candidates:
        proj = project_onto_polyline(c.signal.pos, route.points, route.cumulative)
        bearing = bearing_at_distance(
            route.points, route.cumulative, max(0.0, proj.along_m - APPROACH_LOOKBACK_M)
        )
        out.append(
            CorridorSignal(
                signal_id=c.signal.id,
                name=c.signal.name,
                pos=c.signal.pos,
                along_m=proj.along_m,
                offset_m=proj.offset_m,
                lane=lane_advisory(proj.along_m, route.maneuvers),
                approach_bearing=bearing,
            )
        )
    out.sort(key=lambda s: (s.along_m, s.signal_id))
    return out


def start_command(
    *,
    city_id: str,
    incident_id: uuid.UUID,
    dispatch_id: uuid.UUID,
    unit_id: str,
    station_id: str,
    service: ServiceType,
    incident_type: str,
    location: LatLng,
    plan: Plan,
    now: datetime,
) -> dict[str, Any]:
    return message(
        CommandType.START_DISPATCH.value,
        command_id=f"start:{dispatch_id.hex}",
        city=city_id,
        incident_id=str(incident_id),
        dispatch_id=str(dispatch_id),
        vehicle_id=unit_id,
        station_id=station_id,
        service=service.value,
        incident_type=incident_type,
        destination=[location.lat, location.lng],
        route=plan.route.to_json(),
        corridor=[c.to_json() for c in plan.corridor],
        predicted_eta_s=round(plan.eta_s, 1),
        created_at=now.isoformat(),
    )


class DispatchService:
    def __init__(
        self,
        *,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,
        router: Router,
        eta_model: EtaModel,
        reference: ReferenceCache,
        outbox: OutboxRelay,
        guard: RedisGuard | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.router = router
        self.eta_model = eta_model
        self.reference = reference
        self.outbox = outbox
        self.guard = guard or RedisGuard()
        self._background: set[asyncio.Task[None]] = set()

    async def _city(self, city_id: str) -> City:
        city = await self.reference.city(city_id)
        if city is None:
            raise NotFound(f"unknown city {city_id!r}")
        return city

    async def _publish(self, city_id: str, msg: dict[str, Any]) -> None:
        channel, body = keys.events_channel(city_id), dumps(msg)
        try:
            await self.guard.call(lambda: self.redis.publish(channel, body))
        except RedisError as exc:  # live UI hint only; history is in PG
            log.warning("publish failed", extra={"city": city_id, "error": repr(exc)})

    async def sim_clock(self, wall: datetime) -> datetime:
        """The instant whose city-local hour drives congestion (see module doc)."""
        if self.settings.sim_time_scale == 1.0:
            return wall
        try:
            sim = await self.guard.call(lambda: read_sim_clock(self.redis))
        except RedisError as exc:
            log.warning("sim clock unavailable; using wall time", extra={"error": repr(exc)})
            return wall
        return sim or wall

    # ------------------------------------------------------- post-commit work
    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _after_commit(
        self,
        city_id: str,
        outbox_id: int | None,
        command: dict[str, Any] | None,
        update: dict[str, Any],
    ) -> None:
        """XADD the command (fast path; the outbox relay is the safety net), then PUBLISH.
        Runs in the background so the HTTP response never waits on Redis."""
        try:
            if outbox_id is not None and command is not None:
                await self.outbox.send_now(outbox_id, OUTBOX_TOPIC_COMMANDS, command)
            await self._publish(city_id, update)
        except Exception:  # never surfaces: PG already holds everything needed
            log.exception("post-commit step failed", extra={"city": city_id})

    async def drain(self, timeout_s: float = 5.0) -> None:
        """Wait for in-flight post-commit work (shutdown, tests)."""
        pending = set(self._background)
        if pending:
            await asyncio.wait(pending, timeout=timeout_s)

    # --------------------------------------------------------------- create
    async def create_incident(
        self, city_id: str, raw_type: str, location: LatLng, description: str | None = None
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            itype = normalize_incident_type(raw_type)
        except ValueError as exc:
            DISPATCHES.labels(city=city_id, service="-", outcome="invalid").inc()
            raise InvalidIncident(
                str(exc), {"field": "type", "valid_types": sorted(INCIDENT_TYPE_ALIASES)}
            ) from None
        city = await self._city(city_id)
        if not city.bbox.contains(location):
            DISPATCHES.labels(city=city_id, service="-", outcome="invalid").inc()
            b = city.bbox
            raise InvalidIncident(
                f"location {list(location)} is outside {city.name}",
                {"field": "location", "bbox": [b.south, b.west, b.north, b.east]},
            )
        service = SERVICE_FOR_INCIDENT[itype]
        try:
            result = await self._dispatch(
                city=city,
                itype=itype,
                raw_type=raw_type,
                service=service,
                location=location,
                description=description,
            )
        except DispatchError as exc:
            DISPATCHES.labels(city=city_id, service=service.value, outcome=exc.code).inc()
            raise
        DISPATCHES.labels(city=city_id, service=service.value, outcome="created").inc()
        DISPATCH_LATENCY.observe(time.perf_counter() - started)
        return result

    async def _plans(
        self, city: City, service: ServiceType, location: LatLng, sim_at: datetime
    ) -> list[Plan]:
        async with self.sessionmaker() as s:
            repo = ReferenceRepository(s)
            candidates = await repo.nearest_stations_with_free_units(
                city.id, service, location, limit=self.settings.candidate_stations
            )
            if not candidates:
                raise NoUnitAvailable(
                    f"no {service.value} unit is available in {city.name}",
                    {"service": service.value},
                )
            results = await asyncio.gather(
                *(self.router.route(c.station.pos, location) for c in candidates),
                return_exceptions=True,
            )
            plans: list[Plan] = []
            errors: list[str] = []
            for cand, res in zip(candidates, results, strict=True):
                if isinstance(res, RoutingError):
                    errors.append(f"{cand.station.id}: {res}")
                    continue
                if isinstance(res, BaseException):
                    raise res
                corridor = build_corridor(
                    res,
                    await repo.corridor_candidates(city.id, res.points, self.settings.corridor_m),
                )
                eta = self.eta_model.estimate(
                    EtaQuery(
                        route=res,
                        service=service,
                        city=city,
                        corridor=corridor,
                        along_m=0.0,
                        speed_mps=0.0,
                        at=sim_at,
                    )
                )
                plans.append(Plan(cand, res, tuple(corridor), eta.to_destination_s))
        if not plans:
            raise RoutingUnavailable("routing service unavailable", {"errors": errors[:5]})
        plans.sort(key=lambda p: (p.eta_s, p.candidate.distance_m, p.candidate.station.id))
        return plans

    async def _dispatch(
        self,
        *,
        city: City,
        itype: IncidentType,
        raw_type: str,
        service: ServiceType,
        location: LatLng,
        description: str | None,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        plans = await self._plans(city, service, location, await self.sim_clock(now))
        incident_id, dispatch_id = uuid.uuid4(), uuid.uuid4()
        async with transaction(self.sessionmaker) as s:
            ref = ReferenceRepository(s)
            chosen: tuple[Plan, str] | None = None
            for plan in plans:  # next-best station when the best one's units were taken
                unit = await ref.lock_free_unit(plan.candidate.station.id)
                if unit is not None:
                    chosen = (plan, unit.id)
                    break
            if chosen is None:
                raise NoUnitAvailable(
                    f"all nearby {service.value} units were just taken", {"service": service.value}
                )
            plan, unit_id = chosen
            command = start_command(
                city_id=city.id,
                incident_id=incident_id,
                dispatch_id=dispatch_id,
                unit_id=unit_id,
                station_id=plan.candidate.station.id,
                service=service,
                incident_type=itype.value,
                location=location,
                plan=plan,
                now=now,
            )
            outbox_id = await IncidentRepository(s).create_dispatch(
                NewDispatch(
                    incident_id=incident_id,
                    dispatch_id=dispatch_id,
                    city_id=city.id,
                    incident_type=itype,
                    reported_type=raw_type.strip(),
                    location=location,
                    description=description,
                    unit_id=unit_id,
                    station_id=plan.candidate.station.id,
                    service=service,
                    route_points=plan.route.points,
                    segment_speeds_mps=plan.route.segment_speeds_mps,
                    distance_m=plan.route.length_m,
                    predicted_eta_s=plan.eta_s,
                    eta_model=self.eta_model.name,
                    corridor=[c.to_json() for c in plan.corridor],
                    now=now,
                    command=command,
                )
            )
            incident = await IncidentRepository(s).get_incident(incident_id)
        assert incident is not None
        self._spawn(
            self._after_commit(
                city.id,
                outbox_id,
                command,
                message(
                    StreamMessageType.INCIDENT_UPDATED.value,
                    city=city.id,
                    incident_id=str(incident_id),
                    dispatch_id=str(dispatch_id),
                    vehicle_id=unit_id,
                    status=IncidentStatus.DISPATCHED.value,
                    ts=now.isoformat(),
                    incident=incident,
                ),
            )
        )
        st = plan.candidate.station
        return {
            "incident": incident,
            "vehicle": {
                "id": unit_id,
                "dispatch_id": str(dispatch_id),
                "service": service.value,
                "status": "DISPATCHED",
                "station": {
                    "id": st.id,
                    "name": st.name,
                    "location": [st.pos.lat, st.pos.lng],
                    "straight_line_m": round(plan.candidate.distance_m, 1),
                },
                "route": [[p.lat, p.lng] for p in plan.route.points],
                "distance_m": round(plan.route.length_m, 1),
                "eta_s": round(plan.eta_s, 1),
                "eta_model": self.eta_model.name,
                "corridor": [c.to_json() for c in plan.corridor],
            },
        }

    # --------------------------------------------------------------- cancel
    async def cancel_incident(self, city_id: str, incident_id: uuid.UUID) -> dict[str, Any]:
        await self._city(city_id)
        now = datetime.now(UTC)
        async with transaction(self.sessionmaker) as s:
            repo = IncidentRepository(s)
            inc = await repo.lock_incident(city_id, incident_id)
            if inc is None:
                raise NotFound(f"incident {incident_id} not found in {city_id}")
            status = IncidentStatus(inc.status)
            if status is IncidentStatus.CANCELLED:
                body = await repo.get_incident(incident_id)
                assert body is not None
                return body  # idempotent
            if is_terminal(status):
                raise InvalidState(f"incident is already {status.value}", {"status": status.value})
            dispatch = await repo.active_dispatch(incident_id)
            command = (
                message(
                    CommandType.CANCEL_DISPATCH.value,
                    command_id=f"cancel:{dispatch.id.hex}",
                    city=city_id,
                    incident_id=str(incident_id),
                    dispatch_id=str(dispatch.id),
                    vehicle_id=dispatch.unit_id,
                    reason="cancelled by operator",
                    created_at=now.isoformat(),
                )
                if dispatch is not None
                else None
            )
            outbox_id = await repo.mark_cancelled(inc, dispatch, now, command)
            body = await repo.get_incident(incident_id)
        assert body is not None
        self._spawn(
            self._after_commit(
                city_id,
                outbox_id,
                command,
                message(
                    StreamMessageType.INCIDENT_UPDATED.value,
                    city=city_id,
                    incident_id=str(incident_id),
                    status=IncidentStatus.CANCELLED.value,
                    ts=now.isoformat(),
                    incident=body,
                ),
            )
        )
        return body
