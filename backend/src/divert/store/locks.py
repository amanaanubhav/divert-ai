"""Leader election for the simulation worker: one Redis key, ``SET NX PX`` + renewal.

* ``acquire`` - ``SET lock token NX PX ttl``: whoever sets it is the leader.
* ``renew``   - extend the TTL only if we still own the token (WATCH/MULTI compare-and-set,
  no Lua needed). A failed renewal means leadership is lost: the caller stops writing.
* ``release`` - delete only if we own it (graceful hand-over on shutdown).

The leader treats itself as leader only until ``valid_until`` (last successful renewal +
TTL - safety margin), so a paused/partitioned leader stops ticking *before* its key can
expire and a standby take over. This bounds - but, like any lease without fencing
tokens, cannot fully exclude - an overlap window; the per-city MULTI/EXEC tick keeps any
such overlap from corrupting a hash (fields are written whole) and history events carry
deterministic ids, so the persister de-duplicates replays.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable

from redis.asyncio import Redis
from redis.exceptions import WatchError

from divert.store.streams import to_str


class LeaderLock:
    def __init__(
        self,
        redis: Redis,
        key: str,
        *,
        ttl_ms: int,
        owner: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.redis = redis
        self.key = key
        self.ttl_ms = ttl_ms
        self.owner = owner
        self.token = f"{owner}:{secrets.token_hex(8)}"
        self._clock = clock
        self._valid_until = 0.0

    @property
    def margin_s(self) -> float:
        return self.ttl_ms / 1000.0 / 5.0

    def is_held(self) -> bool:
        """True while our lease is safely within its TTL (local monotonic clock)."""
        return self._clock() < self._valid_until

    def _stamp(self, started: float) -> None:
        self._valid_until = started + self.ttl_ms / 1000.0 - self.margin_s

    async def acquire(self) -> bool:
        started = self._clock()
        ok = await self.redis.set(self.key, self.token, nx=True, px=self.ttl_ms)
        if ok:
            self._stamp(started)
            return True
        # we may already own it (e.g. re-acquire after a slow loop): treat as renewal
        return await self.renew()

    async def renew(self) -> bool:
        started = self._clock()
        async with self.redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(self.key)
                current = await pipe.get(self.key)
                if current is None or to_str(current) != self.token:
                    await pipe.unwatch()  # type: ignore[no-untyped-call]
                    self._valid_until = 0.0
                    return False
                pipe.multi()  # type: ignore[no-untyped-call]
                pipe.pexpire(self.key, self.ttl_ms)
                await pipe.execute()
            except WatchError:
                self._valid_until = 0.0
                return False
        self._stamp(started)
        return True

    async def release(self) -> bool:
        self._valid_until = 0.0
        async with self.redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(self.key)
                current = await pipe.get(self.key)
                if current is None or to_str(current) != self.token:
                    await pipe.unwatch()  # type: ignore[no-untyped-call]
                    return False
                pipe.multi()  # type: ignore[no-untyped-call]
                pipe.delete(self.key)
                await pipe.execute()
            except WatchError:
                return False
        return True

    async def current_owner(self) -> str | None:
        value = await self.redis.get(self.key)
        return None if value is None else to_str(value)
