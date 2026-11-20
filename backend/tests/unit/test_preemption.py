"""Pre-emption rules: when to claim/release, and conflict resolution."""

from __future__ import annotations

import pytest

from divert.domain.enums import ServiceType, SignalState
from divert.services.preemption import Claimant, PreemptionRules, resolve

RULES = PreemptionRules(lead_s=45.0, distance_m=600.0, release_past_m=20.0, tie_s=5.0)


@pytest.mark.parametrize(
    ("vehicle", "signal", "eta", "claim"),
    [
        (0.0, 1000.0, 44.9, True),  # ETA inside the lead time
        (0.0, 1000.0, 45.0, True),
        (0.0, 1000.0, 45.1, False),  # far and slow: not yet
        (0.0, 600.0, 120.0, True),  # within 600 m along the route, whatever the ETA
        (0.0, 600.1, 120.0, False),
        (1015.0, 1000.0, 0.0, True),  # just past but within the 20 m release margin
        (1020.1, 1000.0, 0.0, False),  # more than 20 m past: never claim
    ],
)
def test_should_claim(vehicle: float, signal: float, eta: float, claim: bool) -> None:
    assert RULES.should_claim(vehicle, signal, eta) is claim


def test_release_point_is_20m_past() -> None:
    assert not RULES.is_past(1020.0, 1000.0)
    assert RULES.is_past(1020.01, 1000.0)


def c(vid: str, service: ServiceType, eta: float) -> Claimant:
    return Claimant(vid, service, eta)


def test_no_claimants_is_normal() -> None:
    res = resolve({}, 5.0)
    assert (res.state, res.winner, res.per_vehicle) == (SignalState.NORMAL, None, {})


def test_single_claimant_preempts() -> None:
    res = resolve({"PCR-1": c("PCR-1", ServiceType.POLICE, 30)}, 5.0)
    assert res.state is SignalState.PREEMPTED
    assert res.per_vehicle == {"PCR-1": SignalState.PREEMPTED}


def test_lowest_eta_wins_outside_the_tie_window() -> None:
    claims = {
        "AMB-1": c("AMB-1", ServiceType.AMBULANCE, 30.0),
        "PCR-1": c("PCR-1", ServiceType.POLICE, 20.0),
    }
    res = resolve(claims, 5.0)
    assert res.state is SignalState.CONFLICT
    assert res.winner == "PCR-1"
    assert res.per_vehicle == {"PCR-1": SignalState.PREEMPTED, "AMB-1": SignalState.CONFLICT}


@pytest.mark.parametrize(
    ("claims", "winner"),
    [
        ({"PCR-1": (ServiceType.POLICE, 20.0), "AMB-9": (ServiceType.AMBULANCE, 24.9)}, "AMB-9"),
        (
            {"PCR-1": (ServiceType.POLICE, 20.0), "FIRE-2": (ServiceType.FIRE_ENGINE, 25.0)},
            "FIRE-2",
        ),
        (
            {"FIRE-2": (ServiceType.FIRE_ENGINE, 21.0), "AMB-3": (ServiceType.AMBULANCE, 22.0)},
            "AMB-3",
        ),
        ({"AMB-2": (ServiceType.AMBULANCE, 21.0), "AMB-1": (ServiceType.AMBULANCE, 23.0)}, "AMB-1"),
        ({"PCR-1": (ServiceType.POLICE, 20.0), "AMB-9": (ServiceType.AMBULANCE, 25.1)}, "PCR-1"),
    ],
)
def test_tie_break_priority_then_id(
    claims: dict[str, tuple[ServiceType, float]], winner: str
) -> None:
    res = resolve({vid: c(vid, s, eta) for vid, (s, eta) in claims.items()}, 5.0)
    assert res.winner == winner
    assert sum(st is SignalState.PREEMPTED for st in res.per_vehicle.values()) == 1
