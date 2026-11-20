"""Simulation worker: ``python -m divert.worker``.

Run two or more replicas; exactly one is the *leader* (Redis lock ``SET NX PX`` renewed
every TTL/3), the others wait as hot standbys. The leader:

* is the ONLY writer of live Redis state (vehicles, claims, active signals, junction
  geometry);
* consumes ``divert:commands`` (consumer group ``worker``) - start / cancel dispatch,
  ``leg_planned`` and ``route_replaced`` (all applied idempotently);
* ticks every DIVERT_SIM_TICK_S: advances vehicles (DIVERT_SIM_TIME_SCALE), claims /
  releases corridor signals per vehicle id, and writes each city's tick - state, history
  events, command acks, pub/sub fan-out - in ONE MULTI/EXEC (see store/live.py);
* runs the :class:`~divert.services.planner.LegPlanner` as asyncio tasks: the engine's
  plan / re-route requests of each tick are handed to it; it plans TRANSPORT / RETURN legs
  (PostgreSQL + outbox) and answers through the command stream;
* never touches PostgreSQL on the tick path. On becoming leader it resumes from Redis,
  rebuilds from PostgreSQL any active dispatch Redis lost (with its legs), seeds the
  junction geometry cached in Redis and re-issues every pending plan request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from prometheus_client import start_http_server
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.config import Settings, get_settings
from divert.db.repositories.history import ActiveDispatch, active_dispatches, ended_dispatch_ids
from divert.db.repositories.legs import LegRow
from divert.db.repositories.reference import ReferenceRepository
from divert.db.session import create_engine, create_sessionmaker
from divert.domain.enums import (
    DestinationKind,
    DispatchStatus,
    IncidentStatus,
    IncidentType,
    LegEndReason,
    LegKind,
    ServiceType,
    UnitStatus,
    next_leg_kind,
)
from divert.domain.geo import LatLng
from divert.domain.models import City
from divert.domain.route import CorridorSignal, Route
from divert.domain.signal_plan import JunctionGeometry
from divert.logging import configure_logging, default_instance_id
from divert.metrics import (
    ACTIVE_SIGNALS,
    ACTIVE_VEHICLES,
    COMMANDS,
    HISTORY_EVENTS,
    PREEMPTION_CONFLICTS,
    PREEMPTION_OUTCOMES,
    PREEMPTION_PHASE_STATE,
    PREEMPTIONS,
    SIGNALS_STARVED,
    TICK_DURATION,
    TICK_OVERRUNS,
    WORKER_IS_LEADER,
)
from divert.services.eta import EtaModel, KinematicEtaModel
from divert.services.junctions import geometry_from_json
from divert.services.legs import PendingLeg, is_home
from divert.services.planner import LegPlanner, Router
from divert.services.preemption import PreemptionRules
from divert.services.routing import OsrmRouter
from divert.services.simulation import (
    CityBatch,
    Claim,
    CommandError,
    SimConfig,
    SimulationEngine,
    Vehicle,
    VehicleStatus,
)
from divert.services.traffic import TrafficModel
from divert.store import redis_store as keys
from divert.store.events import CommandType, HistoryEventType, event_id, message
from divert.store.live import (
    last_tick_no,
    merge_batches,
    read_finished,
    read_sim_clock,
    write_city_batch,
)
from divert.store.locks import LeaderLock
from divert.store.streams import StreamMessage, autoclaim, ensure_group, read_group, to_str

log = logging.getLogger("divert.worker")


def _loads(raw: Any) -> dict[str, Any]:
    data = json.loads(to_str(raw))
    return data if isinstance(data, dict) else {}


class Planner(Protocol):
    """What the worker needs of a leg planner (tests inject an in-memory one)."""

    def submit(self, messages: Sequence[dict[str, Any]]) -> None: ...

    async def close(self) -> None: ...


def sim_config(settings: Settings) -> SimConfig:
    return SimConfig(
        scene_time_s={s: settings.scene_time_for(s.value) for s in ServiceType},
        position_sample_s=settings.position_sample_s,
        rules=PreemptionRules.from_settings(settings),
        hospital_handover_s=settings.hospital_handover_s,
        step_s=settings.sim_tick_s * settings.sim_time_scale,
        reroute_min_saving_s=settings.reroute_min_saving_s,
        reroute_max_per_leg=settings.reroute_max_per_leg,
        reroute_behind_share=settings.reroute_behind_share,
        reroute_behind_s=settings.reroute_behind_s,
    )


class EngineView:
    """The planner's window on ONE leadership term's engine (in-process)."""

    def __init__(self, worker: Worker, engine: SimulationEngine) -> None:
        self.worker = worker
        self.engine = engine

    @property
    def step_s(self) -> float:
        return self.worker.settings.sim_tick_s * self.worker.settings.sim_time_scale

    def sim_now(self) -> datetime:
        return self.worker.sim_now

    def city(self, city_id: str) -> City | None:
        return self.engine.cities.get(city_id)

    def traffic(self, city_id: str) -> TrafficModel:
        return self.engine.traffic(city_id)

    def contention(self, city_id: str, exclude_vehicle: str) -> list[tuple[str, str, float]]:
        out: list[tuple[str, str, float]] = []
        for v in self.engine.vehicles.get(city_id, {}).values():
            if v.vehicle_id == exclude_vehicle or not v.emergency or not v.moving:
                continue
            out.extend((v.vehicle_id, k.split("#", 1)[0], t) for k, t in v.signal_etas.items())
        return out

    def geometry(self, city_id: str, c: CorridorSignal) -> JunctionGeometry:
        return self.engine.junctions.geometry(city_id, c)

    def wants(self, dispatch_id: str, seq: int) -> bool:
        if self.worker.engine is not self.engine:
            return False
        v = self.engine.find_dispatch(dispatch_id)
        return v is not None and v.pending is not None and v.pending.seq == seq and v.ready is None

    def plan_failed(self, dispatch_id: str, seq: int, error: str) -> bool:
        return self.engine.plan_failed(dispatch_id, seq, error, wall=self.worker.wall_clock())

    def leg_not_needed(self, dispatch_id: str, seq: int) -> bool:
        if self.worker.engine is not self.engine:
            return False
        return self.engine.leg_not_needed(dispatch_id, seq)

    def reroute_answered(self, dispatch_id: str, seq: int, reroute_no: int) -> None:
        self.engine.reroute_answered(dispatch_id, seq, reroute_no)


type PlannerFactory = Callable[[Worker, SimulationEngine], Planner]


class Worker:
    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        eta_model: EtaModel | None = None,
        instance_id: str | None = None,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        router: Router | None = None,
        planner_factory: PlannerFactory | None = None,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.sessionmaker = sessionmaker
        self.eta_model = eta_model or KinematicEtaModel()
        self.instance_id = instance_id or settings.instance_id or default_instance_id()
        self.lock = LeaderLock(
            redis, keys.LEADER_LOCK, ttl_ms=settings.leader_lock_ttl_ms, owner=self.instance_id
        )
        self.wall_clock = wall_clock
        self.router = router
        self.planner_factory = planner_factory
        self.engine: SimulationEngine | None = None
        self.planner: Planner | None = None
        self.sim_now: datetime = wall_clock()
        self.leader_since: float | None = None
        self._pending: dict[str, CityBatch] = {}
        self._last_claim_sweep = 0.0

    # ------------------------------------------------------------ leadership
    @property
    def is_leader(self) -> bool:
        return self.engine is not None and self.lock.is_held()

    async def try_lead(self) -> bool:
        """Acquire the lock and, if newly acquired, take over the simulation."""
        if not await self.lock.acquire():
            return False
        if self.engine is None:
            await self.take_over()
        return True

    def _make_planner(self, engine: SimulationEngine) -> Planner:
        if self.planner_factory is not None:
            return self.planner_factory(self, engine)
        if self.router is None:
            raise RuntimeError("the worker needs a router to plan legs")
        return LegPlanner(
            settings=self.settings,
            sessionmaker=self.sessionmaker,
            redis=self.redis,
            router=self.router,
            view=EngineView(self, engine),
        )

    async def take_over(self) -> None:
        """Become leader: load cities, resume live state from Redis, rebuild from PG."""
        cities = await self._load_cities()
        engine = SimulationEngine(cities, self.eta_model, sim_config(self.settings))
        await ensure_group(self.redis, keys.STREAM_COMMANDS, keys.GROUP_WORKER)
        self.sim_now = await self._load_sim_clock()
        engine.set_clock(self.sim_now)
        # keep the per-city tick counter monotonic across leader changes (WS contract)
        engine.tick_no = await last_tick_no(self.redis, engine.cities)
        await self._seed_geometry(engine)
        restored = await self._restore_from_redis(engine)
        rebuilt = await self._rebuild_from_pg(engine)
        self.engine = engine
        self.planner = self._make_planner(engine)
        self.leader_since = time.time()
        self._pending.clear()
        WORKER_IS_LEADER.set(1)
        reissued = engine.pending_plan_requests()
        await self._flush(engine.drain())
        self.planner.submit(reissued)
        log.info(
            "became leader",
            extra={
                "instance": self.instance_id,
                "restored": restored,
                "rebuilt": rebuilt,
                "plans_reissued": len(reissued),
                "tick": engine.tick_no,
            },
        )

    def step_down(self) -> None:
        if self.engine is not None:
            log.warning(
                "leadership lost; dropping in-memory state", extra={"instance": self.instance_id}
            )
        self.engine = None
        planner, self.planner = self.planner, None
        if planner is not None:
            with contextlib.suppress(RuntimeError):  # no running loop (interpreter exit)
                asyncio.get_running_loop().create_task(planner.close())
        self._pending.clear()
        WORKER_IS_LEADER.set(0)
        for city_id in self.settings.cities:
            ACTIVE_VEHICLES.labels(city=city_id).set(0)

    async def _load_cities(self) -> dict[str, City]:
        async with self.sessionmaker() as s:
            cities = await ReferenceRepository(s).list_cities()
        return {c.id: c for c in cities if c.id in self.settings.cities}

    async def _load_sim_clock(self) -> datetime:
        if self.settings.sim_time_scale == 1.0:
            return self.wall_clock()
        return await read_sim_clock(self.redis) or self.wall_clock()

    async def _seed_geometry(self, engine: SimulationEngine) -> None:
        """Junction geometry cached by earlier leaders (keeps approaches -> phases stable)."""
        for city_id in engine.cities:
            raw = await self.redis.hgetall(keys.junctions(city_id))
            seeded: dict[str, JunctionGeometry] = {}
            for sid, doc in raw.items():
                try:
                    seeded[to_str(sid)] = geometry_from_json(_loads(doc))
                except (ValueError, TypeError) as exc:
                    log.warning("bad cached junction geometry", extra={"error": repr(exc)})
            engine.junctions.seed(city_id, seeded)

    async def _restore_from_redis(self, engine: SimulationEngine) -> int:
        n = 0
        for city_id in engine.cities:
            engine.mark_finished(city_id, await read_finished(self.redis, city_id))
            engine.mark_known_active_signals(
                city_id, {to_str(k) for k in await self.redis.hkeys(keys.active_signals(city_id))}
            )
            raw = await self.redis.hgetall(keys.vehicles(city_id))
            lives = [_loads(v) for v in raw.values()]
            ended = await self._ended_in_pg([str(v.get("dispatch_id")) for v in lives])
            for live in lives:
                vid = str(live.get("id"))
                try:
                    if str(live.get("dispatch_id")) in ended:
                        raise ValueError("dispatch already ended in PostgreSQL")
                    route_raw = await self.redis.get(keys.vehicle_route(city_id, vid))
                    if route_raw is None:
                        raise ValueError("route missing")
                    claims: dict[str, Claim] = {}
                    for sid in live.get("claims", []):
                        c = await self.redis.hget(keys.claims(city_id, str(sid)), vid)
                        if c is not None:
                            claims[str(sid)] = Claim.from_json(_loads(c))
                    engine.restore(Vehicle.restore(live, _loads(route_raw), claims))
                    n += 1
                except (ValueError, KeyError, TypeError) as exc:
                    log.warning(
                        "dropping unrecoverable live vehicle",
                        extra={"vehicle_id": vid, "error": repr(exc)},
                    )
                    b = engine.batch(city_id)
                    b.vehicle_dels.add(vid)
                    b.route_dels.add(vid)
                    b.vclaim_dels.add(vid)
                    for sid in live.get("claims", []):
                        b.claim_dels.add((str(sid), vid))
        return n

    async def _ended_in_pg(self, dispatch_ids: list[str]) -> set[str]:
        valid = [d for d in dispatch_ids if _is_uuid(d)]
        async with self.sessionmaker() as s:
            return await ended_dispatch_ids(s, valid)

    async def _rebuild_from_pg(self, engine: SimulationEngine) -> int:
        """Active dispatches PostgreSQL knows about but Redis lost (e.g. Redis restarted
        without its AOF): resume them on their current leg (or dwell) from PostgreSQL."""
        async with self.sessionmaker() as s:
            rows = await active_dispatches(s, list(engine.cities))
        n = 0
        wall = self.wall_clock()
        for row in rows:
            if engine.find_dispatch(row.dispatch_id) or engine.is_finished(
                row.city_id, row.dispatch_id
            ):
                continue
            try:
                v = self._vehicle_from_pg(row, wall)
            except (ValueError, KeyError) as exc:
                log.error(
                    "cannot rebuild dispatch",
                    extra={"dispatch_id": row.dispatch_id, "error": repr(exc)},
                )
                engine.batch(row.city_id).history.append(
                    message(
                        HistoryEventType.FAILED.value,
                        event_id=event_id("fail", row.dispatch_id),
                        ts=wall.isoformat(),
                        city=row.city_id,
                        incident_id=row.incident_id,
                        dispatch_id=row.dispatch_id,
                        vehicle_id=row.unit_id,
                        reason=f"rebuild failed: {exc!r}"[:500],
                    )
                )
                continue
            if v is None:  # its RETURN leg ended: only back_in_service was lost
                engine.batch(row.city_id).history.append(
                    message(
                        HistoryEventType.BACK_IN_SERVICE.value,
                        event_id=event_id("bis", row.dispatch_id),
                        ts=wall.isoformat(),
                        city=row.city_id,
                        incident_id=row.incident_id,
                        dispatch_id=row.dispatch_id,
                        vehicle_id=row.unit_id,
                        end_reason="CANCELLED"
                        if row.incident_status == IncidentStatus.CANCELLED.value
                        else "CLEARED",
                    )
                )
                engine.mark_finished(row.city_id, {row.dispatch_id})
                continue
            engine.restore(v, announce_at=wall)
            n += 1
            log.warning("rebuilt vehicle from PostgreSQL", extra={"dispatch_id": row.dispatch_id})
        return n

    def _vehicle_from_pg(self, row: ActiveDispatch, wall: datetime) -> Vehicle | None:
        """Rebuild a vehicle on the leg it was driving (or at the end of its last leg)."""
        leg = row.current_leg
        if leg is None:
            raise ValueError("dispatch has no started leg")
        route = Route.build([LatLng(a, b) for a, b in leg.route_points], leg.segment_speeds_mps)
        scale = self.settings.sim_time_scale
        v = Vehicle(
            vehicle_id=row.unit_id,
            dispatch_id=row.dispatch_id,
            incident_id=row.incident_id,
            city_id=row.city_id,
            service=ServiceType(row.service),
            station_id=row.station_id,
            incident_type=row.incident_type,
            route=route,
            corridor=tuple(
                sorted(
                    (CorridorSignal.from_json(c) for c in leg.corridor),
                    key=lambda c: (c.along_m, c.signal_id, c.pass_no),
                )
            ),
            destination=leg.destination,
            predicted_eta_s=leg.predicted_eta_s,
            started_at=row.dispatched_at.isoformat(),
            eta_s=leg.predicted_eta_s,
            emergency=leg.emergency,
            leg_kind=leg.kind,
            incident_pos=row.scene,
            home=row.home,
            home_road=row.home_road,
        )
        v.leg_seq = leg.seq
        v.next_seq = max(lg.seq for lg in row.legs) + 1
        v.dest_kind = leg.destination_kind
        v.dest_station_id = leg.destination_station_id
        v.dest_name = leg.destination_name
        v.cancelled = row.incident_status == IncidentStatus.CANCELLED.value
        v.phase = UnitStatus(
            row.status.value
            if row.status in (
                DispatchStatus.DISPATCHED, DispatchStatus.ON_SCENE, DispatchStatus.TRANSPORTING,
                DispatchStatus.AT_HOSPITAL, DispatchStatus.RETURNING,
            )
            else UnitStatus.RETURNING.value
        )  # fmt: skip
        v.released = v.phase is UnitStatus.RETURNING
        v.trip_s = leg.started_trip_s or 0.0
        v.leg_started_trip_s = v.trip_s
        if leg.kind is LegKind.TRANSPORT and leg.hospital_choice:
            v.hospital = {"id": leg.destination_station_id, "name": leg.destination_name}
        here = row.last_leg_seq == leg.seq
        if leg.ended_at is None:  # still driving it
            if here and row.last_along_m is not None:
                v.along_m = min(float(row.last_along_m), route.length_m)
                # simulated elapsed time is persisted with each position sample
                v.elapsed_s = (
                    float(row.last_elapsed_s)
                    if row.last_elapsed_s is not None
                    else max(0.0, (wall - leg.started_at).total_seconds()) * scale
                    if leg.started_at is not None
                    else 0.0
                )
            rules = PreemptionRules.from_settings(self.settings)
            v.passed = {c.key for c in v.corridor if rules.is_past(v.along_m, c.along_m, c.legs)}
            v.samples = int(v.elapsed_s // self.settings.position_sample_s) + 1
            v.trip_s += v.elapsed_s
            return v
        if leg.kind is LegKind.RETURN:
            return None
        self._rebuild_stopped(v, row, leg, wall)
        return v

    def _rebuild_stopped(
        self, v: Vehicle, row: ActiveDispatch, leg: LegRow, wall: datetime
    ) -> None:
        """At the end of ``leg`` (arrived, or halted after a cancel): dwell / wait."""
        scale = self.settings.sim_time_scale
        v.status = VehicleStatus.ON_SCENE
        arrived = leg.end_reason is LegEndReason.ARRIVED
        v.along_m = (
            v.route.length_m
            if arrived or row.last_along_m is None
            else min(float(row.last_along_m), v.route.length_m)
        )
        v.travel_s = v.elapsed_s = leg.actual_travel_s or 0.0
        v.trip_s = leg.ended_trip_s if leg.ended_trip_s is not None else v.trip_s + v.elapsed_s
        since = max(0.0, (wall - leg.ended_at).total_seconds()) * scale if leg.ended_at else 0.0
        if arrived and leg.kind is LegKind.TRANSPORT and not v.released:
            v.phase = UnitStatus.AT_HOSPITAL  # (arrived_hospital may not be persisted yet)
        if arrived and not v.cancelled and not v.released:
            v.dwell_target_s = (
                self.settings.hospital_handover_s
                if leg.kind is LegKind.TRANSPORT
                else self.settings.scene_time_for(v.service.value)
            )
            # dwell is not sampled: wall time since arrival (scaled) is an upper bound
            v.dwell_s = min(v.dwell_target_s, since)
        try:
            itype: IncidentType | None = IncidentType(v.incident_type)
        except ValueError:
            itype = None
        kind = next_leg_kind(
            leg.kind, service=v.service, incident_type=itype, cancelled=v.cancelled
        )
        if kind is None:
            return
        planned = [
            lg for lg in row.legs
            if lg.seq > leg.seq and lg.kind is kind and lg.started_at is None
            and lg.end_reason is not LegEndReason.SUPERSEDED
        ]  # fmt: skip
        if planned:
            nxt = min(planned, key=lambda lg: lg.seq)
            seq, origin = nxt.seq, nxt.origin
        else:
            seq = v.next_seq
            origin = leg.destination if arrived else (row.last_pos or v.pos)
            if kind is LegKind.RETURN and is_home(origin, row.home, row.home_road):
                return  # at its own station: back in service when the dwell ends
        v.next_seq = max(v.next_seq, seq + 1)
        t = self.sim_now.timestamp()
        v.pending = PendingLeg(
            seq=seq,
            kind=kind,
            origin=origin,
            requested_t=t,
            start_at=t + max(0.0, v.dwell_target_s - v.dwell_s),
        )
        if leg.kind is LegKind.RESPONSE and arrived:
            v.phase = UnitStatus.RETURNING if v.released else UnitStatus.ON_SCENE
            v.dest_kind = DestinationKind.SCENE

    # -------------------------------------------------------------- commands
    async def process_commands(self, *, block_ms: int | None = None, count: int = 100) -> int:
        """Read and apply pending commands (effects + acks are written by the next tick)."""
        engine = self.engine
        if engine is None:
            return 0
        msgs: list[StreamMessage] = []
        now = time.monotonic()
        if now - self._last_claim_sweep > self.settings.leader_lock_ttl_ms / 1000.0:
            # commands a dead leader read but never committed
            msgs += await autoclaim(
                self.redis, keys.STREAM_COMMANDS, keys.GROUP_WORKER, self.instance_id,
                min_idle_ms=self.settings.leader_lock_ttl_ms, count=count,
            )  # fmt: skip
            self._last_claim_sweep = now
        msgs += await read_group(
            self.redis, keys.STREAM_COMMANDS, keys.GROUP_WORKER, self.instance_id,
            count=count, block_ms=block_ms,
        )  # fmt: skip
        if self.engine is not engine:
            return 0
        engine.set_clock(self.sim_now)
        for msg in msgs:
            await self._apply_command(engine, msg)
        return len(msgs)

    async def _apply_command(self, engine: SimulationEngine, msg: StreamMessage) -> None:
        data = msg.data or {}
        ctype = str(data.get("type", ""))
        wall = self.wall_clock()
        try:
            if msg.data is None:
                raise CommandError(msg.error or "undecodable")
            city_id = str(data.get("city", ""))
            if city_id not in engine.cities and city_id in self.settings.cities:
                engine.cities.update(await self._load_cities())  # seeded after we started
            if ctype == CommandType.START_DISPATCH:
                try:
                    engine.start_dispatch(data, stream_id=msg.id, wall=wall, sim=self.sim_now)
                except CommandError:
                    self._fail_unstarted(engine, data, msg.id, wall)
                    raise
            elif ctype == CommandType.CANCEL_DISPATCH:
                engine.cancel_dispatch(data, stream_id=msg.id, wall=wall)
            elif ctype == CommandType.LEG_PLANNED:
                engine.apply_leg_planned(data, stream_id=msg.id, wall=wall)
            elif ctype == CommandType.ROUTE_REPLACED:
                engine.apply_route_replaced(data, stream_id=msg.id, wall=wall)
            else:
                raise CommandError(f"unknown command type {ctype!r}")
            COMMANDS.labels(type=ctype, outcome="applied").inc()
        except CommandError as exc:
            COMMANDS.labels(type=ctype or "?", outcome="rejected").inc()
            log.error("command rejected", extra={"id": msg.id, "type": ctype, "error": str(exc)})
            city_id = str(data.get("city", ""))
            if city_id in engine.cities:
                engine.ack(city_id, msg.id)
            else:
                await self.redis.xack(keys.STREAM_COMMANDS, keys.GROUP_WORKER, msg.id)

    def _fail_unstarted(
        self, engine: SimulationEngine, cmd: dict[str, Any], stream_id: str, wall: datetime
    ) -> None:
        """A start command that cannot run: report FAILED so the unit is freed."""
        city_id = str(cmd.get("city", ""))
        dispatch_id = str(cmd.get("dispatch_id", ""))
        if (
            city_id not in engine.cities
            or not _is_uuid(dispatch_id)
            or not _is_uuid(str(cmd.get("incident_id", "")))
        ):
            return
        b = engine.batch(city_id)
        b.finished[dispatch_id] = wall.timestamp()
        engine.mark_finished(city_id, {dispatch_id})
        b.history.append(
            message(
                HistoryEventType.FAILED.value,
                event_id=event_id("fail", dispatch_id),
                ts=wall.isoformat(),
                city=city_id,
                incident_id=str(cmd.get("incident_id")),
                dispatch_id=dispatch_id,
                vehicle_id=str(cmd.get("vehicle_id", "")),
                reason="invalid start command",
            )
        )

    # ------------------------------------------------------------------ tick
    async def tick_once(self) -> dict[str, CityBatch]:
        """One simulation step for every city + one MULTI/EXEC per city."""
        engine = self.engine
        if engine is None:
            return {}
        started = time.perf_counter()
        dt_sim = self.settings.sim_tick_s * self.settings.sim_time_scale
        if self.settings.sim_time_scale == 1.0:
            self.sim_now = self.wall_clock()
        else:
            self.sim_now += timedelta(seconds=dt_sim)
        batches = engine.tick(dt_sim=dt_sim, sim=self.sim_now, wall=self.wall_clock())
        await self._flush(batches)
        await self._heartbeat(engine)
        TICK_DURATION.observe(time.perf_counter() - started)
        return batches

    async def _flush(self, batches: dict[str, CityBatch]) -> None:
        requests: list[dict[str, Any]] = []
        for b in batches.values():  # handed to the planner once, not with a retried write
            requests.extend(b.plan_requests)
            b.plan_requests = []
        try:
            for city_id, b in batches.items():
                pending = self._pending.pop(city_id, None)
                batch = merge_batches(pending, b) if pending is not None else b
                if not self.lock.is_held():
                    raise LeadershipLost
                try:
                    await write_city_batch(
                        self.redis, batch, finished_ttl_s=self.settings.finished_dispatch_ttl_s
                    )
                except (RedisError, OSError) as exc:
                    self._pending[city_id] = batch  # retried, merged, on the next tick
                    log.error(
                        "tick write failed; will retry",
                        extra={"city": city_id, "error": repr(exc)},
                    )
                    continue
                self._observe(city_id, batch)
        finally:
            if requests and self.planner is not None:
                self.planner.submit(requests)

    def _observe(self, city_id: str, b: CityBatch) -> None:
        for ev in b.history:
            HISTORY_EVENTS.labels(type=ev["type"]).inc()
            if ev["type"] == HistoryEventType.PREEMPTION_CLAIMED:
                PREEMPTIONS.labels(city=city_id, state=ev.get("state", "?")).inc()
            elif ev["type"] == HistoryEventType.PREEMPTION_CONFLICT:
                PREEMPTION_CONFLICTS.labels(city=city_id).inc()
            elif ev["type"] == HistoryEventType.PREEMPTION_RELEASED:
                PREEMPTION_OUTCOMES.labels(city=city_id, outcome=ev.get("outcome", "?")).inc()
            elif ev["type"] == HistoryEventType.PREEMPT_GREEN:
                PREEMPTION_PHASE_STATE.labels(
                    city=city_id, kind=ev.get("phase_state_at_claim") or "?"
                ).inc()
        if self.engine is not None:
            ACTIVE_VEHICLES.labels(city=city_id).set(self.engine.active_count(city_id))
            SIGNALS_STARVED.labels(city=city_id).set(self.engine.starved.get(city_id, 0))
        if b.tick is not None:
            ACTIVE_SIGNALS.labels(city=city_id).set(len(b.tick.get("signals", [])))

    async def _heartbeat(self, engine: SimulationEngine) -> None:
        try:
            async with self.redis.pipeline(transaction=False) as p:
                p.hset(
                    keys.WORKER_HEARTBEAT,
                    mapping={
                        "instance": self.instance_id,
                        "ts": time.time(),
                        "tick": engine.tick_no,
                        "vehicles": engine.active_count(),
                        "sim_ts": self.sim_now.isoformat(),
                        "leader_since": self.leader_since or 0,
                    },
                )
                p.expire(keys.WORKER_HEARTBEAT, 3600)
                if self.settings.sim_time_scale != 1.0:
                    p.set(keys.SIM_CLOCK, self.sim_now.timestamp())
                await p.execute()
        except (RedisError, OSError) as exc:
            log.warning("heartbeat failed", extra={"error": repr(exc)})

    # ------------------------------------------------------------------ loops
    async def run(self, stop: asyncio.Event) -> None:
        period = self.settings.leader_lock_ttl_ms / 3000.0
        while not stop.is_set():
            try:
                if await self.try_lead():
                    await self._lead(stop)
            except (RedisError, OSError) as exc:
                log.error("redis error in worker loop", extra={"error": repr(exc)})
            except LeadershipLost:
                pass
            except Exception:
                log.exception("worker crashed in leader loop; stepping down")
            await self._close_planner()
            self.step_down()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=period)
        await self.shutdown()

    async def _close_planner(self) -> None:
        planner, self.planner = self.planner, None
        if planner is not None:
            await planner.close()

    async def _lead(self, stop: asyncio.Event) -> None:
        lost = asyncio.Event()

        async def renew() -> None:
            period = self.settings.leader_lock_ttl_ms / 3000.0
            while not stop.is_set():
                try:
                    ok = await self.lock.renew()
                except (RedisError, OSError):
                    ok = self.lock.is_held()  # keep going until the lease would expire
                if not ok or not self.lock.is_held():
                    lost.set()
                    return
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=period)

        async def commands() -> None:
            while not stop.is_set() and not lost.is_set():
                try:
                    await self.process_commands(block_ms=self.settings.command_block_ms)
                except (RedisError, OSError) as exc:
                    log.warning("command read failed", extra={"error": repr(exc)})
                    await asyncio.sleep(0.5)

        async def ticks() -> None:
            period = self.settings.sim_tick_s
            next_at = time.monotonic()
            while not stop.is_set() and not lost.is_set():
                await self.tick_once()
                next_at += period
                delay = next_at - time.monotonic()
                if delay < 0:
                    TICK_OVERRUNS.inc()
                    next_at = time.monotonic()
                    delay = 0
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)

        tasks = [asyncio.create_task(f(), name=f.__name__) for f in (renew, commands, ticks)]
        stopper = asyncio.create_task(stop.wait())
        loser = asyncio.create_task(lost.wait())
        try:
            done, _ = await asyncio.wait(
                [*tasks, stopper, loser], return_when=asyncio.FIRST_COMPLETED
            )
            for t in done:
                if t in tasks and t.exception() is not None:
                    raise t.exception()  # type: ignore[misc]
        finally:
            for t in [*tasks, stopper, loser]:
                t.cancel()
            await asyncio.gather(*tasks, stopper, loser, return_exceptions=True)
        if lost.is_set():
            raise LeadershipLost

    async def shutdown(self) -> None:
        """Graceful stop: write what is pending, hand leadership over immediately."""
        if self.engine is not None and self.lock.is_held():
            with contextlib.suppress(Exception):
                await self._flush(self.engine.drain())
        await self._close_planner()
        with contextlib.suppress(Exception):
            await self.lock.release()
        self.step_down()


class LeadershipLost(Exception):
    pass


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


async def amain(settings: Settings | None = None) -> None:
    cfg = settings or get_settings()
    configure_logging("worker", cfg.log_level, json_logs=cfg.log_json)
    if cfg.metrics_port:
        try:
            start_http_server(cfg.metrics_port)
        except OSError as exc:  # e.g. worker + persister on one dev machine
            log.warning(
                "metrics port unavailable", extra={"port": cfg.metrics_port, "error": repr(exc)}
            )
    engine = create_engine(cfg, application_name="divert-worker")
    redis = keys.create_redis(cfg)
    router = OsrmRouter(
        cfg.osrm_url,
        timeout_s=cfg.osrm_timeout_s,
        retries=cfg.osrm_retries,
        cache=redis,
        cache_ttl_s=cfg.osrm_cache_ttl_s,
        alternatives=cfg.route_alternatives,
        detours=cfg.route_detours,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, stop.set)
    worker = Worker(cfg, redis, create_sessionmaker(engine), router=router)
    log.info("worker starting", extra={"instance": worker.instance_id})
    try:
        await worker.run(stop)
    finally:
        await router.aclose()
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain())


if __name__ == "__main__":
    main()
