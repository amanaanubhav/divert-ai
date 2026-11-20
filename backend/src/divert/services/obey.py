"""Non-emergency motion through signalised junctions (design §4.5). Pure.

A vehicle on a non-emergency leg (RETURN) never claims: it brakes to the stop line of a
junction that is red (or amber / all-red / pre-empted for another approach) for its
approach, if it can still stop there, waits, and moves off on green. The SAME functions
drive the simulation (:mod:`divert.services.simulation`) and the phase-aware RETURN ETA
(:func:`replay`), so predicted and simulated RETURN travel times agree by construction
except where other vehicles' pre-emptions change a junction's plan in between.

* :func:`decide_stop` - one tick's decision: the stop line to hold at, if any;
* :func:`shift_back` - emergency stop advisories never wait inside an earlier junction's
  box (back-to-back junctions; a RETURN vehicle does - see :func:`decide_stop`);
* :func:`motion_caps` - the junction speed caps (+ the stop line) of the speed profile;
* :func:`replay` - tick-by-tick replay of the motion from a state to the leg's end.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from divert.domain.route import CorridorSignal
from divert.services.preemption import ALWAYS_CLAIM_EXTRA_M, PreemptionRules, stopping_distance_m
from divert.services.speed_model import (
    ARRIVAL_TOLERANCE_M,
    JUNCTION_NORMAL_MPS,
    JUNCTION_PREEMPTED_MPS,
    SpeedProfile,
)

STOP_SNAP_M: Final = 0.3  # this close to an active stop line = stopped at it
STOP_SLACK_M: Final = 1.0  # a vehicle that can stop within this past the line still stops
# Replay guard: a RETURN leg that has not arrived after this long is reported as such
# (only reachable when a junction is never green for the vehicle - a modelling bug).
REPLAY_MAX_S: Final = 6 * 3600.0


class GreenView(Protocol):
    """Is the junction green for the vehicle's approach at sim epoch ``t``?"""

    def is_green(self, c: CorridorSignal, t: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class StopDecision:
    stop_at_m: float | None
    stop_for: str | None  # corridor key of that stop line
    passed: tuple[str, ...]  # corridor keys passed (past the box) since the last decision


def shift_back(
    corridor: Sequence[CorridorSignal],
    passed: Collection[str],
    along_m: float,
    index: int,
    stop: float,
    *,
    rules: PreemptionRules,
) -> float:
    """Back-to-back junctions: never wait inside an earlier junction's box - stop at that
    junction's stop line instead (when the vehicle has not entered it yet)."""
    for j in range(index - 1, -1, -1):
        prev = corridor[j]
        if prev.key in passed:
            break
        if stop >= prev.along_m + rules.box_m(prev.legs):
            break
        line = rules.stop_line_m(prev.along_m, prev.legs)
        if along_m > line + STOP_SNAP_M:
            break  # already in that box: clear it, wait at the next line
        stop = line
    return stop


def decide_stop(
    *,
    corridor: Sequence[CorridorSignal],
    passed: Collection[str],
    along_m: float,
    speed_mps: float,
    length_m: float,
    t: float,
    signals: GreenView,
    rules: PreemptionRules,
    committed: str | None,
) -> StopDecision:
    """Stop at the stop line of the first junction ahead (within the look-ahead) that is
    not green for the vehicle, if it can still brake for it (or it was already holding
    for it: ``committed``); otherwise carry on (amber dilemma)."""
    horizon = along_m + max(
        rules.min_claim_distance_m, stopping_distance_m(speed_mps) + ALWAYS_CLAIM_EXTRA_M
    )
    newly: list[str] = []
    done = set(passed)
    for c in corridor:
        if c.key in done:
            continue
        if rules.is_past(along_m, c.along_m, c.legs):
            newly.append(c.key)
            done.add(c.key)
            continue
        line = rules.stop_line_m(c.along_m, c.legs)
        if line > horizon:
            break
        if c.free_left or not rules.must_cross(c.along_m, c.legs, length_m):
            continue
        if along_m > line + STOP_SNAP_M:
            continue  # inside the junction box: clear it
        if signals.is_green(c, t):
            continue
        can_stop = stopping_distance_m(speed_mps) <= line - along_m + STOP_SLACK_M
        if not can_stop and committed != c.key:
            continue  # too close to stop on amber: carry on through
        # No shift back to an earlier junction's stop line here (stage A did): two
        # synthetic, uncoordinated plans a few metres apart may never show green together,
        # so waiting before the first box could last forever. The vehicle waits at this
        # junction's line (possibly inside the earlier box - no cross traffic is modelled).
        return StopDecision(line, c.key, tuple(newly))
    return StopDecision(None, None, tuple(newly))


def motion_caps(
    corridor: Sequence[CorridorSignal], fast: Collection[str], stop_at_m: float | None
) -> list[tuple[float, float]]:
    """Point caps of the speed profile: junction speed (pre-empted for the vehicle / free
    left = fast) and, when holding for a red, 0 at the stop line."""
    caps = [
        (c.along_m, JUNCTION_PREEMPTED_MPS if c.key in fast else JUNCTION_NORMAL_MPS)
        for c in corridor
    ]
    if stop_at_m is not None:
        caps.append((max(0.0, stop_at_m), 0.0))
    return caps


def snap_to_stop(x0: float, x1: float, v1: float, stop: float | None) -> tuple[float, float]:
    """Never past an active stop line: a step that reaches it ends there, stopped."""
    if stop is not None and x0 <= stop + 1e-6 and x1 >= stop - STOP_SNAP_M:
        return max(x0, stop), 0.0
    return x1, v1


@dataclass(frozen=True, slots=True)
class ReplayResult:
    total_s: float
    to_signals_s: dict[str, float]  # corridor key -> time to the junction centre
    arrived: bool


def replay(
    *,
    corridor: Sequence[CorridorSignal],
    length_m: float,
    profile_at: Callable[[float | None, float], SpeedProfile],
    along_m: float,
    speed_mps: float,
    t0: float,
    dt: float,
    signals: GreenView,
    rules: PreemptionRules,
    passed: Collection[str] = (),
    committed: str | None = None,
    max_s: float = REPLAY_MAX_S,
) -> ReplayResult:
    """Replay a non-emergency vehicle tick by tick exactly as the simulation moves it: the
    stop decision at the current tick (``committed``: the stop line it already holds for),
    then per tick: step (``profile_at(stop_at, t)``), snap at the stop line, arrival
    check, and the next stop decision at the tick's time."""
    x, v, t = along_m, speed_mps, t0
    done = set(passed)
    ahead = [c for c in corridor if c.along_m >= along_m]
    to_signals: dict[str, float] = {}
    elapsed = 0.0

    def decide(holding: str | None) -> tuple[float | None, str | None]:
        d = decide_stop(
            corridor=corridor,
            passed=done,
            along_m=x,
            speed_mps=v,
            length_m=length_m,
            t=t,
            signals=signals,
            rules=rules,
            committed=holding,
        )
        done.update(d.passed)
        return d.stop_at_m, d.stop_for

    stop, holding = decide(committed)
    while elapsed < max_s:
        t += dt
        elapsed += dt
        x0 = x
        x, v = profile_at(stop, t).step(x0, v, dt)
        x, v = snap_to_stop(x0, x, v, stop)
        for c in ahead:
            if c.key not in to_signals and x0 < c.along_m <= x:
                share = (x - c.along_m) / (x - x0) if x > x0 else 0.0
                to_signals[c.key] = elapsed - dt * share
        if x >= length_m - ARRIVAL_TOLERANCE_M:
            for c in ahead:
                to_signals.setdefault(c.key, elapsed)
            return ReplayResult(elapsed, to_signals, True)
        stop, holding = decide(holding)
    return ReplayResult(math.inf, to_signals, False)
