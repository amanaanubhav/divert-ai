"""Synthetic fixed-time split-phase plans (design §4.1) and junction geometry."""

from __future__ import annotations

import pytest

from divert.domain.signal_plan import (
    ALL_RED_S,
    AMBER_S,
    MIN_GREEN_S,
    JunctionGeometry,
    PhasePlan,
    Stage,
    cycle_for_legs,
    phase_at,
    phases_for_legs,
    stable_offset_s,
)

T = 1_795_000_000.0  # an arbitrary sim instant (epoch seconds)


@pytest.mark.parametrize(
    ("legs", "phases", "cycle"),
    [(None, 4, 120.0), (0, 4, 120.0), (2, 2, 90.0), (3, 3, 90.0), (4, 4, 120.0), (5, 5, 150.0),
     (6, 6, 150.0), (12, 12, 180.0)],
)  # fmt: skip
def test_cycle_and_phases_by_leg_count(legs: int | None, phases: int, cycle: float) -> None:
    assert phases_for_legs(legs) == phases
    assert cycle_for_legs(legs) == cycle
    plan = PhasePlan.for_junction("J1", legs)
    assert plan.green_s >= MIN_GREEN_S  # pedestrian minimum, even with 12 legs
    assert plan.green_s + AMBER_S + ALL_RED_S == pytest.approx(plan.slot_s)


def test_phase_at_is_deterministic_and_offsets_are_stable() -> None:
    assert stable_offset_s("J6720393130", 120.0) == stable_offset_s("J6720393130", 120.0)
    assert 0.0 <= stable_offset_s("J6720393130", 120.0) < 120.0
    offsets = {stable_offset_s(f"J{i}", 120.0) for i in range(50)}
    assert len(offsets) > 40  # junctions do not all switch together
    for t in (T, T + 17.3, T + 1000.0):
        assert phase_at("J1", 4, t) == phase_at("J1", 4, t)
        assert phase_at("J1", 4, t) == phase_at("J1", 4, t + 120.0)  # cyclic


def test_one_cycle_gives_every_phase_green_amber_all_red_in_order() -> None:
    plan = PhasePlan.for_junction("J42", 3)
    timeline: list[tuple[int, Stage]] = []
    t = T
    while t < T + plan.cycle_s:
        st = plan.status(t)
        if not timeline or timeline[-1] != (st.phase, st.stage):
            timeline.append((st.phase, st.stage))
        t += 0.25
    stages = [s for _, s in timeline]
    assert stages.count(Stage.GREEN) in (3, 4)  # 3 greens (+1 if the window starts mid-green)
    for i, (phase, stage) in enumerate(timeline[:-1]):
        nxt = timeline[i + 1]
        if stage is Stage.GREEN:
            assert nxt == (phase, Stage.AMBER)
        elif stage is Stage.AMBER:
            assert nxt == (phase, Stage.ALL_RED)
        else:
            assert nxt == ((phase + 1) % 3, Stage.GREEN)


def test_status_stage_times() -> None:
    plan = PhasePlan("J", 4, 120.0, 0.0)  # slot 30 s: green 25, amber 3, all-red 2
    st = plan.status(T - (T % 120.0) + 30.0 + 26.0)  # phase 1, 1 s into amber
    assert (st.phase, st.stage) == (1, Stage.AMBER)
    assert st.stage_elapsed_s == pytest.approx(1.0)
    assert st.stage_remaining_s == pytest.approx(2.0)
    assert st.green_elapsed_s == pytest.approx(25.0)


def test_next_green_last_green_and_expected_wait() -> None:
    plan = PhasePlan("J", 4, 120.0, 0.0)
    base = T - (T % 120.0)
    assert plan.next_green_at(0, base + 5.0) == base + 5.0  # green now
    assert plan.next_green_at(2, base + 5.0) == pytest.approx(base + 60.0)
    assert plan.next_green_at(0, base + 26.0) == pytest.approx(base + 120.0)
    assert plan.last_green_end(0, base + 5.0) == base + 5.0
    assert plan.last_green_end(0, base + 40.0) == pytest.approx(base + 25.0)
    assert plan.last_green_end(3, base + 40.0) == pytest.approx(base - 5.0)
    assert plan.red_elapsed_s(0, base + 40.0) == pytest.approx(15.0)
    assert plan.red_s == pytest.approx(95.0)
    assert plan.expected_wait_s() == pytest.approx(95.0 / 120.0 * 95.0 / 2.0)


def test_geometry_from_osrm_and_synthetic() -> None:
    geo = JunctionGeometry.from_osrm([0.0, 90.0, 180.0, 270.0])
    assert geo is not None
    assert geo.legs == 4
    assert geo.approach_bearings == (180.0, 270.0, 0.0, 90.0)  # heading INTO the junction
    assert geo.phase_for(185.0) == 0  # arriving on the north road, heading south
    assert geo.phase_for(2.0) == 2
    assert geo.phase_for(358.0) == 2  # wraps around north
    assert JunctionGeometry.from_osrm([90.0]) is None
    syn = JunctionGeometry.synthetic(30.0)
    assert syn.legs == 4
    assert syn.approach_bearings == (30.0, 120.0, 210.0, 300.0)
    assert syn.phase_for(35.0) == 0
    assert syn.phase_for(125.0) == 1
    assert JunctionGeometry.synthetic(0.0, 3).approach_bearings == (0.0, 120.0, 240.0)
