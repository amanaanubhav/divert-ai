"""The simulation clock: ONE clock drives vehicle motion and signal plans. Pure.

Every tick advances the sim clock by ``dt`` and moves every vehicle by the same ``dt``;
the signal plans (``phase_at(t)``), claim timing, dwell and the RETURN ETA replay all
read that clock. Motion and signals therefore never drift apart, however late a tick
runs.

* ``DIVERT_SIM_TIME_SCALE`` != 1: a fixed step (``sim_tick_s x scale``) per tick; an
  overrunning tick only slows the whole simulation down (the clock is persisted).
* scale 1 (real time): the clock follows the wall clock. A tick simulates the time the
  clock is behind the wall clock - normally one tick period - at most
  :data:`MAX_STEPS_PER_TICK` periods; the rest of a longer stall is caught up over the
  next ticks. Behind by more than :data:`RESYNC_AFTER_S` (the host was suspended, the
  process was frozen) the backlog is skipped, not simulated: the clock jumps to one
  step behind the wall clock, like a leader take-over (logged by the worker).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Assumption: a step of at most 2 tick periods (1 s at the default 0.5 s tick) keeps the
# motion integration, stop-line snapping and claim look-ahead as exact as one nominal
# step (an emergency vehicle at 25 m/s moves 25 m), while a tick that ran 2x late is
# still caught up in a single step.
MAX_STEPS_PER_TICK: Final = 2.0
# Assumption: a real-time simulation more than 30 s behind the wall clock was stalled
# (suspended host, debugger, frozen container) rather than loaded; catching that up at
# one extra step per tick would run the city at double speed for as long again.
RESYNC_AFTER_S: Final = 30.0


@dataclass(frozen=True, slots=True)
class Step:
    """One tick of the sim clock: skip ``skipped_s`` (never simulated), then simulate
    ``dt_s`` (vehicles move ``dt_s``; the clock ends ``skipped_s + dt_s`` later)."""

    dt_s: float
    skipped_s: float = 0.0

    @property
    def advance_s(self) -> float:
        return self.skipped_s + self.dt_s


def fixed_step(nominal_s: float) -> Step:
    """Scaled simulation (scale != 1): the same step every tick."""
    return Step(nominal_s)


def real_time_step(
    behind_s: float,
    nominal_s: float,
    *,
    max_steps: float = MAX_STEPS_PER_TICK,
    resync_after_s: float = RESYNC_AFTER_S,
) -> Step:
    """Scale 1: the step that brings the clock (``behind_s`` behind the wall clock) up
    to it - clamped to ``[0, max_steps x nominal_s]``; a backlog above
    ``resync_after_s`` is skipped down to one nominal step. A clock ahead of the wall
    clock (the wall clock stepped back) waits: ``dt_s`` 0."""
    if behind_s > resync_after_s:
        return Step(nominal_s, skipped_s=behind_s - nominal_s)
    return Step(min(max(behind_s, 0.0), max_steps * nominal_s))
