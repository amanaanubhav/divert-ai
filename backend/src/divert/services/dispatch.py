"""Dispatch: incident report -> nearest free unit -> route + corridor -> one PG transaction.

1. normalise the type (aliases, any case) and check the point is in the city bbox (422)
   - before anything is created (regression guarantee 1);
2. the N nearest stations of the service with AVAILABLE units (PostGIS KNN), and the
   city's active incidents (crowding zones, design §2);
3. route each station with OSRM concurrently (cached): OSRM alternatives plus via-point
   detours, candidates longer than 1.6 x the shortest dropped;
4. per candidate: corridor = signals within DIVERT_CORRIDOR_M of the route (ST_DWithin on
   GiST), every pass of it with its movement and lane advice (design §3); the ETA under the
   traffic model (class-aware congestion + incident zones); junction delays and corridor
   contention with the live emergency vehicles -> cost. Best route per station, then the
   best station + route by cost; the choice is recorded as ``route_choice`` JSON;
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
    TRANSPORT_INCIDENT_STATUSES,
    IncidentStatus,
    IncidentType,
    ServiceType,
    normalize_incident_type,
)
from divert.domain.geo import LatLng
from divert.domain.models import City, IncidentZone, StationCandidate
from divert.domain.route import CorridorSignal, Route
from divert.metrics import DISPATCH_LATENCY, DISPATCHES, ROUTE_CANDIDATES
from divert.services.corridor import build_corridor
from divert.services.eta import EtaModel
from divert.services.outbox import OutboxRelay
from divert.services.reference import ReferenceCache
from divert.services.route_choice import Candidate, Choice, choose, score_route, within_length
from divert.services.routing import RoutingError
from divert.services.traffic import ModelledTraffic
from divert.store import redis_store as keys
from divert.store.events import (
    OUTBOX_TOPIC_COMMANDS,
    CommandType,
    StreamMessageType,
    message,
)
from divert.store.guard import RedisGuard
from divert.store.live import read_sim_clock, read_vehicles
from divert.store.streams import dumps

log = logging.getLogger(__name__)

__all__ = ["DispatchService", "Router", "build_corridor"]


class Router(Protocol):
    async def route(self, origin: LatLng, destination: LatLng) -> Route: ...

    async def alternatives(self, origin: LatLng, destination: LatLng) -> list[Route]:
        """Route candidates (OSRM alternatives + detours); never empty."""
        ...


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


class CannotCancelTransport(DispatchError):
    """A patient is on board (TRANSPORTING) or being handed over (AT_HOSPITAL)."""

    status_code = 409
    code = "cannot_cancel_transport"


class RoutingUnavailable(DispatchError):
    status_code = 503
    code = "routing_unavailable"


@dataclass(frozen=True, slots=True)
class Plan:
    candidate: StationCandidate
    route: Route
    corridor: tuple[CorridorSignal, ...]
    eta_s: float
    choice: Choice | None = None

    @property
    def cost_s(self) -> float:
        return self.choice.chosen.cost_s if self.choice is not None else self.eta_s

    def route_choice(self) -> dict[str, Any] | None:
        if self.choice is None:
            return None
        return {"station_id": self.candidate.station.id, **self.choice.to_json()}


def contention_feed(vehicles: Sequence[dict[str, Any]]) -> list[tuple[str, str, float]]:
    """(vehicle id, signal id, ETA s) of every live emergency vehicle's junctions ahead."""
    out: list[tuple[str, str, float]] = []
    for v in vehicles:
        if not v.get("emergency", True) or v.get("status") != "EN_ROUTE":
            continue
        for key, eta in (v.get("signal_etas") or {}).items():
            try:
                out.append((str(v.get("id")), str(key).split("#", 1)[0], float(eta)))
            except (TypeError, ValueError):
                continue
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
    """The worker's start command (also the outbox payload, so it is persisted in PG)."""
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
        station_pos=[plan.candidate.station.pos.lat, plan.candidate.station.pos.lng],
        route=plan.route.to_json(),
        corridor=[c.to_json() for c in plan.corridor],
        predicted_eta_s=round(plan.eta_s, 1),
        created_at=now.isoformat(),
        emergency=True,
        leg_kind="RESPONSE",
        route_choice=plan.route_choice(),
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

    async def _live_contention(self, city_id: str) -> list[tuple[str, str, float]]:
        """Junction ETAs of the live emergency vehicles (empty when Redis is down)."""
        try:
            live = await self.guard.call(lambda: read_vehicles(self.redis, city_id))
        except RedisError as exc:  # a scoring input only; never fail a dispatch on it
            log.warning("live vehicles unavailable", extra={"city": city_id, "error": repr(exc)})
            return []
        return contention_feed(live)

    async def _candidate(
        self,
        repo: ReferenceRepository,
        index: int,
        route: Route,
        *,
        city: City,
        service: ServiceType,
        sim_at: datetime,
        traffic: ModelledTraffic,
        others: Sequence[tuple[str, str, float]],
    ) -> Candidate:
        corridor = build_corridor(
            route,
            await repo.corridor_candidates(city.id, route.points, self.settings.corridor_m),
            within_m=self.settings.corridor_m,
            emergency=True,
        )
        return score_route(
            index,
            route,
            corridor,
            eta_model=self.eta_model,
            city=city,
            service=service,
            at=sim_at,
            traffic=traffic,
            others=others,
        )

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
            zones: list[IncidentZone] = await IncidentRepository(s).active_incident_zones(city.id)
            traffic = ModelledTraffic(zones)
            others = await self._live_contention(city.id)
            results = await asyncio.gather(
                *(self.router.alternatives(c.station.pos, location) for c in candidates),
                return_exceptions=True,
            )
            plans: list[Plan] = []
            errors: list[str] = []
            scored = 0
            for cand, res in zip(candidates, results, strict=True):
                if isinstance(res, RoutingError):
                    errors.append(f"{cand.station.id}: {res}")
                    continue
                if isinstance(res, BaseException):
                    raise res
                options = [
                    await self._candidate(
                        repo, i, res[i], city=city, service=service, sim_at=sim_at,
                        traffic=traffic, others=others,
                    )
                    for i in within_length(res)
                ]  # fmt: skip
                scored += len(options)
                choice = choose(options)
                best = choice.chosen
                plans.append(Plan(cand, best.route, best.corridor, best.eta_s, choice))
        ROUTE_CANDIDATES.observe(scored)
        if not plans:
            raise RoutingUnavailable("routing service unavailable", {"errors": errors[:5]})
        plans.sort(key=lambda p: (p.cost_s, p.candidate.distance_m, p.candidate.station.id))
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
                    route_choice=plan.route_choice(),
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
                "route_choice": plan.route_choice(),
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
            if status in TRANSPORT_INCIDENT_STATUSES:
                raise CannotCancelTransport(
                    "a patient is on board: the transport cannot be cancelled",
                    {"status": status.value},
                )
            if status in (IncidentStatus.CANCELLED, IncidentStatus.RESOLVED):
                body = await repo.get_incident(incident_id)
                assert body is not None
                return body  # idempotent: the unit is returning (or back); nothing changes
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
