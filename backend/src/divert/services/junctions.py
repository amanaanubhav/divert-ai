"""What each signalised junction is showing (design §4.1, §4.3, §4.5). Pure, in memory.

A :class:`JunctionControl` runs the junction's fixed plan (:mod:`divert.domain.signal_plan`)
except during *overrides* - explicit intervals of (phase, stage) laid down when a phase is
served for an emergency claim (red truncation: rest of the minimum green, amber, all-red,
then green held open-ended; or green extension: the current green held) and when it is
released (amber, all-red, then the most-starved approach gets green for ``recovery_s``,
amber + all-red, back to the plan). ``status(t)`` answers for any instant, so speed
advisories and the obey-the-signal motion of non-emergency vehicles look ahead exactly.

The :class:`JunctionRegistry` holds one control per (city, junction). Geometry comes from
the corridor data: OSRM intersection bearings when known (preferred, and upgraded to
while the junction is idle), else 4 approaches spaced evenly from the first observed
approach. Controls are rebuilt after a leader failover from the restored claims.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final

from divert.domain.enums import JunctionState, PhaseStateAtClaim
from divert.domain.route import CorridorSignal
from divert.domain.signal_plan import (
    ALL_RED_S,
    AMBER_S,
    MIN_GREEN_S,
    JunctionGeometry,
    PhasePlan,
    PhaseStatus,
    Stage,
)

STARVATION_WINDOW_S: Final = 300.0  # "the last 5 minutes"
IDLE_TTL_S: Final = 300.0  # an idle control is forgotten this long after its last use
STARVATION_SHARE: Final = 0.5  # pre-empted for more than half of it
_KEEP_OVERRIDES_S: Final = 60.0  # history kept for status() of the recent past


@dataclass(slots=True)
class Interval:
    start: float
    end: float  # math.inf = until changed
    phase: int
    stage: Stage


class JunctionControl:
    def __init__(self, signal_id: str, geometry: JunctionGeometry) -> None:
        self.signal_id = signal_id
        self.geometry = geometry
        self.plan = PhasePlan.for_junction(signal_id, geometry.legs)
        self.overrides: list[Interval] = []
        self.served_phase: int | None = None
        self.green_at: float | None = None  # when the served phase is/was green
        self.state_at_claim: PhaseStateAtClaim | None = None
        self.recover_until: float | None = None
        self.recovery_phase: int | None = None
        self._served_since: float | None = None
        self._served_log: list[tuple[float, float]] = []  # closed served periods
        self.last_used: float = -math.inf  # sim epoch (registry bookkeeping)

    # ------------------------------------------------------------------ queries
    def status(self, t: float, *, open_holds: bool = True) -> PhaseStatus:
        """What the junction shows at ``t``. ``open_holds=False`` ignores a hold still open
        (its end is unknown) - used to *predict* a junction, e.g. for a RETURN ETA."""
        for iv in reversed(self.overrides):
            if not open_holds and math.isinf(iv.end):
                continue
            if iv.start <= t < iv.end:
                elapsed = t - iv.start
                remaining = iv.end - t
                green_elapsed = elapsed if iv.stage is Stage.GREEN else self._green_len(iv)
                return PhaseStatus(iv.phase, iv.stage, elapsed, remaining, green_elapsed)
        return self.plan.status(t)

    def _green_len(self, iv: Interval) -> float:
        for g in reversed(self.overrides):
            if g.phase == iv.phase and g.stage is Stage.GREEN and g.end <= iv.start:
                return g.end - g.start
        return MIN_GREEN_S

    def is_green_for(self, phase: int, t: float) -> bool:
        return self.status(t).is_green(phase)

    def next_green_at(self, phase: int, t: float) -> float:
        """When ``phase`` is next green (``t`` if now; ``inf`` while the junction is held
        for another phase) - scans the overrides, then the plan after them."""
        if self.is_green_for(phase, t):
            return t
        for iv in self.overrides:
            if iv.end > t and iv.phase == phase and iv.stage is Stage.GREEN:
                return max(t, iv.start)
        tail = max((iv.end for iv in self.overrides), default=t)
        if math.isinf(tail):
            return math.inf
        return self.plan.next_green_at(phase, max(t, tail))

    def state(self, t: float, *, queued: bool = False) -> JunctionState:
        if self.served_phase is not None:
            if self.green_at is None or self.green_at > t:
                return JunctionState.TRANSITIONING
            return JunctionState.QUEUED if queued else JunctionState.PREEMPTED
        if self.recover_until is not None and t < self.recover_until:
            return JunctionState.RECOVERING
        return JunctionState.NORMAL

    def preempted_share(self, t: float, window_s: float = STARVATION_WINDOW_S) -> float:
        lo = t - window_s
        busy = sum(max(0.0, min(e, t) - max(s, lo)) for s, e in self._served_log)
        if self._served_since is not None:
            busy += max(0.0, t - max(self._served_since, lo))
        return busy / window_s

    def is_starved(self, t: float) -> bool:
        return self.preempted_share(t) > STARVATION_SHARE

    def is_idle(self, t: float) -> bool:
        """Nothing to remember: not served, not recovering, nothing in the starvation
        window, and not used for IDLE_TTL_S (keeps a synthetic geometry stable)."""
        return (
            self.served_phase is None
            and (self.recover_until is None or t >= self.recover_until)
            and all(e < t - STARVATION_WINDOW_S for _, e in self._served_log)
            and t - self.last_used > IDLE_TTL_S
        )

    # ------------------------------------------------------------------ changes
    def _cut(self, t: float) -> None:
        kept: list[Interval] = []
        for iv in self.overrides:
            if iv.start >= t:
                continue
            iv.end = min(iv.end, t)
            if iv.end > t - _KEEP_OVERRIDES_S:
                kept.append(iv)
        self.overrides = kept

    def _add(self, start: float, length: float, phase: int, stage: Stage) -> float:
        end = start + length
        if length > 0:
            self.overrides.append(Interval(start, end, phase, stage))
        return end

    def _clear_from(self, cur: PhaseStatus, t: float) -> float:
        """Finish the current phase from ``t``: remaining min green (if ``min_green``),
        amber, all-red. Returns when the junction is all-red-complete."""
        if cur.stage is Stage.GREEN:
            rest = max(0.0, MIN_GREEN_S - cur.green_elapsed_s)
            at = self._add(t, rest, cur.phase, Stage.GREEN)
            at = self._add(at, AMBER_S, cur.phase, Stage.AMBER)
            return self._add(at, ALL_RED_S, cur.phase, Stage.ALL_RED)
        if cur.stage is Stage.AMBER:
            at = self._add(t, cur.stage_remaining_s, cur.phase, Stage.AMBER)
            return self._add(at, ALL_RED_S, cur.phase, Stage.ALL_RED)
        return self._add(t, cur.stage_remaining_s, cur.phase, Stage.ALL_RED)

    def serve(self, phase: int, t: float) -> float:
        """Give ``phase`` the junction (claimed or re-sequenced). Returns its green time."""
        if self.served_phase == phase and self.green_at is not None:
            return self.green_at
        cur = self.status(t)
        self._cut(t)
        if cur.is_green(phase):
            self.overrides.append(Interval(t, math.inf, phase, Stage.GREEN))
            green_at = t
            state = PhaseStateAtClaim.GREEN_EXTEND
        else:
            green_at = self._clear_from(cur, t)
            self.overrides.append(Interval(green_at, math.inf, phase, Stage.GREEN))
            state = PhaseStateAtClaim.RED_TRUNCATE
        if self.served_phase is None:
            self.state_at_claim = state
            self._served_since = t
        self.served_phase = phase
        self.green_at = green_at
        self.recover_until = None
        self.recovery_phase = None
        return green_at

    def adopt(self, phase: int, green_at: float, t: float) -> None:
        """Re-create a served phase after a leader failover (from the restored claim)."""
        self._cut(t)
        if green_at > t:
            self._add(t, green_at - t, phase, Stage.ALL_RED)
        self.overrides.append(Interval(min(green_at, t), math.inf, phase, Stage.GREEN))
        self.served_phase = phase
        self.green_at = green_at
        self.state_at_claim = self.state_at_claim or PhaseStateAtClaim.RED_TRUNCATE
        self._served_since = self._served_since or t

    def _most_starved(self, exclude: int, since: float) -> int:
        """The approach that has waited longest for green (by the plan before ``since``)."""
        best, best_end = exclude, math.inf
        for p in range(self.plan.phases):
            if p == exclude:
                continue
            end = self.plan.last_green_end(p, since)
            if end < best_end - 1e-9:
                best, best_end = p, end
        return best

    def release(self, t: float, recovery_s: float) -> None:
        """No claimant left: amber + all-red, the most-starved approach for ``recovery_s``,
        amber + all-red, then the fixed plan again."""
        if self.served_phase is None:
            return
        began = self._served_since if self._served_since is not None else t
        cur = self.status(t)
        self._cut(t)
        at = self._clear_from(cur, t)  # a held green has had its minimum: amber + all-red
        starved = self._most_starved(cur.phase, began)
        at = self._add(at, recovery_s, starved, Stage.GREEN)
        at = self._add(at, AMBER_S, starved, Stage.AMBER)
        at = self._add(at, ALL_RED_S, starved, Stage.ALL_RED)
        self._served_log.append((began, t))
        self._served_log = [(s, e) for s, e in self._served_log if e >= t - STARVATION_WINDOW_S]
        self._served_since = None
        self.served_phase = None
        self.green_at = None
        self.state_at_claim = None
        self.recover_until = at
        self.recovery_phase = starved


def geometry_json(g: JunctionGeometry) -> dict[str, object]:
    """Value of the Redis junction-geometry cache (``divert:{city}:junctions``)."""
    return {
        "legs": g.legs,
        "approach_bearings": [round(b, 2) for b in g.approach_bearings],
        "source": g.source,
    }


def geometry_from_json(d: Mapping[str, object]) -> JunctionGeometry:
    raw = d.get("approach_bearings")
    bearings = tuple(float(b) for b in raw) if isinstance(raw, list) else ()
    legs = d.get("legs")
    if not bearings or not isinstance(legs, int) or legs != len(bearings):
        raise ValueError(f"bad junction geometry {d!r}")
    return JunctionGeometry(legs, bearings, str(d.get("source", "synthetic")))


class JunctionRegistry:
    """One :class:`JunctionControl` per (city, junction id), plus each junction's
    geometry. Geometry outlives the controls (an idle control is dropped, its geometry is
    kept), is cached in Redis by the worker (:meth:`take_new_geometry`) and seeded back on
    a leader take-over (:meth:`seed`), so a junction keeps the same approaches -> phases
    mapping across failovers instead of being re-derived from the next first approach."""

    def __init__(self) -> None:
        self._by_city: dict[str, dict[str, JunctionControl]] = {}
        self._geometry: dict[str, dict[str, JunctionGeometry]] = {}
        self._new_geometry: dict[str, dict[str, JunctionGeometry]] = {}

    def city(self, city_id: str) -> dict[str, JunctionControl]:
        return self._by_city.setdefault(city_id, {})

    def find(self, city_id: str, signal_id: str) -> JunctionControl | None:
        return self._by_city.get(city_id, {}).get(signal_id)

    # ---------------------------------------------------------------- geometry
    def seed(self, city_id: str, geometries: Mapping[str, JunctionGeometry]) -> None:
        """Geometry cached by an earlier leader (not re-announced as new)."""
        self._geometry.setdefault(city_id, {}).update(geometries)

    def known_geometry(self, city_id: str, signal_id: str) -> JunctionGeometry | None:
        return self._geometry.get(city_id, {}).get(signal_id)

    def geometry(self, city_id: str, signal: CorridorSignal) -> JunctionGeometry:
        """The junction's geometry, fixed on first use: OSRM intersection bearings when
        known (they replace a synthetic geometry while the junction is idle), else
        ``DEFAULT``-leg approaches spaced evenly from this first observed approach."""
        stored = self._geometry.setdefault(city_id, {})
        current = stored.get(signal.signal_id)
        osrm = JunctionGeometry.from_osrm(signal.junction_bearings)
        if current is not None and (current.source == "osrm" or osrm is None):
            return current
        if current is not None:
            ctl = self.find(city_id, signal.signal_id)
            if ctl is not None and (ctl.served_phase is not None or ctl.recover_until is not None):
                return current  # never re-map the phases of a junction in use
        geometry = osrm or JunctionGeometry.synthetic(signal.approach_bearing, signal.legs)
        stored[signal.signal_id] = geometry
        self._new_geometry.setdefault(city_id, {})[signal.signal_id] = geometry
        return geometry

    def take_new_geometry(self, city_id: str) -> dict[str, JunctionGeometry]:
        """Geometry fixed since the last call (the worker caches it in Redis)."""
        return self._new_geometry.pop(city_id, {})

    def get(self, city_id: str, signal: CorridorSignal, t: float | None = None) -> JunctionControl:
        controls = self.city(city_id)
        ctl = controls.get(signal.signal_id)
        geometry = self.geometry(city_id, signal)
        if ctl is None or ctl.geometry != geometry:
            ctl = controls[signal.signal_id] = JunctionControl(signal.signal_id, geometry)
        if t is not None:
            ctl.last_used = max(ctl.last_used, t)
        return ctl

    def phase_of(
        self, city_id: str, signal: CorridorSignal, t: float | None = None
    ) -> tuple[JunctionControl, int]:
        ctl = self.get(city_id, signal, t)
        return ctl, ctl.geometry.phase_for(signal.approach_bearing)

    def is_green(
        self, city_id: str, signal: CorridorSignal, t: float, *, open_holds: bool = True
    ) -> bool:
        """Green for this approach at ``t`` - the control (with its overrides) if the
        junction has one, else its fixed plan. Never creates a control."""
        geometry = self.geometry(city_id, signal)
        phase = geometry.phase_for(signal.approach_bearing)
        ctl = self.find(city_id, signal.signal_id)
        if ctl is None or ctl.geometry != geometry:
            return PhasePlan.for_junction(signal.signal_id, geometry.legs).status(t).is_green(phase)
        return ctl.status(t, open_holds=open_holds).is_green(phase)

    def items(self, city_id: str) -> Iterator[tuple[str, JunctionControl]]:
        yield from list(self._by_city.get(city_id, {}).items())

    def drop_idle(self, city_id: str, t: float, keep: set[str]) -> None:
        controls = self._by_city.get(city_id, {})
        for sid in [s for s, c in controls.items() if s not in keep and c.is_idle(t)]:
            del controls[sid]


class RegistrySignals:
    """:class:`~divert.services.obey.GreenView` over one city of a registry.

    ``open_holds=True``: what the junction really shows (the simulation's own motion);
    ``False``: a prediction that ignores holds still open (the RETURN ETA)."""

    def __init__(self, registry: JunctionRegistry, city_id: str, *, open_holds: bool) -> None:
        self.registry = registry
        self.city_id = city_id
        self.open_holds = open_holds

    def is_green(self, c: CorridorSignal, t: float) -> bool:
        return self.registry.is_green(self.city_id, c, t, open_holds=self.open_holds)
