"""Shared test helpers.

Routes built here are *stubs* for unit tests (straight polylines from a real city
centre / real station with a fixed OSRM-like speed); they are never written to the
committed city data.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from divert.cities.loader import load_snapshot
from divert.domain.enums import Lane, ServiceType
from divert.domain.geo import LatLng, destination_point
from divert.domain.models import City
from divert.domain.route import CorridorSignal, Route
from divert.services.eta import KinematicEtaModel
from divert.services.preemption import PreemptionRules
from divert.services.simulation import SimConfig, SimulationEngine

T0 = datetime(2026, 11, 17, 6, 30, tzinfo=UTC)  # 12:00 IST


def city(city_id: str = "delhi") -> City:
    return load_snapshot(city_id).city


def straight_route(
    start: LatLng, bearing: float = 45.0, n: int = 40, step_m: float = 50.0, speed: float = 12.0
) -> Route:
    pts = [start]
    for _ in range(n):
        pts.append(destination_point(pts[-1], bearing, step_m))
    return Route.build(pts, [speed] * n)


def route_through(points: Sequence[LatLng], step_m: float = 25.0, speed: float = 12.0) -> Route:
    """Polyline through the given points, densified every ``step_m``."""
    from divert.domain.geo import haversine_m, initial_bearing_deg  # noqa: PLC0415

    pts: list[LatLng] = [points[0]]
    for a, b in itertools.pairwise(points):
        d = haversine_m(a, b)
        brg = initial_bearing_deg(a, b)
        k = max(1, int(d // step_m))
        for i in range(1, k):
            pts.append(destination_point(a, brg, d * i / k))
        pts.append(b)
    return Route.build(pts, [speed] * (len(pts) - 1))


def corridor_at(
    route: Route, indices: Sequence[int], prefix: str = "S"
) -> tuple[CorridorSignal, ...]:
    return tuple(
        CorridorSignal(
            signal_id=f"{prefix}{i}",
            pos=route.points[i],
            along_m=route.cumulative[i],
            offset_m=0.0,
            lane=Lane.RIGHT,
            approach_bearing=0.0,
        )
        for i in indices
    )


def start_cmd(
    route: Route,
    corridor: Sequence[CorridorSignal] = (),
    *,
    city_id: str = "delhi",
    vehicle_id: str = "AMB-1",
    service: ServiceType = ServiceType.AMBULANCE,
    dispatch_id: str | None = None,
    incident_id: str | None = None,
    predicted_eta_s: float = 0.0,
) -> dict[str, Any]:
    return {
        "type": "start_dispatch",
        "v": 1,
        "city": city_id,
        "dispatch_id": dispatch_id or str(uuid.uuid4()),
        "incident_id": incident_id or str(uuid.uuid4()),
        "vehicle_id": vehicle_id,
        "station_id": "st-1",
        "service": service.value,
        "incident_type": "MEDICAL",
        "route": route.to_json(),
        "corridor": [c.to_json() for c in corridor],
        "destination": [route.destination.lat, route.destination.lng],
        "predicted_eta_s": predicted_eta_s,
    }


def engine(*cities: str, scene_time_s: float = 120.0, eta_model: Any = None) -> SimulationEngine:
    ids = cities or ("delhi",)
    return SimulationEngine(
        {c: city(c) for c in ids},
        eta_model or KinematicEtaModel(),
        SimConfig(scene_time_s=scene_time_s, position_sample_s=5.0, rules=PreemptionRules()),
    )


class Clock:
    """Deterministic wall/sim clock for engine tests."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now
