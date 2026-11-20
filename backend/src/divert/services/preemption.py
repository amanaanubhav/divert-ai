"""Signal pre-emption rules (pure functions; the worker applies them every tick).

Claims are owned **per vehicle id** (never per vehicle type): a vehicle claims a
corridor signal ahead of it when its ETA to the signal is <= DIVERT_PREEMPT_LEAD_S or
it is within DIVERT_PREEMPT_DISTANCE_M along the route, and releases it once it is more
than DIVERT_PREEMPT_RELEASE_PAST_M past it. Every claim of a vehicle is released on
arrival, cancellation and error.

When several vehicles claim one signal the lowest ETA wins (PREEMPTED); the others get
CONFLICT. ETAs within DIVERT_CONFLICT_TIE_S of the best are a tie, broken by service
priority AMBULANCE > FIRE_ENGINE > POLICE, then by vehicle id.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from divert.domain.enums import SERVICE_PRIORITY, ServiceType, SignalState


@dataclass(frozen=True, slots=True)
class PreemptionRules:
    lead_s: float = 45.0
    distance_m: float = 600.0
    release_past_m: float = 20.0
    tie_s: float = 5.0

    def should_claim(self, vehicle_along_m: float, signal_along_m: float, eta_s: float) -> bool:
        """For a signal not yet claimed by this vehicle."""
        if self.is_past(vehicle_along_m, signal_along_m):
            return False
        ahead = signal_along_m - vehicle_along_m
        return eta_s <= self.lead_s or ahead <= self.distance_m

    def is_past(self, vehicle_along_m: float, signal_along_m: float) -> bool:
        """Release point: the vehicle is more than ``release_past_m`` beyond the signal."""
        return vehicle_along_m - signal_along_m > self.release_past_m


@dataclass(frozen=True, slots=True)
class Claimant:
    vehicle_id: str
    service: ServiceType
    eta_s: float


@dataclass(frozen=True, slots=True)
class Resolution:
    state: SignalState  # NORMAL (no claimants) | PREEMPTED (one) | CONFLICT (contested)
    winner: str | None
    per_vehicle: dict[str, SignalState]  # PREEMPTED for the winner, CONFLICT for others


def resolve(claimants: Mapping[str, Claimant], tie_s: float) -> Resolution:
    if not claimants:
        return Resolution(SignalState.NORMAL, None, {})
    best_eta = min(c.eta_s for c in claimants.values())
    tied = [c for c in claimants.values() if c.eta_s - best_eta <= tie_s]
    winner = min(tied, key=lambda c: (SERVICE_PRIORITY[c.service], c.vehicle_id)).vehicle_id
    per_vehicle = {
        vid: SignalState.PREEMPTED if vid == winner else SignalState.CONFLICT for vid in claimants
    }
    state = SignalState.PREEMPTED if len(claimants) == 1 else SignalState.CONFLICT
    return Resolution(state, winner, per_vehicle)
