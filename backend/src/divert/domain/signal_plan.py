"""Synthetic, deterministic signal timing (design §4.1).

Real Indian signal timing plans are not available, so every junction runs a documented
**fixed-time split-phase plan**: one phase per approach (each approach gets its own green
for all of its movements, so right turns are protected), a cycle of 90 s (<= 3 legs),
120 s (4 legs) or 150 s (5+ legs) split equally, each phase = green + 3 s amber + 2 s
all-red, and at least 10 s of green (pedestrian safety). The plan's offset is a stable
hash of the junction id, so :func:`phase_at` is a pure function of (junction, legs, t).

Approaches come from the junction's leg count, learned from OSRM intersections; unknown
geometry defaults to 4 legs, spaced evenly from the first observed approach.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from divert.domain.geo import bearing_delta_deg

AMBER_S: Final = 3.0
ALL_RED_S: Final = 2.0
CLEARANCE_S: Final = AMBER_S + ALL_RED_S
MIN_GREEN_S: Final = 10.0
DEFAULT_LEGS: Final = 4
MIN_PHASES: Final = 2  # a 2-leg signal (mid-block crossing) still alternates two phases
CYCLE_UP_TO_3_LEGS_S: Final = 90.0
CYCLE_4_LEGS_S: Final = 120.0
CYCLE_5_PLUS_LEGS_S: Final = 150.0
_OFFSET_RESOLUTION: Final = 1000  # offsets in milliseconds


class Stage(StrEnum):
    GREEN = "GREEN"
    AMBER = "AMBER"
    ALL_RED = "ALL_RED"


def phases_for_legs(legs: int | None) -> int:
    return max(MIN_PHASES, DEFAULT_LEGS if legs is None or legs <= 0 else legs)


def cycle_for_legs(legs: int | None) -> float:
    n = phases_for_legs(legs)
    table = CYCLE_UP_TO_3_LEGS_S if n <= 3 else CYCLE_4_LEGS_S if n == 4 else CYCLE_5_PLUS_LEGS_S
    # many-legged junctions: stretch the cycle so every phase keeps its minimum green
    return max(table, n * (MIN_GREEN_S + CLEARANCE_S))


def stable_offset_s(junction_id: str, cycle_s: float) -> float:
    """Deterministic plan offset (CRC-32 of the id; Python's hash() is salted)."""
    span = max(1, round(cycle_s * _OFFSET_RESOLUTION))
    return (zlib.crc32(junction_id.encode()) % span) / _OFFSET_RESOLUTION


@dataclass(frozen=True, slots=True)
class PhaseStatus:
    phase: int  # phase whose slot this is (green, or clearing after its green)
    stage: Stage
    stage_elapsed_s: float
    stage_remaining_s: float
    green_elapsed_s: float  # how long `phase` has been (or was) green in this slot

    def is_green(self, phase: int) -> bool:
        return self.stage is Stage.GREEN and self.phase == phase


@dataclass(frozen=True, slots=True)
class PhasePlan:
    junction_id: str
    phases: int
    cycle_s: float
    offset_s: float

    @classmethod
    def for_junction(cls, junction_id: str, legs: int | None) -> PhasePlan:
        cycle = cycle_for_legs(legs)
        return cls(junction_id, phases_for_legs(legs), cycle, stable_offset_s(junction_id, cycle))

    @property
    def slot_s(self) -> float:
        return self.cycle_s / self.phases

    @property
    def green_s(self) -> float:
        return self.slot_s - CLEARANCE_S

    @property
    def red_s(self) -> float:
        """Time an approach waits per cycle (everyone else's slots)."""
        return self.cycle_s - self.green_s

    @property
    def red_share(self) -> float:
        return self.red_s / self.cycle_s

    def expected_wait_s(self) -> float:
        """Mean wait of a vehicle arriving at a random time: red_share x red / 2 (design §2)."""
        return self.red_share * self.red_s / 2.0

    def _cycle_pos(self, t: float) -> float:
        return (t - self.offset_s) % self.cycle_s

    def status(self, t: float) -> PhaseStatus:
        u = self._cycle_pos(t)
        idx = min(int(u // self.slot_s), self.phases - 1)
        within = u - idx * self.slot_s
        green = self.green_s
        if within < green:
            return PhaseStatus(idx, Stage.GREEN, within, green - within, within)
        if within < green + AMBER_S:
            return PhaseStatus(idx, Stage.AMBER, within - green, green + AMBER_S - within, green)
        return PhaseStatus(
            idx, Stage.ALL_RED, within - green - AMBER_S, self.slot_s - within, green
        )

    def green_start(self, phase: int, t: float) -> float:
        """Start of the green of ``phase`` that is current at ``t``, or the next one."""
        u = self._cycle_pos(t)
        cycle_start = t - u
        start = cycle_start + phase * self.slot_s
        if t >= start + self.green_s:  # this cycle's green is over: the next cycle's
            start += self.cycle_s
        return start

    def next_green_at(self, phase: int, t: float) -> float:
        """``t`` if ``phase`` is green at ``t``, else when its next green starts."""
        return max(t, self.green_start(phase, t))

    def last_green_end(self, phase: int, t: float) -> float:
        """When the last green of ``phase`` ended at or before ``t`` (``t`` if green now)."""
        start = self.green_start(phase, t)
        if start <= t:  # green now
            return t
        return start - self.cycle_s + self.green_s

    def red_elapsed_s(self, phase: int, t: float) -> float:
        return max(0.0, t - self.last_green_end(phase, t))


def phase_at(junction_id: str, legs: int | None, t: float) -> PhaseStatus:
    """Which phase of the junction's fixed plan is running at sim time ``t`` (epoch s)."""
    return PhasePlan.for_junction(junction_id, legs).status(t)


@dataclass(frozen=True, slots=True)
class JunctionGeometry:
    """Approach directions of a junction; phase i serves traffic arriving on approach i.

    ``approach_bearings`` are travel headings *into* the junction (degrees clockwise).
    """

    legs: int
    approach_bearings: tuple[float, ...]
    source: str  # "osrm" | "synthetic"

    @classmethod
    def from_osrm(cls, road_bearings: Sequence[float]) -> JunctionGeometry | None:
        """From an OSRM intersection's road bearings (each points away from the centre)."""
        if len(road_bearings) < MIN_PHASES:
            return None
        heads = tuple((float(b) + 180.0) % 360.0 for b in road_bearings)
        return cls(len(heads), heads, "osrm")

    @classmethod
    def synthetic(cls, first_approach: float, legs: int | None = None) -> JunctionGeometry:
        n = phases_for_legs(legs)
        step = 360.0 / n
        return cls(n, tuple((first_approach + k * step) % 360.0 for k in range(n)), "synthetic")

    def phase_for(self, approach_bearing: float) -> int:
        """Phase whose approach heading is nearest the vehicle's (ties: lower index)."""
        best, best_gap = 0, math.inf
        for i, h in enumerate(self.approach_bearings):
            gap = abs(bearing_delta_deg(h, approach_bearing))
            if gap < best_gap - 1e-9:
                best, best_gap = i, gap
        return best
