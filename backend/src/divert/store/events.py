"""Message vocabularies for the streams and the WebSocket (payload schemas: stage 2).

Every message carries ``type`` and ``v`` (schema version). Unknown types must be
ignored by consumers (forward compatibility); a breaking change bumps ``v``.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, Final

SCHEMA_VERSION: Final = 1


class CommandType(StrEnum):
    """divert:commands (outbox relay -> worker leader)."""

    START_DISPATCH = "start_dispatch"
    CANCEL_DISPATCH = "cancel_dispatch"


class HistoryEventType(StrEnum):
    """divert:history (worker -> persister). Each carries a unique ``event_id``."""

    VEHICLE_POSITION = "vehicle_position"
    EN_ROUTE = "en_route"
    PREEMPTION_CLAIMED = "preemption_claimed"
    PREEMPTION_RELEASED = "preemption_released"
    PREEMPTION_CONFLICT = "preemption_conflict"
    ARRIVED = "arrived"
    CLEARED = "cleared"
    CANCELLED = "cancelled"
    FAILED = "failed"


class StreamMessageType(StrEnum):
    """WebSocket /api/v1/cities/{city}/stream (and pub/sub divert:events:{city})."""

    SNAPSHOT = "snapshot"
    TICK = "tick"
    VEHICLE_ADDED = "vehicle_added"
    VEHICLE_REMOVED = "vehicle_removed"
    INCIDENT_UPDATED = "incident_updated"


# The commands stream topic (outbox rows with this topic are relayed to STREAM_COMMANDS).
OUTBOX_TOPIC_COMMANDS: Final = "commands"

_MAX_EVENT_ID = 64  # incident_events.event_id / signal_preemptions.event_id length


def event_id(prefix: str, *parts: object) -> str:
    """Deterministic, unique id of a history event (idempotency key in PostgreSQL).

    The same logical event (e.g. "dispatch X arrived") always gets the same id, so a
    replay after a worker failover is de-duplicated by ``ON CONFLICT DO NOTHING``.
    """
    raw = ":".join([prefix, *(str(p).replace("-", "") for p in parts)])
    if len(raw) <= _MAX_EVENT_ID:
        return raw
    return (
        f"{prefix}:h:{hashlib.sha256(raw.encode()).hexdigest()[: _MAX_EVENT_ID - len(prefix) - 3]}"
    )


def message(type_: str, **fields: Any) -> dict[str, Any]:
    """A stream / pub-sub / WebSocket message: always ``type`` + ``v`` first."""
    return {"type": type_, "v": SCHEMA_VERSION, **fields}
