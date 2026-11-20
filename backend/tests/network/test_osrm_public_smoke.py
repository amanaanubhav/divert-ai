"""Smoke test against the PUBLIC OSRM demo (opt-in: ``pytest -m network -s``).

One real route per city, between the city's nearest real police station and the real
junction farthest from it within 3 km (both from the committed OSM snapshot), fetched
through DIVERT's own :class:`OsrmRouter` (request shape, retries, parsing) and run
through the speed/ETA model and the corridor builder. Fair use: three requests per run.
"""

from __future__ import annotations

import pytest

from divert.cities.loader import load_snapshot
from divert.domain.enums import ServiceType
from divert.domain.geo import haversine_m, project_onto_polyline
from divert.domain.models import CorridorCandidate
from divert.services.dispatch import build_corridor
from divert.services.eta import EtaQuery, KinematicEtaModel
from divert.services.routing import OsrmRouter
from tests.support import T0

pytestmark = pytest.mark.network
OSRM = "https://router.project-osrm.org"


@pytest.mark.parametrize("city_id", ["delhi", "kolkata", "bengaluru"])
async def test_public_osrm_routes_between_real_points(city_id: str) -> None:
    snap = load_snapshot(city_id)
    center = snap.city.center
    station = min(
        (s for s in snap.stations if s.service is ServiceType.POLICE),
        key=lambda s: haversine_m(center, s.pos),
    )
    target = max(
        (g for g in snap.signals if haversine_m(station.pos, g.pos) <= 3000),
        key=lambda g: haversine_m(station.pos, g.pos),
    )
    router = OsrmRouter(OSRM, timeout_s=20.0, retries=2)
    try:
        route = await router.route(station.pos, target.pos)
    finally:
        await router.aclose()
    straight = haversine_m(station.pos, target.pos)
    assert route.osrm_distance_m >= straight * 0.95  # a road route is never much shorter
    assert route.length_m == pytest.approx(route.osrm_distance_m, rel=0.05)  # haversine vs OSRM
    assert len(route.segment_speeds_mps) == len(route.points) - 1
    assert all(s >= 0 for s in route.segment_speeds_mps)
    assert any(m.type == "depart" for m in route.maneuvers)
    assert any(m.type == "arrive" for m in route.maneuvers)
    # corridor: real signals within 30 m of the real route (brute force, like ST_DWithin)
    near = []
    for sig in snap.signals:
        proj = project_onto_polyline(sig.pos, route.points, route.cumulative)
        if proj.offset_m <= 30.0:
            near.append(CorridorCandidate(sig, proj.offset_m))
    corridor = build_corridor(route, near)
    assert any(c.signal_id == target.id for c in corridor)  # the destination junction
    eta = KinematicEtaModel().estimate(
        EtaQuery(route, ServiceType.POLICE, snap.city, corridor, 0.0, 0.0, T0)
    )
    assert 0.3 * route.osrm_duration_s < eta.to_destination_s < 3.0 * route.osrm_duration_s
    lanes = "".join(c.lane.value for c in corridor)
    print(
        f"\n{city_id}: {station.id} -> {target.id} straight={straight:.0f} m "
        f"road={route.osrm_distance_m:.0f} m pts={len(route.points)} "
        f"osrm_eta={route.osrm_duration_s:.0f} s divert_eta={eta.to_destination_s:.0f} s "
        f"(12:00 IST) corridor={len(corridor)} signals lanes={lanes or '-'} "
        f"maneuvers={len(route.maneuvers)}"
    )
