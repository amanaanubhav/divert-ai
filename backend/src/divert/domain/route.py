"""Route and corridor value objects shared by routing, dispatch and the simulation.

A :class:`Route` is an OSRM driving route reduced to what DIVERT needs: the polyline,
cumulative haversine distances, the per-segment free-flow speed (OSRM ``speed``
annotation), the step maneuvers and the OSRM intersections (bearings of every road at
every intersection the route crosses - the junction geometry for movements and signal
phases). Consecutive duplicate points are merged on construction so that ``points``, the
PostGIS LineString and ``segment_speeds_mps`` always line up one-to-one.

A :class:`CorridorSignal` is one pass of the route through a signalised junction, with
the movement the vehicle makes there and the lane advice for it (design §3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from divert.domain.enums import Lane, Movement
from divert.domain.geo import LatLng, cumulative_distances
from divert.domain.lanes import LaneAdvice


@dataclass(frozen=True, slots=True)
class Maneuver:
    along_m: float
    type: str
    modifier: str | None
    location: LatLng
    exit: int | None = None  # roundabout / rotary exit number (OSRM ``maneuver.exit``)
    name: str | None = None  # road taken by this step (OSRM ``step.name``)


@dataclass(frozen=True, slots=True)
class Intersection:
    """An OSRM intersection placed on our polyline.

    ``bearings`` are the bearings of every road leaving the intersection (clockwise from
    north); ``in_idx`` is the road the route arrives on (absent at the route start) and
    ``out_idx`` the road it leaves on (absent at the route end).
    """

    along_m: float
    location: LatLng
    bearings: tuple[float, ...]
    in_idx: int | None = None
    out_idx: int | None = None

    @property
    def legs(self) -> int:
        return len(self.bearings)

    @property
    def approach_bearing(self) -> float | None:
        """Travel direction when arriving (the reverse of the arrival road's bearing)."""
        if self.in_idx is None or not 0 <= self.in_idx < len(self.bearings):
            return None
        return (self.bearings[self.in_idx] + 180.0) % 360.0

    @property
    def exit_bearing(self) -> float | None:
        if self.out_idx is None or not 0 <= self.out_idx < len(self.bearings):
            return None
        return self.bearings[self.out_idx]


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
    intersections: tuple[Intersection, ...] = ()
    source: str = "osrm"  # "osrm" (OSRM's own route / alternative) | "detour" (via point)
    # OSM node id of each point (0 = unknown, e.g. a snapped end point); empty when OSRM
    # sent none. Consumed at planning time (signal clusters), like the intersections.
    node_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("a route needs at least two distinct points")
        if self.node_ids and len(self.node_ids) != len(self.points):
            raise ValueError("node ids do not match the points")
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
        intersections: Sequence[Intersection] = (),
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
            intersections=tuple(intersections),
        )

    # ------------------------------------------------------------------ JSON
    def to_json(self) -> dict[str, Any]:
        """Geometry + speeds (what the worker needs). Maneuvers and intersections are
        consumed at planning time and live on in the corridor (movement, lane advice)."""
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


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def _opt_int(v: Any) -> int | None:
    return None if v is None else int(v)


@dataclass(frozen=True, slots=True)
class CorridorSignal:
    """One pass of the route through a signalised junction, with everything the worker
    needs to claim it and the movement / lane advice the crew is shown.

    ``key`` identifies the pass (a route can cross the same junction twice: design §4.2
    claim key ``(vehicle, junction, pass_no)``).
    """

    signal_id: str
    pos: LatLng
    along_m: float
    offset_m: float
    lane: Lane  # = advice.side; kept for API compatibility
    approach_bearing: float
    name: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)
    pass_no: int = 0
    movement: Movement = Movement.STRAIGHT
    turn_deg: float = 0.0
    exit_bearing: float | None = None
    roundabout_exit: int | None = None
    free_left: bool = False
    legs: int | None = None  # leg count from the OSRM intersection; None = unknown
    junction_bearings: tuple[float, ...] = ()  # OSRM road bearings at the junction
    advice: LaneAdvice | None = None

    @property
    def key(self) -> str:
        return corridor_key(self.signal_id, self.pass_no)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "signal_id": self.signal_id,
            "name": self.name,
            "pos": [round(self.pos.lat, 7), round(self.pos.lng, 7)],
            "along_m": round(self.along_m, 2),
            "offset_m": round(self.offset_m, 2),
            "lane": self.lane.value,
            "approach_bearing": round(self.approach_bearing, 1),
            "pass_no": self.pass_no,
            "movement": self.movement.value,
            "turn_deg": round(self.turn_deg, 1),
            "exit_bearing": None if self.exit_bearing is None else round(self.exit_bearing, 1),
            "roundabout_exit": self.roundabout_exit,
            "free_left": self.free_left,
            "legs": self.legs,
            "junction_bearings": [round(b, 1) for b in self.junction_bearings],
        }
        if self.advice is not None:
            out["advice"] = self.advice.to_json()
        return out

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> CorridorSignal:
        """Reads every corridor ever stored (fields added later default sensibly)."""
        advice = d.get("advice")
        return cls(
            signal_id=str(d["signal_id"]),
            name=d.get("name"),
            pos=LatLng(float(d["pos"][0]), float(d["pos"][1])),
            along_m=float(d["along_m"]),
            offset_m=float(d.get("offset_m", 0.0)),
            lane=Lane(d.get("lane", Lane.RIGHT.value)),
            approach_bearing=float(d.get("approach_bearing", 0.0)),
            pass_no=int(d.get("pass_no", 0)),
            movement=Movement(d.get("movement", Movement.STRAIGHT.value)),
            turn_deg=float(d.get("turn_deg", 0.0)),
            exit_bearing=_opt_float(d.get("exit_bearing")),
            roundabout_exit=_opt_int(d.get("roundabout_exit")),
            free_left=bool(d.get("free_left", False)),
            legs=_opt_int(d.get("legs")),
            junction_bearings=tuple(float(b) for b in d.get("junction_bearings") or ()),
            advice=None if not advice else LaneAdvice.from_json(advice),
        )


def corridor_key(signal_id: str, pass_no: int) -> str:
    """``J1`` for the first pass, ``J1#1`` for the second ... (stable across releases)."""
    return signal_id if pass_no == 0 else f"{signal_id}#{pass_no}"
