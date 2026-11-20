"""ETA models.

:class:`EtaModel` is the seam for a future learned model (trained on
``dispatches.predicted_eta_s`` vs ``actual_travel_s`` and the position tracks). The
shipped :class:`KinematicEtaModel` integrates the remaining route with exactly the speed
model the simulation uses, assuming every corridor junction ahead will be pre-empted
for the vehicle (which is what DIVERT plans for), so a vehicle that is not delayed by a
conflict arrives at the predicted time.
"""

from __future__ import annotations

import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from divert.domain.enums import ServiceType
from divert.domain.models import City
from divert.domain.route import CorridorSignal, Route
from divert.services.speed_model import SpeedProfile, build_profile


@dataclass(frozen=True, slots=True)
class EtaQuery:
    route: Route
    service: ServiceType
    city: City
    corridor: Sequence[CorridorSignal]
    along_m: float
    speed_mps: float
    at: datetime  # (sim) time of the query; congestion uses the city's local hour


@dataclass(frozen=True, slots=True)
class EtaEstimate:
    to_destination_s: float
    to_signals_s: dict[str, float]  # corridor signals still ahead


class EtaModel(Protocol):
    name: str

    def estimate(self, query: EtaQuery) -> EtaEstimate: ...


def congestion_at(city: City, at: datetime) -> float:
    return city.traffic.factor_for_hour(at.astimezone(city.tz).hour)


def plan_profile(
    route: Route, service: ServiceType, congestion: float, corridor: Sequence[CorridorSignal]
) -> SpeedProfile:
    """Profile with every corridor junction pre-empted for the vehicle."""
    return build_profile(route, service, congestion, [(c.along_m, True) for c in corridor])


class KinematicEtaModel:
    name = "kinematic-v1"

    def __init__(self) -> None:
        # per route object (identity), per congestion factor; freed with the route
        self._cache: weakref.WeakKeyDictionary[Route, dict[float, SpeedProfile]] = (
            weakref.WeakKeyDictionary()
        )

    def profile_for(self, query: EtaQuery) -> SpeedProfile:
        congestion = congestion_at(query.city, query.at)
        by_factor = self._cache.setdefault(query.route, {})
        prof = by_factor.get(congestion)
        if prof is None:
            prof = plan_profile(query.route, query.service, congestion, query.corridor)
            by_factor[congestion] = prof
        return prof

    def estimate(self, query: EtaQuery) -> EtaEstimate:
        prof = self.profile_for(query)
        ahead = [c for c in query.corridor if c.along_m >= query.along_m]
        total, at_signals = prof.time_from(
            query.along_m, query.speed_mps, [c.along_m for c in ahead]
        )
        return EtaEstimate(
            to_destination_s=total,
            to_signals_s={c.signal_id: t for c, t in zip(ahead, at_signals, strict=True)},
        )
