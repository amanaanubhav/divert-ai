"""Route and corridor value objects shared by routing, dispatch and the simulation.

A :class:`Route` is an OSRM driving route reduced to what DIVERT needs: the polyline,
cumulative haversine distances, the per-segment free-flow speed (OSRM ``speed``
annotation) and the turn maneuvers (for lane advisories). Consecutive duplicate points
are merged on construction so that ``points``, the PostGIS LineString and
``segment_speeds_mps`` always line up one-to-one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from divert.domain.enums import Lane
from divert.domain.geo import LatLng, cumulative_distances

LEFT_MODIFIERS = frozenset({"left", "slight left", "sharp left"})
IGNORED_MANEUVERS = frozenset({"depart", "arrive"})
LANE_MATCH_WINDOW_M = 40.0  # a maneuver this close (along the route) belongs to the junction


@dataclass(frozen=True, slots=True)
class Maneuver:
    along_m: float
    type: str
    modifier: str | None
    location: LatLng


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class Route:
    """Identity semantics (eq=False): hashing hundreds of points per lookup is wasteful,
    and caches key profiles on the route object itself."""

    points: tuple[LatLng, ...]
    cumulative: tuple[float, ...]
    segment_speeds_mps: tuple[float, ...]  # len == len(points) - 1
    maneuvers: tuple[Maneuver, ...] = ()
    osrm_distance_m: float = 0.0
    osrm_duration_s: float = 0.0

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("a route needs at least two distinct points")
        if len(self.cumulative) != len(self.points):
            raise ValueError("cumulative distances do not match the points")
        if len(self.segment_speeds_mps) != len(self.points) - 1:
            raise ValueError("need one speed per segment")

    @property
    def length_m(self) -> float:
        return self.cumulative[-1]

    @property
    def origin(self) -> LatLng:
        return self.points[0]

    @property
    def destination(self) -> LatLng:
        return self.points[-1]

    @classmethod
    def build(
        cls,
        points: Sequence[LatLng],
        segment_speeds_mps: Sequence[float],
        *,
        maneuvers: Sequence[Maneuver] = (),
        osrm_distance_m: float | None = None,
        osrm_duration_s: float | None = None,
    ) -> Route:
        """Merge consecutive duplicate points (and their zero-length segments)."""
        if len(segment_speeds_mps) != max(0, len(points) - 1):
            raise ValueError("need one speed per segment")
        pts: list[LatLng] = []
        speeds: list[float] = []
        for i, raw in enumerate(points):
            p = LatLng(float(raw[0]), float(raw[1]))
            if pts and pts[-1] == p:
                continue
            if pts:
                speeds.append(float(segment_speeds_mps[i - 1]))
            pts.append(p)
        cum = cumulative_distances(pts)
        return cls(
            points=tuple(pts),
            cumulative=tuple(cum),
            segment_speeds_mps=tuple(speeds),
            maneuvers=tuple(maneuvers),
            osrm_distance_m=cum[-1] if osrm_distance_m is None else osrm_distance_m,
            osrm_duration_s=0.0 if osrm_duration_s is None else osrm_duration_s,
        )

    # ------------------------------------------------------------------ JSON
    def to_json(self) -> dict[str, Any]:
        return {
            "points": [[round(p.lat, 7), round(p.lng, 7)] for p in self.points],
            "segment_speeds_mps": [round(s, 3) for s in self.segment_speeds_mps],
            "distance_m": round(self.length_m, 2),
            "osrm_distance_m": round(self.osrm_distance_m, 2),
            "osrm_duration_s": round(self.osrm_duration_s, 2),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Route:
        return cls.build(
            [LatLng(float(lat), float(lng)) for lat, lng in data["points"]],
            [float(s) for s in data["segment_speeds_mps"]],
            osrm_distance_m=float(data.get("osrm_distance_m", 0.0)) or None,
            osrm_duration_s=float(data.get("osrm_duration_s", 0.0)),
        )


def lane_advisory(along_m: float, maneuvers: Sequence[Maneuver]) -> Lane:
    """India drives on the left: a left turn at the junction -> 'L'; straight, right
    turn or U-turn -> 'R' (the outer/overtaking lane). No maneuver near -> straight."""
    best: Maneuver | None = None
    for m in maneuvers:
        if m.type in IGNORED_MANEUVERS:
            continue
        gap = abs(m.along_m - along_m)
        if gap <= LANE_MATCH_WINDOW_M and (best is None or gap < abs(best.along_m - along_m)):
            best = m
    if best is not None and best.modifier in LEFT_MODIFIERS:
        return Lane.LEFT
    return Lane.RIGHT


@dataclass(frozen=True, slots=True)
class CorridorSignal:
    """A signalised junction on the route, with everything the worker needs to claim it."""

    signal_id: str
    pos: LatLng
    along_m: float
    offset_m: float
    lane: Lane
    approach_bearing: float
    name: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "name": self.name,
            "pos": [round(self.pos.lat, 7), round(self.pos.lng, 7)],
            "along_m": round(self.along_m, 2),
            "offset_m": round(self.offset_m, 2),
            "lane": self.lane.value,
            "approach_bearing": round(self.approach_bearing, 1),
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> CorridorSignal:
        return cls(
            signal_id=str(d["signal_id"]),
            name=d.get("name"),
            pos=LatLng(float(d["pos"][0]), float(d["pos"][1])),
            along_m=float(d["along_m"]),
            offset_m=float(d.get("offset_m", 0.0)),
            lane=Lane(d.get("lane", Lane.RIGHT.value)),
            approach_bearing=float(d.get("approach_bearing", 0.0)),
        )
