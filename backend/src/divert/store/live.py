"""Live state in Redis: the worker's single-transaction writer and the API's readers.

``write_city_batch`` applies one :class:`~divert.services.simulation.CityBatch` - live
state deltas, history events, command acks and pub/sub messages of ONE city - in ONE
``MULTI/EXEC``. State, the events describing it and the acks of the commands that
caused it therefore commit together or not at all (single-primary Redis, as deployed;
see docs/architecture.md for Redis Cluster).

Every write is a whole field/member (HSET field, SADD member, ...): nothing is read,
modified and written back.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from divert.services.simulation import CityBatch
from divert.store import redis_store as keys
from divert.store.streams import FIELD, dumps, to_str


def merge_batches(older: CityBatch, newer: CityBatch) -> CityBatch:
    """Compose two batches (a failed write is retried merged into the next one).

    Application order inside a batch is: deletions, then sets/adds, then removals of
    members, so the composition keeps the newest value of every field.
    """
    for vid in newer.vehicle_dels:
        older.vehicle_sets.pop(vid, None)
    older.vehicle_dels |= newer.vehicle_dels
    older.vehicle_sets.update(newer.vehicle_sets)
    for vid in newer.route_dels:
        older.route_sets.pop(vid, None)
    older.route_dels |= newer.route_dels
    older.route_sets.update(newer.route_sets)
    for k in newer.claim_dels:
        older.claim_sets.pop(k, None)
    older.claim_dels |= newer.claim_dels
    older.claim_sets.update(newer.claim_sets)
    for vid in newer.vclaim_dels:
        older.vclaim_adds.pop(vid, None)
        older.vclaim_rems.pop(vid, None)
    older.vclaim_dels |= newer.vclaim_dels
    for vid, sigs in newer.vclaim_adds.items():
        older.vclaim_adds.setdefault(vid, set()).update(sigs)
        older.vclaim_rems.get(vid, set()).difference_update(sigs)
    for vid, sigs in newer.vclaim_rems.items():
        older.vclaim_rems.setdefault(vid, set()).update(sigs)
        older.vclaim_adds.get(vid, set()).difference_update(sigs)
    for sid in newer.signal_dels:
        older.signal_sets.pop(sid, None)
    older.signal_dels |= newer.signal_dels
    older.signal_sets.update(newer.signal_sets)
    older.junction_sets.update(newer.junction_sets)
    older.finished.update(newer.finished)
    older.history.extend(newer.history)
    older.publish.extend(newer.publish)
    older.acks.extend(newer.acks)
    older.plan_requests.extend(newer.plan_requests)
    older.tick = newer.tick or older.tick
    return older


async def write_city_batch(  # noqa: PLR0912 - one branch per kind of delta
    redis: Redis,
    b: CityBatch,
    *,
    finished_ttl_s: int = 86_400,
    history_maxlen: int = keys.STREAM_HISTORY_MAXLEN,
) -> None:
    city = b.city_id
    vk = keys.vehicles(city)
    sk = keys.active_signals(city)
    now = time.time()
    async with redis.pipeline(transaction=True) as p:
        # 1. deletions
        if b.vehicle_dels:
            p.hdel(vk, *sorted(b.vehicle_dels))
        if b.route_dels:
            p.delete(*(keys.vehicle_route(city, v) for v in sorted(b.route_dels)))
        for sid, vid in sorted(b.claim_dels):
            p.hdel(keys.claims(city, sid), vid)
        if b.vclaim_dels:
            p.delete(*(keys.vehicle_claims(city, v) for v in sorted(b.vclaim_dels)))
        if b.signal_dels:
            p.hdel(sk, *sorted(b.signal_dels))
        # 2. sets / adds (one field or member per vehicle / claim / signal)
        if b.vehicle_sets:
            p.hset(vk, mapping={vid: dumps(v) for vid, v in b.vehicle_sets.items()})
        for vid, doc in b.route_sets.items():
            p.set(keys.vehicle_route(city, vid), dumps(doc))
        for (sid, vid), claim in b.claim_sets.items():
            p.hset(keys.claims(city, sid), vid, dumps(claim))
        for vid, sigs in b.vclaim_adds.items():
            if sigs:
                p.sadd(keys.vehicle_claims(city, vid), *sorted(sigs))
        # 3. member removals
        for vid, sigs in b.vclaim_rems.items():
            if sigs:
                p.srem(keys.vehicle_claims(city, vid), *sorted(sigs))
        if b.signal_sets:
            p.hset(sk, mapping={sid: dumps(s) for sid, s in b.signal_sets.items()})
        if b.junction_sets:
            p.hset(keys.junctions(city), mapping={s: dumps(g) for s, g in b.junction_sets.items()})
        if b.finished:
            fk = keys.finished_dispatches(city)
            p.zadd(fk, b.finished)
            p.zremrangebyscore(fk, "-inf", now - finished_ttl_s)
        if b.tick is not None:
            p.hset(
                keys.city_tick(city),
                mapping={"tick": b.tick["tick"], "sim_ts": b.tick["sim_ts"], "wall_ts": now},
            )
        # 4. history events + command acks, atomically with the state they describe
        for ev in b.history:
            p.xadd(keys.STREAM_HISTORY, {FIELD: dumps(ev)}, maxlen=history_maxlen, approximate=True)
        if b.acks:
            p.xack(keys.STREAM_COMMANDS, keys.GROUP_WORKER, *b.acks)
        # 5. fan-out to API WebSockets (only if the transaction commits)
        channel = keys.events_channel(city)
        for msg in b.publish:
            p.publish(channel, dumps(msg))
        if b.tick is not None:
            p.publish(channel, dumps(b.tick))
        await p.execute()


# ------------------------------------------------------------------------- readers


def _loads(raw: Any) -> dict[str, Any]:
    data = json.loads(to_str(raw))
    return data if isinstance(data, dict) else {}


async def read_vehicles(redis: Redis, city_id: str) -> list[dict[str, Any]]:
    raw = await redis.hgetall(keys.vehicles(city_id))
    return sorted((_loads(v) for v in raw.values()), key=lambda v: str(v.get("id")))


async def read_vehicle_routes(
    redis: Redis, city_id: str, vehicle_ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    ids = list(vehicle_ids)
    if not ids:
        return {}
    values = await redis.mget([keys.vehicle_route(city_id, v) for v in ids])
    return {vid: _loads(val) for vid, val in zip(ids, values, strict=True) if val is not None}


async def read_active_signals(redis: Redis, city_id: str) -> list[dict[str, Any]]:
    raw = await redis.hgetall(keys.active_signals(city_id))
    return sorted((_loads(v) for v in raw.values()), key=lambda s: str(s.get("signal_id")))


async def read_claims(redis: Redis, city_id: str, signal_id: str) -> dict[str, dict[str, Any]]:
    raw = await redis.hgetall(keys.claims(city_id, signal_id))
    return {to_str(k): _loads(v) for k, v in raw.items()}


async def read_tick(redis: Redis, city_id: str) -> dict[str, str]:
    raw = await redis.hgetall(keys.city_tick(city_id))
    return {to_str(k): to_str(v) for k, v in raw.items()}


async def read_finished(redis: Redis, city_id: str) -> set[str]:
    members: list[Any] = await redis.zrange(keys.finished_dispatches(city_id), 0, -1)
    return {to_str(m) for m in members}


async def heartbeat_age_s(redis: Redis) -> tuple[float | None, dict[str, str]]:
    raw = await redis.hgetall(keys.WORKER_HEARTBEAT)
    hb = {to_str(k): to_str(v) for k, v in raw.items()}
    if "ts" not in hb:
        return None, hb
    return max(0.0, time.time() - float(hb["ts"])), hb


async def read_sim_clock(redis: Redis) -> datetime | None:
    """The leader's simulation clock (written each tick when DIVERT_SIM_TIME_SCALE != 1).

    ``None`` when no leader has written it yet or the value is unreadable.
    """
    raw = await redis.get(keys.SIM_CLOCK)
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(to_str(raw)), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


async def last_tick_no(redis: Redis, city_ids: Iterable[str]) -> int:
    """Highest tick number any city last committed (a new leader resumes from it)."""
    best = 0
    for city_id in city_ids:
        raw = await redis.hget(keys.city_tick(city_id), "tick")
        if raw is None:
            continue
        try:
            best = max(best, int(float(to_str(raw))))
        except (TypeError, ValueError, OverflowError):
            continue
    return best
