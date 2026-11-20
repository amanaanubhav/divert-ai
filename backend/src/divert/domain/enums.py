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
    AVAILABLE = "AVAILABLE"
    DISPATCHED = "DISPATCHED"
    ON_SCENE = "ON_SCENE"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    DISPATCHED = "DISPATCHED"
    EN_ROUTE = "EN_ROUTE"
    ON_SCENE = "ON_SCENE"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


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


class SignalState(StrEnum):
    NORMAL = "NORMAL"
    PREEMPTED = "PREEMPTED"
    CONFLICT = "CONFLICT"


class Lane(StrEnum):
    """Lane advisory. India drives on the left: left turn -> L; straight/right/U -> R."""

    LEFT = "L"
    RIGHT = "R"


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

# Conflict tie-break priority (lower wins) when ETAs are within the tie window.
SERVICE_PRIORITY: Final[dict[ServiceType, int]] = {
    ServiceType.AMBULANCE: 0,
    ServiceType.FIRE_ENGINE: 1,
    ServiceType.POLICE: 2,
}

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
