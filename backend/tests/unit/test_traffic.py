"""Traffic model (design §2): class-aware congestion, incident zones, crowd index,
corridor contention, and the plug-in seam."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from divert.domain.enums import IncidentType, RoadClass
from divert.domain.geo import destination_point
from divert.domain.lanes import KMH
from divert.domain.models import IncidentZone
from divert.domain.route import Route
from divert.services.traffic import (
    FREE_FLOW,
    UNCONGESTED,
    ModelledTraffic,
    TrafficModel,
    city_factor,
    class_factor,
    contention_junctions,
    crowd_index,
)
from tests.support import city, straight_route

AT = datetime(2026, 11, 17, 12, 30, tzinfo=UTC)  # 18:00 IST, the evening peak


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        (RoadClass.EXPRESSWAY, 0.5),
        (RoadClass.ARTERIAL, 0.5),
        (RoadClass.COLLECTOR, 1 - 0.7 * 0.5),
        (RoadClass.LOCAL, 1 - 0.5 * 0.5),
    ],
)
def test_class_sensitivity(cls: RoadClass, expected: float) -> None:
    assert class_factor(0.5, cls) == pytest.approx(expected)
    assert class_factor(1.0, cls) == 1.0  # no peak, no drop


def test_segment_factors_follow_the_road_class_of_each_segment() -> None:
    delhi = city()
    f = city_factor(delhi, AT)
    assert f == delhi.traffic.factor_for_hour(18)
    pts = straight_route(delhi.center, n=3).points
    route = Route.build(pts, [80 * KMH, 55 * KMH, 20 * KMH])  # expressway, arterial, local
    factors = FREE_FLOW.segment_factors(route, delhi, AT)
    assert factors == pytest.approx((f, f, 1 - 0.5 * (1 - f)))
    assert UNCONGESTED.segment_factors(route, delhi, AT) == (1.0, 1.0, 1.0)


def test_incident_zones_slow_only_nearby_segments_and_never_the_vehicle_s_own() -> None:
    delhi = city()
    route = straight_route(delhi.center, bearing=90.0, n=40, step_m=50.0, speed=11.0)
    fire = IncidentZone(
        "fire-1", IncidentType.FIRE, destination_point(route.points[20], 0.0, 100.0)
    )
    med = IncidentZone("med-1", IncidentType.MEDICAL, destination_point(route.points[5], 0.0, 80.0))
    model = ModelledTraffic([fire, med])
    zf = model.zone_factors(route)
    hit = [i for i, z in enumerate(zf) if z < 1.0]
    assert hit
    assert all(z == pytest.approx(0.3) for z in (zf[i] for i in hit))
    # 250 m radius, 100 m off the road: about +-229 m along it -> ~9-10 segments of 50 m
    assert 8 <= len(hit) <= 11
    assert all(14 <= i <= 25 for i in hit)
    assert model.zone_factors(route, exempt_incident_id="fire-1") == [1.0] * 40  # own zone
    assert [z.incident_id for z in model.zone_hits(route)] == ["fire-1"]  # 50 m MEDICAL: missed
    combined = model.segment_factors(route, delhi, AT)
    base = FREE_FLOW.segment_factors(route, delhi, AT)
    assert combined[hit[0]] == pytest.approx(base[hit[0]] * 0.3)
    assert combined[0] == pytest.approx(base[0])


def test_cache_key_changes_with_hour_and_zones() -> None:
    delhi = city()
    a = ModelledTraffic([], version=1)
    b = ModelledTraffic([], version=2)
    assert a.cache_key(delhi, AT) != b.cache_key(delhi, AT)
    night = datetime(2026, 11, 17, 21, 30, tzinfo=UTC)  # 03:00 IST
    assert a.cache_key(delhi, AT) != a.cache_key(delhi, night)


def test_crowd_index() -> None:
    route = straight_route(city().center, n=4, step_m=100.0)
    assert crowd_index(route, [1.0, 1.0, 1.0, 1.0]) == 0.0
    assert crowd_index(route, [0.5, 0.5, 1.0, 1.0]) == pytest.approx(0.25)
    assert crowd_index(route, [1.4, 0.0, 1.0, 1.0]) == pytest.approx(0.25)  # clipped


def test_contention_within_20_s() -> None:
    own = [("J1", 30.0), ("J2", 60.0), ("J3", 90.0)]
    others = [("FIRE-1", "J1", 49.0), ("PCR-1", "J2", 81.0), ("AMB-2", "J3", 91.0)]
    assert contention_junctions(own, others) == ["J1", "J3"]
    assert contention_junctions(own, []) == []


def test_the_shipped_models_satisfy_the_protocol() -> None:
    models: list[TrafficModel] = [FREE_FLOW, UNCONGESTED, ModelledTraffic()]
    assert [m.name for m in models] == ["modelled-v1", "free-flow", "modelled-v1"]
