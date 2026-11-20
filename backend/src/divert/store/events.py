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
    LEG_PLANNED = "leg_planned"  # LegPlanner -> leader, keyed by (dispatch_id, seq)
    ROUTE_REPLACED = "route_replaced"  # re-route, keyed by (dispatch_id, seq, reroute_no)


class HistoryEventType(StrEnum):
    """divert:history (worker -> persister). Each carries a unique ``event_id``."""

    VEHICLE_POSITION = "vehicle_position"
    EN_ROUTE = "en_route"
    PREEMPTION_CLAIMED = "preemption_claimed"
    PREEMPTION_RELEASED = "preemption_released"
    PREEMPTION_CONFLICT = "preemption_conflict"
    PREEMPT_TRANSITION = "preempt_transition"  # red truncation began for the claim
    PREEMPT_GREEN = "preempt_green"  # the claimed phase is green for the vehicle
    PREEMPT_TIMEOUT = "preempt_timeout"  # max hold exceeded; claim released
    ARRIVED = "arrived"  # RESPONSE leg reached the scene
    CLEARED = "cleared"  # scene work done (FIRE / CRIME incident RESOLVED)
    CANCELLED = "cancelled"
    FAILED = "failed"
    # trip legs (design §5)
    LEG_PLANNED = "leg_planned"
    LEG_STARTED = "leg_started"
    LEG_ENDED = "leg_ended"
    LEG_PLAN_FAILED = "leg_plan_failed"
    REROUTED = "rerouted"
    TRANSPORT_STARTED = "transport_started"
    ARRIVED_HOSPITAL = "arrived_hospital"
    HANDOVER_COMPLETE = "handover_complete"  # MEDICAL incident RESOLVED
    RETURNING = "returning"  # the unit is released from the incident
    BACK_IN_SERVICE = "back_in_service"  # unit AVAILABLE, dispatch COMPLETED
    # a cancel the worker could not honour (patient already on board, or the incident was
    # already resolved): the persister restores the incident status the API overwrote
    CANCEL_REJECTED = "cancel_rejected"


class StreamMessageType(StrEnum):
    """WebSocket /api/v1/cities/{city}/stream (and pub/sub divert:events:{city})."""

    SNAPSHOT = "snapshot"
    TICK = "tick"
    VEHICLE_ADDED = "vehicle_added"
    VEHICLE_REMOVED = "vehicle_removed"
    INCIDENT_UPDATED = "incident_updated"
    LEG_CHANGED = "leg_changed"
    ROUTE_REPLACED = "route_replaced"


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
