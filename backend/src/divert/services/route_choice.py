"""Crowd-aware route choice (design §2). Pure.

``cost = predicted travel time (TrafficModel + speed model) + junction delays + penalties``

* junction delay per corridor junction - emergency legs: STRAIGHT 0 s, SLIGHT 1 s, LEFT
  3 s (0 for a free left), RIGHT 6 s, SHARP 8 s, U_TURN 12 s, ROUNDABOUT 4 + 2 x exit s;
  non-emergency legs (RETURN) add the same turn delays; their signal waits are already in
  the travel time (the phase-aware replay of :mod:`divert.services.eta`), so they are not
  added again (stage A added red_share x red / 2 on top - counted twice);
* penalties - corridor contention: 10 s per junction another active emergency vehicle is
  expected at within +-20 s.

Candidates longer than 1.6 x the shortest are dropped. The cheapest wins; candidates
within 5 % of it prefer fewer signalised right / U turns, then fewer contested
junctions, then the shorter distance. Every candidate is recorded in ``route_choice``
JSON with the reason shown in the UI.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from divert.domain.enums import Movement, ServiceType
from divert.domain.models import City, IncidentZone
from divert.domain.route import CorridorSignal, Route
from divert.services.eta import DEFAULT_STEP_S, EtaModel, EtaQuery
from divert.services.obey import GreenView
from divert.services.preemption import PreemptionRules
from divert.services.traffic import (
    CONTENTION_DELAY_S,
    UNCONGESTED,
    TrafficModel,
    contention_junctions,
    crowd_index,
)

TURN_DELAY_S: Final[dict[Movement, float]] = {
    Movement.STRAIGHT: 0.0,
    Movement.SLIGHT_LEFT: 1.0,
    Movement.SLIGHT_RIGHT: 1.0,
    Movement.LEFT: 3.0,
    Movement.RIGHT: 6.0,
    Movement.SHARP_LEFT: 8.0,
    Movement.SHARP_RIGHT: 8.0,
    Movement.U_TURN: 12.0,
}
ROUNDABOUT_BASE_S: Final = 4.0
ROUNDABOUT_PER_EXIT_S: Final = 2.0
ROUNDABOUT_UNKNOWN_EXIT: Final = 2  # assumption when OSRM gives no exit number
MAX_LENGTH_RATIO: Final = 1.6
TIE_SHARE: Final = 0.05
CONGESTED_NOTE_MIN: Final = 0.05  # crowd-index gap worth naming in the reason
LONGER_NOTE_SHARE: Final = 0.10
# turns across the oncoming traffic (India drives on the left)
RIGHT_TURNS: Final = frozenset({Movement.RIGHT, Movement.SHARP_RIGHT, Movement.U_TURN})
_LABELS: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def turn_delay_s(c: CorridorSignal) -> float:
    if c.movement is Movement.ROUNDABOUT:
        exits = c.roundabout_exit if c.roundabout_exit is not None else ROUNDABOUT_UNKNOWN_EXIT
        return ROUNDABOUT_BASE_S + ROUNDABOUT_PER_EXIT_S * exits
    if c.free_left:
        return 0.0
    return TURN_DELAY_S[c.movement]


def junction_delay_s(c: CorridorSignal, *, emergency: bool) -> float:
    """The turn delay - for every leg: a non-emergency leg's signal waits are already in
    its replayed ETA (``emergency`` is kept so callers state which leg they score)."""
    del emergency
    return turn_delay_s(c)


def signalised_right_turns(corridor: Sequence[CorridorSignal]) -> int:
    return sum(1 for c in corridor if c.movement in RIGHT_TURNS)


def label(i: int) -> str:
    return _LABELS[i] if i < len(_LABELS) else f"R{i + 1}"


def congested_road(route: Route, factors: Sequence[float]) -> tuple[str | None, float]:
    """(name, delay share) of the step span that loses the most time to crowding."""
    steps = [m for m in route.maneuvers if m.type != "arrive"]
    if not steps or not factors:
        return None, 0.0
    worst_name: str | None = None
    worst = 0.0
    for k, m in enumerate(steps):
        lo = m.along_m
        hi = steps[k + 1].along_m if k + 1 < len(steps) else route.length_m
        loss = 0.0
        for i, f in enumerate(factors):
            a, b = route.cumulative[i], route.cumulative[i + 1]
            overlap = min(b, hi) - max(a, lo)
            if overlap > 0:
                loss += overlap * max(0.0, 1.0 - f)
        if loss > worst:
            worst, worst_name = loss, m.name
    return worst_name, worst / route.length_m if route.length_m > 0 else 0.0


@dataclass(frozen=True, slots=True)
class Candidate:
    label: str
    source: str  # "osrm" | "detour"
    route: Route
    corridor: tuple[CorridorSignal, ...]
    eta_s: float  # congested travel time (the ETA)
    base_s: float  # same route at free flow (no congestion, no zones)
    junction_delay_s: float
    penalties_s: float
    crowd_index: float
    right_turns: int
    contention: tuple[str, ...] = ()
    zones: tuple[IncidentZone, ...] = ()
    congested_road: str | None = None
    notes: tuple[str, ...] = field(default=())

    @property
    def cost_s(self) -> float:
        return self.eta_s + self.junction_delay_s + self.penalties_s

    @property
    def distance_m(self) -> float:
        return self.route.length_m

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "source": self.source,
            "distance_m": round(self.distance_m, 1),
            "base_time_s": round(self.base_s, 1),
            "congested_time_s": round(self.eta_s, 1),
            "junction_delay_s": round(self.junction_delay_s, 1),
            "penalties_s": round(self.penalties_s, 1),
            "cost_s": round(self.cost_s, 1),
            "crowd_index": round(self.crowd_index, 3),
            "signals": len(self.corridor),
            "right_turns": self.right_turns,
            "contention_junctions": list(self.contention),
            "incident_zones": [
                {"incident_id": z.incident_id, "type": z.type.value} for z in self.zones
            ],
            "congested_road": self.congested_road,
        }


def make_candidate(
    index: int,
    route: Route,
    corridor: Sequence[CorridorSignal],
    *,
    eta_s: float,
    base_s: float,
    factors: Sequence[float],
    emergency: bool,
    contention: Sequence[str] = (),
    zones: Sequence[IncidentZone] = (),
    source: str = "osrm",
) -> Candidate:
    name, _ = congested_road(route, factors)
    return Candidate(
        label=label(index),
        source=source,
        route=route,
        corridor=tuple(corridor),
        eta_s=eta_s,
        base_s=base_s,
        junction_delay_s=sum(junction_delay_s(c, emergency=emergency) for c in corridor),
        penalties_s=CONTENTION_DELAY_S * len(contention),
        crowd_index=crowd_index(route, factors),
        right_turns=signalised_right_turns(corridor),
        contention=tuple(contention),
        zones=tuple(zones),
        congested_road=name,
    )


def score_route(
    index: int,
    route: Route,
    corridor: Sequence[CorridorSignal],
    *,
    eta_model: EtaModel,
    city: City,
    service: ServiceType,
    at: datetime,
    traffic: TrafficModel,
    others: Sequence[tuple[str, str, float]] = (),
    emergency: bool = True,
    exempt_incident_id: str | None = None,
    signals: GreenView | None = None,
    step_s: float = DEFAULT_STEP_S,
    rules: PreemptionRules | None = None,
) -> Candidate:
    """Score one route candidate exactly as the leg will be simulated: the ETA under the
    traffic model (a RETURN leg: the phase-aware replay against ``signals`` at ``step_s``
    per tick), the free-flow base time, junction delays and corridor contention."""
    query = EtaQuery(
        route,
        service,
        city,
        corridor,
        0.0,
        0.0,
        at,
        emergency=emergency,
        traffic=traffic,
        exempt_incident_id=exempt_incident_id,
        signals=signals,
        step_s=step_s,
        rules=rules,
    )
    eta = eta_model.estimate(query)
    base = eta_model.estimate(dataclasses.replace(query, traffic=UNCONGESTED))
    own = [(c.signal_id, eta.to_signals_s.get(c.key, math.inf)) for c in corridor]
    return make_candidate(
        index,
        route,
        corridor,
        eta_s=eta.to_destination_s,
        base_s=base.to_destination_s,
        factors=traffic.segment_factors(route, city, at, exempt_incident_id=exempt_incident_id),
        emergency=emergency,
        contention=contention_junctions(own, others),
        zones=traffic.zone_hits(route, exempt_incident_id=exempt_incident_id),
        source=route.source,
    )


def within_length(routes: Sequence[Route], ratio: float = MAX_LENGTH_RATIO) -> list[int]:
    """Indices of routes no longer than ``ratio`` x the shortest (order kept)."""
    if not routes:
        return []
    shortest = min(r.length_m for r in routes)
    return [i for i, r in enumerate(routes) if r.length_m <= ratio * shortest + 1e-6]


@dataclass(frozen=True, slots=True)
class Choice:
    chosen: Candidate
    candidates: tuple[Candidate, ...]
    reason: str
    tie_break: str | None  # the criterion that decided among near-equal costs

    def to_json(self) -> dict[str, Any]:
        return {
            "chosen": self.chosen.label,
            "reason": self.reason,
            "tie_break": self.tie_break,
            "candidates": [c.to_json() for c in self.candidates],
        }


def _tie_key(c: Candidate) -> tuple[float, ...]:
    return (c.right_turns, len(c.contention), c.distance_m, c.cost_s)


def _differences(winner: Candidate, other: Candidate) -> list[str]:
    notes: list[str] = []
    if other.crowd_index > winner.crowd_index + CONGESTED_NOTE_MIN:
        notes.append(f"{other.congested_road} congested" if other.congested_road else "congested")
    mine = {z.incident_id for z in winner.zones}
    for z in other.zones:
        if z.incident_id not in mine:
            notes.append(f"{z.type.value.lower()} incident on the way")
    extra_turns = other.right_turns - winner.right_turns
    if extra_turns > 0:
        notes.append(f"{extra_turns} extra right turn{'s' if extra_turns > 1 else ''}")
    extra_cont = len(other.contention) - len(winner.contention)
    if extra_cont > 0:
        notes.append(f"{extra_cont} contested junction{'s' if extra_cont > 1 else ''}")
    if other.distance_m > winner.distance_m * (1 + LONGER_NOTE_SHARE):
        notes.append(f"{(other.distance_m - winner.distance_m) / 1000:.1f} km longer")
    return notes


def choose(candidates: Sequence[Candidate], *, tie_share: float = TIE_SHARE) -> Choice:
    """Pick the best candidate and explain why (see module doc)."""
    if not candidates:
        raise ValueError("no route candidates")
    best_cost = min(c.cost_s for c in candidates)
    near = [c for c in candidates if c.cost_s <= best_cost * (1 + tie_share) + 1e-9]
    chosen = min(near, key=_tie_key)
    cheapest = min(candidates, key=lambda c: (c.cost_s, c.label))
    tie_break: str | None = None
    if chosen is not cheapest:
        if chosen.right_turns < cheapest.right_turns:
            tie_break = "fewer right turns"
        elif len(chosen.contention) < len(cheapest.contention):
            tie_break = "fewer contested junctions"
        else:
            tie_break = "shorter"
    others = sorted((c for c in candidates if c is not chosen), key=lambda c: c.cost_s)
    if not others:
        reason = f"Route {chosen.label}: the only route"
    elif tie_break is not None:
        reason = f"Route {chosen.label}: within {tie_share:.0%} of {cheapest.label}, {tie_break}"
    else:
        runner = others[0]
        gain = (runner.cost_s - chosen.cost_s) / 60.0
        notes = _differences(chosen, runner)
        reason = f"Route {chosen.label}: {gain:.1f} min faster than {runner.label}"
        if notes:
            reason += f" ({runner.label}: {', '.join(notes)})"
    return Choice(chosen, tuple(candidates), reason, tie_break)
