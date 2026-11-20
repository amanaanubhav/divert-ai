"""Hospital selection for the TRANSPORT leg (design §1.1). Pure.

1. eligible: AMBULANCE stations (named, active OSM ``amenity=hospital``), excluding
   ``emergency=no`` and the non-acute ``healthcare`` kinds below;
2. the 6 nearest eligible ones (PostGIS KNN, the repository) are routed with the
   crowd-aware scoring; each keeps its best route's cost;
3. a confirmed emergency department (``emergency=yes``) is preferred when its cost is
   within ``max(180 s, 1.25 x the best cost)`` (read literally: an ED reachable within
   3 minutes, or within 1.25 x the best, wins); otherwise the cheapest hospital;
4. the choice and every hospital considered are recorded as ``hospital_choice`` JSON.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from divert.domain.models import Station

# healthcare=* values that are not an acute hospital (design §1.1)
NON_ACUTE_HEALTHCARE: Final = frozenset(
    {"dentist", "blood_bank", "alternative", "doctor", "birthing_centre"}
)
ED_TOLERANCE_ABS_S: Final = 180.0
ED_TOLERANCE_SHARE: Final = 1.25


def is_eligible(station: Station) -> bool:
    return (
        bool(station.name)
        and station.emergency != "no"
        and (station.healthcare or "") not in NON_ACUTE_HEALTHCARE
    )


@dataclass(frozen=True, slots=True)
class HospitalOption:
    """One hospital considered: its best route's cost (None when it could not be routed)."""

    station: Station
    straight_m: float
    cost_s: float | None = None
    eta_s: float | None = None
    distance_m: float | None = None
    route_label: str | None = None
    error: str | None = None

    @property
    def confirmed_ed(self) -> bool:
        return self.station.emergency == "yes"

    def to_json(self) -> dict[str, Any]:
        s = self.station
        return {
            "station_id": s.id,
            "name": s.name,
            "location": [s.pos.lat, s.pos.lng],
            "emergency": s.emergency,
            "healthcare": s.healthcare,
            "straight_line_m": round(self.straight_m, 1),
            "cost_s": None if self.cost_s is None else round(self.cost_s, 1),
            "eta_s": None if self.eta_s is None else round(self.eta_s, 1),
            "distance_m": None if self.distance_m is None else round(self.distance_m, 1),
            "route": self.route_label,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class HospitalChoice:
    chosen: HospitalOption
    options: tuple[HospitalOption, ...]
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "chosen": self.chosen.station.id,
            "reason": self.reason,
            "ed_tolerance_s": round(ed_tolerance_s(self._best_cost()), 1),
            "candidates": [o.to_json() for o in self.options],
        }

    def _best_cost(self) -> float:
        return min(o.cost_s for o in self.options if o.cost_s is not None)


def ed_tolerance_s(best_cost_s: float) -> float:
    """Highest cost at which a confirmed ED is still preferred."""
    return max(ED_TOLERANCE_ABS_S, ED_TOLERANCE_SHARE * best_cost_s)


def choose_hospital(options: Sequence[HospitalOption]) -> HospitalChoice:
    """Pick the destination hospital (see module doc). Raises ValueError when no option
    could be routed."""
    routed = [o for o in options if o.cost_s is not None]
    if not routed:
        raise ValueError("no hospital could be routed")
    best = min(routed, key=lambda o: (o.cost_s, o.straight_m, o.station.id))
    assert best.cost_s is not None
    eds = [o for o in routed if o.confirmed_ed]
    best_ed = min(eds, key=lambda o: (o.cost_s, o.straight_m, o.station.id)) if eds else None
    limit = ed_tolerance_s(best.cost_s)
    if best_ed is not None and best_ed.cost_s is not None and best_ed.cost_s <= limit + 1e-9:
        chosen = best_ed
        if best_ed is best:
            reason = f"{best.station.name}: fastest, confirmed emergency department"
        else:
            reason = (
                f"{best_ed.station.name}: confirmed emergency department, "
                f"{(best_ed.cost_s - best.cost_s) / 60.0:.1f} min more than "
                f"{best.station.name} (within the {limit / 60.0:.1f} min tolerance)"
            )
    else:
        chosen = best
        reason = f"{best.station.name}: fastest"
        if best_ed is not None and best_ed.cost_s is not None:
            reason += (
                f" (nearest confirmed emergency department {best_ed.station.name} is "
                f"{best_ed.cost_s / 60.0:.1f} min, above the {limit / 60.0:.1f} min tolerance)"
            )
        elif not eds:
            reason += " (no confirmed emergency department among the nearest hospitals)"
    return HospitalChoice(chosen, tuple(options), reason)
