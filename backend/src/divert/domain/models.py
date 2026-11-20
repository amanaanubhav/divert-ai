"""Immutable domain value objects for reference data and query results.

These are what repositories return and what services consume; they never carry an
ORM session, so they can be cached per city and shared across requests safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from divert.domain.enums import IncidentType, ServiceType, UnitStatus
from divert.domain.geo import BBox, LatLng


@dataclass(frozen=True, slots=True)
class TrafficProfile:
    hourly_congestion: tuple[float, ...]  # 24 factors, index = local hour
    avg_junction_delay_s: float

    def __post_init__(self) -> None:
        if len(self.hourly_congestion) != 24:
            raise ValueError("hourly_congestion must have 24 entries")
        if not all(0.0 < f <= 1.5 for f in self.hourly_congestion):
            raise ValueError("congestion factors must be in (0, 1.5]")

    def factor_for_hour(self, hour: int) -> float:
        return self.hourly_congestion[hour % 24]


@dataclass(frozen=True, slots=True)
class City:
    id: str
    name: str
    timezone: str
    center: LatLng
    bbox: BBox
    zoom: int
    traffic: TrafficProfile

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True, slots=True)
class Station:
    id: str
    city_id: str
    service: ServiceType
    name: str | None
    pos: LatLng
    osm_ref: str  # "node/123", "way/456", "relation/789"
    emergency: str | None = None  # hospitals: OSM emergency=yes|no (None = untagged)
    healthcare: str | None = None  # hospitals: OSM healthcare=*
    speciality: str | None = None  # hospitals: OSM healthcare:speciality (raw)


@dataclass(frozen=True, slots=True)
class StationAvailability:
    station: Station
    total_units: int
    free_units: int


@dataclass(frozen=True, slots=True)
class Signal:
    id: str
    city_id: str
    name: str | None
    pos: LatLng
    osm_node_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Unit:
    id: str  # callsign, e.g. "AMB-n123456-1"
    station_id: str
    city_id: str
    service: ServiceType
    status: UnitStatus


@dataclass(frozen=True, slots=True)
class StationCandidate:
    """Result of the KNN query: a station that currently has free units."""

    station: Station
    free_units: int
    distance_m: float  # straight-line (geodesic) distance to the incident


@dataclass(frozen=True, slots=True)
class IncidentZone:
    """An active incident, as a source of crowding for other vehicles' routes."""

    incident_id: str
    type: IncidentType
    center: LatLng


@dataclass(frozen=True, slots=True)
class CorridorCandidate:
    """A signal within the corridor buffer of a route (from ST_DWithin)."""

    signal: Signal
    offset_m: float  # geodesic distance from the signal to the route line
