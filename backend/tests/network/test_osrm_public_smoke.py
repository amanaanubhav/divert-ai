"""Smoke test against the PUBLIC OSRM demo (opt-in: ``pytest -m network -s``).

One real trip per city, between the city's nearest real police station and the real
junction farthest from it within 3 km (both from the committed OSM snapshot), fetched
through DIVERT's own :class:`OsrmRouter` with alternatives (plus via-point detours when
OSRM returns a single route), each candidate run through the corridor builder (movements,
lane advice), the ETA under the traffic model and the crowd-aware scoring. Fair use: one
request per city, plus at most two detour requests.
"""

from __future__ import annotations

import pytest

from divert.cities.loader import load_snapshot
from divert.domain.enums import ServiceType
from divert.domain.geo import haversine_m, project_onto_polyline
from divert.domain.models import CorridorCandidate
from divert.services.corridor import build_corridor
from divert.services.eta import KinematicEtaModel
from divert.services.route_choice import choose, score_route, within_length
from divert.services.routing import OsrmRouter
from divert.services.traffic import ModelledTraffic
from tests.support import T0

pytestmark = pytest.mark.network
OSRM = "https://router.project-osrm.org"


@pytest.mark.parametrize("city_id", ["delhi", "kolkata", "bengaluru"])
async def test_public_osrm_alternatives_are_scored(city_id: str) -> None:
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
    router = OsrmRouter(OSRM, timeout_s=20.0, retries=2, alternatives=3, detours=2)
    try:
        routes = await router.alternatives(station.pos, target.pos)
    finally:
        await router.aclose()
    assert routes
    straight = haversine_m(station.pos, target.pos)
    model = KinematicEtaModel()
    candidates = []
    for i in within_length(routes):
        route = routes[i]
        assert route.osrm_distance_m >= straight * 0.95  # a road route is never much shorter
        assert route.length_m == pytest.approx(route.osrm_distance_m, rel=0.05)
        assert len(route.segment_speeds_mps) == len(route.points) - 1
        assert route.intersections, "OSRM intersections are kept for the junction geometry"
        near = []
        for sig in snap.signals:  # brute force, like ST_DWithin
            proj = project_onto_polyline(sig.pos, route.points, route.cumulative)
            if proj.offset_m <= 30.0:
                near.append(CorridorCandidate(sig, proj.offset_m))
        corridor = build_corridor(route, near)
        candidates.append(
            score_route(
                i, route, corridor, eta_model=model, city=snap.city,
                service=ServiceType.POLICE, at=T0, traffic=ModelledTraffic(),
            )
        )  # fmt: skip
    choice = choose(candidates)
    assert any(c.signal_id == target.id for c in choice.chosen.corridor)  # destination junction
    for c in candidates:
        assert 0.3 * c.route.osrm_duration_s < c.eta_s < 3.0 * c.route.osrm_duration_s
    print(f"\n{city_id}: {station.id} -> {target.id} straight={straight:.0f} m, "
          f"{len(routes)} route(s) from OSRM+detours, {len(candidates)} within 1.6x")  # fmt: skip
    for c in candidates:
        moves = " ".join(
            f"{j.movement.value[:5]}{'*' if j.free_left else ''}/{j.lane.value}{j.legs or '?'}"
            for j in c.corridor
        )
        print(
            f"  {c.label} [{c.source}] {c.distance_m:.0f} m base={c.base_s:.0f} s "
            f"eta={c.eta_s:.0f} s junction_delay={c.junction_delay_s:.0f} s "
            f"cost={c.cost_s:.0f} s crowd={c.crowd_index:.2f} rights={c.right_turns} "
            f"signals={len(c.corridor)}: {moves or '-'}"
        )
    print(f"  -> {choice.reason}")
