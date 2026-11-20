"""History persister: ``python -m divert.persister`` (scale out freely).

Consumes ``divert:history`` through the consumer group ``persister`` and writes
PostgreSQL idempotently (see db/repositories/history.py):

* a batch is applied in ONE transaction and acked only after COMMIT;
* if the batch fails, each message is retried alone; a message that keeps failing
  (DIVERT_PERSISTER_MAX_RETRIES deliveries) or can never apply (schema violation) is
  copied to ``divert:history:dlq`` and acked;
* messages left pending by a crashed persister are taken over with XAUTOCLAIM;
* hourly (one persister at a time, PG advisory lock): position + outbox retention.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from prometheus_client import start_http_server
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from divert.config import Settings, get_settings
from divert.db.repositories.history import BadEvent, HistoryWriter, prune_outbox, prune_positions
from divert.db.session import create_engine, create_sessionmaker, transaction
from divert.logging import configure_logging, default_instance_id
from divert.metrics import (
    DLQ_MESSAGES,
    HISTORY_PENDING,
    HISTORY_STREAM_LAG,
    PERSISTED,
    PERSISTER_LAG,
)
from divert.store import redis_store as keys
from divert.store.streams import (
    StreamMessage,
    ack,
    autoclaim,
    dead_letter,
    delivery_counts,
    ensure_group,
    group_lag,
    read_group,
)

log = logging.getLogger("divert.persister")
RETENTION_LOCK_KEY = 0x0D17E28


class Persister:
    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        instance_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.sessionmaker = sessionmaker
        self.consumer = instance_id or settings.instance_id or default_instance_id()
        self.last_event_ts: datetime | None = None
        self._last_retention = 0.0

    async def setup(self) -> None:
        await ensure_group(self.redis, keys.STREAM_HISTORY, keys.GROUP_PERSISTER)

    async def _dlq(self, msg: StreamMessage, reason: str, deliveries: int) -> None:
        await dead_letter(
            self.redis,
            keys.STREAM_HISTORY_DLQ,
            msg,
            source=keys.STREAM_HISTORY,
            reason=reason,
            deliveries=deliveries,
        )
        await ack(self.redis, keys.STREAM_HISTORY, keys.GROUP_PERSISTER, [msg.id])
        DLQ_MESSAGES.labels(stream=keys.STREAM_HISTORY).inc()
        PERSISTED.labels(type=str((msg.data or {}).get("type", "?")), outcome="dlq").inc()

    def _seen(self, data: dict[str, object]) -> None:
        with contextlib.suppress(ValueError, TypeError):
            ts = datetime.fromisoformat(str(data.get("ts")))
            if self.last_event_ts is None or ts > self.last_event_ts:
                self.last_event_ts = ts

    async def process_once(self, *, block_ms: int | None = None) -> int:
        """One round: retries of stale pending messages + a batch of new ones."""
        s = self.settings
        msgs = await autoclaim(
            self.redis, keys.STREAM_HISTORY, keys.GROUP_PERSISTER, self.consumer,
            min_idle_ms=s.persister_retry_idle_ms, count=s.persister_batch,
        )  # fmt: skip
        msgs += await read_group(
            self.redis, keys.STREAM_HISTORY, keys.GROUP_PERSISTER, self.consumer,
            count=s.persister_batch, block_ms=None if msgs else block_ms,
        )  # fmt: skip
        if not msgs:
            return 0
        good: list[StreamMessage] = []
        for m in msgs:
            if m.data is None:
                await self._dlq(m, m.error or "undecodable", 1)
            else:
                good.append(m)
        if not good:
            return len(msgs)
        try:
            async with transaction(self.sessionmaker) as session:
                writer = HistoryWriter(session)
                results = [await writer.apply(m.data) for m in good if m.data is not None]
        except Exception as exc:
            log.warning(
                "batch failed; retrying messages one by one",
                extra={"error": repr(exc), "size": len(good)},
            )
            await self._one_by_one(good)
        else:
            await ack(self.redis, keys.STREAM_HISTORY, keys.GROUP_PERSISTER, [m.id for m in good])
            for m, applied in zip(good, results, strict=True):
                assert m.data is not None
                PERSISTED.labels(
                    type=str(m.data.get("type")), outcome="ok" if applied else "duplicate"
                ).inc()
                self._seen(m.data)
        self._update_lag_metric()
        return len(msgs)

    async def _one_by_one(self, msgs: list[StreamMessage]) -> None:
        for m in msgs:
            assert m.data is not None
            try:
                async with transaction(self.sessionmaker) as session:
                    applied = await HistoryWriter(session).apply(m.data)
            except BadEvent as exc:
                await self._dlq(m, f"bad event: {exc}", 1)
                continue
            except Exception as exc:
                counts = await delivery_counts(
                    self.redis, keys.STREAM_HISTORY, keys.GROUP_PERSISTER, [m.id]
                )
                deliveries = counts.get(m.id, 1)
                if deliveries >= self.settings.persister_max_retries:
                    await self._dlq(m, f"failed {deliveries}x: {exc!r}", deliveries)
                else:
                    PERSISTED.labels(type=str(m.data.get("type")), outcome="retry").inc()
                    log.warning(
                        "event failed; will retry",
                        extra={"id": m.id, "deliveries": deliveries, "error": repr(exc)},
                    )
                continue
            await ack(self.redis, keys.STREAM_HISTORY, keys.GROUP_PERSISTER, [m.id])
            PERSISTED.labels(
                type=str(m.data.get("type")), outcome="ok" if applied else "duplicate"
            ).inc()
            self._seen(m.data)

    def _update_lag_metric(self) -> None:
        if self.last_event_ts is not None:
            PERSISTER_LAG.set(max(0.0, (datetime.now(UTC) - self.last_event_ts).total_seconds()))

    async def refresh_stream_metrics(self) -> None:
        lag = await group_lag(self.redis, keys.STREAM_HISTORY, keys.GROUP_PERSISTER)
        if lag is not None:
            HISTORY_STREAM_LAG.set(lag[0])
            HISTORY_PENDING.set(lag[1])

    async def retention_once(self) -> tuple[int, int]:
        """Prune old positions and relayed outbox rows (one persister at a time)."""
        now = datetime.now(UTC)
        pos_cut = now - timedelta(days=self.settings.position_retention_days)
        out_cut = now - timedelta(days=self.settings.outbox_retention_days)
        positions = outbox = 0
        async with transaction(self.sessionmaker) as session:
            got = await session.scalar(
                sa.select(sa.func.pg_try_advisory_xact_lock(RETENTION_LOCK_KEY))
            )
            if not got:
                return (0, 0)
            while (n := await prune_positions(session, pos_cut)) > 0:
                positions += n
            while (n := await prune_outbox(session, out_cut)) > 0:
                outbox += n
        if positions or outbox:
            log.info("retention", extra={"positions": positions, "outbox": outbox})
        return positions, outbox

    async def run(self, stop: asyncio.Event) -> None:
        await self.setup()
        while not stop.is_set():
            try:
                await self.process_once(block_ms=self.settings.persister_block_ms)
                if time.monotonic() - self._last_retention > self.settings.retention_interval_s:
                    self._last_retention = time.monotonic()
                    await self.retention_once()
                await self.refresh_stream_metrics()
            except (RedisError, OSError) as exc:
                log.error("redis error in persister", extra={"error": repr(exc)})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
            except Exception:
                log.exception("persister round failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1.0)


async def amain(settings: Settings | None = None) -> None:
    cfg = settings or get_settings()
    configure_logging("persister", cfg.log_level, json_logs=cfg.log_json)
    if cfg.metrics_port:
        try:
            start_http_server(cfg.metrics_port)
        except OSError as exc:  # e.g. worker + persister on one dev machine
            log.warning(
                "metrics port unavailable", extra={"port": cfg.metrics_port, "error": repr(exc)}
            )
    engine = create_engine(cfg, application_name="divert-persister")
    redis = keys.create_redis(cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    persister = Persister(cfg, redis, create_sessionmaker(engine))
    log.info("persister starting", extra={"consumer": persister.consumer})
    try:
        await persister.run(stop)
    finally:
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain())


if __name__ == "__main__":
    main()
