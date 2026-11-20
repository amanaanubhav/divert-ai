"""Traffic: how crowded each road segment is (design §2). Pure - no I/O.

:class:`TrafficModel` is the plug-in point. Its one job is a speed multiplier per route
segment (1 = free flow, lower = slower) at a given instant; the ETA model and the
simulation both apply exactly these multipliers, which is what keeps predicted and
simulated travel times consistent.

The shipped :class:`ModelledTraffic` is a *model*, not a measurement:

* **class-aware time-of-day congestion** - the city's hourly factor ``f_city`` scaled by
  road class: ``f_class = 1 - sensitivity x (1 - f_city)``, sensitivity 1.0 for
  EXPRESSWAY / ARTERIAL, 0.7 COLLECTOR, 0.5 LOCAL (big roads take the peak drop fully);
* **active incident zones** - FIRE 250 m x0.3, CRIME 150 m x0.6, MEDICAL 50 m x0.85 on
  every segment that comes within the radius of another active incident (the vehicle's
  own incident is exempt).

Live feed: a TomTom / HERE / Google flow feed plugs in as another ``TrafficModel`` whose
``segment_factors`` map-matches the route to the feed's flow segments and returns
``current_speed / free_flow_speed`` per segment (needs an API key, costs money, and its
``cache_key`` must change whenever the feed refreshes). Nothing else changes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from divert.domain.enums import IncidentType, RoadClass
from divert.domain.geo import haversine_m, project_onto_polyline
from divert.domain.lanes import road_class
from divert.domain.models import City, IncidentZone
from divert.domain.route import Route

# Peak sensitivity by road class (assumption): share of the city's congestion drop a road
# of that class takes.
CLASS_SENSITIVITY: Final[dict[RoadClass, float]] = {
    RoadClass.EXPRESSWAY: 1.0,
    RoadClass.ARTERIAL: 1.0,
    RoadClass.COLLECTOR: 0.7,
    RoadClass.LOCAL: 0.5,
}


@dataclass(frozen=True, slots=True)
class ZoneRule:
    radius_m: float
    factor: float


# Active incident zones (assumptions, design §2): appliances and crowds block lanes.
ZONE_RULES: Final[dict[IncidentType, ZoneRule]] = {
    IncidentType.FIRE: ZoneRule(250.0, 0.3),
    IncidentType.CRIME: ZoneRule(150.0, 0.6),
    IncidentType.MEDICAL: ZoneRule(50.0, 0.85),
}
# Corridor contention: another emergency vehicle expected at the same junction within
# +-20 s costs about the priority window + one transition (design §2: "~10 s").
_M_PER_DEG_LAT: Final = 111_320.0  # metres per degree of latitude (bounding-box test only)
CONTENTION_WINDOW_S: Final = 20.0
CONTENTION_DELAY_S: Final = 10.0


def city_factor(city: City, at: datetime) -> float:
    """The city's hourly congestion factor at the city-local hour of ``at``."""
    return city.traffic.factor_for_hour(at.astimezone(city.tz).hour)


def class_factor(f_city: float, cls: RoadClass) -> float:
    return 1.0 - CLASS_SENSITIVITY[cls] * (1.0 - f_city)


def zone_rule(zone: IncidentZone) -> ZoneRule:
    return ZONE_RULES[zone.type]


class TrafficModel(Protocol):
    """Speed multiplier per route segment; see the module doc for the live-feed seam."""

    name: str

    def cache_key(self, city: City, at: datetime) -> tuple[object, ...]:
        """Equal keys => equal ``segment_factors`` for the same route (profile caching)."""
        ...

    def segment_factors(
        self, route: Route, city: City, at: datetime, *, exempt_incident_id: str | None = None
    ) -> tuple[float, ...]: ...

    def zone_hits(
        self, route: Route, *, exempt_incident_id: str | None = None
    ) -> list[IncidentZone]: ...


def zone_segments(route: Route, zone: IncidentZone) -> list[int]:
    """Indices of segments that come within the zone radius of its centre."""
    radius = zone_rule(zone).radius_m
    c = zone.center
    pad_lat = radius / _M_PER_DEG_LAT
    pad_lng = radius / (_M_PER_DEG_LAT * max(0.01, math.cos(math.radians(c.lat))))
    pts = route.points
    out: list[int] = []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        # cheap reject: the segment's bounding box, grown by the radius, misses the centre
        if not (
            min(a.lat, b.lat) - pad_lat <= c.lat <= max(a.lat, b.lat) + pad_lat
            and min(a.lng, b.lng) - pad_lng <= c.lng <= max(a.lng, b.lng) + pad_lng
        ):
            continue
        if project_onto_polyline(c, (a, b), (0.0, haversine_m(a, b))).offset_m <= radius:
            out.append(i)
    return out


class ModelledTraffic:
    """Class-aware hourly congestion x active incident zones (see module doc)."""

    name = "modelled-v1"

    def __init__(self, zones: Sequence[IncidentZone] = (), *, version: object = None) -> None:
        self.zones: tuple[IncidentZone, ...] = tuple(zones)
        # part of the profile cache key: a counter the owner bumps when the zones change,
        # or (default) the zones themselves
        self.version = (
            version
            if version is not None
            else tuple((z.incident_id, z.type.value, z.center.lat, z.center.lng) for z in zones)
        )

    def cache_key(self, city: City, at: datetime) -> tuple[object, ...]:
        return (self.name, city.id, round(city_factor(city, at), 6), self.version)

    def class_factors(self, route: Route, city: City, at: datetime) -> list[float]:
        f_city = city_factor(city, at)
        return [class_factor(f_city, road_class(s)) for s in route.segment_speeds_mps]

    def zone_factors(self, route: Route, *, exempt_incident_id: str | None = None) -> list[float]:
        out = [1.0] * len(route.segment_speeds_mps)
        for zone in self.zones:
            if zone.incident_id == exempt_incident_id:
                continue
            for i in zone_segments(route, zone):
                out[i] = min(out[i], zone_rule(zone).factor)
        return out

    def segment_factors(
        self, route: Route, city: City, at: datetime, *, exempt_incident_id: str | None = None
    ) -> tuple[float, ...]:
        cls = self.class_factors(route, city, at)
        zones = self.zone_factors(route, exempt_incident_id=exempt_incident_id)
        return tuple(c * z for c, z in zip(cls, zones, strict=True))

    def zone_hits(
        self, route: Route, *, exempt_incident_id: str | None = None
    ) -> list[IncidentZone]:
        return [
            z for z in self.zones if z.incident_id != exempt_incident_id and zone_segments(route, z)
        ]


class UncongestedTraffic:
    """Every segment at its free-flow speed (the "base time" of route choice)."""

    name = "free-flow"

    def cache_key(self, city: City, at: datetime) -> tuple[object, ...]:
        return (self.name,)

    def segment_factors(
        self, route: Route, city: City, at: datetime, *, exempt_incident_id: str | None = None
    ) -> tuple[float, ...]:
        return (1.0,) * len(route.segment_speeds_mps)

    def zone_hits(
        self, route: Route, *, exempt_incident_id: str | None = None
    ) -> list[IncidentZone]:
        return []


FREE_FLOW: Final = ModelledTraffic()  # class-aware congestion, no incident zones
UNCONGESTED: Final = UncongestedTraffic()


def crowd_index(route: Route, factors: Sequence[float]) -> float:
    """Length-weighted mean of (1 - speed factor), clipped to [0, 1]: 0 free, 1 jammed."""
    total = route.length_m
    if total <= 0 or not factors:
        return 0.0
    acc = 0.0
    for i, f in enumerate(factors):
        seg = route.cumulative[i + 1] - route.cumulative[i]
        acc += seg * min(1.0, max(0.0, 1.0 - f))
    return acc / total


def contention_junctions(
    own_etas_s: Sequence[tuple[str, float]],
    others: Sequence[tuple[str, str, float]],
    *,
    window_s: float = CONTENTION_WINDOW_S,
) -> list[str]:
    """Junctions another active emergency vehicle is expected at within +-window of us.

    ``own_etas_s``: (signal_id, our ETA); ``others``: (vehicle_id, signal_id, its ETA),
    both relative to now.
    """
    by_signal: dict[str, list[float]] = {}
    for _, sid, eta in others:
        by_signal.setdefault(sid, []).append(eta)
    out = []
    for sid, eta in own_etas_s:
        if any(math.isfinite(o) and abs(o - eta) <= window_s for o in by_signal.get(sid, [])):
            out.append(sid)
    return out
