"""Pre-emption rules (design §4.2-§4.5): transition / queue / required lead, claim bounds,
junction box, conflicts (compatible phases, priority window, re-sequencing, crossing
lock) and the speed advisory."""

from __future__ import annotations

import pytest

from divert.config import Settings
from divert.domain.enums import (
    RANK_AMBULANCE,
    RANK_FIRE_ENGINE,
    RANK_PATIENT_ON_BOARD,
    RANK_POLICE,
    LegKind,
    RoadClass,
    ServiceType,
    priority_rank,
)
from divert.domain.signal_plan import PhaseStatus, Stage
from divert.services.preemption import (
    MARGIN_S,
    Claimant,
    PreemptionRules,
    queue_clear_s,
    queued_green_times,
    required_lead,
    resolve_junction,
    speed_advisory,
    stopping_distance_m,
    transition_s,
)

RULES = PreemptionRules()


def st(phase: int, stage: Stage, elapsed: float, remaining: float, green: float) -> PhaseStatus:
    return PhaseStatus(phase, stage, elapsed, remaining, green)


@pytest.mark.parametrize(
    ("status", "phase", "expected"),
    [
        (st(1, Stage.GREEN, 4.0, 21.0, 4.0), 1, 0.0),  # green now: extend it
        (st(0, Stage.GREEN, 4.0, 21.0, 4.0), 1, 6.0 + 3.0 + 2.0),  # mid minimum green
        (st(0, Stage.GREEN, 18.0, 7.0, 18.0), 1, 3.0 + 2.0),  # minimum green served
        (st(0, Stage.AMBER, 1.0, 2.0, 25.0), 1, 2.0 + 2.0),  # amber: rest + all-red
        (st(0, Stage.ALL_RED, 0.5, 1.5, 25.0), 1, 1.5),  # all-red: rest
        (st(1, Stage.AMBER, 1.0, 2.0, 25.0), 1, 2.0 + 2.0),  # its own amber: next green
    ],
)
def test_transition_time(status: PhaseStatus, phase: int, expected: float) -> None:
    assert transition_s(status, phase) == pytest.approx(expected)


def test_queue_discharge() -> None:
    # ARTERIAL 0.20 veh/s/lane x (2 - 0.5) = 0.30; 40 s of red -> 12 veh per lane; 2 s each
    assert queue_clear_s(RoadClass.ARTERIAL, 0.5, 2, 40.0) == pytest.approx(24.0)
    assert queue_clear_s(RoadClass.ARTERIAL, 0.5, 3, 40.0) == pytest.approx(24.0)  # per lane
    assert queue_clear_s(RoadClass.LOCAL, 1.0, 1, 40.0) == pytest.approx(0.05 * 40.0 * 2.0)
    assert queue_clear_s(RoadClass.COLLECTOR, 0.8, 2, 0.0) == 0.0
    # green for 10 s already: that much of the queue has gone
    assert queue_clear_s(RoadClass.ARTERIAL, 0.5, 2, 40.0, green_elapsed_s=10.0) == pytest.approx(
        14.0
    )
    assert queue_clear_s(RoadClass.ARTERIAL, 0.5, 2, 40.0, green_elapsed_s=99.0) == 0.0
    assert queue_clear_s(RoadClass.EXPRESSWAY, 0.5, 3, 40.0) == pytest.approx(24.0)


def test_required_lead() -> None:
    lead = required_lead(11.0, 24.0)
    assert lead.required_s == pytest.approx(11.0 + 24.0 + MARGIN_S)
    assert not lead.green_now
    assert required_lead(0.0, 3.0).green_now


@pytest.mark.parametrize(
    ("eta", "ahead", "speed", "lead", "claim"),
    [
        (60.0, 149.0, 0.0, 10.0, True),  # inside the 150 m floor: always
        (60.0, 151.0, 0.0, 10.0, False),
        (60.0, 199.0, 30.0, 10.0, True),  # 30 m/s: 150 m stopping + 50 m
        (60.0, 201.0, 30.0, 10.0, False),
        (35.0, 900.0, 25.0, 35.0, True),  # ETA within the required lead
        (35.1, 900.0, 25.0, 35.0, False),
        (91.0, 900.0, 10.0, 120.0, False),  # never earlier than the 90 s bound
        (90.0, 900.0, 10.0, 120.0, True),
    ],
)
def test_should_claim_bounds(
    eta: float, ahead: float, speed: float, lead: float, claim: bool
) -> None:
    assert (
        RULES.should_claim(eta_s=eta, distance_ahead_m=ahead, speed_mps=speed, required_lead_s=lead)
        is claim
    )


def test_stopping_distance() -> None:
    assert stopping_distance_m(30.0) == pytest.approx(150.0)
    assert RULES.always_claim_m(0.0) == 150.0
    assert RULES.always_claim_m(30.0) == pytest.approx(200.0)


@pytest.mark.parametrize(
    ("legs", "box"), [(None, 20.0), (3, 20.0), (4, 20.0), (5, 30.0), (7, 30.0)]
)
def test_junction_box_by_leg_count(legs: int | None, box: float) -> None:
    assert RULES.box_m(legs) == box
    assert not RULES.is_past(1000.0 + box, 1000.0, legs)
    assert RULES.is_past(1000.0 + box + 0.01, 1000.0, legs)
    assert RULES.stop_line_m(1000.0, legs) == pytest.approx(1000.0 - box / 2)


def test_destination_inside_the_box_is_claimed_only_if_the_vehicle_crosses() -> None:
    assert RULES.must_cross(1000.0, 4, leg_length_m=1000.0)  # ends at the junction centre
    assert RULES.must_cross(1000.0, 4, leg_length_m=995.0)  # ends inside the box
    assert not RULES.must_cross(1000.0, 4, leg_length_m=990.0)  # stops before the line
    assert not RULES.must_cross(1000.32, 4, leg_length_m=990.0)


def test_rules_from_settings_and_deprecated_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    r = PreemptionRules.from_settings(Settings())
    assert (r.max_lead_s, r.min_claim_distance_m, r.priority_window_s, r.max_hold_s) == (
        90.0,
        150.0,
        10.0,
        60.0,
    )
    assert (r.release_past_m, r.release_past_large_m, r.recovery_s) == (20.0, 30.0, 20.0)
    monkeypatch.setenv("DIVERT_PREEMPT_LEAD_S", "45")
    monkeypatch.setenv("DIVERT_PREEMPT_DISTANCE_M", "600")
    monkeypatch.setenv("DIVERT_CONFLICT_TIE_S", "5")
    old = PreemptionRules.from_settings(Settings())
    assert (old.max_lead_s, old.min_claim_distance_m, old.priority_window_s) == (45.0, 600.0, 5.0)


# ------------------------------------------------------------------------ conflicts


def test_priority_ranks() -> None:
    assert priority_rank(ServiceType.AMBULANCE, LegKind.TRANSPORT) == RANK_PATIENT_ON_BOARD == 0
    assert priority_rank(ServiceType.AMBULANCE) == RANK_AMBULANCE == 1
    assert priority_rank(ServiceType.FIRE_ENGINE) == RANK_FIRE_ENGINE == 2
    assert priority_rank(ServiceType.POLICE) == RANK_POLICE == 3


def c(vid: str, rank: int, eta: float, phase: int) -> Claimant:
    return Claimant(vid, rank, eta, phase)


def test_no_claimants() -> None:
    res = resolve_junction({}, window_s=10.0)
    assert (res.order, res.winner, res.served) == ((), None, frozenset())


def test_same_phase_claimants_are_compatible() -> None:
    res = resolve_junction(
        {"AMB-1": c("AMB-1", 1, 30.0, 2), "PCR-1": c("PCR-1", 3, 10.0, 2)}, window_s=10.0
    )
    assert res.order == (2,)
    assert res.served == {"AMB-1", "PCR-1"}
    assert res.queue_pos == {"AMB-1": 0, "PCR-1": 0}


def test_earliest_eta_wins_outside_the_priority_window() -> None:
    res = resolve_junction(
        {"AMB-1": c("AMB-1", 1, 30.1, 0), "PCR-1": c("PCR-1", 3, 20.0, 1)}, window_s=10.0
    )
    assert res.winner == "PCR-1"
    assert res.order == (1, 0)
    assert res.queue_pos == {"PCR-1": 0, "AMB-1": 1}


@pytest.mark.parametrize(
    ("claims", "winner"),
    [
        ({"PCR-1": (3, 20.0, 0), "AMB-9": (1, 29.9, 1)}, "AMB-9"),  # within 10 s: priority
        ({"PCR-1": (3, 20.0, 0), "FIRE-2": (2, 30.0, 1)}, "FIRE-2"),
        ({"FIRE-2": (2, 21.0, 0), "AMB-3": (1, 22.0, 1)}, "AMB-3"),
        ({"AMB-1": (1, 21.0, 0), "AMB-T": (0, 30.0, 1)}, "AMB-T"),  # patient on board
        ({"AMB-2": (1, 21.0, 0), "AMB-1": (1, 23.0, 1)}, "AMB-2"),  # same rank: earliest
        ({"AMB-2": (1, 21.0, 0), "AMB-1": (1, 21.0, 1)}, "AMB-1"),  # exact tie: vehicle id
    ],
)
def test_priority_window_and_ties(claims: dict[str, tuple[int, float, int]], winner: str) -> None:
    res = resolve_junction(
        {vid: c(vid, r, eta, p) for vid, (r, eta, p) in claims.items()}, window_s=10.0
    )
    assert res.winner == winner
    assert len(res.served) == 1


def test_new_higher_priority_arrival_resequences_the_queue() -> None:
    base = {"PCR-1": c("PCR-1", 3, 5.0, 0), "FIRE-1": c("FIRE-1", 2, 40.0, 1)}
    assert resolve_junction(base, window_s=10.0).order == (0, 1)
    with_amb = {**base, "AMB-1": c("AMB-1", 1, 12.0, 2)}
    res = resolve_junction(with_amb, window_s=10.0)
    assert res.order == (2, 0, 1)  # the ambulance (within 10 s of PCR) jumps the queue
    assert res.queue_pos == {"AMB-1": 0, "PCR-1": 1, "FIRE-1": 2}


def test_winner_within_3_s_of_crossing_is_never_bumped() -> None:
    claims = {"PCR-1": c("PCR-1", 3, 2.5, 0), "AMB-1": c("AMB-1", 1, 4.0, 1)}
    assert resolve_junction(claims, window_s=10.0).winner == "AMB-1"
    locked = resolve_junction(claims, window_s=10.0, locked_phase=0)
    assert locked.winner == "PCR-1"
    assert locked.order == (0, 1)


def test_queued_green_times_chain_min_green_and_clearance() -> None:
    # served phase green at 100; its vehicle clears at 104 -> min green (110) + 5 = 115
    assert queued_green_times(100.0, [104.0, 130.0, 0.0]) == [100.0, 115.0, 135.0]


@pytest.mark.parametrize(
    ("dist", "t_green", "eta_stop", "speed", "stop"),
    [
        (100.0, 10.0, 12.0, None, False),  # arrives after the green anyway
        (100.0, -1.0, 2.0, None, False),  # already green
        (100.0, 20.0, 8.0, 5.0, False),  # slow to arrive at the green
        (100.0, 50.0, 8.0, 2.0, False),  # exactly the 2 m/s floor
        (100.0, 60.0, 8.0, None, True),  # would need < 2 m/s: stop at the line
        (0.0, 5.0, 0.0, None, True),  # at the stop line, still red
    ],
)
def test_speed_advisory(
    dist: float, t_green: float, eta_stop: float, speed: float | None, stop: bool
) -> None:
    adv = speed_advisory(dist, t_green, eta_stop)
    assert adv.stop is stop
    if speed is None:
        assert adv.speed_mps is None
    else:
        assert adv.speed_mps == pytest.approx(speed)
