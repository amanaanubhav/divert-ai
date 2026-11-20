"""Every Redis key DIVERT uses, defined in one place.

Redis holds only hot, rebuildable state and messaging; PostgreSQL is the system of
record. Ownership rules (the prototype's three regression guarantees live here):

* The **worker leader is the only writer** of live state (vehicles, claims, active
  signal states). The API only reads it and talks to the worker via a stream.
* **Per-vehicle hash fields**: ``vehicles(city)`` is a HASH with one field per vehicle,
  so no code path ever read-modify-writes a shared JSON blob.
* **Per-vehicle signal claims**: ``claims(city, signal)`` is a HASH vehicle_id -> claim,
  and ``vehicle_claims(city, vehicle)`` is the SET of signals a vehicle holds, so ALL of
  a vehicle's claims can be released on arrival / cancel / error in one transaction.
* One city's tick is written in **one MULTI/EXEC**. All per-city keys share the Redis
  Cluster hash tag ``{<city>}`` so that transaction stays single-slot under Cluster.

Streams and pub/sub channels are global (not part of per-city transactions).
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from divert.config import Settings

PREFIX: Final = "divert"
_ID_RE: Final = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# --------------------------------------------------------------------------- streams
STREAM_COMMANDS: Final = f"{PREFIX}:commands"  # api (outbox relay) -> worker
STREAM_HISTORY: Final = f"{PREFIX}:history"  # worker -> persister
STREAM_HISTORY_DLQ: Final = f"{PREFIX}:history:dlq"  # persister poison messages
GROUP_WORKER: Final = "worker"
GROUP_PERSISTER: Final = "persister"
STREAM_COMMANDS_MAXLEN: Final = 100_000  # approximate trimming (MAXLEN ~)
STREAM_HISTORY_MAXLEN: Final = 1_000_000

# ------------------------------------------------------------------- worker control
LEADER_LOCK: Final = f"{PREFIX}:worker:leader"  # STRING token, SET NX PX + renewal
WORKER_HEARTBEAT: Final = f"{PREFIX}:worker:heartbeat"  # HASH instance, ts, tick, vehicles
SIM_CLOCK: Final = f"{PREFIX}:sim:clock"  # STRING sim epoch seconds (survives failover)


def _check(part: str, what: str) -> str:
    if not _ID_RE.fullmatch(part):
        raise ValueError(f"invalid {what} for a redis key: {part!r}")
    return part


def _city(city_id: str) -> str:
    """Hash-tagged city segment: ``{delhi}``."""
    return "{" + _check(city_id, "city id") + "}"


# --------------------------------------------------------------------- live state


def vehicles(city_id: str) -> str:
    """HASH vehicle_id -> live vehicle JSON (position, speed, eta, status, ...)."""
    return f"{PREFIX}:{_city(city_id)}:vehicles"


def vehicle_route(city_id: str, vehicle_id: str) -> str:
    """STRING route JSON (geometry, cumulative distances, corridor); static per dispatch."""
    return f"{PREFIX}:{_city(city_id)}:route:{_check(vehicle_id, 'vehicle id')}"


def claims(city_id: str, signal_id: str) -> str:
    """HASH vehicle_id -> claim JSON {state, eta_s, lane, bearing, event_id, ...}."""
    return f"{PREFIX}:{_city(city_id)}:claims:{_check(signal_id, 'signal id')}"


def vehicle_claims(city_id: str, vehicle_id: str) -> str:
    """SET of signal ids currently claimed by the vehicle (release-all index)."""
    return f"{PREFIX}:{_city(city_id)}:vclaims:{_check(vehicle_id, 'vehicle id')}"


def active_signals(city_id: str) -> str:
    """HASH signal_id -> resolved state JSON; only non-NORMAL signals are present."""
    return f"{PREFIX}:{_city(city_id)}:signals:active"


def junctions(city_id: str) -> str:
    """HASH signal_id -> junction geometry JSON {legs, approach_bearings, source}: the
    approaches -> phases mapping fixed on a junction's first use (design §4.1), so a new
    leader keeps it (written by the leader in its tick transaction, never expires)."""
    return f"{PREFIX}:{_city(city_id)}:junctions"


def city_tick(city_id: str) -> str:
    """HASH {tick, sim_ts, wall_ts} of the last tick written for the city."""
    return f"{PREFIX}:{_city(city_id)}:tick"


def finished_dispatches(city_id: str) -> str:
    """ZSET dispatch_id -> wall ts it ended in the worker (cleared/cancelled/failed).

    Makes ``start_dispatch`` idempotent: a redelivered command for a dispatch that has
    already finished is ignored. Trimmed to DIVERT_FINISHED_DISPATCH_TTL_S.
    """
    return f"{PREFIX}:{_city(city_id)}:finished"


def city_keys_pattern(city_id: str) -> str:
    """SCAN pattern for every live-state key of one city (ops / tests)."""
    return f"{PREFIX}:{_city(city_id)}:*"


# ------------------------------------------------------------------------ pub/sub


def events_channel(city_id: str) -> str:
    """PUB/SUB worker -> API WebSocket fan-out (tick, vehicle_added, ...)."""
    return f"{PREFIX}:events:{_check(city_id, 'city id')}"


# -------------------------------------------------------------------------- caches


def osrm_route_cache(profile: str, coords: str, *, scope: str = "") -> str:
    """STRING cached OSRM response; ``coords`` is the OSRM coordinate string and ``scope``
    everything else that changes the answer (OSRM base URL + request parameters), so a
    different server or parameter set never reads another's cached routes."""
    digest = hashlib.sha256(f"{scope}\n{coords}".encode()).hexdigest()[:32]
    return f"{PREFIX}:osrm:{_check(profile, 'profile')}:{digest}"


def rate_limit(scope: str, client: str, window: int) -> str:
    """STRING counter for a fixed window (INCR + EXPIRE)."""
    client_digest = hashlib.sha256(client.encode()).hexdigest()[:16]
    return f"{PREFIX}:rl:{_check(scope, 'scope')}:{client_digest}:{window}"


# -------------------------------------------------------------------------- client


def create_redis(settings: Settings, *, request_path: bool = False) -> Redis:
    """decode_responses=True: every key/value is ``str``; JSON payloads are encoded by us.

    ``request_path=True`` (API): short socket timeouts and no client retries - the caller
    wraps calls in :class:`divert.store.guard.RedisGuard`, which bounds and short-circuits
    them while Redis is down. Workers / pub-sub use the default, patient client.
    """
    if request_path:
        timeout = settings.redis_request_timeout_s
        fast: Redis = Redis.from_url(
            settings.redis_url.get_secret_value(),
            decode_responses=True,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            health_check_interval=30,
            retry=Retry(NoBackoff(), 0),
        )
        return fast
    client: Redis = Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout_s,
        socket_connect_timeout=settings.redis_socket_timeout_s,
        health_check_interval=30,
        retry_on_timeout=True,
    )
    return client
