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


def engine(
    *cities: str,
    scene_time_s: float = 120.0,
    eta_model: Any = None,
    hospital_handover_s: float = 30.0,
) -> SimulationEngine:
    ids = cities or ("delhi",)
    return SimulationEngine(
        {c: city(c) for c in ids},
        eta_model or KinematicEtaModel(),
        SimConfig(
            scene_time_s=scene_time_s,
            position_sample_s=5.0,
            rules=PreemptionRules(),
            hospital_handover_s=hospital_handover_s,
        ),
    )


class Clock:
    """Deterministic wall/sim clock for engine tests."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


# ------------------------------------------------------------------ trip legs (stubs)

STUB_HOSPITAL_OFFSET_M = 800.0  # a stub TRANSPORT leg ends this far north of the scene


def stub_leg_command(
    req: dict[str, Any], *, speed: float = 12.0, corridor_every: int | None = None
) -> dict[str, Any]:
    """A ``leg_planned`` command answering a ``plan_request`` with a straight stub route
    (unit tests only: no OSRM, no PostgreSQL). TRANSPORT ends 800 m north of the scene at
    a stub hospital, RETURN at the unit's home position from the request.
    ``corridor_every``: a stub junction every that many route points (25 m apart)."""
    from divert.domain.enums import DestinationKind, LegKind  # noqa: PLC0415
    from divert.services.legs import destination_json, leg_planned_command  # noqa: PLC0415

    origin = LatLng(*req["origin"])
    kind = LegKind(req["kind"])
    if kind is LegKind.TRANSPORT:
        dest = destination_point(origin, 0.0, STUB_HOSPITAL_OFFSET_M)
        destination = destination_json(DestinationKind.HOSPITAL, dest, "H-stub", "Stub Hospital")
        hospital: dict[str, Any] | None = {
            "id": "H-stub",
            "name": "Stub Hospital",
            "location": [dest.lat, dest.lng],
            "emergency": "yes",
        }
    else:
        dest = LatLng(*req["home"])
        destination = destination_json(
            DestinationKind.STATION, dest, req.get("station_id"), "Home station"
        )
        hospital = None
    route = route_through([origin, dest], step_m=25.0, speed=speed)
    corridor = (
        corridor_at(
            route,
            range(corridor_every, len(route.points) - 1, corridor_every),
            prefix=f"{kind.value[0]}{req['seq']}-",
        )
        if corridor_every
        else ()
    )
    return leg_planned_command(
        city_id=req["city"],
        dispatch_id=req["dispatch_id"],
        incident_id=req["incident_id"],
        vehicle_id=req["vehicle_id"],
        seq=int(req["seq"]),
        kind=kind,
        route=route,
        corridor=corridor,
        destination=destination,
        predicted_eta_s=0.0,
        route_choice=None,
        hospital=hospital,
        planned_at=T0,
    )


def fulfil_plans(
    eng: SimulationEngine,
    batches: dict[str, Any],
    wall: datetime,
    *,
    corridor_every: int | None = None,
) -> int:
    """Answer every plan request of these batches at once (engine-level unit tests)."""
    n = 0
    for b in batches.values():
        for req in b.plan_requests:
            if req.get("type") == "plan_request":
                cmd = stub_leg_command(req, corridor_every=corridor_every)
                eng.apply_leg_planned(cmd, stream_id=None, wall=wall)
                n += 1
    return n
