"""The simulation engine: moves vehicles, claims/releases signals, emits events.

Pure and synchronous - no I/O. The worker feeds it commands and calls :meth:`tick`;
each tick returns one :class:`CityBatch` per city with *everything* to write for that
city (live-state deltas, history events, pub/sub messages, command acks), which the
worker applies in a single MULTI/EXEC. Guarantees (the prototype's regressions):

1. claims are owned per vehicle id and stored per (signal, vehicle) field;
2. ALL claims of a vehicle are released on arrival, cancellation and on any error while
   stepping it (the vehicle is then FAILED);
3. nothing is read-modify-written: the worker is the only writer and writes per-vehicle /
   per-claim / per-signal fields, one transaction per city per tick.

Signals (design §4): every junction runs its synthetic fixed plan
(:class:`~divert.services.junctions.JunctionControl`). A vehicle on an emergency leg
claims a junction once its ETA is within the dynamic lead (transition + queue + margin,
:mod:`divert.services.preemption`); claimants are sequenced per junction every tick
(compatible phases together, priority ranks within the window, a winner within 3 s of
crossing is never bumped); claimants that would arrive before their green get a speed
advisory or stop at the stop line; a claim is released past the junction box, on max
hold (TIMEOUT, re-claim after the recovery) and on every exit path; the junction then
RECOVERS. A vehicle on a non-emergency leg (``emergency=False``, stage B's RETURN) never
claims: it brakes for red (or a junction pre-empted for another approach) and goes on
green.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from divert.domain.enums import (
    DestinationKind,
    IncidentStatus,
    IncidentType,
    JunctionState,
    Lane,
    LegEndReason,
    LegKind,
    Movement,
    PhaseStateAtClaim,
    PreemptOutcome,
    RoadClass,
    ServiceType,
    SignalState,
    UnitStatus,
    next_leg_kind,
    priority_rank,
)
from divert.domain.geo import LatLng, bearing_at_distance, interpolate_by_distance
from divert.domain.models import City, IncidentZone
from divert.domain.route import CorridorSignal, Route, corridor_key
from divert.domain.signal_plan import CLEARANCE_S
from divert.services.eta import EtaModel, EtaQuery
from divert.services.junctions import (
    JunctionControl,
    JunctionRegistry,
    RegistrySignals,
    geometry_json,
)
from divert.services.legs import (
    REROUTE_COOLDOWN_S,
    REROUTE_ZONE_TYPES,
    SPLICE_MARGIN_M,
    LegPlan,
    PendingLeg,
    destination_json,
    is_behind_plan,
    is_home,
    reroute_origin_m,
    splice,
)
from divert.services.obey import (
    STOP_SNAP_M,
    decide_stop,
    motion_caps,
    shift_back,
    snap_to_stop,
)
from divert.services.preemption import (
    CROSSING_LOCK_S,
    NO_ADVISORY,
    Advisory,
    Claimant,
    Lead,
    PreemptionRules,
    queue_clear_s,
    queued_green_times,
    required_lead,
    resolve_junction,
    speed_advisory,
    transition_s,
)
from divert.services.speed_model import (
    ARRIVAL_TOLERANCE_M,
    BRAKE_MPS2,
    JUNCTION_PREEMPTED_MPS,
    SpeedProfile,
    build_profile,
)
from divert.services.traffic import ModelledTraffic, city_factor, class_factor, zone_segments
from divert.store.events import (
    HistoryEventType,
    StreamMessageType,
    event_id,
    message,
)

log = logging.getLogger(__name__)

STOPPED_MPS: Final = 0.1  # slower than this counts as waiting
# A RETURN vehicle's ETA is replayed (phase-aware) this often, decremented in between.
NON_EMERGENCY_ETA_REFRESH_S: Final = 5.0
_RELEASE_OUTCOME: Final[dict[str, PreemptOutcome]] = {
    "passed": PreemptOutcome.CLEARED,
    "arrived": PreemptOutcome.CLEARED,
    "cleared": PreemptOutcome.CLEARED,
    "timeout": PreemptOutcome.TIMEOUT,
    "rerouted": PreemptOutcome.REROUTED,
}


class VehicleStatus(StrEnum):
    EN_ROUTE = "EN_ROUTE"
    ON_SCENE = "ON_SCENE"


class CommandError(ValueError):
    """A command that can never be applied (it is acked and, if possible, failed)."""


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds")


def _iso_epoch(t: float | None) -> str | None:
    if t is None or not math.isfinite(t):
        return None
    return _iso(datetime.fromtimestamp(t, UTC))


def _r(x: float, nd: int = 2) -> float:
    return round(float(x), nd)


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def _claim_phase_state(c: Claim, t: float) -> JunctionState:
    """Per claimant: TRANSITIONING / PREEMPTED (served) or QUEUED (lost the junction)."""
    if c.state is not SignalState.PREEMPTED:
        return JunctionState.QUEUED
    if c.green_at is None or c.green_at > t:
        return JunctionState.TRANSITIONING
    return JunctionState.PREEMPTED


@dataclass(slots=True)
class Claim:
    """A vehicle's claim on one junction (one pass; ``attempt`` > 0 after a TIMEOUT)."""

    signal_id: str
    event_id: str
    state: SignalState  # PREEMPTED (its phase is served) | CONFLICT (queued)
    eta_s: float
    eta_at_claim_s: float
    lane: Lane
    bearing: float
    pos: LatLng
    claimed_at: str
    conflicts: int = 0  # -1 marks "new": resolution emits preemption_claimed
    pass_no: int = 0
    attempt: int = 0
    claimed_t: float = 0.0  # sim epoch of the claim
    phase: int = 0
    rank: int = 1
    movement: Movement = Movement.STRAIGHT
    lane_index: int = 1
    lanes_total: int = 1
    free_left: bool = False
    legs: int | None = None
    required_lead_s: float = 0.0
    transition_s: float = 0.0
    queue_clear_s: float = 0.0
    phase_state_at_claim: PhaseStateAtClaim | None = None
    green_at: float | None = None  # sim epoch its phase went green for it (while served)
    expected_green_at: float | None = None  # sim epoch, served or queued
    queue_pos: int = 0
    crossed_at: str | None = None
    waited_s: float = 0.0
    announced_transition: bool = False
    announced_green: bool = False

    @property
    def key(self) -> str:
        return corridor_key(self.signal_id, self.pass_no)

    def to_json(self, vehicle: Vehicle) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "vehicle_id": vehicle.vehicle_id,
            "dispatch_id": vehicle.dispatch_id,
            "service": vehicle.service.value,
            "event_id": self.event_id,
            "state": self.state.value,
            "eta_s": _r(self.eta_s, 1),
            "eta_at_claim_s": _r(self.eta_at_claim_s, 1),
            "lane": self.lane.value,
            "approach_bearing": _r(self.bearing, 1),
            "pos": [self.pos.lat, self.pos.lng],
            "claimed_at": self.claimed_at,
            "conflicts": self.conflicts,
            "pass_no": self.pass_no,
            "attempt": self.attempt,
            "claimed_t": _r(self.claimed_t, 3),
            "phase": self.phase,
            "rank": self.rank,
            "movement": self.movement.value,
            "lane_index": self.lane_index,
            "lanes_total": self.lanes_total,
            "free_left": self.free_left,
            "legs": self.legs,
            "required_lead_s": _r(self.required_lead_s, 1),
            "transition_s": _r(self.transition_s, 1),
            "queue_clear_s": _r(self.queue_clear_s, 1),
            "phase_state_at_claim": (
                None if self.phase_state_at_claim is None else self.phase_state_at_claim.value
            ),
            "green_at": None if self.green_at is None else _r(self.green_at, 3),
            "expected_green_at": (
                None if self.expected_green_at is None else _r(self.expected_green_at, 3)
            ),
            "queue_pos": self.queue_pos,
            "crossed_at": self.crossed_at,
            "waited_s": _r(self.waited_s, 1),
            "announced_transition": self.announced_transition,
            "announced_green": self.announced_green,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Claim:
        psac = d.get("phase_state_at_claim")
        return cls(
            signal_id=str(d["signal_id"]),
            event_id=str(d["event_id"]),
            state=SignalState(d["state"]),
            eta_s=float(d.get("eta_s", 0.0)),
            eta_at_claim_s=float(d.get("eta_at_claim_s", d.get("eta_s", 0.0))),
            lane=Lane(d.get("lane", Lane.RIGHT.value)),
            bearing=float(d.get("approach_bearing", 0.0)),
            pos=LatLng(float(d["pos"][0]), float(d["pos"][1])),
            claimed_at=str(d["claimed_at"]),
            conflicts=int(d.get("conflicts", 0)),
            pass_no=int(d.get("pass_no", 0)),
            attempt=int(d.get("attempt", 0)),
            claimed_t=float(d.get("claimed_t", 0.0)),
            phase=int(d.get("phase", 0)),
            rank=int(d.get("rank", 1)),
            movement=Movement(d.get("movement", Movement.STRAIGHT.value)),
            lane_index=int(d.get("lane_index", 1)),
            lanes_total=int(d.get("lanes_total", 1)),
            free_left=bool(d.get("free_left", False)),
            legs=None if d.get("legs") is None else int(d["legs"]),
            required_lead_s=float(d.get("required_lead_s", 0.0)),
            transition_s=float(d.get("transition_s", 0.0)),
            queue_clear_s=float(d.get("queue_clear_s", 0.0)),
            phase_state_at_claim=None if psac is None else PhaseStateAtClaim(psac),
            green_at=_opt_float(d.get("green_at")),
            expected_green_at=_opt_float(d.get("expected_green_at")),
            queue_pos=int(d.get("queue_pos", 0)),
            crossed_at=d.get("crossed_at"),
            waited_s=float(d.get("waited_s", 0.0)),
            announced_transition=bool(d.get("announced_transition", False)),
            announced_green=bool(d.get("announced_green", False)),
        )


@dataclass(slots=True)
class Vehicle:
    """One unit on one dispatch: the leg it drives (or the dwell / wait between legs).

    ``phase`` is the unit status (DISPATCHED, ON_SCENE, TRANSPORTING, AT_HOSPITAL,
    RETURNING); ``status`` is the legacy two-value view (EN_ROUTE while driving a leg,
    ON_SCENE while stopped). Per-leg fields (route, corridor, destination, elapsed_s,
    predicted_eta_s, claims, passed, samples) are reset when the next leg starts.
    """

    vehicle_id: str
    dispatch_id: str
    incident_id: str
    city_id: str
    service: ServiceType
    station_id: str
    incident_type: str
    route: Route
    corridor: tuple[CorridorSignal, ...]
    destination: LatLng
    predicted_eta_s: float
    started_at: str
    status: VehicleStatus = VehicleStatus.EN_ROUTE
    along_m: float = 0.0
    speed_mps: float = 0.0
    elapsed_s: float = 0.0  # sim seconds on the current leg
    scene_s: float = 0.0  # legacy name of dwell_s
    eta_s: float = 0.0
    signal_etas: dict[str, float] = field(default_factory=dict)  # corridor key -> ETA
    claims: dict[str, Claim] = field(default_factory=dict)  # signal id -> claim
    passed: set[str] = field(default_factory=set)  # corridor keys
    preempted_passed: int = 0  # whole trip
    samples: int = 0
    travel_s: float | None = None  # sim seconds the last finished leg took
    emergency: bool = True  # False: obeys signals, never claims (RETURN)
    leg_kind: LegKind = LegKind.RESPONSE
    speed_advisory_mps: float | None = None
    stop_at_m: float | None = None  # wait at this stop line for green
    stop_for: str | None = None  # corridor key of that stop line
    cooldown: dict[str, float] = field(default_factory=dict)  # key -> no re-claim before
    attempts: dict[str, int] = field(default_factory=dict)  # key -> claims so far (timeouts)
    # --- trip legs (design §1) --------------------------------------------------------
    phase: UnitStatus = UnitStatus.DISPATCHED
    leg_seq: int = 0
    next_seq: int = 1  # seq of the next leg to request
    incident_pos: LatLng | None = None  # the scene (the RESPONSE destination)
    home: LatLng | None = None  # the unit's station (the RETURN destination)
    # the station's road point: where OSRM snaps it (the RESPONSE route's first point)
    home_road: LatLng | None = None
    dest_kind: DestinationKind = DestinationKind.SCENE
    dest_station_id: str | None = None
    dest_name: str | None = None
    dwell_s: float = 0.0
    dwell_target_s: float = 0.0
    pending: PendingLeg | None = None  # next leg requested from the planner
    ready: LegPlan | None = None  # planned, waiting for the dwell to end (memory only)
    halt_at_m: float | None = None  # cancelled while driving: brake to a stop here
    cancelled: bool = False  # the incident was cancelled
    released: bool = False  # released from the incident (RETURNING)
    trip_s: float = 0.0  # sim seconds since the dispatch started (legs + dwells + waits)
    leg_started_trip_s: float = 0.0
    hospital: dict[str, Any] | None = None  # {id, name, location, emergency} on TRANSPORT
    reroutes: int = 0  # route_replaced applied on the current leg
    reroute_pending: int | None = None  # reroute_no requested, not answered yet
    reroute_after_t: float = -math.inf  # sim epoch: no re-route evaluation before
    zones_seen: set[str] = field(default_factory=set)  # incident zones known to this leg
    eta_refresh_t: float = -math.inf  # sim epoch of the last RETURN ETA replay
    _motion_key: tuple[object, ...] | None = None
    _motion: SpeedProfile | None = None
    _factors_key: tuple[object, ...] | None = None
    _factors: tuple[float, ...] = ()
    _entries: dict[str, CorridorSignal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._entries = {c.key: c for c in self.corridor}
        if self.incident_pos is None:
            self.incident_pos = self.destination
        if self.home is None:
            self.home = self.route.origin

    @property
    def rank(self) -> int:
        return priority_rank(self.service, self.leg_kind)

    def at_home(self, pos: LatLng) -> bool:
        """``pos`` is at the unit's own station (its OSM point or its road point)."""
        return is_home(pos, self.home or self.route.origin, self.home_road)

    @property
    def moving(self) -> bool:
        return self.status is VehicleStatus.EN_ROUTE

    @property
    def planning(self) -> bool:
        """Stopped, dwell over, waiting for the next leg's plan ("planning route")."""
        return (
            not self.moving
            and self.pending is not None
            and self.ready is None
            and self.dwell_s >= self.dwell_target_s
        )

    @property
    def at_scene(self) -> bool:
        """The incident still occupies its scene because of this unit (crowding zone)."""
        return (
            self.phase in (UnitStatus.DISPATCHED, UnitStatus.ON_SCENE)
            and not self.cancelled
            and not self.released
        )

    def entry(self, key: str) -> CorridorSignal | None:
        return self._entries.get(key)

    def set_leg_route(self, route: Route, corridor: Sequence[CorridorSignal]) -> None:
        self.route = route
        self.corridor = tuple(corridor)
        self._entries = {c.key: c for c in self.corridor}
        self._motion_key = self._motion = self._factors_key = None
        self._factors = ()

    # ------------------------------------------------------------- geometry
    @property
    def pos(self) -> LatLng:
        p, _ = interpolate_by_distance(self.route.points, self.route.cumulative, self.along_m)
        return p

    @property
    def heading(self) -> float:
        return bearing_at_distance(self.route.points, self.route.cumulative, self.along_m)

    def next_junction(self) -> CorridorSignal | None:
        for c in self.corridor:
            if c.key not in self.passed and c.along_m >= self.along_m:
                return c
        return None

    def next_movement(self) -> dict[str, Any] | None:
        c = self.next_junction()
        if c is None or not self.moving:
            return None
        adv = c.advice
        return {
            "signal_id": c.signal_id,
            "pass_no": c.pass_no,
            "movement": c.movement.value,
            "lane_index": adv.lane_index if adv else None,
            "lanes_total": adv.lanes_total if adv else None,
            "side": c.lane.value,
            "text": adv.text if adv else None,
            "advise_at_m": None if adv is None else _r(adv.advise_at_m, 1),
            "distance_m": _r(c.along_m - self.along_m, 1),
            "free_left": c.free_left,
        }

    def leg_json(self) -> dict[str, Any]:
        return {"seq": self.leg_seq, "kind": self.leg_kind.value, "emergency": self.emergency}

    def destination_json(self) -> dict[str, Any]:
        return destination_json(
            self.dest_kind, self.destination, self.dest_station_id, self.dest_name
        )

    # ---------------------------------------------------------- serialisation
    def _public(self) -> dict[str, Any]:
        """Fields shared by the tick, the live hash and vehicle_added."""
        p = self.pos
        return {
            "id": self.vehicle_id,
            "dispatch_id": self.dispatch_id,
            "incident_id": self.incident_id,
            "service": self.service.value,
            "status": self.status.value,
            "phase": self.phase.value,
            "leg": self.leg_json(),
            # v1 type kept ([lat, lng] of the current leg's end); the design's
            # {kind, station_id, name} (+ location) is destination_info
            "destination": [self.destination.lat, self.destination.lng],
            "destination_info": self.destination_json(),
            "planning": self.planning,
            "pos": [_r(p.lat, 7), _r(p.lng, 7)],
            "heading": _r(self.heading, 1),
            "speed_mps": _r(self.speed_mps, 2),
            "eta_s": _r(self.eta_s, 1),
            "preempted_passed": self.preempted_passed,
            "next_movement": self.next_movement(),
            "speed_advisory_mps": (
                None if self.speed_advisory_mps is None else _r(self.speed_advisory_mps, 2)
            ),
        }

    def live_json(self) -> dict[str, Any]:
        """Field of the ``vehicles(city)`` hash: enough to resume after a failover."""
        inc = self.incident_pos or self.destination
        home = self.home or self.route.origin
        road = self.home_road
        return {
            **self._public(),
            "city": self.city_id,
            "station_id": self.station_id,
            "incident_type": self.incident_type,
            "along_m": _r(self.along_m, 2),
            "distance_m": _r(self.route.length_m, 2),
            "predicted_eta_s": _r(self.predicted_eta_s, 1),
            "elapsed_s": _r(self.elapsed_s, 2),
            "scene_s": _r(self.dwell_s, 2),
            "travel_s": None if self.travel_s is None else _r(self.travel_s, 2),
            "passed": sorted(self.passed),
            "samples": self.samples,
            "started_at": self.started_at,
            "claims": sorted(self.claims),
            "emergency": self.emergency,
            "leg_kind": self.leg_kind.value,
            "rank": self.rank,
            "stop_at_m": None if self.stop_at_m is None else _r(self.stop_at_m, 2),
            "stop_for": self.stop_for,
            "cooldown": {k: _r(t, 3) for k, t in sorted(self.cooldown.items())},
            "attempts": dict(sorted(self.attempts.items())),
            # ETAs to the junctions ahead (corridor contention for new dispatches)
            "signal_etas": (
                {k: _r(t, 1) for k, t in sorted(self.signal_etas.items())}
                if self.emergency and self.moving
                else {}
            ),
            "incident_location": [inc.lat, inc.lng],
            "home": [home.lat, home.lng],
            "home_road": None if road is None else [_r(road.lat, 7), _r(road.lng, 7)],
            "dwell_s": _r(self.dwell_s, 2),
            "dwell_target_s": _r(self.dwell_target_s, 2),
            "next_seq": self.next_seq,
            "pending": None if self.pending is None else self.pending.to_json(),
            "plan_error": None if self.pending is None else self.pending.error,
            "halt_at_m": None if self.halt_at_m is None else _r(self.halt_at_m, 2),
            "cancelled": self.cancelled,
            "released": self.released,
            "trip_s": _r(self.trip_s, 2),
            "leg_started_trip_s": _r(self.leg_started_trip_s, 2),
            "hospital": self.hospital,
            "reroutes": self.reroutes,
            "reroute_after_t": (
                None if not math.isfinite(self.reroute_after_t) else _r(self.reroute_after_t, 3)
            ),
            "zones_seen": sorted(self.zones_seen),
        }

    def tick_json(self) -> dict[str, Any]:
        return {
            **self._public(),
            "along_m": _r(self.along_m, 1),
            "distance_m": _r(self.route.length_m, 1),
        }

    def added_json(self) -> dict[str, Any]:
        """``vehicle_added.vehicle``: tick fields plus the static dispatch facts."""
        return {
            **self.tick_json(),
            "station_id": self.station_id,
            "incident_type": self.incident_type,
            "predicted_eta_s": _r(self.predicted_eta_s, 1),
            "started_at": self.started_at,
        }

    def route_json(self) -> dict[str, Any]:
        """Value of ``vehicle_route(city, vehicle)``: the current leg's route."""
        return {
            "dispatch_id": self.dispatch_id,
            "seq": self.leg_seq,
            "route": self.route.to_json(),
            "corridor": [c.to_json() for c in self.corridor],
            "destination": [self.destination.lat, self.destination.lng],
        }

    @classmethod
    def from_command(cls, cmd: Mapping[str, Any], started_at: str) -> Vehicle:
        try:
            route = Route.from_json(cmd["route"])
            dest = _point(cmd["destination"])
            station = cmd.get("station_pos")
            leg_kind = LegKind(cmd.get("leg_kind", LegKind.RESPONSE.value))
            v = cls(
                vehicle_id=str(cmd["vehicle_id"]),
                dispatch_id=str(cmd["dispatch_id"]),
                incident_id=str(cmd["incident_id"]),
                city_id=str(cmd["city"]),
                service=ServiceType(cmd["service"]),
                station_id=str(cmd["station_id"]),
                incident_type=str(cmd.get("incident_type", "")),
                route=route,
                corridor=tuple(
                    sorted(
                        (CorridorSignal.from_json(c) for c in cmd.get("corridor", [])),
                        key=lambda c: (c.along_m, c.signal_id, c.pass_no),
                    )
                ),
                destination=dest,
                predicted_eta_s=float(cmd["predicted_eta_s"]),
                started_at=started_at,
                eta_s=float(cmd["predicted_eta_s"]),
                emergency=bool(cmd.get("emergency", True)),
                leg_kind=leg_kind,
                home=None if station is None else _point(station),
                home_road=_road_point(cmd.get("home_road"), route, leg_kind),
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise CommandError(f"invalid start_dispatch command: {exc!r}") from exc
        if v.leg_kind is LegKind.RETURN:  # (tests start a RETURN leg directly)
            v.phase, v.released, v.dest_kind = UnitStatus.RETURNING, True, DestinationKind.STATION
        elif v.leg_kind is LegKind.TRANSPORT:
            v.phase, v.dest_kind = UnitStatus.TRANSPORTING, DestinationKind.HOSPITAL
        return v

    @classmethod
    def restore(
        cls, live: Mapping[str, Any], route_doc: Mapping[str, Any], claims: Mapping[str, Claim]
    ) -> Vehicle:
        dest_doc = live.get("destination_info") or live.get("destination")
        leg = live.get("leg") or {}
        cmd = {
            **live,
            "vehicle_id": live["id"],
            "city": live["city"],
            "route": route_doc["route"],
            "corridor": route_doc.get("corridor", []),
            "destination": route_doc.get("destination")
            or (dest_doc.get("location") if isinstance(dest_doc, Mapping) else dest_doc),
            "station_pos": live.get("home"),
            "emergency": leg.get("emergency", live.get("emergency", True)),
            "leg_kind": leg.get("kind", live.get("leg_kind", LegKind.RESPONSE.value)),
        }
        v = cls.from_command(cmd, str(live.get("started_at", "")))
        v.status = VehicleStatus(live.get("status", VehicleStatus.EN_ROUTE.value))
        v.along_m = float(live.get("along_m", 0.0))
        v.speed_mps = float(live.get("speed_mps", 0.0))
        v.elapsed_s = float(live.get("elapsed_s", 0.0))
        v.eta_s = float(live.get("eta_s", v.predicted_eta_s))
        v.preempted_passed = int(live.get("preempted_passed", 0))
        v.passed = {str(s) for s in live.get("passed", [])}
        v.samples = int(live.get("samples", 0))
        travel = live.get("travel_s")
        v.travel_s = None if travel is None else float(travel)
        v.speed_advisory_mps = _opt_float(live.get("speed_advisory_mps"))
        v.stop_at_m = _opt_float(live.get("stop_at_m"))
        v.stop_for = live.get("stop_for")
        v.cooldown = {str(k): float(t) for k, t in (live.get("cooldown") or {}).items()}
        v.attempts = {str(k): int(n) for k, n in (live.get("attempts") or {}).items()}
        v.claims = dict(claims)
        # legs (absent in live state written before stage B: a RESPONSE leg)
        v.phase = UnitStatus(live.get("phase") or _legacy_phase(v.status))
        v.leg_seq = int(leg.get("seq", 0))
        v.next_seq = int(live.get("next_seq", v.leg_seq + 1))
        if isinstance(dest_doc, Mapping):
            v.dest_kind = DestinationKind(dest_doc.get("kind", DestinationKind.SCENE.value))
            v.dest_station_id = dest_doc.get("station_id")
            v.dest_name = dest_doc.get("name")
        inc = live.get("incident_location")
        v.incident_pos = v.destination if inc is None else _point(inc)
        v.dwell_s = float(live.get("dwell_s", live.get("scene_s", 0.0)))
        v.dwell_target_s = float(live.get("dwell_target_s", 0.0))
        pending = live.get("pending")
        v.pending = None if not pending else PendingLeg.from_json(pending)
        v.halt_at_m = _opt_float(live.get("halt_at_m"))
        v.cancelled = bool(live.get("cancelled", False))
        v.released = bool(live.get("released", v.phase is UnitStatus.RETURNING))
        v.trip_s = float(live.get("trip_s", v.elapsed_s))
        v.leg_started_trip_s = float(live.get("leg_started_trip_s", 0.0))
        v.hospital = live.get("hospital")
        v.reroutes = int(live.get("reroutes", 0))
        after = live.get("reroute_after_t")
        v.reroute_after_t = -math.inf if after is None else float(after)
        v.zones_seen = {str(z) for z in live.get("zones_seen", [])}
        return v


def _legacy_phase(status: VehicleStatus) -> UnitStatus:
    return UnitStatus.DISPATCHED if status is VehicleStatus.EN_ROUTE else UnitStatus.ON_SCENE


def _point(raw: Any) -> LatLng:
    return LatLng(float(raw[0]), float(raw[1]))


def _road_point(raw: Any, route: Route, leg_kind: LegKind) -> LatLng | None:
    """The station's road point: as stored, else the start of a RESPONSE route (OSRM's
    snap of the station); unknown for a later leg restored from an older live document."""
    if raw:
        return _point(raw)
    return route.origin if leg_kind is LegKind.RESPONSE else None


@dataclass(slots=True)
class CityBatch:
    """Everything one city's tick writes - applied by the worker in ONE MULTI/EXEC.

    ``plan_requests`` are not written to Redis: the worker hands them to the LegPlanner
    (their state - the vehicle's ``pending`` leg - is in the vehicle's live document)."""

    city_id: str
    vehicle_sets: dict[str, dict[str, Any]] = field(default_factory=dict)
    vehicle_dels: set[str] = field(default_factory=set)
    route_sets: dict[str, dict[str, Any]] = field(default_factory=dict)
    route_dels: set[str] = field(default_factory=set)
    claim_sets: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    claim_dels: set[tuple[str, str]] = field(default_factory=set)
    vclaim_adds: dict[str, set[str]] = field(default_factory=dict)
    vclaim_rems: dict[str, set[str]] = field(default_factory=dict)
    vclaim_dels: set[str] = field(default_factory=set)
    signal_sets: dict[str, dict[str, Any]] = field(default_factory=dict)
    signal_dels: set[str] = field(default_factory=set)
    junction_sets: dict[str, dict[str, Any]] = field(default_factory=dict)
    finished: dict[str, float] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    publish: list[dict[str, Any]] = field(default_factory=list)
    acks: list[str] = field(default_factory=list)
    plan_requests: list[dict[str, Any]] = field(default_factory=list)
    tick: dict[str, Any] | None = None

    def is_empty(self) -> bool:
        return not (
            self.vehicle_sets or self.vehicle_dels or self.route_sets or self.route_dels
            or self.claim_sets or self.claim_dels or self.vclaim_adds or self.vclaim_rems
            or self.vclaim_dels or self.signal_sets or self.signal_dels or self.junction_sets
            or self.finished or self.history or self.publish or self.acks
            or self.plan_requests or self.tick
        )  # fmt: skip


@dataclass(frozen=True, slots=True)
class SimConfig:
    """``scene_time_s``: on-scene dwell per service (a number = the same for all);
    ``hospital_handover_s``: dwell at the hospital; ``step_s``: sim seconds per tick
    (the RETURN ETA replays at this step until the first tick sets the real one)."""

    scene_time_s: float | Mapping[ServiceType, float] = 120.0
    position_sample_s: float = 5.0
    rules: PreemptionRules = field(default_factory=PreemptionRules)
    hospital_handover_s: float = 600.0
    step_s: float = 0.5
    reroute_min_saving_s: float = 60.0
    reroute_max_per_leg: int = 2
    reroute_behind_share: float = 0.25
    reroute_behind_s: float = 60.0

    def scene_time(self, service: ServiceType) -> float:
        if isinstance(self.scene_time_s, int | float):
            return float(self.scene_time_s)
        return float(self.scene_time_s.get(service, 0.0))


@dataclass(slots=True)
class _Advice:
    stop_line_m: float
    key: str
    index: int
    advisory: Advisory


class SimulationEngine:
    def __init__(self, cities: Mapping[str, City], eta_model: EtaModel, config: SimConfig) -> None:
        self.cities: dict[str, City] = dict(cities)
        self.eta_model = eta_model
        self.config = config
        self.vehicles: dict[str, dict[str, Vehicle]] = {}
        self.finished: dict[str, set[str]] = {}
        self.tick_no = 0
        self.junctions = JunctionRegistry()
        self.starved: dict[str, int] = {}  # junctions pre-empted > 50 % of the last 5 min
        self._batches: dict[str, CityBatch] = {}
        self._active_signals: dict[str, set[str]] = {}
        self._traffic: dict[str, tuple[int, ModelledTraffic]] = {}  # (fleet version, model)
        self._zone_sig: dict[str, tuple[tuple[str, str, float, float], ...]] = {}
        self._traffic_version = 0
        self._fleet_version = 0  # bumped whenever a vehicle is added, removed or released
        self._last_signal_doc: dict[tuple[str, str], dict[str, Any]] = {}
        self._step_s = config.step_s  # sim seconds per tick (set by every tick)
        self._sim_t = 0.0  # sim epoch of the last tick (commands apply at this time)

    # ----------------------------------------------------------------- helpers
    def batch(self, city_id: str) -> CityBatch:
        b = self._batches.get(city_id)
        if b is None:
            b = self._batches[city_id] = CityBatch(city_id)
        return b

    def city_vehicles(self, city_id: str) -> dict[str, Vehicle]:
        return self.vehicles.setdefault(city_id, {})

    def find_dispatch(self, dispatch_id: str) -> Vehicle | None:
        for vs in self.vehicles.values():
            for v in vs.values():
                if v.dispatch_id == dispatch_id:
                    return v
        return None

    def is_finished(self, city_id: str, dispatch_id: str) -> bool:
        return dispatch_id in self.finished.get(city_id, set())

    def active_count(self, city_id: str | None = None) -> int:
        if city_id is not None:
            return len(self.vehicles.get(city_id, {}))
        return sum(len(v) for v in self.vehicles.values())

    def _base(self, v: Vehicle, wall: datetime) -> dict[str, Any]:
        return {
            "ts": _iso(wall),
            "city": v.city_id,
            "incident_id": v.incident_id,
            "dispatch_id": v.dispatch_id,
            "vehicle_id": v.vehicle_id,
        }

    def _history(
        self, v: Vehicle, type_: HistoryEventType, eid: str, wall: datetime, **f: Any
    ) -> None:
        self.batch(v.city_id).history.append(
            message(type_.value, event_id=eid, **self._base(v, wall), **f)
        )

    def _incident_updated(self, v: Vehicle, status: str, wall: datetime, **f: Any) -> None:
        self.batch(v.city_id).publish.append(
            message(
                StreamMessageType.INCIDENT_UPDATED.value,
                city=v.city_id,
                incident_id=v.incident_id,
                dispatch_id=v.dispatch_id,
                vehicle_id=v.vehicle_id,
                status=status,
                ts=_iso(wall),
                **f,
            )
        )

    def _leg_event_id(self, prefix: str, v: Vehicle, seq: int, *extra: object) -> str:
        return event_id(prefix, v.dispatch_id, seq, *extra)

    # ----------------------------------------------------------------- traffic
    def traffic(self, city_id: str) -> ModelledTraffic:
        """The city's traffic model: class-aware congestion + the incident zones of every
        unit still occupying its scene (each vehicle is exempt from its own incident)."""
        cached = self._traffic.get(city_id)
        if cached is not None and cached[0] == self._fleet_version:
            return cached[1]
        zones: dict[str, IncidentZone] = {}
        for v in self.vehicles.get(city_id, {}).values():
            if not v.at_scene or v.incident_pos is None:
                continue
            try:
                itype = IncidentType(v.incident_type)
            except ValueError:
                continue
            zones[v.incident_id] = IncidentZone(v.incident_id, itype, v.incident_pos)
        ordered = sorted(zones.values(), key=lambda z: z.incident_id)
        sig = tuple((z.incident_id, z.type.value, z.center.lat, z.center.lng) for z in ordered)
        if cached is not None and self._zone_sig.get(city_id) == sig:
            model = cached[1]
        else:
            self._traffic_version += 1
            model = ModelledTraffic(ordered, version=self._traffic_version)
            self._zone_sig[city_id] = sig
        self._traffic[city_id] = (self._fleet_version, model)
        return model

    # ---------------------------------------------------------------- commands
    def ack(self, city_id: str, stream_id: str | None) -> None:
        if stream_id:
            self.batch(city_id).acks.append(stream_id)

    def start_dispatch(
        self, cmd: Mapping[str, Any], *, stream_id: str | None, wall: datetime, sim: datetime
    ) -> Vehicle | None:
        """Apply a start_dispatch command. Idempotent: duplicates are only acked."""
        city_id = str(cmd.get("city", ""))
        dispatch_id = str(cmd.get("dispatch_id", ""))
        if city_id not in self.cities:
            raise CommandError(f"unknown city {city_id!r}")
        self.ack(city_id, stream_id)
        if self.is_finished(city_id, dispatch_id) or self.find_dispatch(dispatch_id):
            return None
        v = Vehicle.from_command(cmd, _iso(wall))
        existing = self.city_vehicles(city_id).get(v.vehicle_id)
        if existing is not None:
            # A unit is never on two dispatches (PG guarantees it); a stale vehicle here
            # means its terminal event was lost - end it before reusing the id.
            self._end(existing, "failed", wall, reason="superseded by a new dispatch")
        v.zones_seen = {z.incident_id for z in self.traffic(city_id).zones}
        self.city_vehicles(city_id)[v.vehicle_id] = v
        self._fleet_version += 1
        b = self.batch(city_id)
        try:
            self._refresh_eta(v, sim)
        except Exception as exc:
            log.exception("ETA failed at dispatch start", extra={"dispatch_id": dispatch_id})
            self._end(v, "failed", wall, reason=f"simulation error: {exc!r}"[:500])
            return None
        b.route_sets[v.vehicle_id] = v.route_json()
        b.vehicle_sets[v.vehicle_id] = v.live_json()
        self._announce(v, wall)
        return v

    def _announce(self, v: Vehicle, wall: datetime) -> None:
        """vehicle_added (+ en_route on the RESPONSE leg; deterministic id: replays are
        de-duplicated)."""
        b = self.batch(v.city_id)
        b.publish.append(
            message(
                StreamMessageType.VEHICLE_ADDED.value,
                city=v.city_id,
                vehicle=v.added_json(),
                route=[[p.lat, p.lng] for p in v.route.points],
                corridor=[c.to_json() for c in v.corridor],
                destination=[v.destination.lat, v.destination.lng],
            )
        )
        if not v.moving or v.leg_kind is not LegKind.RESPONSE:
            return
        self._history(
            v,
            HistoryEventType.EN_ROUTE,
            event_id("enr", v.dispatch_id),
            wall,
            station_id=v.station_id,
            service=v.service.value,
            predicted_eta_s=_r(v.predicted_eta_s, 1),
            distance_m=_r(v.route.length_m, 1),
        )
        self._incident_updated(v, "EN_ROUTE", wall, eta_s=_r(v.eta_s, 1))
        self._sample(v, wall)

    def cancel_dispatch(
        self, cmd: Mapping[str, Any], *, stream_id: str | None, wall: datetime
    ) -> bool:
        """Cancel per phase (design §1): from RESPONSE / ON_SCENE the unit is released and
        drives back (RETURN from where it stops); with a patient on board, or once the
        incident is resolved, the cancel is refused (``cancel_rejected``)."""
        city_id = str(cmd.get("city", ""))
        dispatch_id = str(cmd.get("dispatch_id", ""))
        if city_id not in self.cities:
            raise CommandError(f"unknown city {city_id!r}")
        self.ack(city_id, stream_id)
        v = self.find_dispatch(dispatch_id)
        if v is not None:
            return self._cancel(v, wall, str(cmd.get("reason", "cancelled")))
        if self.is_finished(city_id, dispatch_id):
            return False
        # Never started here (or its start was reordered after this cancel): the unit is
        # still at its station - report it back in service, and make a late start a no-op.
        b = self.batch(city_id)
        b.finished[dispatch_id] = wall.timestamp()
        self.finished.setdefault(city_id, set()).add(dispatch_id)
        base = {
            "ts": _iso(wall),
            "city": city_id,
            "incident_id": str(cmd.get("incident_id", "")),
            "dispatch_id": dispatch_id,
            "vehicle_id": str(cmd.get("vehicle_id", "")),
        }
        b.history.append(
            message(
                HistoryEventType.CANCELLED.value,
                event_id=event_id("cxl", dispatch_id),
                reason=str(cmd.get("reason", "cancelled")),
                never_started=True,
                **base,
            )
        )
        b.history.append(
            message(
                HistoryEventType.BACK_IN_SERVICE.value,
                event_id=event_id("bis", dispatch_id),
                end_reason="CANCELLED",
                trip_s=0.0,
                **base,
            )
        )
        return False

    def _cancel(self, v: Vehicle, wall: datetime, reason: str) -> bool:
        if v.phase in (UnitStatus.TRANSPORTING, UnitStatus.AT_HOSPITAL):
            self._cancel_rejected(v, wall, IncidentStatus(v.phase.value), "patient on board")
            return False
        if v.cancelled:
            return False
        if v.released:  # the worker already resolved the incident
            self._cancel_rejected(v, wall, IncidentStatus.RESOLVED, "incident already resolved")
            return False
        v.cancelled = True
        self._history(
            v,
            HistoryEventType.CANCELLED,
            event_id("cxl", v.dispatch_id),
            wall,
            reason=reason,
            phase=v.phase.value,
            leg_seq=v.leg_seq,
            trip_s=_r(v.trip_s, 1),
        )
        self._incident_updated(v, "CANCELLED", wall, reason=reason)
        if v.moving:  # RESPONSE: release every claim, brake to a stop, RETURN from there
            self._leg_ended(v, wall, LegEndReason.CANCELLED)
            self._release_all(v, wall, reason="cancelled")
            v.emergency = False
            v.halt_at_m = min(v.route.length_m, v.along_m + v.speed_mps**2 / (2.0 * BRAKE_MPS2))
            halt_s = v.speed_mps / BRAKE_MPS2
            origin, _ = interpolate_by_distance(v.route.points, v.route.cumulative, v.halt_at_m)
            self._supersede_pending(v, wall)
            self._release_unit(v, wall)
            self._request_leg(v, LegKind.RETURN, origin, wall, start_in_s=halt_s)
        else:  # ON_SCENE: end the dwell; a pending TRANSPORT is replaced by a RETURN
            v.dwell_target_s = min(v.dwell_target_s, v.dwell_s)
            needs_return = v.pending is None or v.pending.kind is not LegKind.RETURN
            if needs_return:
                self._supersede_pending(v, wall)
            self._release_unit(v, wall)
            if needs_return:
                origin = v.incident_pos or v.destination
                self._request_leg(v, LegKind.RETURN, origin, wall, start_in_s=0.0)
            self._try_start_ready(v, wall)
        self.batch(v.city_id).vehicle_sets[v.vehicle_id] = v.live_json()
        return True

    def _cancel_rejected(
        self, v: Vehicle, wall: datetime, status: IncidentStatus, why: str
    ) -> None:
        """The API already marked the incident CANCELLED; tell the persister to restore
        the status the worker knows (deterministic id per refused cancel)."""
        self._history(
            v,
            HistoryEventType.CANCEL_REJECTED,
            event_id("cxj", v.dispatch_id),
            wall,
            status=status.value,
            reason=why,
            phase=v.phase.value,
        )
        self._incident_updated(v, status.value, wall, reason=f"cancel refused: {why}")

    def _supersede_pending(self, v: Vehicle, wall: datetime) -> None:
        """Drop a requested (or planned, not started) leg that no longer applies."""
        p = v.pending
        if p is None:
            return
        v.pending = None
        v.ready = None
        self._history(
            v,
            HistoryEventType.LEG_ENDED,
            self._leg_event_id("leg-end", v, p.seq),
            wall,
            seq=p.seq,
            kind=p.kind.value,
            end_reason=LegEndReason.SUPERSEDED.value,
            started=False,
        )
        self.batch(v.city_id).plan_requests.append(
            message("plan_cancel", city=v.city_id, dispatch_id=v.dispatch_id, seq=p.seq)
        )

    def restore(self, v: Vehicle, *, announce_at: datetime | None = None) -> None:
        """Re-insert a vehicle rebuilt from Redis or PostgreSQL (leader take-over).

        ``announce_at``: (PG rebuild) re-announce it - its start may never have been
        recorded (dispatched while no leader was running); event ids are deterministic,
        so an en_route already persisted is de-duplicated. Junction controls are rebuilt
        from the restored claims at the next tick; a pending leg request is re-issued by
        :meth:`pending_plan_requests`.
        """
        self.city_vehicles(v.city_id)[v.vehicle_id] = v
        self._fleet_version += 1
        if announce_at is not None:
            self._announce(v, announce_at)
        b = self.batch(v.city_id)
        b.vehicle_sets[v.vehicle_id] = v.live_json()
        b.route_sets[v.vehicle_id] = v.route_json()
        for sid, claim in v.claims.items():
            b.claim_sets[(sid, v.vehicle_id)] = claim.to_json(v)
            b.vclaim_adds.setdefault(v.vehicle_id, set()).add(sid)

    def pending_plan_requests(self) -> list[dict[str, Any]]:
        """Plan requests of every vehicle waiting for a leg (a new leader re-issues them;
        the planner answers from PostgreSQL when the leg was already planned)."""
        out: list[dict[str, Any]] = []
        for vs in self.vehicles.values():
            for v in vs.values():
                if v.pending is not None and v.ready is None:
                    out.append(self._plan_request(v, v.pending))
        return out

    def mark_known_active_signals(self, city_id: str, signal_ids: set[str]) -> None:
        """Signals currently non-NORMAL in Redis; the first tick deletes stale ones."""
        self._active_signals.setdefault(city_id, set()).update(signal_ids)

    def mark_finished(self, city_id: str, dispatch_ids: set[str]) -> None:
        self.finished.setdefault(city_id, set()).update(dispatch_ids)

    def set_clock(self, sim: datetime) -> None:
        """The sim time commands apply at (the worker sets it before applying them)."""
        self._sim_t = sim.timestamp()

    # ------------------------------------------------------------- leg planning
    def _plan_request(self, v: Vehicle, p: PendingLeg) -> dict[str, Any]:
        home = v.home or v.route.origin
        inc = v.incident_pos or v.destination
        return message(
            "plan_request",
            city=v.city_id,
            dispatch_id=v.dispatch_id,
            incident_id=v.incident_id,
            vehicle_id=v.vehicle_id,
            service=v.service.value,
            station_id=v.station_id,
            incident_type=v.incident_type,
            seq=p.seq,
            kind=p.kind.value,
            origin=[p.origin.lat, p.origin.lng],
            home=[home.lat, home.lng],
            incident_location=[inc.lat, inc.lng],
            requested_t=_r(p.requested_t, 3),
            start_at=_r(p.start_at, 3),
        )

    def _request_leg(
        self, v: Vehicle, kind: LegKind, origin: LatLng, wall: datetime, *, start_in_s: float
    ) -> None:
        """Ask the LegPlanner for the next leg (its seq is fixed here, deterministically).
        A RETURN from the unit's own station is not planned: it is home already."""
        if kind is LegKind.RETURN and v.at_home(origin):
            return
        p = PendingLeg(
            seq=v.next_seq,
            kind=kind,
            origin=origin,
            requested_t=self._sim_t,
            start_at=self._sim_t + max(0.0, start_in_s),
        )
        v.next_seq += 1
        v.pending = p
        self.batch(v.city_id).plan_requests.append(self._plan_request(v, p))
        del wall

    def apply_leg_planned(
        self, cmd: Mapping[str, Any], *, stream_id: str | None, wall: datetime
    ) -> bool:
        """A planned leg (idempotent, keyed by (dispatch_id, seq)): kept until the dwell
        ends, or started at once when the vehicle is already waiting for it."""
        city_id = str(cmd.get("city", ""))
        if city_id not in self.cities:
            raise CommandError(f"unknown city {city_id!r}")
        self.ack(city_id, stream_id)
        v = self.find_dispatch(str(cmd.get("dispatch_id", "")))
        try:
            seq = int(cmd["seq"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError(f"invalid leg_planned command: {exc!r}") from exc
        if v is None or v.pending is None or v.pending.seq != seq or v.ready is not None:
            return False  # finished, superseded, duplicate or already started
        try:
            plan = LegPlan.from_command(cmd)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise CommandError(f"invalid leg_planned command: {exc!r}") from exc
        if plan.kind is not v.pending.kind:
            return False
        v.ready = plan
        v.pending.error = None
        self._history(
            v,
            HistoryEventType.LEG_PLANNED,
            self._leg_event_id("leg-pln", v, seq),
            wall,
            seq=seq,
            kind=plan.kind.value,
            emergency=plan.emergency,
            distance_m=_r(plan.route.length_m, 1),
            predicted_eta_s=_r(plan.predicted_eta_s, 1),
            destination=destination_json(
                plan.dest_kind, plan.destination, plan.dest_station_id, plan.dest_name
            ),
        )
        self._try_start_ready(v, wall)
        self.batch(v.city_id).vehicle_sets[v.vehicle_id] = v.live_json()
        return True

    def plan_failed(self, dispatch_id: str, seq: int, error: str, *, wall: datetime) -> bool:
        """The planner could not plan the leg (it retries with backoff): record it and show
        "planning route (retrying)". Returns False when the request no longer applies."""
        v = self.find_dispatch(dispatch_id)
        if v is None or v.pending is None or v.pending.seq != seq:
            return False
        p = v.pending
        p.attempts += 1
        p.error = error[:300]
        self._history(
            v,
            HistoryEventType.LEG_PLAN_FAILED,
            self._leg_event_id("leg-pf", v, seq, p.attempts),
            wall,
            seq=seq,
            kind=p.kind.value,
            attempt=p.attempts,
            error=p.error,
        )
        self.batch(v.city_id).vehicle_sets[v.vehicle_id] = v.live_json()
        return True

    def leg_not_needed(self, dispatch_id: str, seq: int) -> bool:
        """The planner found that the requested RETURN has no length: its origin and the
        station snap to the same road point (a station mapped off the road, reached
        without the unit's road point being known). The unit is home: that point becomes
        its road point, the request is dropped, and the unit is back in service once it
        has stopped and its dwell is over (:meth:`_dwell`). Returns False when the request
        no longer applies."""
        v = self.find_dispatch(dispatch_id)
        p = None if v is None else v.pending
        if v is None or p is None or p.seq != seq or p.kind is not LegKind.RETURN:
            return False
        if v.ready is not None:
            return False
        # where the unit stands - or, still braking after a cancel, where it will stop
        v.home_road = p.origin if v.moving else v.pos
        v.pending = None
        self.batch(v.city_id).vehicle_sets[v.vehicle_id] = v.live_json()
        return True

    def _try_start_ready(self, v: Vehicle, wall: datetime) -> None:
        if v.ready is not None and not v.moving and v.dwell_s >= v.dwell_target_s:
            self._start_leg(v, v.ready, wall)

    def _start_leg(self, v: Vehicle, plan: LegPlan, wall: datetime) -> None:
        """Begin a planned leg at the vehicle's position (the plan's origin)."""
        sim = datetime.fromtimestamp(self._sim_t, UTC) if self._sim_t else wall
        self._release_all(v, wall, reason="leg_changed")  # (none held between legs)
        v.set_leg_route(plan.route, plan.corridor)
        v.leg_seq, v.leg_kind, v.emergency = plan.seq, plan.kind, plan.emergency
        v.destination = plan.destination
        v.dest_kind, v.dest_station_id, v.dest_name = (
            plan.dest_kind,
            plan.dest_station_id,
            plan.dest_name,
        )
        v.status = VehicleStatus.EN_ROUTE
        v.along_m = v.speed_mps = v.elapsed_s = 0.0
        v.travel_s = None
        v.passed, v.claims, v.cooldown, v.attempts = set(), {}, {}, {}
        v.signal_etas = {}
        v.stop_at_m = v.stop_for = v.speed_advisory_mps = v.halt_at_m = None
        v.samples = 0
        v.dwell_s = v.dwell_target_s = 0.0
        v.pending, v.ready = None, None
        v.reroutes, v.reroute_pending, v.reroute_after_t = 0, None, -math.inf
        v.zones_seen = {z.incident_id for z in self.traffic(v.city_id).zones}
        v.leg_started_trip_s = v.trip_s
        if plan.kind is LegKind.TRANSPORT:
            v.phase = UnitStatus.TRANSPORTING
            v.hospital = plan.hospital
        elif plan.kind is LegKind.RETURN:
            self._release_unit(v, wall)
        self._fleet_version += 1
        if not v.emergency:
            self._obey_signals(v, sim)
        # the prediction of record is made at departure, with the departure's conditions
        v.predicted_eta_s = self._predict(v, sim)
        v.eta_s = v.predicted_eta_s
        v.eta_refresh_t = sim.timestamp()
        payload = {
            "seq": v.leg_seq,
            "kind": v.leg_kind.value,
            "emergency": v.emergency,
            "predicted_eta_s": _r(v.predicted_eta_s, 1),
            "distance_m": _r(v.route.length_m, 1),
            "destination": v.destination_json(),
            "trip_s": _r(v.trip_s, 1),
            "eta_model": self.eta_model.name,
        }
        self._history(
            v, HistoryEventType.LEG_STARTED, self._leg_event_id("leg-st", v, v.leg_seq), wall,
            **payload,
        )  # fmt: skip
        if plan.kind is LegKind.TRANSPORT:
            self._history(
                v,
                HistoryEventType.TRANSPORT_STARTED,
                event_id("trs", v.dispatch_id),
                wall,
                hospital=plan.hospital,
                hospital_station_id=plan.dest_station_id,
                predicted_eta_s=_r(v.predicted_eta_s, 1),
                scene_s=_r(v.trip_s - v.leg_started_trip_s, 1),
                trip_s=_r(v.trip_s, 1),
            )
            self._incident_updated(v, "TRANSPORTING", wall, hospital=plan.hospital)
        b = self.batch(v.city_id)
        b.route_sets[v.vehicle_id] = v.route_json()
        b.vehicle_sets[v.vehicle_id] = v.live_json()
        b.publish.append(
            message(
                StreamMessageType.LEG_CHANGED.value,
                city=v.city_id,
                vehicle_id=v.vehicle_id,
                dispatch_id=v.dispatch_id,
                incident_id=v.incident_id,
                phase=v.phase.value,
                leg=v.leg_json(),
                destination=[v.destination.lat, v.destination.lng],
                destination_info=v.destination_json(),
                predicted_eta_s=_r(v.predicted_eta_s, 1),
                route=[[p.lat, p.lng] for p in v.route.points],
                corridor=[c.to_json() for c in v.corridor],
                hospital=v.hospital,
            )
        )
        self._announce(v, wall)  # vehicle_added again: older clients redraw the route
        self._sample(v, wall)

    def _predict(self, v: Vehicle, sim: datetime) -> float:
        est = self.eta_model.estimate(self._eta_query(v, sim))
        if not math.isfinite(est.to_destination_s) or est.to_destination_s < 0:
            raise ValueError(f"ETA model returned {est.to_destination_s!r}")
        return est.to_destination_s

    def _release_unit(self, v: Vehicle, wall: datetime) -> None:
        """The unit is released from its incident (scene cleared, handover done, or
        cancelled): unit RETURNING, it no longer occupies the scene."""
        if v.released:
            return
        v.released = True
        v.phase = UnitStatus.RETURNING
        self._fleet_version += 1
        self._history(
            v,
            HistoryEventType.RETURNING,
            event_id("ret", v.dispatch_id),
            wall,
            cancelled=v.cancelled,
            trip_s=_r(v.trip_s, 1),
            pos=[v.pos.lat, v.pos.lng],
        )

    # -------------------------------------------------------------- re-routing
    def apply_route_replaced(
        self, cmd: Mapping[str, Any], *, stream_id: str | None, wall: datetime
    ) -> bool:
        """A re-route (idempotent, keyed by (dispatch_id, seq, reroute_no)): the vehicle
        keeps going from where it is; claims beyond the re-route point are released and
        re-evaluated on the new path at the next tick."""
        city_id = str(cmd.get("city", ""))
        if city_id not in self.cities:
            raise CommandError(f"unknown city {city_id!r}")
        self.ack(city_id, stream_id)
        v = self.find_dispatch(str(cmd.get("dispatch_id", "")))
        try:
            seq, no = int(cmd["seq"]), int(cmd["reroute_no"])
            at_m = float(cmd["origin_along_m"])
            new_route = Route.from_json(cmd["route"])
            new_corridor = [CorridorSignal.from_json(c) for c in cmd.get("corridor", [])]
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise CommandError(f"invalid route_replaced command: {exc!r}") from exc
        if v is None or v.leg_seq != seq or not v.moving or no != v.reroutes + 1:
            return False  # finished, another leg, or a duplicate
        if v.reroute_pending == no:
            v.reroute_pending = None
        if v.along_m > at_m - SPLICE_MARGIN_M or not v.emergency:
            return False  # too late for this re-route point (or no longer an emergency)
        route, corridor = splice(v.route, v.corridor, at_m, new_route, new_corridor)
        for sid, claim in list(v.claims.items()):
            if claim.key not in {c.key for c in corridor if c.along_m <= at_m}:
                self._release(v, sid, wall, reason="rerouted")
        old_len = v.route.length_m
        v.set_leg_route(route, corridor)
        v.reroutes = no
        sim = datetime.fromtimestamp(self._sim_t, UTC) if self._sim_t else wall
        self._refresh_eta(v, sim)
        saved = float(cmd.get("saved_s", 0.0))
        self._history(
            v,
            HistoryEventType.REROUTED,
            event_id("rrt", v.dispatch_id, seq, no),
            wall,
            seq=seq,
            reroute_no=no,
            reason=str(cmd.get("reason", "")),
            saved_s=_r(saved, 1),
            old_remaining_s=cmd.get("old_remaining_s"),
            new_remaining_s=cmd.get("new_remaining_s"),
            origin_along_m=_r(at_m, 1),
            old_distance_m=_r(old_len, 1),
            distance_m=_r(route.length_m, 1),
            route=route.to_json(),
            corridor=[c.to_json() for c in corridor],
            route_choice=cmd.get("route_choice"),
        )
        b = self.batch(v.city_id)
        b.route_sets[v.vehicle_id] = v.route_json()
        b.vehicle_sets[v.vehicle_id] = v.live_json()
        b.publish.append(
            message(
                StreamMessageType.ROUTE_REPLACED.value,
                city=v.city_id,
                vehicle_id=v.vehicle_id,
                dispatch_id=v.dispatch_id,
                incident_id=v.incident_id,
                leg=v.leg_json(),
                reroute_no=no,
                reason=str(cmd.get("reason", "")),
                saved_s=_r(saved, 1),
                route=[[p.lat, p.lng] for p in route.points],
                corridor=[c.to_json() for c in corridor],
            )
        )
        return True

    def reroute_answered(self, dispatch_id: str, seq: int, reroute_no: int) -> None:
        """The planner kept the current route (no >= 60 s saving): allow the next
        evaluation after the cool-down."""
        v = self.find_dispatch(dispatch_id)
        if v is not None and v.leg_seq == seq and v.reroute_pending == reroute_no:
            v.reroute_pending = None

    def _check_reroute(self, v: Vehicle, t: float) -> None:
        """Re-route triggers (design §2), emergency legs only: (a) a new FIRE / CRIME zone
        on the remaining path, (b) more than 25 % and 60 s behind plan."""
        cfg = self.config
        if (
            not v.emergency
            or v.halt_at_m is not None
            or v.reroutes >= cfg.reroute_max_per_leg
            or v.reroute_pending is not None
            or t < v.reroute_after_t
        ):
            return
        reason = None
        zones = self.traffic(v.city_id).zones
        new = [z for z in zones if z.incident_id not in v.zones_seen]
        for z in new:
            v.zones_seen.add(z.incident_id)
            if z.incident_id == v.incident_id or z.type.value not in REROUTE_ZONE_TYPES:
                continue
            ahead = [i for i in zone_segments(v.route, z) if v.route.cumulative[i + 1] > v.along_m]
            if ahead:
                reason = f"{z.type.value.lower()} incident on the route"
                break
        if reason is None and is_behind_plan(
            v.elapsed_s,
            v.eta_s,
            v.predicted_eta_s,
            share=cfg.reroute_behind_share,
            min_s=cfg.reroute_behind_s,
        ):
            reason = "running late"
        if reason is None:
            return
        at_m = reroute_origin_m(v.along_m, v.speed_mps, v.route.length_m)
        v.reroute_after_t = t + REROUTE_COOLDOWN_S
        if at_m is None:
            return
        to_origin = self._motion_profile(v, datetime.fromtimestamp(t, UTC)).time_from(
            v.along_m, v.speed_mps, [at_m]
        )[1][0]
        no = v.reroutes + 1
        v.reroute_pending = no
        origin, _ = interpolate_by_distance(v.route.points, v.route.cumulative, at_m)
        self.batch(v.city_id).plan_requests.append(
            message(
                "reroute_request",
                city=v.city_id,
                dispatch_id=v.dispatch_id,
                incident_id=v.incident_id,
                vehicle_id=v.vehicle_id,
                service=v.service.value,
                seq=v.leg_seq,
                kind=v.leg_kind.value,
                reroute_no=no,
                reason=reason,
                origin=[origin.lat, origin.lng],
                origin_along_m=_r(at_m, 2),
                destination=[v.destination.lat, v.destination.lng],
                remaining_s=_r(max(0.0, v.eta_s - to_origin), 1),
                at_t=_r(t + to_origin, 3),
            )
        )

    # -------------------------------------------------------------------- tick
    def tick(self, *, dt_sim: float, sim: datetime, wall: datetime) -> dict[str, CityBatch]:
        """Advance every vehicle by ``dt_sim`` simulated seconds; return the batches."""
        self.tick_no += 1
        self._step_s = dt_sim
        self._sim_t = sim.timestamp()
        cities = sorted(
            set(self.vehicles)
            | {c for c, s in self._active_signals.items() if s}
            | {c for c in self.cities if self.junctions.city(c)}
        )
        for city_id in cities:
            vs = self.vehicles.get(city_id, {})
            for v in list(vs.values()):
                try:
                    self._advance(v, dt_sim, sim, wall)
                except Exception as exc:
                    log.exception(
                        "vehicle step failed",
                        extra={"vehicle_id": v.vehicle_id, "dispatch_id": v.dispatch_id},
                    )
                    self._end(v, "failed", wall, reason=f"simulation error: {exc!r}"[:500])
            self._resolve_city(city_id, wall, sim)
            b = self.batch(city_id)
            b.junction_sets.update(
                {
                    sid: geometry_json(g)
                    for sid, g in self.junctions.take_new_geometry(city_id).items()
                }
            )
            if vs or b.signal_dels or b.vehicle_dels or b.signal_sets:
                b.tick = message(
                    StreamMessageType.TICK.value,
                    city=city_id,
                    tick=self.tick_no,
                    sim_ts=_iso(sim),
                    ts=_iso(wall),
                    vehicles=[v.tick_json() for v in vs.values()],
                    signals=[b.signal_sets[s] for s in sorted(b.signal_sets)],
                )
        out = {c: b for c, b in self._batches.items() if not b.is_empty()}
        self._batches = {}
        return out

    def drain(self) -> dict[str, CityBatch]:
        """Pending command effects without advancing time (used on shutdown/take-over)."""
        out = {c: b for c, b in self._batches.items() if not b.is_empty()}
        self._batches = {}
        return out

    def _eta_query(self, v: Vehicle, sim: datetime) -> EtaQuery:
        return EtaQuery(
            route=v.route,
            service=v.service,
            city=self.cities[v.city_id],
            corridor=v.corridor,
            along_m=v.along_m,
            speed_mps=v.speed_mps,
            at=sim,
            emergency=v.emergency,
            traffic=self.traffic(v.city_id),
            exempt_incident_id=v.incident_id,
            signals=None
            if v.emergency
            else RegistrySignals(self.junctions, v.city_id, open_holds=False),
            step_s=self._step_s,
            rules=self.config.rules,
            passed=frozenset(v.passed),
            stop_for=v.stop_for,
        )

    def _refresh_eta(self, v: Vehicle, sim: datetime) -> None:
        if not v.emergency and v.halt_at_m is None:
            t = sim.timestamp()
            if t - v.eta_refresh_t < NON_EMERGENCY_ETA_REFRESH_S:  # decrement in between
                v.eta_s = max(0.0, v.eta_s - self._step_s)
                return
            v.eta_refresh_t = t
        elif v.halt_at_m is not None:  # braking to a stop after a cancel
            v.eta_s, v.signal_etas = 0.0, {}
            return
        est = self.eta_model.estimate(self._eta_query(v, sim))
        if not math.isfinite(est.to_destination_s) or est.to_destination_s < 0:
            raise ValueError(f"ETA model returned {est.to_destination_s!r}")
        v.eta_s = est.to_destination_s
        v.signal_etas = est.to_signals_s

    def _fast_junctions(self, v: Vehicle) -> frozenset[str]:
        """Corridor keys passed at the pre-empted junction speed (claim served, free left)."""
        fast = {c.key for c in v.corridor if c.free_left}
        if v.emergency:
            fast |= {c.key for c in v.claims.values() if c.state is SignalState.PREEMPTED}
        return frozenset(fast)

    def _motion_profile(self, v: Vehicle, sim: datetime) -> SpeedProfile:
        city = self.cities[v.city_id]
        traffic = self.traffic(v.city_id)
        fkey = (*traffic.cache_key(city, sim), v.incident_id)
        if v._factors_key != fkey:
            v._factors = traffic.segment_factors(
                v.route, city, sim, exempt_incident_id=v.incident_id
            )
            v._factors_key = fkey
        fast = self._fast_junctions(v)
        stop = v.halt_at_m if v.halt_at_m is not None else v.stop_at_m
        key = (fkey, fast, stop, v.emergency)
        if v._motion is None or v._motion_key != key:
            v._motion = build_profile(
                v.route,
                v.service,
                v._factors,
                motion_caps(v.corridor, fast, stop),
                emergency=v.emergency,
            )
            v._motion_key = key
        return v._motion

    def _advance(self, v: Vehicle, dt: float, sim: datetime, wall: datetime) -> None:
        b = self.batch(v.city_id)
        v.trip_s += dt
        if not v.moving:
            self._dwell(v, dt, wall)
            if self.city_vehicles(v.city_id).get(v.vehicle_id) is v:  # (not finished)
                b.vehicle_sets[v.vehicle_id] = v.live_json()
            return
        prof = self._motion_profile(v, sim)
        x0 = v.along_m
        x1, v1 = prof.step(x0, v.speed_mps, dt, cap_mps=v.speed_advisory_mps)
        stop = v.halt_at_m if v.halt_at_m is not None else v.stop_at_m
        v.along_m, v.speed_mps = snap_to_stop(x0, x1, v1, stop)
        v.elapsed_s += dt
        if v.halt_at_m is not None:
            if v.speed_mps <= STOPPED_MPS and v.along_m >= v.halt_at_m - 1.0:
                self._halted(v, wall)
            self._sample(v, wall)
            b.vehicle_sets[v.vehicle_id] = v.live_json()
            return
        if v.along_m >= v.route.length_m - ARRIVAL_TOLERANCE_M:
            self._leg_arrived(v, wall)
            return
        self._refresh_eta(v, sim)
        self._track_claims(v, dt, wall)
        if v.emergency:
            self._update_claims(v, wall, sim)
            self._check_reroute(v, sim.timestamp())
        else:
            self._obey_signals(v, sim)
        self._sample(v, wall)
        b.vehicle_sets[v.vehicle_id] = v.live_json()

    def _halted(self, v: Vehicle, wall: datetime) -> None:
        """A cancelled vehicle has braked to a stop: wait here for its RETURN plan."""
        v.status = VehicleStatus.ON_SCENE
        v.speed_mps = 0.0
        v.travel_s = v.elapsed_s
        v.halt_at_m = None
        v.stop_at_m = v.stop_for = v.speed_advisory_mps = None
        v.signal_etas = {}
        v.eta_s = 0.0
        self._try_start_ready(v, wall)

    def _dwell(self, v: Vehicle, dt: float, wall: datetime) -> None:
        """Stopped: on-scene / handover dwell, then wait for (or start) the next leg - or,
        released at its own station, back in service without a RETURN leg."""
        if v.dwell_s < v.dwell_target_s:
            v.dwell_s = min(v.dwell_target_s, v.dwell_s + dt)
        if v.dwell_s >= v.dwell_target_s:  # guarded: its events are emitted once
            self._dwell_done(v, wall)
        self._try_start_ready(v, wall)
        if self._home_already(v):
            self._finish(v, wall)

    @staticmethod
    def _home_already(v: Vehicle) -> bool:
        return (
            not v.moving
            and v.released
            and v.pending is None
            and v.ready is None
            and v.dwell_s >= v.dwell_target_s
            and v.at_home(v.pos)
        )

    def _dwell_done(self, v: Vehicle, wall: datetime) -> None:
        if v.cancelled or v.released:
            return
        if v.phase is UnitStatus.AT_HOSPITAL:
            self._history(
                v,
                HistoryEventType.HANDOVER_COMPLETE,
                event_id("hnd", v.dispatch_id),
                wall,
                handover_s=_r(v.dwell_s, 1),
                hospital_station_id=v.dest_station_id,
                trip_s=_r(v.trip_s, 1),
            )
            self._incident_updated(v, "RESOLVED", wall)
            self._release_unit(v, wall)
        elif v.phase is UnitStatus.ON_SCENE and (
            v.pending is None or v.pending.kind is LegKind.RETURN
        ):
            self._history(
                v,
                HistoryEventType.CLEARED,
                event_id("clr", v.dispatch_id),
                wall,
                scene_s=_r(v.dwell_s, 1),
                trip_s=_r(v.trip_s, 1),
            )
            self._incident_updated(v, "RESOLVED", wall)
            self._release_unit(v, wall)

    def _leg_ended(self, v: Vehicle, wall: datetime, reason: LegEndReason) -> None:
        self._history(
            v,
            HistoryEventType.LEG_ENDED,
            self._leg_event_id("leg-end", v, v.leg_seq),
            wall,
            seq=v.leg_seq,
            kind=v.leg_kind.value,
            end_reason=reason.value,
            started=True,
            travel_s=_r(v.elapsed_s, 2),
            predicted_eta_s=_r(v.predicted_eta_s, 1),
            distance_m=_r(min(v.along_m, v.route.length_m), 1),
            trip_s=_r(v.trip_s, 1),
            reroutes=v.reroutes,
        )

    def _leg_arrived(self, v: Vehicle, wall: datetime) -> None:
        """The current leg reached its destination."""
        self._release_all(v, wall, reason="arrived")
        v.status = VehicleStatus.ON_SCENE
        v.along_m = v.route.length_m
        v.speed_mps = 0.0
        v.eta_s = 0.0
        v.travel_s = v.elapsed_s
        v.signal_etas = {}
        self._leg_ended(v, wall, LegEndReason.ARRIVED)
        p = v.pos
        if v.leg_kind is LegKind.RETURN:
            self._finish(v, wall)
            return
        if v.leg_kind is LegKind.RESPONSE:
            saved = v.preempted_passed * self.cities[v.city_id].traffic.avg_junction_delay_s
            self._history(
                v,
                HistoryEventType.ARRIVED,
                event_id("arr", v.dispatch_id),
                wall,
                pos=[p.lat, p.lng],
                travel_s=_r(v.elapsed_s, 2),
                predicted_eta_s=_r(v.predicted_eta_s, 1),
                est_time_saved_s=_r(saved, 1),
                preempted_passed=v.preempted_passed,
                trip_s=_r(v.trip_s, 1),
            )
            self._incident_updated(v, "ON_SCENE", wall, travel_s=_r(v.elapsed_s, 1))
            v.phase = UnitStatus.ON_SCENE
            v.dwell_s, v.dwell_target_s = 0.0, self.config.scene_time(v.service)
            try:
                itype: IncidentType | None = IncidentType(v.incident_type)
            except ValueError:
                itype = None
            kind = next_leg_kind(
                LegKind.RESPONSE, service=v.service, incident_type=itype, cancelled=v.cancelled
            )
        else:  # TRANSPORT reached the hospital
            self._history(
                v,
                HistoryEventType.ARRIVED_HOSPITAL,
                event_id("ahs", v.dispatch_id),
                wall,
                pos=[p.lat, p.lng],
                hospital_station_id=v.dest_station_id,
                travel_s=_r(v.elapsed_s, 2),
                predicted_eta_s=_r(v.predicted_eta_s, 1),
                trip_s=_r(v.trip_s, 1),
            )
            self._incident_updated(v, "AT_HOSPITAL", wall, hospital=v.hospital)
            v.phase = UnitStatus.AT_HOSPITAL
            v.dwell_s, v.dwell_target_s = 0.0, self.config.hospital_handover_s
            kind = LegKind.RETURN
        self._fleet_version += 1
        if kind is not None:
            self._request_leg(v, kind, v.pos, wall, start_in_s=v.dwell_target_s)
        if v.dwell_target_s <= 0.0:
            self._dwell_done(v, wall)
        self.batch(v.city_id).route_sets[v.vehicle_id] = v.route_json()
        self.batch(v.city_id).vehicle_sets[v.vehicle_id] = v.live_json()

    def _sample(self, v: Vehicle, wall: datetime) -> None:
        step = self.config.position_sample_s
        if v.elapsed_s + 1e-9 < v.samples * step:
            return
        p = v.pos
        sample_id = (
            event_id("pos", v.dispatch_id, v.samples)
            if v.leg_seq == 0
            else event_id("pos", v.dispatch_id, f"s{v.leg_seq}", v.samples)
        )
        self._history(
            v,
            HistoryEventType.VEHICLE_POSITION,
            sample_id,
            wall,
            pos=[_r(p.lat, 7), _r(p.lng, 7)],
            speed_mps=_r(v.speed_mps, 2),
            along_m=_r(v.along_m, 2),
            elapsed_s=_r(v.elapsed_s, 2),
            sample=v.samples,
            leg_seq=v.leg_seq,
            trip_s=_r(v.trip_s, 2),
        )
        v.samples = int(v.elapsed_s // step) + 1

    # ------------------------------------------------------------------ claims
    def _track_claims(self, v: Vehicle, dt: float, wall: datetime) -> None:
        """crossed_at (junction centre reached) and waited_s (stopped at its stop line)."""
        for claim in v.claims.values():
            c = v.entry(claim.key)
            if c is not None and claim.crossed_at is None and v.along_m >= c.along_m:
                claim.crossed_at = _iso(wall)
            if v.speed_mps <= STOPPED_MPS and v.stop_for == claim.key:
                claim.waited_s += dt

    def _lead(
        self, v: Vehicle, c: CorridorSignal, ctl: JunctionControl, phase: int, sim: datetime
    ) -> Lead:
        """required_lead = T_transition + T_queue + margin (design §4.2)."""
        rules = self.config.rules
        t = sim.timestamp()
        # (a junction held for another approach shows that phase's long green: transition =
        # amber + all-red; the conflict resolution then sequences the claimants)
        status = ctl.status(t)
        adv = c.advice
        cls = adv.road_class if adv is not None else RoadClass.COLLECTOR
        lanes = adv.lanes_total if adv is not None else 1
        f_class = class_factor(city_factor(self.cities[v.city_id], sim), cls)
        if status.is_green(phase):
            queue = queue_clear_s(
                cls, f_class, lanes, ctl.plan.red_s, green_elapsed_s=status.green_elapsed_s
            )
        else:
            queue = queue_clear_s(cls, f_class, lanes, ctl.plan.red_elapsed_s(phase, t))
        return required_lead(transition_s(status, phase), queue, margin_s=rules.margin_s)

    def _claim_event_id(self, prefix: str, v: Vehicle, claim: Claim, *extra: object) -> str:
        parts: list[object] = [v.dispatch_id, claim.signal_id]
        if claim.pass_no or claim.attempt:
            parts += [claim.pass_no, claim.attempt]
        if v.leg_seq:  # the same junction may be claimed again on a later leg
            parts.append(f"s{v.leg_seq}")
        return event_id(prefix, *parts, *extra)

    def _update_claims(self, v: Vehicle, wall: datetime, sim: datetime) -> None:
        rules = self.config.rules
        b = self.batch(v.city_id)
        t = sim.timestamp()
        for c in v.corridor:
            key = c.key
            if key in v.passed:
                continue
            claim = v.claims.get(c.signal_id)
            if claim is not None and claim.pass_no == c.pass_no:
                if rules.is_past(v.along_m, c.along_m, c.legs):
                    self._release(v, c.signal_id, wall, reason="passed")
                    v.passed.add(key)
                else:
                    claim.eta_s = v.signal_etas.get(key, claim.eta_s)
                continue
            if rules.is_past(v.along_m, c.along_m, c.legs):
                v.passed.add(key)
                continue
            if claim is not None:  # still holding this junction for an earlier pass
                continue
            if c.free_left or not rules.must_cross(c.along_m, c.legs, v.route.length_m):
                continue
            if v.cooldown.get(key, -math.inf) > t:
                continue
            eta = v.signal_etas.get(key)
            if eta is None:
                continue
            ctl, phase = self.junctions.phase_of(v.city_id, c, t)
            lead = self._lead(v, c, ctl, phase, sim)
            if not rules.should_claim(
                eta_s=eta,
                distance_ahead_m=c.along_m - v.along_m,
                speed_mps=v.speed_mps,
                required_lead_s=lead.required_s,
            ):
                continue
            adv = c.advice
            claim = Claim(
                signal_id=c.signal_id,
                event_id="",
                state=SignalState.PREEMPTED,  # provisional; resolved this tick
                eta_s=eta,
                eta_at_claim_s=eta,
                lane=c.lane,
                bearing=c.approach_bearing,
                pos=c.pos,
                claimed_at=_iso(wall),
                conflicts=-1,  # marks "new": resolution emits preemption_claimed
                pass_no=c.pass_no,
                attempt=v.attempts.get(key, 0),
                claimed_t=t,
                phase=phase,
                rank=v.rank,
                movement=c.movement,
                lane_index=adv.lane_index if adv is not None else 1,
                lanes_total=adv.lanes_total if adv is not None else 1,
                free_left=c.free_left,
                legs=c.legs,
                required_lead_s=lead.required_s,
                transition_s=lead.transition_s,
                queue_clear_s=lead.queue_clear_s,
            )
            claim.event_id = self._claim_event_id("clm", v, claim)
            v.claims[c.signal_id] = claim
            b.vclaim_adds.setdefault(v.vehicle_id, set()).add(c.signal_id)

    def _claim_fields(self, v: Vehicle, claim: Claim) -> dict[str, Any]:
        """The claim as persisted in ``signal_preemptions`` (design §4.6 columns)."""
        return {
            "signal_id": claim.signal_id,
            "claim_event_id": claim.event_id,
            "state": claim.state.value,
            "lane": claim.lane.value,
            "approach_bearing": _r(claim.bearing, 1),
            "eta_at_claim_s": _r(claim.eta_at_claim_s, 1),
            "claimed_at": claim.claimed_at,
            "leg_seq": v.leg_seq,
            "leg_kind": v.leg_kind.value,
            "pass_no": claim.pass_no,
            "attempt": claim.attempt,
            "rank": claim.rank,
            "phase": claim.phase,
            "movement": claim.movement.value,
            "lane_index": claim.lane_index,
            "lanes_total": claim.lanes_total,
            "free_left": claim.free_left,
            "required_lead_s": _r(claim.required_lead_s, 1),
            "transition_s": _r(claim.transition_s, 1),
            "queue_clear_s": _r(claim.queue_clear_s, 1),
            "phase_state_at_claim": (
                None if claim.phase_state_at_claim is None else claim.phase_state_at_claim.value
            ),
            "green_at": _iso_epoch(claim.green_at),
        }

    def _release(self, v: Vehicle, sid: str, wall: datetime, *, reason: str) -> None:
        claim = v.claims.pop(sid, None)
        if claim is None:
            return
        b = self.batch(v.city_id)
        b.claim_dels.add((sid, v.vehicle_id))
        b.claim_sets.pop((sid, v.vehicle_id), None)
        b.vclaim_rems.setdefault(v.vehicle_id, set()).add(sid)
        if v.stop_for == claim.key:
            v.stop_at_m, v.stop_for = None, None
        if claim.conflicts < 0:  # claimed and released within one tick: never resolved
            return
        if reason == "passed" and claim.state is SignalState.PREEMPTED:
            v.preempted_passed += 1
        outcome = _RELEASE_OUTCOME.get(reason, PreemptOutcome.CANCELLED)
        self._history(
            v,
            HistoryEventType.PREEMPTION_RELEASED,
            self._claim_event_id("rel", v, claim),
            wall,
            **self._claim_fields(v, claim),
            released_at=_iso(wall),
            reason=reason,
            outcome=outcome.value,
            crossed_at=claim.crossed_at,
            waited_s=_r(claim.waited_s, 1),
        )

    def _release_all(self, v: Vehicle, wall: datetime, *, reason: str) -> None:
        for sid in list(v.claims):
            self._release(v, sid, wall, reason=reason)
        v.speed_advisory_mps, v.stop_at_m, v.stop_for = None, None, None
        b = self.batch(v.city_id)
        b.vclaim_dels.add(v.vehicle_id)
        b.vclaim_adds.pop(v.vehicle_id, None)
        b.vclaim_rems.pop(v.vehicle_id, None)

    def _timeout(self, v: Vehicle, claim: Claim, wall: datetime, t: float) -> None:
        """Max hold exceeded: release (TIMEOUT) and let the vehicle re-claim after the
        junction has recovered (cross traffic gets its turn first)."""
        rules = self.config.rules
        self._history(
            v,
            HistoryEventType.PREEMPT_TIMEOUT,
            self._claim_event_id("pto", v, claim),
            wall,
            **self._claim_fields(v, claim),
            hold_s=_r(t - (claim.green_at or t), 1),
            max_hold_s=rules.max_hold_s,
        )
        key = claim.key
        self._release(v, claim.signal_id, wall, reason="timeout")
        v.cooldown[key] = t + 2 * CLEARANCE_S + rules.recovery_s  # clear, recover, clear
        v.attempts[key] = claim.attempt + 1

    def _shift_back(self, v: Vehicle, index: int, stop: float) -> float:
        """Back-to-back junctions: never wait inside an earlier junction's box."""
        return shift_back(v.corridor, v.passed, v.along_m, index, stop, rules=self.config.rules)

    def _obey_signals(self, v: Vehicle, sim: datetime) -> None:
        """Non-emergency motion (:func:`divert.services.obey.decide_stop`, shared with the
        RETURN ETA replay): stop at the stop line on red (or a junction pre-empted for
        another approach) if the vehicle can still brake for it, go on green."""
        d = decide_stop(
            corridor=v.corridor,
            passed=v.passed,
            along_m=v.along_m,
            speed_mps=v.speed_mps,
            length_m=v.route.length_m,
            t=sim.timestamp(),
            signals=RegistrySignals(self.junctions, v.city_id, open_holds=True),
            rules=self.config.rules,
            committed=v.stop_for,
        )
        v.passed.update(d.passed)
        v.stop_at_m, v.stop_for, v.speed_advisory_mps = d.stop_at_m, d.stop_for, None

    # ---------------------------------------------------------- resolution
    def _resolve_city(self, city_id: str, wall: datetime, sim: datetime) -> None:
        t = sim.timestamp()
        by_signal: dict[str, dict[str, tuple[Vehicle, Claim]]] = {}
        for v in self.vehicles.get(city_id, {}).values():
            if v.emergency:  # recomputed below from this tick's resolution
                v.speed_advisory_mps, v.stop_at_m, v.stop_for = None, None, None
            for sid, claim in v.claims.items():
                by_signal.setdefault(sid, {})[v.vehicle_id] = (v, claim)
        b = self.batch(city_id)
        now_active: set[str] = set()
        advice: dict[str, list[_Advice]] = {}
        timeouts: list[tuple[Vehicle, Claim]] = []
        for sid in sorted(by_signal):
            doc = self._resolve_junction(city_id, sid, by_signal[sid], wall, t, advice, timeouts)
            if doc is not None:
                self._last_signal_doc[(city_id, sid)] = doc
                b.signal_sets[sid] = doc
                now_active.add(sid)
        for v, claim in timeouts:
            self._timeout(v, claim, wall, t)
        for vid, items in advice.items():
            self._apply_advice(self.vehicles[city_id][vid], items)
        claimed_now = {sid for v in self.vehicles.get(city_id, {}).values() for sid in v.claims}
        now_active &= claimed_now
        self._recover(city_id, t, claimed_now, now_active)
        for sid in self._active_signals.get(city_id, set()) - now_active:
            b.signal_dels.add(sid)
            b.signal_sets.pop(sid, None)
            self._last_signal_doc.pop((city_id, sid), None)
        self._active_signals[city_id] = now_active

    def _recover(self, city_id: str, t: float, claimed: set[str], active: set[str]) -> None:
        """Junctions no longer claimed (released, timed out): -> RECOVERING -> NORMAL.
        Adds the recovering ones to ``active``; counts starved junctions."""
        b = self.batch(city_id)
        starved = 0
        for sid, ctl in self.junctions.items(city_id):
            if sid not in claimed and ctl.served_phase is not None:
                ctl.release(t, self.config.rules.recovery_s)
            if ctl.is_starved(t):
                starved += 1
            if sid in active or ctl.state(t) is not JunctionState.RECOVERING:
                continue
            last = self._last_signal_doc.get((city_id, sid))
            if last is None:  # never shown (cannot recover without having been served)
                continue
            b.signal_sets[sid] = self._recovering_doc(ctl, last)
            active.add(sid)
        self.starved[city_id] = starved
        self.junctions.drop_idle(city_id, t, keep=active)

    def _resolve_junction(  # noqa: PLR0917 - one junction's slice of the tick
        self,
        city_id: str,
        sid: str,
        holders: dict[str, tuple[Vehicle, Claim]],
        wall: datetime,
        t: float,
        advice: dict[str, list[_Advice]],
        timeouts: list[tuple[Vehicle, Claim]],
    ) -> dict[str, Any] | None:
        """Sequence one junction's claimants, serve the first phase, settle every claim.
        Returns the junction's signals/active document."""
        rules = self.config.rules
        any_v, any_claim = next(iter(holders.values()))
        entry = any_v.entry(any_claim.key)
        if entry is None:  # the corridor lost the entry (cannot happen): drop the claims
            for v, _ in holders.values():
                self._release(v, sid, wall, reason="failed")
            return None
        ctl = self.junctions.get(city_id, entry, t)
        for v, claim in holders.values():
            e = v.entry(claim.key)
            claim.phase = ctl.geometry.phase_for(e.approach_bearing) if e else claim.phase
        if ctl.served_phase is None:  # leader failover: adopt the restored service
            restored = [
                c for _, c in holders.values()
                if c.green_at is not None and c.state is SignalState.PREEMPTED
            ]  # fmt: skip
            if restored:
                ctl.adopt(restored[0].phase, restored[0].green_at or t, t)
        locked: int | None = None
        green_now = ctl.served_phase is not None and (ctl.green_at or math.inf) <= t
        if green_now and any(
            c.phase == ctl.served_phase and c.eta_s <= CROSSING_LOCK_S for _, c in holders.values()
        ):
            locked = ctl.served_phase
        res = resolve_junction(
            {vid: Claimant(vid, c.rank, c.eta_s, c.phase) for vid, (_, c) in holders.items()},
            window_s=rules.priority_window_s,
            locked_phase=locked,
        )
        green_at = ctl.serve(res.order[0], t)
        cross_s = rules.box_m(ctl.geometry.legs) / JUNCTION_PREEMPTED_MPS
        clear = [
            t + max(c.eta_s for _, c in holders.values() if c.phase == p) + cross_s
            for p in res.order
        ]
        greens = queued_green_times(green_at, clear)
        b = self.batch(city_id)
        for vid, (v, claim) in sorted(holders.items()):
            pos = res.queue_pos[vid]
            self._settle(v, claim, pos, greens[pos], res.winner, wall, t)
            if self._held_too_long(claim, t):
                timeouts.append((v, claim))
                continue
            e = v.entry(claim.key)
            if e is not None:
                advice.setdefault(vid, []).append(self._advice(v, e, claim, t))
            b.claim_sets[(sid, vid)] = claim.to_json(v)
        state = ctl.state(t, queued=len(res.order) > 1)
        return self._signal_doc(sid, ctl, holders, winner=res.winner, state=state, t=t)

    def _settle(  # noqa: PLR0917 - the claim's share of the resolution
        self,
        v: Vehicle,
        claim: Claim,
        pos: int,
        green_at: float,
        winner: str | None,
        wall: datetime,
        t: float,
    ) -> None:
        """Apply a claimant's place in the service order and emit its events."""
        served = pos == 0
        new_state = SignalState.PREEMPTED if served else SignalState.CONFLICT
        is_new = claim.conflicts < 0
        became_conflict = new_state is SignalState.CONFLICT and (
            is_new or claim.state is not SignalState.CONFLICT
        )
        claim.state = new_state
        claim.queue_pos = pos
        claim.expected_green_at = green_at
        if served:
            claim.green_at = max(green_at, claim.claimed_t)
            if claim.phase_state_at_claim is None:
                claim.phase_state_at_claim = (
                    PhaseStateAtClaim.GREEN_EXTEND
                    if green_at <= claim.claimed_t
                    else PhaseStateAtClaim.RED_TRUNCATE
                )
        else:
            claim.green_at = None
        if is_new:
            claim.conflicts = 0
            self._history(
                v,
                HistoryEventType.PREEMPTION_CLAIMED,
                claim.event_id,
                wall,
                **self._claim_fields(v, claim),
                eta_s=_r(claim.eta_s, 1),
            )
        if became_conflict:
            claim.conflicts += 1
            self._history(
                v,
                HistoryEventType.PREEMPTION_CONFLICT,
                self._claim_event_id("cfl", v, claim, claim.conflicts),
                wall,
                **self._claim_fields(v, claim),
                winner=winner,
                eta_s=_r(claim.eta_s, 1),
                expected_green_at=_iso_epoch(claim.expected_green_at),
            )
        self._announce_phase(v, claim, wall, t)

    def _held_too_long(self, claim: Claim, t: float) -> bool:
        """Max hold: served and green for it, but not at the junction within the limit."""
        return (
            claim.state is SignalState.PREEMPTED
            and claim.green_at is not None
            and claim.green_at <= t
            and claim.crossed_at is None
            and t - claim.green_at > self.config.rules.max_hold_s
        )

    def _announce_phase(self, v: Vehicle, claim: Claim, wall: datetime, t: float) -> None:
        """preempt_transition (red truncation started) / preempt_green (green for it)."""
        if claim.state is not SignalState.PREEMPTED or claim.green_at is None:
            return
        if claim.green_at > t and not claim.announced_transition:
            claim.announced_transition = True
            self._history(
                v,
                HistoryEventType.PREEMPT_TRANSITION,
                self._claim_event_id("ptr", v, claim),
                wall,
                **self._claim_fields(v, claim),
                transition_s_left=_r(claim.green_at - t, 1),
            )
        if claim.green_at <= t and not claim.announced_green:
            claim.announced_green = True
            self._history(
                v,
                HistoryEventType.PREEMPT_GREEN,
                self._claim_event_id("pgr", v, claim),
                wall,
                **self._claim_fields(v, claim),
                eta_s=_r(claim.eta_s, 1),
            )

    def _advice(self, v: Vehicle, c: CorridorSignal, claim: Claim, t: float) -> _Advice:
        rules = self.config.rules
        line = rules.stop_line_m(c.along_m, c.legs)
        index = next((i for i, e in enumerate(v.corridor) if e.key == c.key), 0)
        green = claim.green_at if claim.state is SignalState.PREEMPTED else claim.expected_green_at
        if green is None or v.along_m > line + STOP_SNAP_M:
            return _Advice(line, c.key, index, NO_ADVISORY)
        half_box_s = (c.along_m - line) / JUNCTION_PREEMPTED_MPS
        adv = speed_advisory(line - v.along_m, green - t, max(0.0, claim.eta_s - half_box_s))
        return _Advice(line, c.key, index, adv)

    def _apply_advice(self, v: Vehicle, items: list[_Advice]) -> None:
        """Nearest stop wins; otherwise the slowest speed advisory ahead applies."""
        speeds = [a.advisory.speed_mps for a in items if a.advisory.speed_mps is not None]
        stops = sorted((a for a in items if a.advisory.stop), key=lambda a: a.stop_line_m)
        if stops:
            first = stops[0]
            v.stop_at_m = self._shift_back(v, first.index, first.stop_line_m)
            v.stop_for = first.key
            v.speed_advisory_mps = 0.0
        elif speeds:
            v.speed_advisory_mps = min(speeds)

    @staticmethod
    def _signal_doc(
        sid: str,
        ctl: JunctionControl,
        holders: Mapping[str, tuple[Vehicle, Claim]],
        *,
        winner: str | None,
        state: JunctionState,
        t: float,
    ) -> dict[str, Any]:
        lead = holders.get(winner) if winner is not None else None
        wv, wc = lead if lead is not None else next(iter(holders.values()))
        green_at = ctl.green_at
        return {
            "signal_id": sid,
            "state": state.value,
            "pos": [wc.pos.lat, wc.pos.lng],
            "vehicle_id": wv.vehicle_id,
            "service": wv.service.value,
            "lane": wc.lane.value,
            "approach_bearing": _r(wc.bearing, 1),
            "eta_s": _r(wc.eta_s, 1),
            "phase_info": {
                "served_phase": ctl.served_phase,
                "phases": ctl.plan.phases,
                "cycle_s": _r(ctl.plan.cycle_s, 1),
                "geometry": ctl.geometry.source,
                "transition_s_left": (0.0 if green_at is None else _r(max(0.0, green_at - t), 1)),
                "expected_green_at": _iso_epoch(green_at),
                "hold_s": 0.0 if green_at is None else _r(max(0.0, t - green_at), 1),
                "state_at_claim": (
                    None if ctl.state_at_claim is None else ctl.state_at_claim.value
                ),
            },
            "claimants": [
                {
                    "vehicle_id": vid,
                    "service": v.service.value,
                    "eta_s": _r(c.eta_s, 1),
                    "state": c.state.value,
                    "phase_state": _claim_phase_state(c, t).value,
                    "movement": c.movement.value,
                    "rank": c.rank,
                    "lane": c.lane.value,
                    "lane_index": c.lane_index,
                    "lanes_total": c.lanes_total,
                    "phase": c.phase,
                    "queue_pos": c.queue_pos,
                    "expected_green_at": _iso_epoch(c.expected_green_at),
                }
                for vid, (v, c) in sorted(holders.items())
            ],
        }

    @staticmethod
    def _recovering_doc(ctl: JunctionControl, last: dict[str, Any]) -> dict[str, Any]:
        """Legacy fields keep the last winner's values so older clients still render it."""
        base = dict(last)
        base.update(
            {
                "state": JunctionState.RECOVERING.value,
                "eta_s": 0.0,
                "claimants": [],
                "phase_info": {
                    "served_phase": None,
                    "phases": ctl.plan.phases,
                    "cycle_s": _r(ctl.plan.cycle_s, 1),
                    "geometry": ctl.geometry.source,
                    "recovery_phase": ctl.recovery_phase,
                    "recovering_until": _iso_epoch(ctl.recover_until),
                    "transition_s_left": 0.0,
                    "expected_green_at": None,
                    "hold_s": 0.0,
                },
            }
        )
        return base

    # --------------------------------------------------------------- endings
    def _remove(self, v: Vehicle, wall: datetime, *, reason: str) -> None:
        """Take a vehicle out of the simulation for good (every claim is released)."""
        self._release_all(v, wall, reason=reason)
        vs = self.vehicles.get(v.city_id, {})
        if vs.get(v.vehicle_id) is v:
            del vs[v.vehicle_id]
            self._fleet_version += 1
        b = self.batch(v.city_id)
        b.vehicle_sets.pop(v.vehicle_id, None)
        b.route_sets.pop(v.vehicle_id, None)
        b.vehicle_dels.add(v.vehicle_id)
        b.route_dels.add(v.vehicle_id)
        b.finished[v.dispatch_id] = wall.timestamp()
        self.finished.setdefault(v.city_id, set()).add(v.dispatch_id)
        b.publish.append(
            message(
                StreamMessageType.VEHICLE_REMOVED.value,
                city=v.city_id,
                vehicle_id=v.vehicle_id,
                dispatch_id=v.dispatch_id,
                incident_id=v.incident_id,
                reason=reason,
            )
        )

    def _finish(self, v: Vehicle, wall: datetime) -> None:
        """RETURN leg done: the unit is back in service and the dispatch COMPLETED."""
        self._history(
            v,
            HistoryEventType.BACK_IN_SERVICE,
            event_id("bis", v.dispatch_id),
            wall,
            station_id=v.station_id,
            trip_s=_r(v.trip_s, 1),
            end_reason="CANCELLED" if v.cancelled else "CLEARED",
            preempted_passed=v.preempted_passed,
        )
        self._remove(v, wall, reason="back_in_service")

    def _end(self, v: Vehicle, how: str, wall: datetime, *, reason: str | None = None) -> None:
        """A dispatch that cannot go on (simulation error, superseded): FAILED, the unit is
        freed, every claim released."""
        if how != "failed":  # pragma: no cover - only failures end a dispatch abruptly now
            raise ValueError(f"unsupported ending {how!r}")
        if v.moving:
            self._leg_ended(v, wall, LegEndReason.FAILED)
        extra: dict[str, Any] = {"leg_seq": v.leg_seq, "trip_s": _r(v.trip_s, 1)}
        if reason:
            extra["reason"] = reason
        self._history(v, HistoryEventType.FAILED, event_id("fail", v.dispatch_id), wall, **extra)
        self._remove(v, wall, reason="failed")
        if not v.released and not v.cancelled:
            self._incident_updated(v, "FAILED", wall, **({"reason": reason} if reason else {}))
