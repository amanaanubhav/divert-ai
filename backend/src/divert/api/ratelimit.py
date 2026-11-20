"""Fixed-window rate limit for mutating requests (Redis INCR + EXPIRE per minute).

Keyed by API key (if sent) else client address. Fails open - immediately, via the
request-path circuit breaker - if Redis is unreachable: an emergency dispatch must never
be refused or delayed because the limiter is down.
"""

from __future__ import annotations

import logging
import time

from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from divert.api.deps import CtxDep, client_id
from divert.api.errors import ApiError
from divert.metrics import RATE_LIMITED
from divert.store import redis_store as keys

log = logging.getLogger(__name__)
WINDOW_S = 60


async def check_rate_limit(
    redis: Redis, scope: str, client: str, limit: int
) -> tuple[bool, int, int]:
    """Returns (allowed, remaining, retry_after_s)."""
    now = time.time()
    window = int(now // WINDOW_S)
    key = keys.rate_limit(scope, client, window)
    async with redis.pipeline(transaction=True) as p:
        p.incr(key)
        p.expire(key, WINDOW_S * 2)
        count, _ = await p.execute()
    retry_after = max(1, int((window + 1) * WINDOW_S - now))
    return int(count) <= limit, max(0, limit - int(count)), retry_after


async def rate_limit_mutations(request: Request, ctx: CtxDep) -> None:
    s = ctx.settings
    who = request.headers.get("x-api-key") or client_id(request, s.trust_proxy_headers)
    try:
        allowed, _, retry_after = await ctx.guard.call(
            lambda: check_rate_limit(ctx.redis, "mutate", who, s.rate_limit_per_min)
        )
    except RedisError as exc:
        log.warning("rate limiter unavailable; allowing request", extra={"error": repr(exc)})
        return
    if not allowed:
        RATE_LIMITED.labels(scope="mutate").inc()
        raise ApiError(
            429,
            "rate_limited",
            f"more than {s.rate_limit_per_min} requests per minute",
            {"limit_per_min": s.rate_limit_per_min, "retry_after_s": retry_after},
            headers={"Retry-After": str(retry_after)},
        )
