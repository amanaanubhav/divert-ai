"""Speed model + ETA: caps, braking envelope, analytic integration, ETA vs simulation."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from divert.domain.enums import ServiceType
from divert.services.eta import EtaQuery, KinematicEtaModel, congestion_at
from divert.services.preemption import PreemptionRules
from divert.services.simulation import VehicleStatus
from divert.services.speed_model import (
    BRAKE_MPS2,
    EMERGENCY_ADVANTAGE,
    JUNCTION_NORMAL_MPS,
    JUNCTION_PREEMPTED_MPS,
    KMH,
    PROFILES,
    SpeedProfile,
    build_profile,
    segment_cap_mps,
)
from tests.support import Clock, city, corridor_at, engine, start_cmd, straight_route


def test_vehicle_profiles_match_the_spec() -> None:
    amb, fire, pol = (PROFILES[s] for s in ServiceType)
    assert (amb.max_speed_mps, amb.accel_mps2) == pytest.approx((80 * KMH, 2.0))
    assert (fire.max_speed_mps, fire.accel_mps2) == pytest.approx((60 * KMH, 1.0))
    assert (pol.max_speed_mps, pol.accel_mps2) == pytest.approx((90 * KMH, 2.5))
    assert {p.brake_mps2 for p in PROFILES.values()} == {3.0}
    assert pytest.approx(15 * KMH) == JUNCTION_NORMAL_MPS
    assert pytest.approx(35 * KMH) == JUNCTION_PREEMPTED_MPS


@pytest.mark.parametrize(
    ("osrm", "congestion", "service", "expected"),
    [
        (10.0, 1.0, ServiceType.AMBULANCE, 10.0 * EMERGENCY_ADVANTAGE),
        (10.0, 0.5, ServiceType.AMBULANCE, 10.0 * 0.5 * EMERGENCY_ADVANTAGE),
        (30.0, 1.0, ServiceType.FIRE_ENGINE, 60 * KMH),  # capped at vehicle max
        (30.0, 1.0, ServiceType.POLICE, 90 * KMH),  # 39 m/s capped at 25 m/s
        (0.0, 1.0, ServiceType.POLICE, 1.0 * EMERGENCY_ADVANTAGE),  # zero speed floored
    ],
)
def test_segment_cap(osrm: float, congestion: float, service: ServiceType, expected: float) -> None:
    assert segment_cap_mps(osrm, congestion, PROFILES[service]) == pytest.approx(expected)


def test_constant_cap_time_matches_closed_form() -> None:
    # 1 km, cap 20 m/s, a = 2, b = 3, start and stop at ~rest
    prof = PROFILES[ServiceType.AMBULANCE]
    sp = SpeedProfile([0.0, 1000.0], [20.0], [], prof)
    t, _ = sp.time_from(0.0, 0.0)
    v_end = 1.0  # arrival crawl
    d_acc = 20.0**2 / (2 * 2.0)
    d_brk = (20.0**2 - v_end**2) / (2 * 3.0)
    expected = 20.0 / 2.0 + (1000.0 - d_acc - d_brk) / 20.0 + (20.0 - v_end) / 3.0
    assert t == pytest.approx(expected, rel=1e-9)


def test_braking_envelope_before_a_junction() -> None:
    prof = PROFILES[ServiceType.POLICE]
    sp = SpeedProfile([0.0, 2000.0], [25.0], [(1000.0, JUNCTION_NORMAL_MPS)], prof)
    assert sp.allowed_speed(1000.0) == pytest.approx(JUNCTION_NORMAL_MPS)
    for d in (5.0, 20.0, 50.0, 80.0):
        env = math.sqrt(JUNCTION_NORMAL_MPS**2 + 2 * BRAKE_MPS2 * d)
        assert sp.allowed_speed(1000.0 - d) == pytest.approx(min(25.0, env))
    assert sp.allowed_speed(500.0) == pytest.approx(25.0)
    # simulated with the step function, the vehicle never exceeds the envelope
    x, v = 0.0, 0.0
    while x < 1100.0:
        x, v = sp.step(x, v, 0.5)
        assert v <= sp.allowed_speed(x) + 1e-9


def test_targets_get_times_in_order_and_zero_behind() -> None:
    prof = PROFILES[ServiceType.AMBULANCE]
    sp = SpeedProfile([0.0, 500.0, 1500.0], [15.0, 20.0], [(700.0, JUNCTION_PREEMPTED_MPS)], prof)
    total, at = sp.time_from(100.0, 10.0, [50.0, 700.0, 1200.0])
    assert at[0] == 0.0
    assert 0.0 < at[1] < at[2] < total


def test_target_past_the_route_end_gets_the_arrival_time_not_zero() -> None:
    # Regression: a corridor junction at the destination can project a few cm beyond the
    # last vertex (8873.0 vs 8872.68 m in a real Kolkata run). Its ETA used to stay 0.0, so
    # the vehicle claimed it from its first tick until arrival.
    prof = PROFILES[ServiceType.FIRE_ENGINE]
    sp = SpeedProfile([0.0, 500.0, 1500.0], [15.0, 20.0], [], prof)
    total, at = sp.time_from(0.0, 0.0, [700.0, 1500.0, 1500.32])
    assert 0.0 < at[0] < at[1]
    assert at[1] == pytest.approx(total)
    assert at[2] == pytest.approx(total)


def test_signal_at_the_destination_is_not_claimed_early() -> None:
    prof = PROFILES[ServiceType.FIRE_ENGINE]
    sp = SpeedProfile(
        [0.0, 4000.0, 8872.68], [15.0, 15.0], [(8873.0, JUNCTION_PREEMPTED_MPS)], prof
    )
    _, at = sp.time_from(1922.5, 15.0, [8873.0])
    assert at[0] > 300.0
    assert not PreemptionRules().should_claim(1922.5, 8873.0, at[0])


def test_congestion_uses_city_local_hour() -> None:
    delhi = city("delhi")
    # 03:30 UTC = 09:00 IST
    at = datetime(2026, 11, 17, 3, 30, tzinfo=UTC)
    assert congestion_at(delhi, at) == delhi.traffic.factor_for_hour(9)
    assert congestion_at(delhi, at + timedelta(hours=1)) == delhi.traffic.factor_for_hour(10)


@pytest.mark.parametrize("service", list(ServiceType))
@pytest.mark.parametrize("hour_utc", [0, 4, 12])
def test_simulated_arrival_within_10pct_of_dispatch_eta(
    service: ServiceType, hour_utc: int
) -> None:
    """The ETA predicted at dispatch vs the simulated arrival on a stubbed route."""
    delhi = city("delhi")
    route = straight_route(delhi.center, bearing=30.0, n=80, step_m=40.0, speed=11.0)
    corridor = corridor_at(route, [10, 30, 50, 70])
    clock = Clock(datetime(2026, 11, 17, hour_utc, 15, tzinfo=UTC))
    model = KinematicEtaModel()
    eta = model.estimate(
        EtaQuery(route, service, delhi, corridor, along_m=0.0, speed_mps=0.0, at=clock.now)
    ).to_destination_s
    eng = engine("delhi", eta_model=model)
    v = eng.start_dispatch(
        start_cmd(route, corridor, service=service, predicted_eta_s=eta),
        stream_id=None, wall=clock.now, sim=clock.now,
    )  # fmt: skip
    assert v is not None
    while v.status is VehicleStatus.EN_ROUTE:
        now = clock.advance(0.5)
        eng.tick(dt_sim=0.5, sim=now, wall=now)
        assert v.elapsed_s < 3 * eta
    assert v.travel_s is not None
    assert v.travel_s == pytest.approx(eta, rel=0.10)
    assert v.preempted_passed == 4  # every corridor junction was pre-empted for it


def test_unpreempted_junction_slows_the_vehicle() -> None:
    delhi = city("delhi")
    route = straight_route(delhi.center, n=40, step_m=50.0, speed=14.0)
    junction = route.cumulative[20]
    fast = build_profile(route, ServiceType.AMBULANCE, 1.0, [(junction, True)])
    slow = build_profile(route, ServiceType.AMBULANCE, 1.0, [(junction, False)])
    assert slow.allowed_speed(junction) < fast.allowed_speed(junction)
    assert slow.time_from(0.0, 0.0)[0] > fast.time_from(0.0, 0.0)[0]
