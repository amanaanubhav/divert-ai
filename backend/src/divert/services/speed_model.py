"""Vehicle kinematics along a route (see docs/simulation-model.md).

Speed limit at a point of the route:

* segment cap  = min(vehicle max, OSRM segment speed x city congestion factor (local hour)
  x emergency advantage 1.3);
* junction cap = 35 km/h through a junction pre-empted *for this vehicle*, 15 km/h through
  any other signalised junction on the route (red, or green for a competing vehicle);
* the route end is a stop (arrival crawl speed).

A backward pass turns the point caps into a braking envelope (deceleration 3.0 m/s^2),
so the vehicle slows *before* a junction instead of at it. The simulation moves a
vehicle with ``step`` (accelerate at the vehicle's rate, never above the envelope) and
the ETA integrates the same envelope analytically (``time_from``), so predicted and
simulated travel times agree by construction.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from divert.domain.enums import ServiceType
from divert.domain.route import Route

KMH: Final = 1000.0 / 3600.0
EMERGENCY_ADVANTAGE: Final = 1.3
JUNCTION_NORMAL_MPS: Final = 15.0 * KMH
JUNCTION_PREEMPTED_MPS: Final = 35.0 * KMH
BRAKE_MPS2: Final = 3.0
ARRIVAL_SPEED_MPS: Final = 1.0  # final crawl, keeps the time to the stop finite
MIN_SEGMENT_SPEED_MPS: Final = 1.0  # floor for zero/missing OSRM speeds
ARRIVAL_TOLERANCE_M: Final = 0.5


@dataclass(frozen=True, slots=True)
class VehicleProfile:
    max_speed_mps: float
    accel_mps2: float
    brake_mps2: float = BRAKE_MPS2


PROFILES: Final[dict[ServiceType, VehicleProfile]] = {
    ServiceType.AMBULANCE: VehicleProfile(80.0 * KMH, 2.0),
    ServiceType.FIRE_ENGINE: VehicleProfile(60.0 * KMH, 1.0),
    ServiceType.POLICE: VehicleProfile(90.0 * KMH, 2.5),
}


def segment_cap_mps(osrm_speed_mps: float, congestion: float, profile: VehicleProfile) -> float:
    base = max(MIN_SEGMENT_SPEED_MPS, osrm_speed_mps)
    return max(
        MIN_SEGMENT_SPEED_MPS, min(profile.max_speed_mps, base * congestion * EMERGENCY_ADVANTAGE)
    )


def junction_cap_mps(preempted_for_vehicle: bool) -> float:
    return JUNCTION_PREEMPTED_MPS if preempted_for_vehicle else JUNCTION_NORMAL_MPS


def _interval_time(  # noqa: PLR0917 - hot path, positional on purpose
    v0: float, length: float, cap: float, w_end: float, a: float, b: float
) -> tuple[float, float]:
    """Minimum time over one interval of constant cap, entering at v0 and leaving at
    most at w_end (accelerate at a, cruise at cap, brake at b). Returns (time, v_exit)."""
    if length <= 0.0:
        return 0.0, v0
    v0 = min(v0, cap, math.sqrt(w_end * w_end + 2.0 * b * length))
    w = min(w_end, cap)
    v_acc = math.sqrt(v0 * v0 + 2.0 * a * length)
    if v_acc <= w:  # never needs to brake in this interval
        return (v_acc - v0) / a, v_acc
    s1 = (w * w + 2.0 * b * length - v0 * v0) / (2.0 * (a + b))
    peak = math.sqrt(v0 * v0 + 2.0 * a * s1)
    if peak <= cap:
        return (peak - v0) / a + (peak - w) / b, w
    d1 = (cap * cap - v0 * v0) / (2.0 * a)
    d3 = (cap * cap - w * w) / (2.0 * b)
    d2 = max(0.0, length - d1 - d3)
    return (cap - v0) / a + d2 / cap + (cap - w) / b, w


class SpeedProfile:
    """Speed limits along one route for one vehicle under fixed conditions.

    Knots are the route vertices plus the junction positions; each interval between two
    knots has a constant cap; each knot may carry a point cap (junction) and ``limit[k]``
    is the highest speed at knot k from which every later cap can still be honoured.
    """

    __slots__ = ("_caps", "_junction_knots", "_limit", "_x", "profile", "total_m")

    def __init__(
        self,
        cumulative: Sequence[float],
        segment_caps: Sequence[float],
        junctions: Sequence[tuple[float, float]],
        profile: VehicleProfile,
    ) -> None:
        if len(segment_caps) != len(cumulative) - 1:
            raise ValueError("need one cap per segment")
        self.profile = profile
        self.total_m = float(cumulative[-1])
        point_caps: dict[float, float] = {}
        for along, cap in junctions:
            x = min(max(0.0, float(along)), self.total_m)
            point_caps[x] = min(cap, point_caps.get(x, math.inf))
        xs = sorted(set(cumulative) | set(point_caps))
        # cap of the interval [xs[k], xs[k+1]] = cap of the route segment containing it
        caps: list[float] = []
        for k in range(len(xs) - 1):
            mid = 0.5 * (xs[k] + xs[k + 1])
            seg = min(max(bisect.bisect_right(cumulative, mid) - 1, 0), len(segment_caps) - 1)
            caps.append(segment_caps[seg])
        n = len(xs) - 1
        limit = [math.inf] * (n + 1)
        limit[n] = ARRIVAL_SPEED_MPS
        b = profile.brake_mps2
        for k in range(n - 1, -1, -1):
            reach = math.sqrt(limit[k + 1] ** 2 + 2.0 * b * (xs[k + 1] - xs[k]))
            lim = min(caps[k], reach, point_caps.get(xs[k], math.inf))
            if k > 0:
                lim = min(lim, caps[k - 1])
            limit[k] = lim
        self._x = xs
        self._caps = caps
        self._limit = limit
        self._junction_knots = {x: i for i, x in enumerate(xs) if x in point_caps}

    def _interval(self, x: float) -> int:
        return min(max(bisect.bisect_right(self._x, x) - 1, 0), len(self._caps) - 1)

    def allowed_speed(self, x: float) -> float:
        """Highest speed allowed at distance ``x`` along the route (cap and envelope)."""
        if x >= self.total_m:
            return ARRIVAL_SPEED_MPS
        k = self._interval(x)
        b = self.profile.brake_mps2
        v = min(self._caps[k], math.sqrt(self._limit[k + 1] ** 2 + 2.0 * b * (self._x[k + 1] - x)))
        if x <= self._x[k]:  # exactly on a knot: its point cap (junction) applies too
            v = min(v, self._limit[k])
        return v

    def step(self, along_m: float, speed_mps: float, dt_s: float) -> tuple[float, float]:
        """Advance one simulation step; returns (along_m, speed_mps)."""
        a = self.profile.accel_mps2
        v1 = min(speed_mps + a * dt_s, self.allowed_speed(along_m))
        x1 = min(self.total_m, along_m + 0.5 * (speed_mps + v1) * dt_s)
        v1 = min(v1, self.allowed_speed(x1))
        return x1, max(0.0, v1)

    def time_from(
        self, along_m: float, speed_mps: float, targets: Sequence[float] = ()
    ) -> tuple[float, list[float]]:
        """Minimum time from (along_m, speed_mps) to the route end, and to each target
        position (targets behind ``along_m`` get 0). Exact for this envelope."""
        a, b = self.profile.accel_mps2, self.profile.brake_mps2
        x = min(max(0.0, along_m), self.total_m)
        k = self._interval(x)
        v = min(speed_mps, self.allowed_speed(x))
        order = sorted(range(len(targets)), key=lambda i: targets[i])
        out = [0.0] * len(targets)
        ti = 0
        while ti < len(order) and targets[order[ti]] <= x:
            ti += 1
        t = 0.0
        pos = x
        n = len(self._caps)
        while k < n:
            end = self._x[k + 1]
            # targets inside this interval (not on a knot) are resolved by splitting it
            while ti < len(order) and targets[order[ti]] < end:
                tx = targets[order[ti]]
                if tx > pos:
                    w = math.sqrt(self._limit[k + 1] ** 2 + 2.0 * b * (end - tx))
                    dt, v = _interval_time(v, tx - pos, self._caps[k], w, a, b)
                    t += dt
                    pos = tx
                out[order[ti]] = t
                ti += 1
            dt, v = _interval_time(v, end - pos, self._caps[k], self._limit[k + 1], a, b)
            t += dt
            pos = end
            while ti < len(order) and targets[order[ti]] <= end:
                out[order[ti]] = t
                ti += 1
            k += 1
        # Targets at or past the route end (a junction at the destination whose projected
        # along_m rounds a few cm beyond the last vertex) are reached on arrival. Left at 0
        # they read as "imminent" and would be claimed from the first tick to the last.
        while ti < len(order):
            out[order[ti]] = t
            ti += 1
        return t, out


def build_profile(
    route: Route,
    service: ServiceType,
    congestion: float,
    junctions: Sequence[tuple[float, bool]],
) -> SpeedProfile:
    """``junctions``: (along_m, preempted_for_this_vehicle) for each corridor signal ahead."""
    prof = PROFILES[service]
    caps = [segment_cap_mps(s, congestion, prof) for s in route.segment_speeds_mps]
    return SpeedProfile(
        route.cumulative,
        caps,
        [(along, junction_cap_mps(pre)) for along, pre in junctions],
        prof,
    )
