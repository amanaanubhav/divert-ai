"""Per-process cache of city reference data (cities, stations, signals) from PostgreSQL.

Reference data changes only when ``divert seed`` runs, so each API replica keeps it in
memory for DIVERT_REFERENCE_CACHE_TTL_S. Unit availability is *not* cached (it is live).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.db.repositories.reference import CityReference, ReferenceRepository
from divert.domain.models import City


class ReferenceCache:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.ttl_s = ttl_s
        self._clock = clock
        self._refs: dict[str, tuple[float, CityReference | None]] = {}
        self._cities: tuple[float, list[City]] | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    def _fresh(self, loaded_at: float) -> bool:
        return self._clock() - loaded_at < self.ttl_s

    async def cities(self) -> list[City]:
        if self._cities is not None and self._fresh(self._cities[0]):
            return self._cities[1]
        async with self.sessionmaker() as s:
            cities = await ReferenceRepository(s).list_cities()
        self._cities = (self._clock(), cities)
        return cities

    async def reference(self, city_id: str) -> CityReference | None:
        hit = self._refs.get(city_id)
        if hit is not None and self._fresh(hit[0]):
            return hit[1]
        lock = self._locks.setdefault(city_id, asyncio.Lock())
        async with lock:
            hit = self._refs.get(city_id)
            if hit is not None and self._fresh(hit[0]):
                return hit[1]
            async with self.sessionmaker() as s:
                ref = await ReferenceRepository(s).load_city_reference(city_id)
            self._refs[city_id] = (self._clock(), ref)
            return ref

    async def city(self, city_id: str) -> City | None:
        ref = await self.reference(city_id)
        return None if ref is None else ref.city

    def invalidate(self) -> None:
        self._refs.clear()
        self._cities = None
