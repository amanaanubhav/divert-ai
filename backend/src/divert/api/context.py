"""Process-wide application context (created in the lifespan, one per API process)."""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from divert.api.ws_hub import CityHub
from divert.config import Settings
from divert.services.dispatch import DispatchService
from divert.services.eta import EtaModel
from divert.services.outbox import OutboxRelay
from divert.services.reference import ReferenceCache
from divert.store.guard import RedisGuard


@dataclass(slots=True)
class AppContext:
    """``redis`` is the request-path client (short timeouts, no retries); every call on it
    goes through ``guard`` so a Redis outage costs a request at most ``guard.timeout_s``
    once per cooldown. The WebSocket hub keeps its own patient pub/sub client."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    guard: RedisGuard
    eta_model: EtaModel
    reference: ReferenceCache
    outbox: OutboxRelay
    dispatch: DispatchService
    hub: CityHub
