"""LegPlanner (design §1): plans TRANSPORT / RETURN legs and evaluates re-routes.

Runs as asyncio tasks inside the LEADER worker; the simulation engine stays pure. The
engine puts ``plan_request`` / ``reroute_request`` / ``plan_cancel`` messages in its
tick batches (the request itself is part of the vehicle's live state, so a new leader
re-issues it); the worker hands them to :meth:`LegPlanner.submit`.

Planning a leg:

1. TRANSPORT: the 6 nearest eligible hospitals (PostGIS KNN) are routed concurrently and
   scored; :func:`divert.domain.hospitals.choose_hospital` picks one (confirmed ED
   preferred within tolerance). RETURN: the unit's home station;
2. every candidate route gets its corridor (movement + lane advice per junction pass) and
   the crowd-aware score (:mod:`divert.services.route_choice`) at the time the leg is
   expected to start - a RETURN leg with the phase-aware replay against the junction
   geometry the simulation uses (pinned here, cached in Redis by the worker);
3. ONE PostgreSQL transaction writes the ``dispatch_legs`` row and the outbox row holding
   the ``leg_planned`` command; then the command is XADDed (retried with backoff while
   Redis is down; the API outbox relay is the safety net). The leader applies it
   idempotently, keyed by (dispatch_id, seq);
4. a re-issued request whose leg already exists is answered from PostgreSQL (the stored
   command is re-sent), so a planned leg is never lost and never planned twice.

OSRM (or PostgreSQL) failure: retry with exponential backoff (2 s doubling to 60 s,
unlimited), a ``leg_plan_failed`` event and a metric per failure; the vehicle waits in
place - never a teleport, never a straight line.

Re-routing (emergency legs): route from the re-route point the engine chose, score, and
send ``route_replaced`` (outbox + XADD) only when it saves at least
DIVERT_REROUTE_MIN_SAVING_S; otherwise tell the engine the current route stays.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import sqlalchemy as sa
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.config import Settings
from divert.db.models import Outbox
from divert.db.repositories.legs import LegRepository, LegRow, NewLeg
from divert.db.repositories.reference import ReferenceRepository
from divert.db.session import transaction
from divert.domain.enums import DestinationKind, LegKind, ServiceType
from divert.domain.geo import LatLng
from divert.domain.hospitals import HospitalChoice, HospitalOption, choose_hospital
from divert.domain.models import City, StationCandidate
from divert.domain.route import CorridorSignal, Route
from divert.domain.signal_plan import JunctionGeometry
from divert.metrics import LEG_PLAN_DURATION, LEG_PLAN_FAILURES, LEGS_PLANNED, REROUTES
from divert.services.corridor import build_corridor
from divert.services.eta import EtaModel, KinematicEtaModel, PlanSignals
from divert.services.legs import (
    destination_json,
    leg_planned_command,
    route_replaced_command,
)
from divert.services.outbox import OutboxRelay
from divert.services.outbox import publish as outbox_publish
from divert.services.preemption import PreemptionRules
from divert.services.route_choice import Candidate, Choice, choose, score_route, within_length
from divert.services.routing import SAME_POINT, RoutingError
from divert.services.traffic import TrafficModel
from divert.store.events import OUTBOX_TOPIC_COMMANDS

log = logging.getLogger(__name__)

PLAN_REQUEST: Final = "plan_request"
REROUTE_REQUEST: Final = "reroute_request"
PLAN_CANCEL: Final = "plan_cancel"


class Router(Protocol):
    async def alternatives(self, origin: LatLng, destination: LatLng) -> list[Route]: ...


class EngineView(Protocol):
    """What the planner needs from the leader's simulation (in-process, event loop)."""

    @property
    def step_s(self) -> float: ...

    def sim_now(self) -> datetime: ...

    def city(self, city_id: str) -> City | None: ...

    def traffic(self, city_id: str) -> TrafficModel: ...

    def contention(self, city_id: str, exclude_vehicle: str) -> list[tuple[str, str, float]]: ...

    def geometry(self, city_id: str, c: CorridorSignal) -> JunctionGeometry: ...

    def wants(self, dispatch_id: str, seq: int) -> bool: ...

    def plan_failed(self, dispatch_id: str, seq: int, error: str) -> bool: ...

    def leg_not_needed(self, dispatch_id: str, seq: int) -> bool: ...

    def reroute_answered(self, dispatch_id: str, seq: int, reroute_no: int) -> None: ...


class PlanError(Exception):
    """The leg cannot be planned right now (retried with backoff)."""


@dataclass(frozen=True, slots=True)
class PlanRequest:
    city_id: str
    dispatch_id: str
    incident_id: str
    vehicle_id: str
    service: ServiceType
    station_id: str
    seq: int
    kind: LegKind
    origin: LatLng
    home: LatLng
    start_at: float  # sim epoch

    @property
    def key(self) -> tuple[str, int]:
        return (self.dispatch_id, self.seq)

    @classmethod
    def from_message(cls, m: dict[str, Any]) -> PlanRequest:
        return cls(
            city_id=str(m["city"]),
            dispatch_id=str(m["dispatch_id"]),
            incident_id=str(m["incident_id"]),
            vehicle_id=str(m["vehicle_id"]),
            service=ServiceType(m["service"]),
            station_id=str(m["station_id"]),
            seq=int(m["seq"]),
            kind=LegKind(m["kind"]),
            origin=LatLng(float(m["origin"][0]), float(m["origin"][1])),
            home=LatLng(float(m["home"][0]), float(m["home"][1])),
            start_at=float(m.get("start_at") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class RerouteRequest:
    city_id: str
    dispatch_id: str
    incident_id: str
    vehicle_id: str
    service: ServiceType
    seq: int
    reroute_no: int
    reason: str
    origin: LatLng
    origin_along_m: float
    destination: LatLng
    remaining_s: float
    at_t: float

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.dispatch_id, self.seq, self.reroute_no)

    @classmethod
    def from_message(cls, m: dict[str, Any]) -> RerouteRequest:
        return cls(
            city_id=str(m["city"]),
            dispatch_id=str(m["dispatch_id"]),
            incident_id=str(m["incident_id"]),
            vehicle_id=str(m["vehicle_id"]),
            service=ServiceType(m["service"]),
            seq=int(m["seq"]),
            reroute_no=int(m["reroute_no"]),
            reason=str(m.get("reason", "")),
            origin=LatLng(float(m["origin"][0]), float(m["origin"][1])),
            origin_along_m=float(m["origin_along_m"]),
            destination=LatLng(float(m["destination"][0]), float(m["destination"][1])),
            remaining_s=float(m["remaining_s"]),
            at_t=float(m.get("at_t") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class _Scored:
    choice: Choice
    destination: LatLng
    station_id: str | None
    name: str | None
    hospital: HospitalChoice | None = None


def backoff_s(attempt: int, base_s: float, max_s: float) -> float:
    """2 s, 4 s, 8 s ... capped at 60 s (attempt counts from 1)."""
    return float(min(max_s, base_s * 2 ** max(0, attempt - 1)))


def command_from_row(row: LegRow, req: PlanRequest) -> dict[str, Any]:
    """The ``leg_planned`` command of a leg already in PostgreSQL (re-issued request)."""
    route = Route.build([LatLng(a, b) for a, b in row.route_points], row.segment_speeds_mps)
    hospital = _hospital_json(row.hospital_choice)
    return leg_planned_command(
        city_id=req.city_id,
        dispatch_id=req.dispatch_id,
        incident_id=req.incident_id,
        vehicle_id=req.vehicle_id,
        seq=row.seq,
        kind=row.kind,
        route=route,
        corridor=[CorridorSignal.from_json(c) for c in row.corridor],
        destination=destination_json(
            row.destination_kind, row.destination, row.destination_station_id, row.destination_name
        ),
        predicted_eta_s=row.predicted_eta_s,
        route_choice=row.route_choice,
        hospital=hospital,
        planned_at=row.planned_at,
    )


def _hospital_json(choice: dict[str, Any] | None) -> dict[str, Any] | None:
    if not choice:
        return None
    chosen = choice.get("chosen")
    for c in choice.get("candidates", []):
        if c.get("station_id") == chosen:
            return {
                "id": chosen,
                "name": c.get("name"),
                "location": c.get("location"),
                "emergency": c.get("emergency"),
            }
    return None


class LegPlanner:
    def __init__(
        self,
        *,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,
        router: Router,
        view: EngineView,
        outbox: OutboxRelay | None = None,
        eta_factory: Callable[[], EtaModel] = KinematicEtaModel,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.router = router
        self.view = view
        self.outbox = outbox or OutboxRelay(sessionmaker, redis)
        self.eta_factory = eta_factory
        self.sleep = sleep
        self.rules = PreemptionRules.from_settings(settings)
        self._tasks: dict[tuple[Any, ...], asyncio.Task[None]] = {}

    # ---------------------------------------------------------------- intake
    def submit(self, messages: Sequence[dict[str, Any]]) -> None:
        for m in messages:
            kind = m.get("type")
            try:
                if kind == PLAN_REQUEST:
                    req = PlanRequest.from_message(m)
                    self._spawn(("plan", *req.key), self._plan_with_retries(req))
                elif kind == REROUTE_REQUEST:
                    rr = RerouteRequest.from_message(m)
                    self._spawn(("reroute", *rr.key), self._reroute(rr))
                elif kind == PLAN_CANCEL:
                    task = self._tasks.get(("plan", str(m["dispatch_id"]), int(m["seq"])))
                    if task is not None:
                        task.cancel()
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                log.error("bad planner message", extra={"type": kind, "error": repr(exc)})

    def _spawn(self, key: tuple[Any, ...], coro: Coroutine[Any, Any, None]) -> None:
        running = self._tasks.get(key)
        if running is not None and not running.done():
            coro.close()
            return
        task = asyncio.ensure_future(coro)
        self._tasks[key] = task
        task.add_done_callback(functools.partial(self._done, key))

    def _done(self, key: tuple[Any, ...], task: asyncio.Task[None]) -> None:
        if self._tasks.get(key) is task:
            del self._tasks[key]
        if not task.cancelled() and task.exception() is not None:
            log.error("planner task crashed", extra={"key": key, "error": repr(task.exception())})

    @property
    def busy(self) -> int:
        return len(self._tasks)

    async def idle(self, timeout_s: float = 30.0) -> None:
        """Wait until every planning task finished (tests)."""
        deadline = time.monotonic() + timeout_s
        while self._tasks and time.monotonic() < deadline:
            await asyncio.wait(list(self._tasks.values()), timeout=0.05)

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # -------------------------------------------------------------- planning
    async def _plan_with_retries(self, req: PlanRequest) -> None:
        attempt = 0
        started = time.perf_counter()
        while self.view.wants(req.dispatch_id, req.seq):
            try:
                await self.plan(req)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._already_home(req, exc):
                    return
                attempt += 1
                reason = "routing" if isinstance(exc, RoutingError) else type(exc).__name__
                LEG_PLAN_FAILURES.labels(city=req.city_id, kind=req.kind.value, reason=reason).inc()
                log.warning(
                    "leg planning failed; retrying",
                    extra={
                        "dispatch_id": req.dispatch_id,
                        "seq": req.seq,
                        "attempt": attempt,
                        "error": repr(exc),
                    },
                )
                self.view.plan_failed(req.dispatch_id, req.seq, f"{reason}: {exc}")
                await self.sleep(
                    backoff_s(
                        attempt,
                        self.settings.leg_plan_backoff_s,
                        self.settings.leg_plan_backoff_max_s,
                    )
                )
                continue
            LEG_PLAN_DURATION.labels(kind=req.kind.value).observe(time.perf_counter() - started)
            return

    def _already_home(self, req: PlanRequest, exc: Exception) -> bool:
        """A RETURN whose origin and station snap to the same road point has no route, and
        never will: the unit is home (B2), so tell the engine instead of retrying."""
        if req.kind is not LegKind.RETURN:
            return False
        if not isinstance(exc, RoutingError) or exc.kind != SAME_POINT:
            return False
        done = self.view.leg_not_needed(req.dispatch_id, req.seq)
        log.info(
            "RETURN not needed: the unit is at its station's road point",
            extra={"dispatch_id": req.dispatch_id, "seq": req.seq, "applied": done},
        )
        return True

    async def plan(self, req: PlanRequest) -> dict[str, Any]:
        """Plan (or re-send) one leg; returns the ``leg_planned`` command sent."""
        dispatch_id = uuid.UUID(req.dispatch_id)
        async with self.sessionmaker() as s:
            existing = await LegRepository(s).get_leg(dispatch_id, req.seq)
        if existing is not None:
            cmd = command_from_row(existing, req)
            await self._send(None, cmd, req)
            return cmd
        city = self.view.city(req.city_id)
        if city is None:
            raise PlanError(f"unknown city {req.city_id!r}")
        at = datetime.fromtimestamp(max(req.start_at, self.view.sim_now().timestamp()), UTC)
        if req.kind is LegKind.TRANSPORT:
            scored = await self._to_hospital(req, city, at)
        else:
            scored = await self._to_station(req, city, at)
        best = scored.choice.chosen
        planned_at = datetime.now(UTC)
        hospital = None
        if scored.hospital is not None:
            st = scored.hospital.chosen.station
            hospital = {
                "id": st.id,
                "name": st.name,
                "location": [st.pos.lat, st.pos.lng],
                "emergency": st.emergency,
            }
        dest_kind = (
            DestinationKind.HOSPITAL if req.kind is LegKind.TRANSPORT else DestinationKind.STATION
        )
        route_choice = {"station_id": scored.station_id, **scored.choice.to_json()}
        cmd = leg_planned_command(
            city_id=req.city_id,
            dispatch_id=req.dispatch_id,
            incident_id=req.incident_id,
            vehicle_id=req.vehicle_id,
            seq=req.seq,
            kind=req.kind,
            route=best.route,
            corridor=best.corridor,
            destination=destination_json(
                dest_kind, scored.destination, scored.station_id, scored.name
            ),
            predicted_eta_s=best.eta_s,
            route_choice=route_choice,
            hospital=hospital,
            planned_at=planned_at,
        )
        leg = NewLeg(
            dispatch_id=dispatch_id,
            seq=req.seq,
            kind=req.kind,
            origin=req.origin,
            destination=scored.destination,
            destination_kind=dest_kind,
            destination_station_id=scored.station_id,
            route_points=best.route.points,
            segment_speeds_mps=best.route.segment_speeds_mps,
            distance_m=best.route.length_m,
            predicted_eta_s=best.eta_s,
            corridor=[c.to_json() for c in best.corridor],
            route_choice=route_choice,
            hospital_choice=None if scored.hospital is None else scored.hospital.to_json(),
            eta_model=self.eta_factory().name,
            planned_at=planned_at,
        )
        async with transaction(self.sessionmaker) as s:
            repo = LegRepository(s)
            outbox_id = await repo.insert_leg_with_command(leg, cmd)
            stored = None if outbox_id is not None else await repo.get_leg(dispatch_id, req.seq)
        if stored is not None:  # another planner won the race: send what is stored
            cmd = command_from_row(stored, req)
        LEGS_PLANNED.labels(city=req.city_id, kind=req.kind.value).inc()
        await self._send(outbox_id, cmd, req)
        return cmd

    async def _send(self, outbox_id: int | None, cmd: dict[str, Any], req: PlanRequest) -> None:
        """XADD the command; while Redis is down keep retrying with backoff until it is
        sent - by us, or by the API outbox relay, which delivers the row too (the leader
        applies it once either way) - or the leader no longer waits for it."""
        attempt = 0
        while True:
            if outbox_id is not None:
                if await self.outbox.send_now(outbox_id, OUTBOX_TOPIC_COMMANDS, cmd):
                    return
            else:
                try:
                    await outbox_publish(self.redis, 0, OUTBOX_TOPIC_COMMANDS, cmd)
                    return
                except (RedisError, OSError) as exc:
                    log.warning("leg_planned re-send failed", extra={"error": repr(exc)})
            attempt += 1
            await self.sleep(
                backoff_s(
                    attempt, self.settings.leg_plan_backoff_s, self.settings.leg_plan_backoff_max_s
                )
            )
            if not self.view.wants(req.dispatch_id, req.seq) or (
                outbox_id is not None and await self._relayed(outbox_id)
            ):
                return

    async def _relayed(self, outbox_id: int) -> bool:
        """The API outbox relay already sent the row."""
        try:
            async with self.sessionmaker() as s:
                sent = await s.scalar(sa.select(Outbox.sent_at).where(Outbox.id == outbox_id))
        except Exception as exc:  # PostgreSQL blip: keep retrying the XADD
            log.warning("outbox row check failed", extra={"error": repr(exc)})
            return False
        return sent is not None

    # ------------------------------------------------------------- candidates
    async def _score_routes(
        self,
        req_city: str,
        vehicle_id: str,
        incident_id: str,
        routes: Sequence[Route],
        *,
        city: City,
        service: ServiceType,
        at: datetime,
        emergency: bool,
    ) -> list[Candidate]:
        """Corridor (PG) + crowd-aware score of every route within 1.6 x the shortest; the
        CPU-heavy scoring (a RETURN leg's replay) runs in a thread on frozen inputs."""
        keep = within_length(routes)
        corridors: list[list[CorridorSignal]] = []
        async with self.sessionmaker() as s:
            repo = ReferenceRepository(s)
            for i in keep:
                found = await repo.corridor_candidates(
                    city.id, routes[i].points, self.settings.corridor_m
                )
                corridors.append(
                    build_corridor(
                        routes[i], found, within_m=self.settings.corridor_m, emergency=emergency
                    )
                )
        traffic = self.view.traffic(req_city)
        others = self.view.contention(req_city, vehicle_id)
        geometry = {c.signal_id: self.view.geometry(req_city, c) for cor in corridors for c in cor}
        step_s = self.view.step_s
        rules = self.rules
        factory = self.eta_factory

        def work() -> list[Candidate]:
            model = factory()
            signals = PlanSignals(geometry)
            return [
                score_route(
                    i,
                    routes[i],
                    cor,
                    eta_model=model,
                    city=city,
                    service=service,
                    at=at,
                    traffic=traffic,
                    others=others,
                    emergency=emergency,
                    exempt_incident_id=incident_id,
                    signals=None if emergency else signals,
                    step_s=step_s,
                    rules=rules,
                )
                for i, cor in zip(keep, corridors, strict=True)
            ]

        return await asyncio.to_thread(work)

    async def _to_station(self, req: PlanRequest, city: City, at: datetime) -> _Scored:
        async with self.sessionmaker() as s:
            station = await ReferenceRepository(s).get_station(req.station_id)
        target = station.pos if station is not None else req.home
        routes = await self.router.alternatives(req.origin, target)
        cands = await self._score_routes(
            req.city_id, req.vehicle_id, req.incident_id, routes,
            city=city, service=req.service, at=at, emergency=False,
        )  # fmt: skip
        return _Scored(
            choose(cands),
            target,
            req.station_id,
            None if station is None else station.name,
        )

    async def _to_hospital(self, req: PlanRequest, city: City, at: datetime) -> _Scored:
        async with self.sessionmaker() as s:
            hospitals: list[StationCandidate] = await ReferenceRepository(s).nearest_hospitals(
                city.id, req.origin, self.settings.hospital_candidates
            )
        if not hospitals:
            raise PlanError("no eligible hospital in the city")
        results = await asyncio.gather(
            *(self.router.alternatives(req.origin, h.station.pos) for h in hospitals),
            return_exceptions=True,
        )
        options: list[HospitalOption] = []
        choices: dict[str, Choice] = {}
        errors: list[BaseException] = []
        for h, res in zip(hospitals, results, strict=True):
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, BaseException):
                errors.append(res)
                options.append(HospitalOption(h.station, h.distance_m, error=str(res)[:200]))
                continue
            cands = await self._score_routes(
                req.city_id, req.vehicle_id, req.incident_id, res,
                city=city, service=req.service, at=at, emergency=True,
            )  # fmt: skip
            choice = choose(cands)
            choices[h.station.id] = choice
            best = choice.chosen
            options.append(
                HospitalOption(
                    h.station,
                    h.distance_m,
                    cost_s=best.cost_s,
                    eta_s=best.eta_s,
                    distance_m=best.distance_m,
                    route_label=best.label,
                )
            )
        if not choices:
            first = errors[0] if errors else PlanError("no hospital routable")
            if isinstance(first, Exception):
                raise first
            raise PlanError(str(first))
        hc = choose_hospital(options)
        st = hc.chosen.station
        return _Scored(choices[st.id], st.pos, st.id, st.name, hc)

    # -------------------------------------------------------------- re-routes
    async def _reroute(self, rr: RerouteRequest) -> None:
        try:
            replaced = await self.reroute(rr)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            REROUTES.labels(city=rr.city_id, outcome="failed").inc()
            log.warning(
                "re-route evaluation failed",
                extra={"dispatch_id": rr.dispatch_id, "error": repr(exc)},
            )
            replaced = False
        if not replaced:
            self.view.reroute_answered(rr.dispatch_id, rr.seq, rr.reroute_no)

    async def reroute(self, rr: RerouteRequest) -> bool:
        """Route from the re-route point; send route_replaced only if it saves enough."""
        city = self.view.city(rr.city_id)
        if city is None:
            return False
        at = datetime.fromtimestamp(max(rr.at_t, self.view.sim_now().timestamp()), UTC)
        routes = await self.router.alternatives(rr.origin, rr.destination)
        cands = await self._score_routes(
            rr.city_id, rr.vehicle_id, rr.incident_id, routes,
            city=city, service=rr.service, at=at, emergency=True,
        )  # fmt: skip
        choice = choose(cands)
        best = choice.chosen
        saved = rr.remaining_s - best.eta_s
        if saved < self.settings.reroute_min_saving_s:
            REROUTES.labels(city=rr.city_id, outcome="kept").inc()
            return False
        cmd = route_replaced_command(
            city_id=rr.city_id,
            dispatch_id=rr.dispatch_id,
            vehicle_id=rr.vehicle_id,
            seq=rr.seq,
            reroute_no=rr.reroute_no,
            origin_along_m=rr.origin_along_m,
            route=best.route,
            corridor=best.corridor,
            saved_s=saved,
            old_remaining_s=rr.remaining_s,
            new_remaining_s=best.eta_s,
            reason=f"{rr.reason}: {saved / 60.0:.1f} min faster",
            route_choice=choice.to_json(),
        )
        async with transaction(self.sessionmaker) as s:
            outbox_id = await LegRepository(s).add_command(cmd)
        REROUTES.labels(city=rr.city_id, outcome="replaced").inc()
        with contextlib.suppress(Exception):  # the API outbox relay retries
            await self.outbox.send_now(outbox_id, OUTBOX_TOPIC_COMMANDS, cmd)
        return True
