"""Hospital selection for the TRANSPORT leg (design §1.1). Pure.

1. eligible: AMBULANCE stations (named, active OSM ``amenity=hospital``), excluding
   ``emergency=no``, the non-acute ``healthcare`` kinds below and - unless tagged
   ``emergency=yes`` - single-speciality facilities that cannot take an emergency
   patient (:func:`non_emergency_facility`: dental, eye, skin, fertility, physiotherapy,
   diagnostic and alternative-medicine institutes; by their ``healthcare:speciality``
   tag, or by a name keyword when untagged);
2. the 6 nearest eligible ones (PostGIS KNN, the repository) are routed with the
   crowd-aware scoring; each keeps its best route's cost;
3. a confirmed emergency department (``emergency=yes``) is preferred when its cost is
   within ``max(180 s, 1.25 x the best cost)`` (read literally: an ED reachable within
   3 minutes, or within 1.25 x the best, wins); otherwise the cheapest hospital;
4. the choice and every hospital considered are recorded as ``hospital_choice`` JSON.
"""

from __future__ import annotations

import re
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

# OSM healthcare:speciality values (lower-cased stems, matched inside each ';'/','-separated
# value) of specialities that do not take emergency patients: no emergency department,
# outpatient or elective work only. Assumption (stage 5 fix-2 B6): a hospital whose
# EVERY listed speciality is one of these cannot receive an ambulance patient; one other
# speciality (general, emergency, maternity, ENT, cardiology, ...) keeps it eligible.
NON_EMERGENCY_SPECIALITY_STEMS: Final = (
    # dentistry
    "dent", "orthodont", "implant", "prosthodont", "pedodont", "periodont", "oral", "maxillo",
    # eye
    "ophthalm", "optometr", "optic", "eye", "cataract", "cornea", "retina", "glaucoma",
    # skin and cosmetic
    "dermatolog", "cosmet", "skin",
    # fertility
    "fertility", "ivf",
    # physiotherapy
    "physiother",
    # diagnostics only
    "diagnostic", "radiolog", "patholog", "laborator", "blood_check",
    # alternative medicine (as healthcare=alternative)
    "ayurved", "homeopath", "homoeopath", "unani", "siddha", "naturopath",
)  # fmt: skip
# Fallback for hospitals WITHOUT a speciality tag: whole words of the name that mark one
# of those specialities ("Netra" / "Nethralaya": eye / eye hospital in Hindi, Bengali,
# Kannada). Assumption: they count only BEFORE the word "hospital" ("Netaji Eye
# Hospital", "Dental College & Hospital"); after it they name an extra unit of a general
# hospital ("Leela Hospital and Diagnostic Center", "Sheetla Hospital and Eye Institute").
# A name that also names general care (GENERAL_CARE_NAME) stays eligible.
NON_EMERGENCY_NAME_WORDS: Final = frozenset({
    "dental", "dentist", "dentists", "dentistry", "teeth", "tooth", "orthodontic",
    "orthodontics", "eye", "eyes", "netra", "nethra", "netralaya", "nethralaya",
    "ophthalmic", "ophthalmology", "optical", "opticals", "skin", "dermatology", "ivf",
    "fertility", "physiotherapy", "diagnostic", "diagnostics", "pathology", "ayurveda",
    "ayurvedic", "homoeopathic", "homeopathic", "unani",
})  # fmt: skip
HOSPITAL_WORDS: Final = frozenset({"hospital", "hospitals"})
GENERAL_CARE_NAME: Final = re.compile(
    r"multi[\s-]*special|super[\s-]*special|general|\bmedical\b|trauma|emergency"
)
_SPECIALITY_SPLIT: Final = re.compile(r"[;,]")
_WORD: Final = re.compile(r"[a-z]+")


def specialities(raw: str | None) -> tuple[str, ...]:
    """The values of an OSM ``healthcare:speciality`` tag, lower-cased."""
    return tuple(v for p in _SPECIALITY_SPLIT.split((raw or "").lower()) if (v := p.strip()))


def _non_emergency_speciality(value: str) -> bool:
    return any(stem in value for stem in NON_EMERGENCY_SPECIALITY_STEMS)


def name_says_non_emergency(name: str | None) -> bool:
    """The name-keyword fallback for untagged hospitals (see NON_EMERGENCY_NAME_WORDS)."""
    text = (name or "").lower()
    if GENERAL_CARE_NAME.search(text):
        return False
    words = _WORD.findall(text)
    end = next((i for i, w in enumerate(words) if w in HOSPITAL_WORDS), len(words))
    return any(w in NON_EMERGENCY_NAME_WORDS for w in words[:end])


def non_emergency_facility(speciality: str | None, name: str | None) -> bool:
    """Only specialities that cannot take an emergency patient: every tagged speciality
    is one of them - or, untagged, the name says so."""
    tagged = specialities(speciality)
    if tagged:
        return all(_non_emergency_speciality(v) for v in tagged)
    return name_says_non_emergency(name)


def is_eligible(station: Station) -> bool:
    """Can receive an ambulance patient (a TRANSPORT destination, see the module doc)."""
    if not station.name or station.emergency == "no":
        return False
    if (station.healthcare or "") in NON_ACUTE_HEALTHCARE:
        return False
    if station.emergency == "yes":  # a confirmed emergency department, whatever else
        return True
    return not non_emergency_facility(station.speciality, station.name)


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
            "speciality": s.speciality,
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
