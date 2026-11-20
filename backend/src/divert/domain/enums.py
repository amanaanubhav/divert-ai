"""Domain enumerations shared by the database, the API and the simulation."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class IncidentType(StrEnum):
    MEDICAL = "MEDICAL"
    FIRE = "FIRE"
    CRIME = "CRIME"


class ServiceType(StrEnum):
    """Which kind of station/vehicle answers an incident."""

    AMBULANCE = "AMBULANCE"
    FIRE_ENGINE = "FIRE_ENGINE"
    POLICE = "POLICE"


class UnitStatus(StrEnum):
    """Unit (vehicle) status (design §1): AVAILABLE -> DISPATCHED -> ON_SCENE ->
    [TRANSPORTING -> AT_HOSPITAL] -> RETURNING -> AVAILABLE, plus OUT_OF_SERVICE."""

    AVAILABLE = "AVAILABLE"
    DISPATCHED = "DISPATCHED"
    ON_SCENE = "ON_SCENE"
    TRANSPORTING = "TRANSPORTING"
    AT_HOSPITAL = "AT_HOSPITAL"
    RETURNING = "RETURNING"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"


# A unit in one of these is on a dispatch (units.current_dispatch_id is set).
BUSY_UNIT_STATUSES: Final = frozenset(
    {
        UnitStatus.DISPATCHED,
        UnitStatus.ON_SCENE,
        UnitStatus.TRANSPORTING,
        UnitStatus.AT_HOSPITAL,
        UnitStatus.RETURNING,
    }
)


class IncidentStatus(StrEnum):
    """DISPATCHED -> EN_ROUTE -> ON_SCENE -> [TRANSPORTING -> AT_HOSPITAL] -> RESOLVED,
    plus CANCELLED and FAILED (OPEN: reported, nothing sent yet)."""

    OPEN = "OPEN"
    DISPATCHED = "DISPATCHED"
    EN_ROUTE = "EN_ROUTE"
    ON_SCENE = "ON_SCENE"
    TRANSPORTING = "TRANSPORTING"
    AT_HOSPITAL = "AT_HOSPITAL"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


# Incidents that still occupy their scene (crowding zones, design §2). A MEDICAL incident
# in TRANSPORTING / AT_HOSPITAL has left the scene.
ACTIVE_INCIDENT_STATUSES: Final = frozenset(
    {
        IncidentStatus.OPEN,
        IncidentStatus.DISPATCHED,
        IncidentStatus.EN_ROUTE,
        IncidentStatus.ON_SCENE,
    }
)
TERMINAL_INCIDENT_STATUSES: Final = frozenset(
    {IncidentStatus.RESOLVED, IncidentStatus.CANCELLED, IncidentStatus.FAILED}
)
# A patient is on board (or being handed over): cancelling is refused (design §1).
TRANSPORT_INCIDENT_STATUSES: Final = frozenset(
    {IncidentStatus.TRANSPORTING, IncidentStatus.AT_HOSPITAL}
)
NON_TERMINAL_INCIDENT_STATUSES: Final = ACTIVE_INCIDENT_STATUSES | TRANSPORT_INCIDENT_STATUSES


class DispatchStatus(StrEnum):
    """One dispatch = the whole trip of one unit (design §5), ending COMPLETED when the
    unit is back in service (or FAILED). Ordered: a status only moves forward."""

    DISPATCHED = "DISPATCHED"
    ON_SCENE = "ON_SCENE"
    TRANSPORTING = "TRANSPORTING"
    AT_HOSPITAL = "AT_HOSPITAL"
    RETURNING = "RETURNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


DISPATCH_STATUS_ORDER: Final[tuple[DispatchStatus, ...]] = (
    DispatchStatus.DISPATCHED,
    DispatchStatus.ON_SCENE,
    DispatchStatus.TRANSPORTING,
    DispatchStatus.AT_HOSPITAL,
    DispatchStatus.RETURNING,
    DispatchStatus.COMPLETED,
)


def dispatch_statuses_before(status: DispatchStatus) -> tuple[DispatchStatus, ...]:
    """Statuses a dispatch may move to ``status`` from (forward only; FAILED is final)."""
    if status is DispatchStatus.FAILED:
        return DISPATCH_STATUS_ORDER[:-1]
    return DISPATCH_STATUS_ORDER[: DISPATCH_STATUS_ORDER.index(status)]


class DestinationKind(StrEnum):
    """Where the current leg ends."""

    SCENE = "SCENE"
    HOSPITAL = "HOSPITAL"
    STATION = "STATION"


class LegEndReason(StrEnum):
    """How a leg ended (``dispatch_legs.end_reason``)."""

    ARRIVED = "ARRIVED"
    CANCELLED = "CANCELLED"  # the incident was cancelled while the leg was driven
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"  # planned, never started (cancel while it was pending)


class SignalState(StrEnum):
    """Per-vehicle claim label (persisted in ``signal_preemptions.state``).

    PREEMPTED: the claimant's phase is (or is being made) green for it; CONFLICT: it lost
    the junction to an incompatible claimant and is queued. The junction's own state is
    :class:`JunctionState`.
    """

    NORMAL = "NORMAL"
    PREEMPTED = "PREEMPTED"
    CONFLICT = "CONFLICT"


class JunctionState(StrEnum):
    """State of a signalised junction (design §4.3); NORMAL is never stored or streamed."""

    NORMAL = "NORMAL"  # running its fixed-time plan
    TRANSITIONING = "TRANSITIONING"  # clearing the current phase toward the claimed one
    PREEMPTED = "PREEMPTED"  # the claimed phase is held green for the winner
    QUEUED = "QUEUED"  # served, and an incompatible claimant waits for its turn
    RECOVERING = "RECOVERING"  # released: amber + all-red, then the most-starved approach


class Lane(StrEnum):
    """Which side of the carriageway to be on (``LaneAdvice.side``). India drives on the
    left: lane 1 (kerb) is L, the median-side lane is R, a middle lane - or the single
    lane of a one-lane road when going straight - is C."""

    LEFT = "L"
    RIGHT = "R"
    CENTRE = "C"


class Movement(StrEnum):
    """Turn a vehicle makes at a junction (design §3). Negative turn angle = left."""

    STRAIGHT = "STRAIGHT"
    SLIGHT_LEFT = "SLIGHT_LEFT"
    LEFT = "LEFT"
    SHARP_LEFT = "SHARP_LEFT"
    SLIGHT_RIGHT = "SLIGHT_RIGHT"
    RIGHT = "RIGHT"
    SHARP_RIGHT = "SHARP_RIGHT"
    U_TURN = "U_TURN"
    ROUNDABOUT = "ROUNDABOUT"


class RoadClass(StrEnum):
    """Road class estimated from the OSRM free-flow speed (design §2)."""

    EXPRESSWAY = "EXPRESSWAY"
    ARTERIAL = "ARTERIAL"
    COLLECTOR = "COLLECTOR"
    LOCAL = "LOCAL"


class LegKind(StrEnum):
    """A trip leg (design §1): RESPONSE station -> scene, TRANSPORT scene -> hospital
    (AMBULANCE on MEDICAL), RETURN -> home station (obeys signals)."""

    RESPONSE = "RESPONSE"
    TRANSPORT = "TRANSPORT"
    RETURN = "RETURN"


EMERGENCY_LEGS: Final = frozenset({LegKind.RESPONSE, LegKind.TRANSPORT})


def next_leg_kind(
    current: LegKind, *, service: ServiceType, incident_type: IncidentType | None, cancelled: bool
) -> LegKind | None:
    """The leg that follows ``current`` (None after RETURN): an ambulance on a MEDICAL
    incident takes the patient to hospital unless the incident was cancelled."""
    if current is LegKind.RETURN:
        return None
    if (
        current is LegKind.RESPONSE
        and not cancelled
        and service is ServiceType.AMBULANCE
        and incident_type is IncidentType.MEDICAL
    ):
        return LegKind.TRANSPORT
    return LegKind.RETURN


class PreemptOutcome(StrEnum):
    """How a claim ended (design §4.6)."""

    CLEARED = "CLEARED"  # the vehicle crossed (or reached its destination in the box)
    TIMEOUT = "TIMEOUT"  # max hold exceeded; the vehicle may re-claim later
    CANCELLED = "CANCELLED"  # dispatch cancelled / failed / superseded
    REROUTED = "REROUTED"  # the junction is not on the new route (stage B)


class PhaseStateAtClaim(StrEnum):
    """Was the claimed phase already green (hold it) or red (truncate the others)?"""

    GREEN_EXTEND = "green_extend"
    RED_TRUNCATE = "red_truncate"


SERVICE_FOR_INCIDENT: Final[dict[IncidentType, ServiceType]] = {
    IncidentType.MEDICAL: ServiceType.AMBULANCE,
    IncidentType.FIRE: ServiceType.FIRE_ENGINE,
    IncidentType.CRIME: ServiceType.POLICE,
}

# Accepted spellings (case-insensitive). POLICE must keep working: it was the
# dashboard's name for CRIME and a regression fixed in the prototype.
INCIDENT_TYPE_ALIASES: Final[dict[str, IncidentType]] = {
    "MEDICAL": IncidentType.MEDICAL,
    "HEALTH": IncidentType.MEDICAL,
    "AMBULANCE": IncidentType.MEDICAL,
    "FIRE": IncidentType.FIRE,
    "CRIME": IncidentType.CRIME,
    "POLICE": IncidentType.CRIME,
}

# Service order (lower first) used where only the service is known.
SERVICE_PRIORITY: Final[dict[ServiceType, int]] = {
    ServiceType.AMBULANCE: 0,
    ServiceType.FIRE_ENGINE: 1,
    ServiceType.POLICE: 2,
}

# Conflict priority ranks (design §4.4; lower wins inside DIVERT_PRIORITY_WINDOW_S).
RANK_PATIENT_ON_BOARD: Final = 0  # AMBULANCE on TRANSPORT
RANK_AMBULANCE: Final = 1  # AMBULANCE on RESPONSE
RANK_FIRE_ENGINE: Final = 2
RANK_POLICE: Final = 3


def priority_rank(service: ServiceType, leg: LegKind = LegKind.RESPONSE) -> int:
    """0 ambulance with a patient, 1 ambulance responding, 2 fire engine, 3 police."""
    if service is ServiceType.AMBULANCE:
        return RANK_PATIENT_ON_BOARD if leg is LegKind.TRANSPORT else RANK_AMBULANCE
    if service is ServiceType.FIRE_ENGINE:
        return RANK_FIRE_ENGINE
    return RANK_POLICE


CALLSIGN_PREFIX: Final[dict[ServiceType, str]] = {
    ServiceType.AMBULANCE: "AMB",
    ServiceType.FIRE_ENGINE: "FIRE",
    ServiceType.POLICE: "PCR",
}


def normalize_incident_type(raw: str) -> IncidentType:
    """Resolve a reported type (any case, aliases allowed). Raises ValueError otherwise.

    Must be called before anything is created for a dispatch (regression guarantee 1).
    """
    key = str(raw).strip().upper()
    try:
        return INCIDENT_TYPE_ALIASES[key]
    except KeyError:
        valid = ", ".join(sorted(INCIDENT_TYPE_ALIASES))
        raise ValueError(f"unknown incident type {raw!r}; valid: {valid}") from None
