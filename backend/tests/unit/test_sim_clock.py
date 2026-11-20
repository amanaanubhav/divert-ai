"""ONE simulation clock for vehicle motion and signal plans (stage 5 fix-2 B2).

At real-time scale the worker used to move vehicles a fixed 0.5 s per tick while the
signal plans read the wall clock; overrunning ticks let the plans outrun the vehicles
and broke the +-10 % RETURN ETA (the replay assumes both advance together)."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import timedelta

import pytest

from divert.domain.geo import destination_point
from divert.services.sim_clock import (
    MAX_STEPS_PER_TICK,
    RESYNC_AFTER_S,
    fixed_step,
    real_time_step,
)
from tests.support import Clock, city, corridor_at, route_through, start_cmd
from tests.unit.conftest import UnitWorker

C = city("delhi").center
# wall seconds each tick takes on a loaded machine (the period is 0.5 s): overruns of
# 20-180 %, one tick nearly three periods late
OVERRUNNING_TICKS = (0.75, 0.6, 1.4, 0.5, 0.9, 0.75, 1.1)


def test_real_time_step_follows_the_wall_clock_within_limits() -> None:
    assert real_time_step(0.5, 0.5).dt_s == 0.5  # on time
    assert real_time_step(0.75, 0.5).dt_s == 0.75  # a late tick simulates the time it took
    capped = real_time_step(1.4, 0.5)
    assert (capped.dt_s, capped.skipped_s) == (MAX_STEPS_PER_TICK * 0.5, 0.0)  # rest: later
    assert real_time_step(-0.2, 0.5).dt_s == 0.0  # the wall clock stepped back: wait
    stall = real_time_step(RESYNC_AFTER_S + 10.0, 0.5)  # suspended host: skip, not replay
    assert (stall.dt_s, stall.skipped_s, stall.advance_s) == (0.5, RESYNC_AFTER_S + 9.5, 40.0)
    assert fixed_step(5.0).advance_s == 5.0


@pytest.mark.parametrize("bearing", [0.0, 100.0, 200.0, 300.0])
async def test_return_eta_holds_when_ticks_overrun_at_real_time(
    make_worker: Callable[..., UnitWorker], clock: Clock, bearing: float
) -> None:
    w = make_worker("w1")
    assert await w.try_lead()
    assert w.engine is not None
    route = route_through([C, destination_point(C, bearing, 1500.0)], step_m=25.0)
    cmd = start_cmd(route, corridor_at(route, [12, 24, 36, 48]))  # 4 junctions, 300 m apart
    cmd["emergency"], cmd["leg_kind"] = False, "RETURN"  # obeys the signal plans
    await w.send(cmd)
    await w.process_commands()
    v = w.engine.vehicles["delhi"]["AMB-1"]
    predicted = v.eta_s  # the phase-aware replay at departure
    walls = itertools.cycle(OVERRUNNING_TICKS)
    for _ in range(5000):
        if v.travel_s is not None:
            break
        before = w.sim_now
        clock.advance(next(walls))
        await w.tick_once()
        # the clock the plans read advanced exactly as far as the vehicles moved ...
        assert (w.sim_now - before).total_seconds() == pytest.approx(w.engine._step_s)
        # ... and stays within one capped step of the wall clock
        assert timedelta(0) <= clock.now - w.sim_now <= timedelta(seconds=0.5)
    assert v.travel_s is not None
    assert v.travel_s == pytest.approx(predicted, rel=0.10)


async def test_scaled_simulation_keeps_a_fixed_step_whatever_the_wall_clock(
    make_worker: Callable[..., UnitWorker], clock: Clock
) -> None:
    w = make_worker("w1", sim_time_scale=10.0)
    assert await w.try_lead()
    start = w.sim_now
    for wall in (0.5, 1.7, 0.2):
        clock.advance(wall)
        await w.tick_once()
    assert (w.sim_now - start).total_seconds() == pytest.approx(15.0)
    assert w.engine is not None
    assert w.engine._step_s == 5.0
