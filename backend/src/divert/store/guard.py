"""Request-path guard for Redis: a hard per-call deadline plus a circuit breaker.

Redis is an optimisation or a live-view source on the API's request path (rate limit,
OSRM cache, sim clock, post-commit XADD/PUBLISH, live reads) - never the system of
record. When it is down, a request must not wait for socket timeouts and client retries
(it used to take ~47 s to dispatch). So every request-path Redis call goes through
:class:`RedisGuard`:

* each call is bounded by ``timeout_s`` (``asyncio.wait_for``);
* any failure opens the circuit for ``cooldown_s``: calls then fail immediately with
  :class:`RedisCircuitOpen`, without touching the network;
* after the cooldown the next call is a trial: success closes the circuit, failure
  re-opens it.

Every error it raises is a :class:`redis.exceptions.RedisError`, so callers keep a single
``except RedisError`` (fail open / fall back / 503).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from divert.metrics import REDIS_CIRCUIT_OPEN, REDIS_CIRCUIT_TRIPS

log = logging.getLogger(__name__)


class RedisCircuitOpen(RedisConnectionError):
    """Redis was failing moments ago; the call was skipped without touching it."""


class RedisGuard:
    def __init__(
        self,
        *,
        timeout_s: float = 0.5,
        cooldown_s: float = 5.0,
        name: str = "api",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout_s = timeout_s
        self.cooldown_s = cooldown_s
        self.name = name
        self._clock = clock
        self._open_until = 0.0
        self._tripped = False
        self.last_error: str | None = None

    @property
    def is_open(self) -> bool:
        """True while calls are being short-circuited."""
        return self._clock() < self._open_until

    @property
    def state(self) -> str:
        if self.is_open:
            return "open"
        return "half_open" if self._tripped else "closed"

    async def call[T](self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run ``fn()`` under the deadline, or raise a RedisError without running it."""
        if self.is_open:
            raise RedisCircuitOpen(f"redis circuit open ({self.last_error})")
        try:
            result = await asyncio.wait_for(fn(), timeout=self.timeout_s)
        except TimeoutError as exc:
            self._trip(f"timeout after {self.timeout_s}s")
            raise RedisTimeoutError(f"redis call exceeded {self.timeout_s}s") from exc
        except RedisError as exc:
            self._trip(repr(exc))
            raise
        except OSError as exc:  # DNS failure, connection refused, ...
            self._trip(repr(exc))
            raise RedisConnectionError(repr(exc)) from exc
        self._close()
        return result

    def _trip(self, error: str) -> None:
        was_open = self._tripped
        self._open_until = self._clock() + self.cooldown_s
        self._tripped = True
        self.last_error = error[:200]
        REDIS_CIRCUIT_OPEN.labels(guard=self.name).set(1)
        if not was_open:
            REDIS_CIRCUIT_TRIPS.labels(guard=self.name).inc()
            log.warning(
                "redis unavailable; circuit open",
                extra={"guard": self.name, "error": self.last_error, "cooldown_s": self.cooldown_s},
            )

    def _close(self) -> None:
        if self._tripped:
            log.info("redis reachable again; circuit closed", extra={"guard": self.name})
        self._tripped = False
        self._open_until = 0.0
        self.last_error = None
        REDIS_CIRCUIT_OPEN.labels(guard=self.name).set(0)
