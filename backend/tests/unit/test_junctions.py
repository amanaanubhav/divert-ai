"""Junction control (design §4.1, §4.3, §4.5): green extension vs red truncation, signal
states, re-sequencing, release and recovery, starvation, geometry registry."""

from __future__ import annotations

import math

import pytest

from divert.domain.enums import JunctionState, Lane, PhaseStateAtClaim
from divert.domain.geo import LatLng
from divert.domain.route import CorridorSignal
from divert.domain.signal_plan import JunctionGeometry, Stage
from divert.services.junctions import (
    IDLE_TTL_S,
    JunctionControl,
    JunctionRegistry,
)
from divert.services.preemption import transition_s

GEO4 = JunctionGeometry.synthetic(0.0, 4)


def control(offset_s: float = 0.0) -> tuple[JunctionControl, float]:
    """A 4-phase control (slot 30 s: green 25, amber 3, all-red 2) and a cycle start."""
    ctl = JunctionControl("J", GEO4)
    ctl.plan = type(ctl.plan)("J", 4, 120.0, offset_s)
    base = 1_795_000_000.0
    return ctl, base - (base % 120.0)


def test_green_extension_holds_the_current_green() -> None:
    ctl, base = control()
    t = base + 20.0  # phase 0 green, 5 s left in the plan
    green_at = ctl.serve(0, t)
    assert green_at == t
    assert ctl.state_at_claim is PhaseStateAtClaim.GREEN_EXTEND
    assert ctl.state(t) is JunctionState.PREEMPTED
    assert ctl.is_green_for(0, t + 60.0)  # held well past its planned end
    assert not ctl.is_green_for(1, t + 60.0)
    assert ctl.next_green_at(1, t + 60.0) == math.inf  # held for another approach


@pytest.mark.parametrize(
    ("into_slot", "expected_transition"),
    [
        (4.0, 6.0 + 5.0),  # mid minimum green: 6 s more green, amber, all-red
        (15.0, 5.0),  # minimum served: amber + all-red
        (26.0, 2.0 + 2.0),  # amber
        (28.5, 1.5),  # all-red
    ],
)
def test_red_truncation_matches_the_transition_formula(
    into_slot: float, expected_transition: float
) -> None:
    ctl, base = control()
    t = base + into_slot
    assert transition_s(ctl.status(t), 2) == pytest.approx(expected_transition)
    green_at = ctl.serve(2, t)
    assert green_at == pytest.approx(t + expected_transition)
    assert ctl.state_at_claim is PhaseStateAtClaim.RED_TRUNCATE
    assert ctl.state(t) is JunctionState.TRANSITIONING
    assert ctl.state(green_at - 0.01) is JunctionState.TRANSITIONING
    assert ctl.state(green_at) is JunctionState.PREEMPTED
    assert ctl.state(green_at, queued=True) is JunctionState.QUEUED
    assert ctl.is_green_for(2, green_at)
    assert ctl.next_green_at(2, t) == pytest.approx(green_at)
    # the stages in between are the old phase's green / amber / all-red
    stages = {ctl.status(t + x).stage for x in (0.0, expected_transition - 0.1)}
    assert Stage.GREEN not in stages or into_slot < 10.0


def test_resequencing_to_another_phase_clears_the_held_one() -> None:
    ctl, base = control()
    t = base + 20.0
    ctl.serve(0, t)
    later = t + 30.0
    green_at = ctl.serve(3, later)  # a higher-priority claimant from approach 3
    assert green_at == pytest.approx(later + 5.0)  # held green had its minimum: 3 + 2
    assert ctl.status(later + 1.0).stage is Stage.AMBER
    assert ctl.status(later + 4.0).stage is Stage.ALL_RED
    assert ctl.is_green_for(3, later + 5.0)
    assert ctl.state_at_claim is PhaseStateAtClaim.GREEN_EXTEND  # of the episode


def test_release_recovers_the_most_starved_approach_then_the_plan() -> None:
    ctl, base = control()
    t = base + 40.0  # phase 1 green in the plan
    ctl.serve(3, t)  # red truncation for approach 3
    held_until = t + 60.0
    ctl.release(held_until, recovery_s=20.0)
    assert ctl.served_phase is None
    assert ctl.state(held_until) is JunctionState.RECOVERING
    assert ctl.status(held_until + 1.0).stage is Stage.AMBER
    assert ctl.status(held_until + 4.0).stage is Stage.ALL_RED
    # when the pre-emption began (phase 1 green), phase 2 had waited longest: its last
    # green ended 35 s before the cycle start (phase 3's 5 s before, phase 0's 25 s after)
    assert ctl.recovery_phase == 2
    assert ctl.is_green_for(2, held_until + 5.0)
    assert ctl.is_green_for(2, held_until + 24.9)
    assert ctl.recover_until == pytest.approx(held_until + 5.0 + 20.0 + 5.0)
    end = ctl.recover_until or 0.0
    assert ctl.state(end) is JunctionState.NORMAL
    assert ctl.status(end + 1.0) == ctl.plan.status(end + 1.0)  # back on the fixed plan


def test_starvation_share() -> None:
    ctl, base = control()
    t = base + 20.0
    ctl.serve(0, t)
    assert ctl.preempted_share(t + 100.0) == pytest.approx(100.0 / 300.0)
    assert not ctl.is_starved(t + 100.0)
    assert ctl.is_starved(t + 200.0)
    ctl.release(t + 200.0, 20.0)
    assert ctl.preempted_share(t + 300.0) == pytest.approx(200.0 / 300.0)
    assert not ctl.is_starved(t + 700.0)


def test_adopt_after_failover() -> None:
    ctl, base = control()
    t = base + 50.0
    ctl.adopt(2, t - 10.0, t)  # green since 10 s ago (from the restored claim)
    assert ctl.state(t) is JunctionState.PREEMPTED
    assert ctl.is_green_for(2, t)
    ctl2, _ = control()
    ctl2.adopt(1, t + 3.0, t)  # restored mid-transition
    assert ctl2.state(t) is JunctionState.TRANSITIONING
    assert ctl2.is_green_for(1, t + 3.0)


def sig(
    bearings: tuple[float, ...] = (), approach: float = 10.0, legs: int | None = None
) -> CorridorSignal:
    return CorridorSignal(
        "J9", LatLng(28.6, 77.2), 500.0, 0.0, Lane.RIGHT, approach,
        legs=legs, junction_bearings=bearings,
    )  # fmt: skip


def test_registry_geometry_and_idle_drop() -> None:
    reg = JunctionRegistry()
    t = 1_795_000_000.0
    ctl, phase = reg.phase_of("delhi", sig(approach=10.0), t)
    assert ctl.geometry.source == "synthetic"
    assert ctl.geometry.approach_bearings[0] == 10.0  # first observed approach
    assert phase == 0
    _, p2 = reg.phase_of("delhi", sig(approach=100.0), t)
    assert p2 == 1  # another approach of the same junction
    # OSRM geometry replaces the synthetic one while idle
    osrm, _ = reg.phase_of("delhi", sig(bearings=(0.0, 120.0, 240.0)), t)
    assert osrm.geometry.source == "osrm"
    assert osrm.plan.phases == 3
    osrm.serve(0, t)
    assert reg.get("delhi", sig(), t) is osrm  # never swapped while served
    reg.drop_idle("delhi", t + IDLE_TTL_S + 1.0, keep=set())
    assert reg.find("delhi", "J9") is osrm  # served: kept
    osrm.release(t + 10.0, 20.0)
    reg.drop_idle("delhi", t + 10.0 + IDLE_TTL_S + 400.0, keep=set())
    assert reg.find("delhi", "J9") is None
