"""Redis Streams helpers: JSON messages, consumer groups, retries and the DLQ.

Every stream entry has a single field ``data`` holding a JSON object that carries
``type`` and ``v`` (see :mod:`divert.store.events`). Consumers ack only after the
effect of a message is durable; unacked messages stay in the group's PEL and are
picked up again with XAUTOCLAIM (by any consumer) once they have been idle long enough.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

log = logging.getLogger(__name__)

FIELD = "data"


def to_str(value: bytes | str | float | None) -> str:
    """Coerce a redis-py reply (``decode_responses=True`` gives ``str``) to ``str``."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def dumps(msg: Mapping[str, Any]) -> str:
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass(frozen=True, slots=True)
class StreamMessage:
    id: str
    data: dict[str, Any] | None  # None when the entry could not be decoded (poison)
    raw: dict[str, str]
    error: str | None = None


def _decode(entry_id: Any, fields: Mapping[Any, Any]) -> StreamMessage:
    raw = {to_str(k): to_str(v) for k, v in fields.items()}
    try:
        data = json.loads(raw[FIELD])
        if not isinstance(data, dict):
            raise ValueError("payload is not a JSON object")
        return StreamMessage(to_str(entry_id), data, raw)
    except (KeyError, ValueError) as exc:
        return StreamMessage(to_str(entry_id), None, raw, f"undecodable: {exc}")


async def xadd_json(
    redis: Redis, stream: str, msg: Mapping[str, Any], *, maxlen: int | None = None
) -> str:
    entry_id = await redis.xadd(
        stream, {FIELD: dumps(msg)}, maxlen=maxlen, approximate=maxlen is not None
    )
    return to_str(entry_id)


async def ensure_group(redis: Redis, stream: str, group: str) -> None:
    """Create the consumer group (and the stream) if missing; idempotent."""
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def read_group(
    redis: Redis,
    stream: str,
    group: str,
    consumer: str,
    *,
    count: int,
    block_ms: int | None,
) -> list[StreamMessage]:
    reply: Any = await redis.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block_ms)
    # RESP2: [[stream, entries]]; RESP3: {stream: [entries]}
    groups = reply.items() if isinstance(reply, dict) else (reply or [])
    out: list[StreamMessage] = []
    for _stream, raw_entries in groups:
        entries = raw_entries
        for entry_id, fields in entries:
            if fields is None:  # entry deleted (trimmed) while pending
                continue
            out.append(_decode(entry_id, fields))
    return out


async def autoclaim(
    redis: Redis, stream: str, group: str, consumer: str, *, min_idle_ms: int, count: int
) -> list[StreamMessage]:
    """Take over messages idle in the PEL (crashed consumer or failed processing)."""
    reply: Any = await redis.xautoclaim(stream, group, consumer, min_idle_ms, "0-0", count=count)
    # reply: [next_start_id, [[id, fields], ...], [deleted ids]] (Redis >= 7)
    entries = reply[1] if len(reply) > 1 else []
    return [_decode(eid, fields) for eid, fields in entries if fields]


async def delivery_counts(
    redis: Redis, stream: str, group: str, ids: Sequence[str]
) -> dict[str, int]:
    """Times each pending message has been delivered (XPENDING extended form)."""
    out: dict[str, int] = {}
    for mid in ids:
        rows = await redis.xpending_range(stream, group, min=mid, max=mid, count=1)
        if rows:
            out[mid] = int(rows[0]["times_delivered"])
    return out


async def ack(redis: Redis, stream: str, group: str, ids: Iterable[str]) -> int:
    id_list = list(ids)
    if not id_list:
        return 0
    return int(await redis.xack(stream, group, *id_list))


async def dead_letter(
    redis: Redis,
    dlq: str,
    msg: StreamMessage,
    *,
    source: str,
    reason: str,
    deliveries: int,
) -> str:
    """Copy a poison message to the DLQ with the reason (caller acks the original)."""
    record = {
        "type": "dead_letter",
        "v": 1,
        "source_stream": source,
        "source_id": msg.id,
        "reason": reason[:2000],
        "deliveries": deliveries,
        "dead_at": datetime.now(UTC).isoformat(),
        "raw": msg.raw.get(FIELD, dumps(msg.raw)),
    }
    entry = await xadd_json(redis, dlq, record, maxlen=100_000)
    log.error(
        "message moved to DLQ",
        extra={"stream": source, "id": msg.id, "dlq_id": entry, "reason": reason[:300]},
    )
    return entry


async def group_lag(redis: Redis, stream: str, group: str) -> tuple[int, int] | None:
    """(lag, pending) of a consumer group, or None if the group does not exist."""
    try:
        groups = await redis.xinfo_groups(stream)
    except ResponseError:
        return None
    for g in groups:
        if to_str(g.get("name")) == group:
            lag = g.get("lag")
            return (int(lag) if lag is not None else 0, int(g.get("pending", 0)))
    return None
