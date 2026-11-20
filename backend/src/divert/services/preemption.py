"""Signal pre-emption rules (design §4.2-§4.5). Pure functions; the engine applies them.

**When to claim** (dynamic lead). A vehicle on an emergency leg claims junction J once its
ETA to J is at most ``required_lead = T_transition + T_queue + 5 s``:

* ``T_transition`` - 0 if the vehicle's phase is green now (green extension: hold it),
  else the rest of the current phase's 10 s minimum green + 3 s amber + 2 s all-red (or
  what is left of the amber / all-red) - red truncation;
* ``T_queue`` - discharge the queue in the approach: ``arrival_rate x lanes x red_elapsed``
  vehicles at a 2.0 s saturation headway per lane (rates per lane: ARTERIAL/EXPRESSWAY
  0.20, COLLECTOR 0.12, LOCAL 0.05 veh/s, x ``(2 - f_class)``; all assumptions).

Bounds: always claim within ``max(v^2 / (2 x 3.0) + 50 m, DIVERT_PREEMPT_MIN_CLAIM_DISTANCE_M)``
and never with an ETA above DIVERT_PREEMPT_MAX_LEAD_S. Free-left junctions are never
claimed; a junction whose stop line the leg never reaches (destination before it) is not
claimed either. A claim is released once the vehicle is past the junction box (20 m, or
30 m with 5+ legs).

**Several claimants** (:func:`resolve_junction`). Claimants on the same phase are
compatible (served together). Otherwise the winner is the earliest ETA unless a better
priority rank is within DIVERT_PRIORITY_WINDOW_S of it (ranks 0-3, ties by ETA then id);
the other phases are queued in the same order. The order is recomputed every tick, but a
served phase whose vehicle is within 3 s of crossing on green is never bumped.

**Speed advisory** (:func:`speed_advisory`): a claimant that would reach the stop line
before its green slows to arrive at the green (never below 2 m/s); if even that is too
early it stops at the stop line.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from divert.config import Settings
from divert.domain.enums import RoadClass
from divert.domain.signal_plan import ALL_RED_S, CLEARANCE_S, MIN_GREEN_S, PhaseStatus, Stage

MARGIN_S: Final = 5.0  # added to every required lead
SATURATION_HEADWAY_S: Final = 2.0  # per vehicle per lane (assumption)
ARRIVAL_RATE_PER_LANE: Final[dict[RoadClass, float]] = {  # veh/s/lane at free flow
    RoadClass.EXPRESSWAY: 0.20,  # not in the design table: treated like ARTERIAL
    RoadClass.ARTERIAL: 0.20,
    RoadClass.COLLECTOR: 0.12,
    RoadClass.LOCAL: 0.05,
}
CLAIM_BRAKE_MPS2: Final = 3.0  # stopping distance used by the always-claim bound
ALWAYS_CLAIM_EXTRA_M: Final = 50.0
CROSSING_LOCK_S: Final = 3.0  # a served winner this close to crossing is never bumped
ADVISORY_MIN_MPS: Final = 2.0
LARGE_JUNCTION_LEGS: Final = 5  # 5+ legs: the larger box
STOP_LINE_BOX_SHARE: Final = 0.5  # stop line = junction centre - half the box (assumption)


def transition_s(status: PhaseStatus, phase: int) -> float:
    """Time until ``phase`` can be green, from the junction's current phase status."""
    if status.is_green(phase):
        return 0.0
    if status.stage is Stage.GREEN:
        return max(0.0, MIN_GREEN_S - status.green_elapsed_s) + CLEARANCE_S
    if status.stage is Stage.AMBER:
        return status.stage_remaining_s + ALL_RED_S
    return status.stage_remaining_s  # all-red


def queue_clear_s(
    road_class: RoadClass,
    f_class: float,
    lanes: int,
    red_elapsed_s: float,
    *,
    green_elapsed_s: float = 0.0,
) -> float:
    """Time to discharge the approach queue built up over ``red_elapsed_s`` of red, minus
    what has already discharged during ``green_elapsed_s`` of green."""
    n = max(1, lanes)
    rate = ARRIVAL_RATE_PER_LANE[road_class] * (2.0 - f_class)
    queue_veh = rate * n * max(0.0, red_elapsed_s)
    return max(0.0, queue_veh * SATURATION_HEADWAY_S / n - max(0.0, green_elapsed_s))


@dataclass(frozen=True, slots=True)
class Lead:
    transition_s: float
    queue_clear_s: float
    required_s: float
    green_now: bool


def required_lead(transition: float, queue: float, *, margin_s: float = MARGIN_S) -> Lead:
    return Lead(transition, queue, transition + queue + margin_s, transition == 0.0)


def stopping_distance_m(speed_mps: float, brake_mps2: float = CLAIM_BRAKE_MPS2) -> float:
    return speed_mps * speed_mps / (2.0 * brake_mps2)


@dataclass(frozen=True, slots=True)
class PreemptionRules:
    """Configurable bounds (DIVERT_PREEMPT_* / DIVERT_PRIORITY_WINDOW_S)."""

    max_lead_s: float = 90.0
    min_claim_distance_m: float = 150.0
    release_past_m: float = 20.0
    release_past_large_m: float = 30.0
    priority_window_s: float = 10.0
    max_hold_s: float = 60.0
    recovery_s: float = 20.0
    margin_s: float = MARGIN_S

    @classmethod
    def from_settings(cls, settings: Settings) -> PreemptionRules:
        return cls(
            max_lead_s=settings.preempt_max_lead_s,
            min_claim_distance_m=settings.preempt_min_claim_distance_m,
            release_past_m=settings.preempt_release_past_m,
            release_past_large_m=settings.preempt_release_past_large_m,
            priority_window_s=settings.priority_window_s,
            max_hold_s=settings.preempt_max_hold_s,
            recovery_s=settings.preempt_recovery_s,
        )

    def box_m(self, legs: int | None) -> float:
        """Junction box: release this far past the junction centre."""
        if legs is not None and legs >= LARGE_JUNCTION_LEGS:
            return self.release_past_large_m
        return self.release_past_m

    def stop_line_m(self, signal_along_m: float, legs: int | None) -> float:
        return signal_along_m - STOP_LINE_BOX_SHARE * self.box_m(legs)

    def is_past(
        self, vehicle_along_m: float, signal_along_m: float, legs: int | None = None
    ) -> bool:
        return vehicle_along_m - signal_along_m > self.box_m(legs)

    def must_cross(self, signal_along_m: float, legs: int | None, leg_length_m: float) -> bool:
        """A leg ending before the stop line never enters the junction (design §4.2)."""
        return leg_length_m > self.stop_line_m(signal_along_m, legs)

    def always_claim_m(self, speed_mps: float) -> float:
        return max(stopping_distance_m(speed_mps) + ALWAYS_CLAIM_EXTRA_M, self.min_claim_distance_m)

    def should_claim(
        self, *, eta_s: float, distance_ahead_m: float, speed_mps: float, required_lead_s: float
    ) -> bool:
        """For a junction ahead that this vehicle may claim and does not hold yet."""
        if distance_ahead_m <= self.always_claim_m(speed_mps):
            return True
        if eta_s > self.max_lead_s:
            return False
        return eta_s <= required_lead_s


# ------------------------------------------------------------------------ conflicts


@dataclass(frozen=True, slots=True)
class Claimant:
    vehicle_id: str
    rank: int  # 0 patient on board ... 3 police (enums.priority_rank)
    eta_s: float
    phase: int


@dataclass(frozen=True, slots=True)
class Resolution:
    order: tuple[int, ...]  # phases in service order; order[0] is served now
    winner: str | None  # the claimant that decided order[0]
    served: frozenset[str]  # every claimant on order[0] (compatible)
    queue_pos: Mapping[str, int] = field(default_factory=dict)  # 0 served, 1.. queued

    @property
    def served_phase(self) -> int | None:
        return self.order[0] if self.order else None


def _pick(claimants: list[Claimant], window_s: float) -> Claimant:
    earliest = min(c.eta_s for c in claimants)
    near = [c for c in claimants if c.eta_s <= earliest + window_s]
    return min(near, key=lambda c: (c.rank, c.eta_s, c.vehicle_id))


def resolve_junction(
    claimants: Mapping[str, Claimant], *, window_s: float, locked_phase: int | None = None
) -> Resolution:
    """Service order of the claimed phases at one junction (see module doc).

    ``locked_phase``: the served phase whose vehicle is green and within CROSSING_LOCK_S
    of crossing - it stays first whatever arrives.
    """
    if not claimants:
        return Resolution((), None, frozenset())
    remaining = list(claimants.values())
    order: list[int] = []
    winner: str | None = None
    if locked_phase is not None and any(c.phase == locked_phase for c in remaining):
        locked = [c for c in remaining if c.phase == locked_phase]
        winner = min(locked, key=lambda c: (c.eta_s, c.rank, c.vehicle_id)).vehicle_id
        order.append(locked_phase)
        remaining = [c for c in remaining if c.phase != locked_phase]
    while remaining:
        best = _pick(remaining, window_s)
        if winner is None:
            winner = best.vehicle_id
        order.append(best.phase)
        remaining = [c for c in remaining if c.phase != best.phase]
    pos = {vid: order.index(c.phase) for vid, c in claimants.items()}
    served = frozenset(vid for vid, p in pos.items() if p == 0)
    return Resolution(tuple(order), winner, served, pos)


def queued_green_times(
    first_green_at: float,
    clear_times: list[float],
) -> list[float]:
    """Expected green start of each phase in the service order.

    ``clear_times[k]``: when the last vehicle of phase order[k] is expected to clear the
    junction. Phase k+1 gets green after phase k has had its minimum green and cleared,
    plus amber + all-red (the "+5 s transition" of design §4.3).
    """
    out = [first_green_at]
    for k in range(len(clear_times) - 1):
        out.append(max(out[k] + MIN_GREEN_S, clear_times[k]) + CLEARANCE_S)
    return out


@dataclass(frozen=True, slots=True)
class Advisory:
    speed_mps: float | None  # cruise at most this fast until the stop line
    stop: bool  # stop at the stop line and wait for green


NO_ADVISORY: Final = Advisory(None, False)


def speed_advisory(
    distance_to_stop_m: float,
    time_to_green_s: float,
    eta_to_stop_s: float,
    *,
    min_mps: float = ADVISORY_MIN_MPS,
) -> Advisory:
    """Arrive at the stop line when it turns green rather than stop hard (design §4.4)."""
    if time_to_green_s <= 0.0 or eta_to_stop_s >= time_to_green_s:
        return NO_ADVISORY  # green by the time it gets there anyway
    if distance_to_stop_m <= 0.0:
        return Advisory(None, True)
    v = distance_to_stop_m / time_to_green_s
    if v >= min_mps:
        return Advisory(v, False)
    return Advisory(None, True)
