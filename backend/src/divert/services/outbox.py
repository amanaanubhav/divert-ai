"""Transactional outbox: worker commands are written to PostgreSQL in the same
transaction as the incident/dispatch rows, then relayed to the Redis stream.

* fast path - right after commit (in the background) the API XADDs the row and stamps
  ``sent_at``;
* relay     - a background task re-sends rows still unsent after
  DIVERT_OUTBOX_RELAY_MIN_AGE_S (Redis blip, API crash between commit and XADD).
  ``FOR UPDATE SKIP LOCKED`` lets every API replica run it without double work.

Delivery is at-least-once; the worker applies commands idempotently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.db.models import Outbox
from divert.db.session import transaction
from divert.metrics import OUTBOX_BACKLOG, OUTBOX_RELAYED
from divert.store import redis_store as keys
from divert.store.events import OUTBOX_TOPIC_COMMANDS
from divert.store.guard import RedisGuard
from divert.store.streams import xadd_json

log = logging.getLogger(__name__)

TOPIC_STREAMS: Final = {OUTBOX_TOPIC_COMMANDS: (keys.STREAM_COMMANDS, keys.STREAM_COMMANDS_MAXLEN)}


async def publish(redis: Redis, outbox_id: int, topic: str, payload: dict[str, Any]) -> str:
    stream, maxlen = TOPIC_STREAMS[topic]
    return await xadd_json(redis, stream, {**payload, "outbox_id": outbox_id}, maxlen=maxlen)


class OutboxRelay:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,
        *,
        interval_s: float = 2.0,
        min_age_s: float = 2.0,
        batch: int = 100,
        guard: RedisGuard | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.interval_s = interval_s
        self.min_age_s = min_age_s
        self.batch = batch
        self.guard = guard

    async def _publish(self, outbox_id: int, topic: str, payload: dict[str, Any]) -> str:
        """XADD, through the request-path guard when one is set (bounded, and skipped while
        Redis is down - rows then simply stay unsent instead of holding locks)."""
        if self.guard is None:
            return await publish(self.redis, outbox_id, topic, payload)
        return await self.guard.call(lambda: publish(self.redis, outbox_id, topic, payload))

    async def send_now(self, outbox_id: int, topic: str, payload: dict[str, Any]) -> bool:
        """Post-commit fast path. Never raises: on failure the relay will retry."""
        try:
            await self._publish(outbox_id, topic, payload)
        except (RedisError, OSError) as exc:
            log.warning(
                "outbox fast-path send failed; relay will retry",
                extra={"outbox_id": outbox_id, "error": repr(exc)},
            )
            return False
        try:
            async with transaction(self.sessionmaker) as s:
                await s.execute(
                    sa.update(Outbox)
                    .where(Outbox.id == outbox_id, Outbox.sent_at.is_(None))
                    .values(sent_at=sa.func.now(), attempts=Outbox.attempts + 1)
                )
        except Exception as exc:
            log.warning(
                "could not stamp outbox row", extra={"outbox_id": outbox_id, "error": repr(exc)}
            )
        OUTBOX_RELAYED.labels(path="inline").inc()
        return True

    async def relay_once(self) -> int:
        """Send every unsent row older than min_age (oldest first). Returns rows sent."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self.min_age_s)
        sent = 0
        async with transaction(self.sessionmaker) as s:
            rows = (
                await s.execute(
                    sa.select(Outbox.id, Outbox.topic, Outbox.payload)
                    .where(Outbox.sent_at.is_(None), Outbox.created_at <= cutoff)
                    .order_by(Outbox.id)
                    .limit(self.batch)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            done: list[int] = []
            for row in rows:
                try:
                    await self._publish(row.id, row.topic, row.payload)
                except (RedisError, OSError, KeyError) as exc:
                    await s.execute(
                        sa.update(Outbox)
                        .where(Outbox.id == row.id)
                        .values(attempts=Outbox.attempts + 1, last_error=repr(exc)[:2000])
                    )
                    if not isinstance(exc, KeyError):
                        break  # Redis is down: keep order, retry the rest next round
                    continue
                done.append(row.id)
            if done:
                await s.execute(
                    sa.update(Outbox)
                    .where(Outbox.id.in_(done))
                    .values(sent_at=sa.func.now(), attempts=Outbox.attempts + 1, last_error=None)
                )
                sent = len(done)
        if sent:
            OUTBOX_RELAYED.labels(path="relay").inc(sent)
            log.info("outbox relayed", extra={"rows": sent})
        return sent

    async def backlog(self) -> int:
        async with self.sessionmaker() as s:
            n = await s.scalar(sa.select(sa.func.count()).where(Outbox.sent_at.is_(None)))
        OUTBOX_BACKLOG.set(int(n or 0))
        return int(n or 0)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.relay_once()
            except Exception:
                log.exception("outbox relay round failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s)
