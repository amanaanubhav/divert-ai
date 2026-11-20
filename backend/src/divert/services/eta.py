"""ETA models.

:class:`EtaModel` is the seam for a future learned model (trained on
``dispatch_legs.predicted_eta_s`` vs ``actual_travel_s`` and the position tracks). The
shipped :class:`KinematicEtaModel` uses exactly the motion model the simulation uses -
the same per-segment traffic factors (class-aware congestion and incident zones,
:mod:`divert.services.traffic`) and the same junction caps:

* **emergency legs** (RESPONSE, TRANSPORT) integrate the speed envelope analytically and
  assume every corridor junction ahead will be pre-empted for the vehicle (which is what
  DIVERT plans for), so a vehicle that is not delayed by a conflict arrives on time;
* **non-emergency legs** (RETURN) are *replayed* tick by tick with the simulation's own
  obey-the-signal motion (:func:`divert.services.obey.replay`) against the junctions'
  deterministic fixed plans (``phase_at``), so the predicted waits at red are the waits
  the vehicle will actually have - unless another vehicle's pre-emption changes a
  junction's plan in between. This is ``kinematic-v3``; v2 added the expected wait
  (red_share x red / 2) per junction instead, which missed the +-10 % on short legs.
"""

from __future__ import annotations

import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from divert.domain.enums import ServiceType
from divert.domain.models import City
from divert.domain.route import CorridorSignal, Route
from divert.domain.signal_plan import JunctionGeometry, PhasePlan
from divert.services.obey import GreenView, motion_caps, replay
from divert.services.preemption import PreemptionRules
from divert.services.speed_model import (
    JUNCTION_NORMAL_MPS,
    JUNCTION_PREEMPTED_MPS,
    Factors,
    SpeedProfile,
    build_profile,
)
from divert.services.traffic import FREE_FLOW, TrafficModel, city_factor

DEFAULT_STEP_S: Final = 0.5  # sim seconds per tick at DIVERT_SIM_TICK_S=0.5, scale 1
_DEFAULT_RULES: Final = PreemptionRules()


@dataclass(frozen=True, slots=True)
class EtaQuery:
    route: Route
    service: ServiceType
    city: City
    corridor: Sequence[CorridorSignal]
    along_m: float
    speed_mps: float
    at: datetime  # (sim) time of the query; congestion uses the city's local hour
    emergency: bool = True
    traffic: TrafficModel | None = None  # None: class-aware congestion, no incident zones
    exempt_incident_id: str | None = None  # the vehicle's own incident zone
    # --- non-emergency replay only -----------------------------------------------------
    signals: GreenView | None = None  # None: the fixed plans (geometry from the corridor)
    step_s: float = DEFAULT_STEP_S  # the simulation's sim seconds per tick
    rules: PreemptionRules | None = None
    passed: frozenset[str] = frozenset()  # corridor keys already behind the vehicle
    stop_for: str | None = None  # corridor key of the stop line it already holds for

    @property
    def traffic_model(self) -> TrafficModel:
        return self.traffic if self.traffic is not None else FREE_FLOW


@dataclass(frozen=True, slots=True)
class EtaEstimate:
    to_destination_s: float
    to_signals_s: dict[str, float]  # corridor key -> ETA, for corridor signals still ahead


class EtaModel(Protocol):
    name: str

    def estimate(self, query: EtaQuery) -> EtaEstimate: ...


def congestion_at(city: City, at: datetime) -> float:
    """The city's hourly factor (the class-aware factor of an ARTERIAL)."""
    return city_factor(city, at)


def plan_junction_caps(
    corridor: Sequence[CorridorSignal], *, emergency: bool
) -> list[tuple[float, float]]:
    """Junction caps of the *plan*: emergency legs expect every junction pre-empted."""
    cap = JUNCTION_PREEMPTED_MPS if emergency else JUNCTION_NORMAL_MPS
    return [(c.along_m, JUNCTION_PREEMPTED_MPS if c.free_left else cap) for c in corridor]


def plan_profile(
    route: Route,
    service: ServiceType,
    factors: Factors,
    corridor: Sequence[CorridorSignal],
    *,
    emergency: bool = True,
) -> SpeedProfile:
    return build_profile(
        route,
        service,
        factors,
        plan_junction_caps(corridor, emergency=emergency),
        emergency=emergency,
    )


def segment_factors(query: EtaQuery) -> tuple[float, ...]:
    return query.traffic_model.segment_factors(
        query.route, query.city, query.at, exempt_incident_id=query.exempt_incident_id
    )


def plan_geometry(c: CorridorSignal) -> JunctionGeometry:
    """Junction geometry from the corridor alone: OSRM bearings, else synthetic from this
    approach (what the junction registry creates for a junction nobody used yet)."""
    return JunctionGeometry.from_osrm(c.junction_bearings) or JunctionGeometry.synthetic(
        c.approach_bearing, c.legs
    )


def expected_signal_wait_s(c: CorridorSignal) -> float:
    """Mean wait at a junction for a vehicle that obeys its fixed plan (red_share x red/2).
    Shown for explainability; the RETURN ETA uses the deterministic replay instead."""
    if c.free_left:
        return 0.0
    return PhasePlan.for_junction(c.signal_id, plan_geometry(c).legs).expected_wait_s()


class PlanSignals:
    """The junctions' fixed plans only (no pre-emption). ``geometry`` pins a junction's
    approaches (e.g. the simulation's registry); otherwise :func:`plan_geometry`."""

    def __init__(self, geometry: dict[str, JunctionGeometry] | None = None) -> None:
        self._geometry = dict(geometry or {})
        self._cache: dict[str, tuple[PhasePlan, int]] = {}

    def _plan(self, c: CorridorSignal) -> tuple[PhasePlan, int]:
        hit = self._cache.get(c.key)
        if hit is None:
            geo = self._geometry.get(c.signal_id) or plan_geometry(c)
            hit = (PhasePlan.for_junction(c.signal_id, geo.legs), geo.phase_for(c.approach_bearing))
            self._cache[c.key] = hit
        return hit

    def is_green(self, c: CorridorSignal, t: float) -> bool:
        plan, phase = self._plan(c)
        return plan.status(t).is_green(phase)


class KinematicEtaModel:
    name = "kinematic-v3"

    def __init__(self) -> None:
        # per route object (identity), per traffic state; freed with the route
        self._cache: weakref.WeakKeyDictionary[Route, dict[tuple[object, ...], SpeedProfile]] = (
            weakref.WeakKeyDictionary()
        )

    def profile_for(self, query: EtaQuery) -> SpeedProfile:
        key = (
            *query.traffic_model.cache_key(query.city, query.at),
            query.emergency,
            query.exempt_incident_id,
            len(query.corridor),
        )
        by_key = self._cache.setdefault(query.route, {})
        prof = by_key.get(key)
        if prof is None:
            if len(by_key) > 32:  # traffic changes hourly; keep the cache small
                by_key.clear()
            prof = plan_profile(
                query.route,
                query.service,
                segment_factors(query),
                query.corridor,
                emergency=query.emergency,
            )
            by_key[key] = prof
        return prof

    def estimate(self, query: EtaQuery) -> EtaEstimate:
        if not query.emergency:
            return self._replay(query)
        prof = self.profile_for(query)
        ahead = [c for c in query.corridor if c.along_m >= query.along_m]
        total, at_signals = prof.time_from(
            query.along_m, query.speed_mps, [c.along_m for c in ahead]
        )
        return EtaEstimate(
            to_destination_s=total,
            to_signals_s={c.key: t for c, t in zip(ahead, at_signals, strict=True)},
        )

    def _replay(self, query: EtaQuery) -> EtaEstimate:
        """Non-emergency: tick-by-tick replay of the obey-the-signal motion."""
        route, city, traffic = query.route, query.city, query.traffic_model
        fast = frozenset(c.key for c in query.corridor if c.free_left)
        factors: dict[tuple[object, ...], tuple[float, ...]] = {}
        profiles: dict[tuple[object, ...], SpeedProfile] = {}

        def profile_at(stop: float | None, t: float) -> SpeedProfile:
            at = datetime.fromtimestamp(t, UTC)
            fkey = traffic.cache_key(city, at)
            f = factors.get(fkey)
            if f is None:
                f = factors[fkey] = traffic.segment_factors(
                    route, city, at, exempt_incident_id=query.exempt_incident_id
                )
            pkey = (*fkey, stop)
            prof = profiles.get(pkey)
            if prof is None:
                prof = profiles[pkey] = build_profile(
                    route,
                    query.service,
                    f,
                    motion_caps(query.corridor, fast, stop),
                    emergency=False,
                )
            return prof

        res = replay(
            corridor=query.corridor,
            length_m=route.length_m,
            profile_at=profile_at,
            along_m=query.along_m,
            speed_mps=query.speed_mps,
            t0=query.at.timestamp(),
            dt=query.step_s,
            signals=query.signals or PlanSignals(),
            rules=query.rules or _DEFAULT_RULES,
            passed=query.passed,
            committed=query.stop_for,
        )
        return EtaEstimate(to_destination_s=res.total_s, to_signals_s=res.to_signals_s)
