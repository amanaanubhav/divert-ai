"""Simulation worker: ``python -m divert.worker``.

Run two or more replicas; exactly one is the *leader* (Redis lock ``SET NX PX`` renewed
every TTL/3), the others wait as hot standbys. The leader:

* is the ONLY writer of live Redis state (vehicles, claims, active signals);
* consumes ``divert:commands`` (consumer group ``worker``) - start/cancel dispatch;
* ticks every DIVERT_SIM_TICK_S: advances vehicles (DIVERT_SIM_TIME_SCALE), claims /
  releases corridor signals per vehicle id, and writes each city's tick - state, history
  events, command acks, pub/sub fan-out - in ONE MULTI/EXEC (see store/live.py);
* never touches PostgreSQL on the hot path. On becoming leader it resumes from Redis and
  rebuilds from PostgreSQL any active dispatch Redis lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import start_http_server
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.config import Settings, get_settings
from divert.db.repositories.history import ActiveDispatch, active_dispatches, ended_dispatch_ids
from divert.db.repositories.reference import ReferenceRepository
from divert.db.session import create_engine, create_sessionmaker
from divert.domain.enums import ServiceType
from divert.domain.geo import LatLng
from divert.domain.models import City
from divert.domain.route import CorridorSignal, Route
from divert.logging import configure_logging, default_instance_id
from divert.metrics import (
    ACTIVE_SIGNALS,
    ACTIVE_VEHICLES,
    COMMANDS,
    HISTORY_EVENTS,
    PREEMPTION_CONFLICTS,
    PREEMPTIONS,
    TICK_DURATION,
    TICK_OVERRUNS,
    WORKER_IS_LEADER,
)
from divert.services.eta import EtaModel, KinematicEtaModel
from divert.services.preemption import PreemptionRules
from divert.services.simulation import (
    CityBatch,
    Claim,
    CommandError,
    SimConfig,
    SimulationEngine,
    Vehicle,
    VehicleStatus,
)
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
        self.engine: SimulationEngine | None = None
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

    async def take_over(self) -> None:
        """Become leader: load cities, resume live state from Redis, rebuild from PG."""
        cities = await self._load_cities()
        rules = PreemptionRules(
            lead_s=self.settings.preempt_lead_s,
            distance_m=self.settings.preempt_distance_m,
            release_past_m=self.settings.preempt_release_past_m,
            tie_s=self.settings.conflict_tie_s,
        )
        engine = SimulationEngine(
            cities,
            self.eta_model,
            SimConfig(
                scene_time_s=self.settings.scene_time_s,
                position_sample_s=self.settings.position_sample_s,
                rules=rules,
            ),
        )
        await ensure_group(self.redis, keys.STREAM_COMMANDS, keys.GROUP_WORKER)
        self.sim_now = await self._load_sim_clock()
        # keep the per-city tick counter monotonic across leader changes (WS contract)
        engine.tick_no = await last_tick_no(self.redis, engine.cities)
        restored = await self._restore_from_redis(engine)
        rebuilt = await self._rebuild_from_pg(engine)
        self.engine = engine
        self.leader_since = time.time()
        self._pending.clear()
        WORKER_IS_LEADER.set(1)
        await self._flush(engine.drain())
        log.info(
            "became leader",
            extra={
                "instance": self.instance_id,
                "restored": restored,
                "rebuilt": rebuilt,
                "tick": engine.tick_no,
            },
        )

    def step_down(self) -> None:
        if self.engine is not None:
            log.warning(
                "leadership lost; dropping in-memory state", extra={"instance": self.instance_id}
            )
        self.engine = None
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
        without its AOF): resume them from their last persisted position."""
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
                b = engine.batch(row.city_id)
                b.history.append(
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
            engine.restore(v, announce_at=wall)
            n += 1
            log.warning("rebuilt vehicle from PostgreSQL", extra={"dispatch_id": row.dispatch_id})
        return n

    def _vehicle_from_pg(self, row: ActiveDispatch, wall: datetime) -> Vehicle:
        route = Route.build([LatLng(a, b) for a, b in row.route_points], row.segment_speeds_mps)
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
                sorted((CorridorSignal.from_json(c) for c in row.corridor), key=lambda c: c.along_m)
            ),
            destination=row.destination,
            predicted_eta_s=row.predicted_eta_s,
            started_at=row.dispatched_at.isoformat(),
            eta_s=row.predicted_eta_s,
        )
        scale = self.settings.sim_time_scale
        if row.arrived_at is not None:
            v.status = VehicleStatus.ON_SCENE
            v.along_m = route.length_m
            # simulated travel time as persisted from the `arrived` event; the wall-clock
            # fallback (scaled) only covers a missing value
            v.travel_s = (
                row.actual_travel_s
                if row.actual_travel_s is not None
                else max(0.0, (row.arrived_at - row.dispatched_at).total_seconds()) * scale
            )
            v.elapsed_s = v.travel_s
            # scene time is not sampled: wall time since arrival (scaled) is an upper
            # bound (it includes any no-leader freeze), capped at the scene time
            v.scene_s = min(
                self.settings.scene_time_s,
                max(0.0, (wall - row.arrived_at).total_seconds()) * scale,
            )
        elif row.last_along_m is not None and row.last_ts is not None:
            v.along_m = min(float(row.last_along_m), route.length_m)
            # simulated elapsed time is persisted with each position sample. Wall-clock
            # differences would add any no-leader freeze (x time scale) to the travel time.
            v.elapsed_s = (
                float(row.last_elapsed_s)
                if row.last_elapsed_s is not None
                else max(0.0, (row.last_ts - row.dispatched_at).total_seconds()) * scale
            )
        v.passed = {
            c.signal_id
            for c in v.corridor
            if c.along_m < v.along_m - self.settings.preempt_release_past_m
        }
        v.samples = int(v.elapsed_s // self.settings.position_sample_s) + 1
        return v

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
                    "tick write failed; will retry", extra={"city": city_id, "error": repr(exc)}
                )
                continue
            self._observe(city_id, batch)

    def _observe(self, city_id: str, b: CityBatch) -> None:
        for ev in b.history:
            HISTORY_EVENTS.labels(type=ev["type"]).inc()
            if ev["type"] == HistoryEventType.PREEMPTION_CLAIMED:
                PREEMPTIONS.labels(city=city_id, state=ev.get("state", "?")).inc()
            elif ev["type"] == HistoryEventType.PREEMPTION_CONFLICT:
                PREEMPTION_CONFLICTS.labels(city=city_id).inc()
        if self.engine is not None:
            ACTIVE_VEHICLES.labels(city=city_id).set(self.engine.active_count(city_id))
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
            self.step_down()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=period)
        await self.shutdown()

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
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, stop.set)
    worker = Worker(cfg, redis, create_sessionmaker(engine))
    log.info("worker starting", extra={"instance": worker.instance_id})
    try:
        await worker.run(stop)
    finally:
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain())


if __name__ == "__main__":
    main()
