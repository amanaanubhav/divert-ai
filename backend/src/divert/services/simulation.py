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
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from divert.domain.enums import Lane, ServiceType, SignalState
from divert.domain.geo import LatLng, bearing_at_distance, interpolate_by_distance
from divert.domain.models import City
from divert.domain.route import CorridorSignal, Route
from divert.services.eta import EtaModel, EtaQuery, congestion_at
from divert.services.preemption import Claimant, PreemptionRules, resolve
from divert.services.speed_model import ARRIVAL_TOLERANCE_M, SpeedProfile, build_profile
from divert.store.events import (
    HistoryEventType,
    StreamMessageType,
    event_id,
    message,
)

log = logging.getLogger(__name__)


class VehicleStatus(StrEnum):
    EN_ROUTE = "EN_ROUTE"
    ON_SCENE = "ON_SCENE"


class CommandError(ValueError):
    """A command that can never be applied (it is acked and, if possible, failed)."""


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds")


def _r(x: float, nd: int = 2) -> float:
    return round(float(x), nd)


@dataclass(slots=True)
class Claim:
    signal_id: str
    event_id: str
    state: SignalState
    eta_s: float
    eta_at_claim_s: float
    lane: Lane
    bearing: float
    pos: LatLng
    claimed_at: str
    conflicts: int = 0

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
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Claim:
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
        )


@dataclass(slots=True)
class Vehicle:
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
    elapsed_s: float = 0.0
    scene_s: float = 0.0
    eta_s: float = 0.0
    signal_etas: dict[str, float] = field(default_factory=dict)
    claims: dict[str, Claim] = field(default_factory=dict)
    passed: set[str] = field(default_factory=set)
    preempted_passed: int = 0
    samples: int = 0
    travel_s: float | None = None
    _motion_key: tuple[float, frozenset[str]] | None = None
    _motion: SpeedProfile | None = None

    # ------------------------------------------------------------- geometry
    @property
    def pos(self) -> LatLng:
        p, _ = interpolate_by_distance(self.route.points, self.route.cumulative, self.along_m)
        return p

    @property
    def heading(self) -> float:
        return bearing_at_distance(self.route.points, self.route.cumulative, self.along_m)

    # ---------------------------------------------------------- serialisation
    def live_json(self) -> dict[str, Any]:
        """Field of the ``vehicles(city)`` hash: enough to resume after a failover."""
        p = self.pos
        return {
            "id": self.vehicle_id,
            "dispatch_id": self.dispatch_id,
            "incident_id": self.incident_id,
            "city": self.city_id,
            "service": self.service.value,
            "station_id": self.station_id,
            "incident_type": self.incident_type,
            "status": self.status.value,
            "pos": [_r(p.lat, 7), _r(p.lng, 7)],
            "heading": _r(self.heading, 1),
            "along_m": _r(self.along_m, 2),
            "distance_m": _r(self.route.length_m, 2),
            "speed_mps": _r(self.speed_mps, 2),
            "eta_s": _r(self.eta_s, 1),
            "predicted_eta_s": _r(self.predicted_eta_s, 1),
            "elapsed_s": _r(self.elapsed_s, 2),
            "scene_s": _r(self.scene_s, 2),
            "travel_s": None if self.travel_s is None else _r(self.travel_s, 2),
            "preempted_passed": self.preempted_passed,
            "passed": sorted(self.passed),
            "samples": self.samples,
            "started_at": self.started_at,
            "destination": [self.destination.lat, self.destination.lng],
            "claims": sorted(self.claims),
        }

    def tick_json(self) -> dict[str, Any]:
        p = self.pos
        return {
            "id": self.vehicle_id,
            "dispatch_id": self.dispatch_id,
            "incident_id": self.incident_id,
            "service": self.service.value,
            "status": self.status.value,
            "pos": [_r(p.lat, 7), _r(p.lng, 7)],
            "heading": _r(self.heading, 1),
            "speed_mps": _r(self.speed_mps, 2),
            "eta_s": _r(self.eta_s, 1),
            "along_m": _r(self.along_m, 1),
            "distance_m": _r(self.route.length_m, 1),
            "preempted_passed": self.preempted_passed,
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
        """Value of ``vehicle_route(city, vehicle)``: static for the dispatch."""
        return {
            "dispatch_id": self.dispatch_id,
            "route": self.route.to_json(),
            "corridor": [c.to_json() for c in self.corridor],
            "destination": [self.destination.lat, self.destination.lng],
        }

    @classmethod
    def from_command(cls, cmd: Mapping[str, Any], started_at: str) -> Vehicle:
        try:
            route = Route.from_json(cmd["route"])
            return cls(
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
                        key=lambda c: c.along_m,
                    )
                ),
                destination=LatLng(float(cmd["destination"][0]), float(cmd["destination"][1])),
                predicted_eta_s=float(cmd["predicted_eta_s"]),
                started_at=started_at,
                eta_s=float(cmd["predicted_eta_s"]),
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise CommandError(f"invalid start_dispatch command: {exc!r}") from exc

    @classmethod
    def restore(
        cls, live: Mapping[str, Any], route_doc: Mapping[str, Any], claims: Mapping[str, Claim]
    ) -> Vehicle:
        cmd = {
            **live,
            "vehicle_id": live["id"],
            "city": live["city"],
            "route": route_doc["route"],
            "corridor": route_doc.get("corridor", []),
            "destination": route_doc.get("destination", live.get("destination")),
        }
        v = cls.from_command(cmd, str(live.get("started_at", "")))
        v.status = VehicleStatus(live.get("status", VehicleStatus.EN_ROUTE.value))
        v.along_m = float(live.get("along_m", 0.0))
        v.speed_mps = float(live.get("speed_mps", 0.0))
        v.elapsed_s = float(live.get("elapsed_s", 0.0))
        v.scene_s = float(live.get("scene_s", 0.0))
        v.eta_s = float(live.get("eta_s", v.predicted_eta_s))
        v.preempted_passed = int(live.get("preempted_passed", 0))
        v.passed = {str(s) for s in live.get("passed", [])}
        v.samples = int(live.get("samples", 0))
        travel = live.get("travel_s")
        v.travel_s = None if travel is None else float(travel)
        v.claims = dict(claims)
        return v


@dataclass(slots=True)
class CityBatch:
    """Everything one city's tick writes - applied by the worker in ONE MULTI/EXEC."""

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
    finished: dict[str, float] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    publish: list[dict[str, Any]] = field(default_factory=list)
    acks: list[str] = field(default_factory=list)
    tick: dict[str, Any] | None = None

    def is_empty(self) -> bool:
        return not (
            self.vehicle_sets or self.vehicle_dels or self.route_sets or self.route_dels
            or self.claim_sets or self.claim_dels or self.vclaim_adds or self.vclaim_rems
            or self.vclaim_dels or self.signal_sets or self.signal_dels or self.finished
            or self.history or self.publish or self.acks or self.tick
        )  # fmt: skip


@dataclass(frozen=True, slots=True)
class SimConfig:
    scene_time_s: float = 120.0
    position_sample_s: float = 5.0
    rules: PreemptionRules = field(default_factory=PreemptionRules)


class SimulationEngine:
    def __init__(self, cities: Mapping[str, City], eta_model: EtaModel, config: SimConfig) -> None:
        self.cities: dict[str, City] = dict(cities)
        self.eta_model = eta_model
        self.config = config
        self.vehicles: dict[str, dict[str, Vehicle]] = {}
        self.finished: dict[str, set[str]] = {}
        self.tick_no = 0
        self._batches: dict[str, CityBatch] = {}
        self._active_signals: dict[str, set[str]] = {}

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
        self.city_vehicles(city_id)[v.vehicle_id] = v
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
        """vehicle_added + en_route (deterministic id: replays are de-duplicated)."""
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
        if v.status is not VehicleStatus.EN_ROUTE:
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
        city_id = str(cmd.get("city", ""))
        dispatch_id = str(cmd.get("dispatch_id", ""))
        if city_id not in self.cities:
            raise CommandError(f"unknown city {city_id!r}")
        self.ack(city_id, stream_id)
        v = self.find_dispatch(dispatch_id)
        if v is not None:
            self._end(v, "cancelled", wall, reason=str(cmd.get("reason", "cancelled")))
            return True
        if self.is_finished(city_id, dispatch_id):
            return False
        # Never started here (or its start was reordered after this cancel): still report
        # it so the persister frees the unit, and make a late start a no-op.
        b = self.batch(city_id)
        b.finished[dispatch_id] = wall.timestamp()
        self.finished.setdefault(city_id, set()).add(dispatch_id)
        b.history.append(
            message(
                HistoryEventType.CANCELLED.value,
                event_id=event_id("cxl", dispatch_id),
                ts=_iso(wall),
                city=city_id,
                incident_id=str(cmd.get("incident_id", "")),
                dispatch_id=dispatch_id,
                vehicle_id=str(cmd.get("vehicle_id", "")),
                reason=str(cmd.get("reason", "cancelled")),
            )
        )
        return False

    def restore(self, v: Vehicle, *, announce_at: datetime | None = None) -> None:
        """Re-insert a vehicle rebuilt from Redis or PostgreSQL (leader take-over).

        ``announce_at``: (PG rebuild) re-announce it - its start may never have been
        recorded (dispatched while no leader was running); event ids are deterministic,
        so an en_route already persisted is de-duplicated.
        """
        self.city_vehicles(v.city_id)[v.vehicle_id] = v
        if announce_at is not None:
            self._announce(v, announce_at)
        b = self.batch(v.city_id)
        b.vehicle_sets[v.vehicle_id] = v.live_json()
        b.route_sets[v.vehicle_id] = v.route_json()
        for sid, claim in v.claims.items():
            b.claim_sets[(sid, v.vehicle_id)] = claim.to_json(v)
            b.vclaim_adds.setdefault(v.vehicle_id, set()).add(sid)

    def mark_known_active_signals(self, city_id: str, signal_ids: set[str]) -> None:
        """Signals currently non-NORMAL in Redis; the first tick deletes stale ones."""
        self._active_signals.setdefault(city_id, set()).update(signal_ids)

    def mark_finished(self, city_id: str, dispatch_ids: set[str]) -> None:
        self.finished.setdefault(city_id, set()).update(dispatch_ids)

    # -------------------------------------------------------------------- tick
    def tick(self, *, dt_sim: float, sim: datetime, wall: datetime) -> dict[str, CityBatch]:
        """Advance every vehicle by ``dt_sim`` simulated seconds; return the batches."""
        self.tick_no += 1
        for city_id in list(self.vehicles) + [
            c for c in self._active_signals if c not in self.vehicles
        ]:
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
            self._resolve_city(city_id, wall)
            b = self.batch(city_id)
            if vs or b.signal_dels or b.vehicle_dels:
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

    def _refresh_eta(self, v: Vehicle, sim: datetime) -> None:
        est = self.eta_model.estimate(
            EtaQuery(
                route=v.route,
                service=v.service,
                city=self.cities[v.city_id],
                corridor=v.corridor,
                along_m=v.along_m,
                speed_mps=v.speed_mps,
                at=sim,
            )
        )
        if not math.isfinite(est.to_destination_s) or est.to_destination_s < 0:
            raise ValueError(f"ETA model returned {est.to_destination_s!r}")
        v.eta_s = est.to_destination_s
        v.signal_etas = est.to_signals_s

    def _motion_profile(self, v: Vehicle, sim: datetime) -> SpeedProfile:
        congestion = congestion_at(self.cities[v.city_id], sim)
        preempted = frozenset(
            sid for sid, c in v.claims.items() if c.state is SignalState.PREEMPTED
        )
        key = (congestion, preempted)
        if v._motion is None or v._motion_key != key:
            v._motion = build_profile(
                v.route,
                v.service,
                congestion,
                [(c.along_m, c.signal_id in preempted) for c in v.corridor],
            )
            v._motion_key = key
        return v._motion

    def _advance(self, v: Vehicle, dt: float, sim: datetime, wall: datetime) -> None:
        b = self.batch(v.city_id)
        if v.status is VehicleStatus.ON_SCENE:
            v.scene_s += dt
            if v.scene_s >= self.config.scene_time_s:
                self._end(v, "cleared", wall)
            else:
                b.vehicle_sets[v.vehicle_id] = v.live_json()
            return
        prof = self._motion_profile(v, sim)
        v.along_m, v.speed_mps = prof.step(v.along_m, v.speed_mps, dt)
        v.elapsed_s += dt
        if v.along_m >= v.route.length_m - ARRIVAL_TOLERANCE_M:
            self._arrive(v, wall)
            return
        self._refresh_eta(v, sim)
        self._update_claims(v, wall)
        self._sample(v, wall)
        b.vehicle_sets[v.vehicle_id] = v.live_json()

    def _sample(self, v: Vehicle, wall: datetime) -> None:
        step = self.config.position_sample_s
        if v.elapsed_s + 1e-9 < v.samples * step:
            return
        p = v.pos
        self._history(
            v,
            HistoryEventType.VEHICLE_POSITION,
            event_id("pos", v.dispatch_id, v.samples),
            wall,
            pos=[_r(p.lat, 7), _r(p.lng, 7)],
            speed_mps=_r(v.speed_mps, 2),
            along_m=_r(v.along_m, 2),
            elapsed_s=_r(v.elapsed_s, 2),
            sample=v.samples,
        )
        v.samples = int(v.elapsed_s // step) + 1

    # ------------------------------------------------------------------ claims
    def _update_claims(self, v: Vehicle, wall: datetime) -> None:
        rules = self.config.rules
        b = self.batch(v.city_id)
        for c in v.corridor:
            sid = c.signal_id
            if sid in v.passed:
                continue
            claim = v.claims.get(sid)
            if claim is not None:
                if rules.is_past(v.along_m, c.along_m):
                    self._release(v, sid, wall, reason="passed")
                    v.passed.add(sid)
                else:
                    claim.eta_s = v.signal_etas.get(sid, 0.0)
                continue
            if rules.is_past(v.along_m, c.along_m):
                v.passed.add(sid)
                continue
            eta = v.signal_etas.get(sid, 0.0)
            if rules.should_claim(v.along_m, c.along_m, eta):
                v.claims[sid] = Claim(
                    signal_id=sid,
                    event_id=event_id("clm", v.dispatch_id, sid),
                    state=SignalState.PREEMPTED,  # provisional; resolved this tick
                    eta_s=eta,
                    eta_at_claim_s=eta,
                    lane=c.lane,
                    bearing=c.approach_bearing,
                    pos=c.pos,
                    claimed_at=_iso(wall),
                    conflicts=-1,  # marks "new": resolution emits preemption_claimed
                )
                b.vclaim_adds.setdefault(v.vehicle_id, set()).add(sid)

    def _claim_fields(self, v: Vehicle, claim: Claim) -> dict[str, Any]:
        return {
            "signal_id": claim.signal_id,
            "claim_event_id": claim.event_id,
            "state": claim.state.value,
            "lane": claim.lane.value,
            "approach_bearing": _r(claim.bearing, 1),
            "eta_at_claim_s": _r(claim.eta_at_claim_s, 1),
            "claimed_at": claim.claimed_at,
        }

    def _release(self, v: Vehicle, sid: str, wall: datetime, *, reason: str) -> None:
        claim = v.claims.pop(sid, None)
        if claim is None:
            return
        b = self.batch(v.city_id)
        b.claim_dels.add((sid, v.vehicle_id))
        b.claim_sets.pop((sid, v.vehicle_id), None)
        b.vclaim_rems.setdefault(v.vehicle_id, set()).add(sid)
        if claim.conflicts < 0:  # claimed and released within one tick: never resolved
            return
        if reason == "passed" and claim.state is SignalState.PREEMPTED:
            v.preempted_passed += 1
        self._history(
            v,
            HistoryEventType.PREEMPTION_RELEASED,
            event_id("rel", v.dispatch_id, sid),
            wall,
            **self._claim_fields(v, claim),
            released_at=_iso(wall),
            reason=reason,
        )

    def _release_all(self, v: Vehicle, wall: datetime, *, reason: str) -> None:
        for sid in list(v.claims):
            self._release(v, sid, wall, reason=reason)
        b = self.batch(v.city_id)
        b.vclaim_dels.add(v.vehicle_id)
        b.vclaim_adds.pop(v.vehicle_id, None)
        b.vclaim_rems.pop(v.vehicle_id, None)

    def _resolve_city(self, city_id: str, wall: datetime) -> None:
        by_signal: dict[str, dict[str, tuple[Vehicle, Claim]]] = {}
        for v in self.vehicles.get(city_id, {}).values():
            for sid, claim in v.claims.items():
                by_signal.setdefault(sid, {})[v.vehicle_id] = (v, claim)
        b = self.batch(city_id)
        prev = self._active_signals.get(city_id, set())
        now_active: set[str] = set()
        for sid, holders in by_signal.items():
            res = resolve(
                {vid: Claimant(vid, v.service, c.eta_s) for vid, (v, c) in holders.items()},
                self.config.rules.tie_s,
            )
            for vid, (v, claim) in holders.items():
                new_state = res.per_vehicle[vid]
                is_new = claim.conflicts < 0
                became_conflict = new_state is SignalState.CONFLICT and (
                    is_new or claim.state is not SignalState.CONFLICT
                )
                claim.state = new_state
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
                        event_id("cfl", v.dispatch_id, sid, claim.conflicts),
                        wall,
                        **self._claim_fields(v, claim),
                        winner=res.winner,
                        eta_s=_r(claim.eta_s, 1),
                    )
                b.claim_sets[(sid, vid)] = claim.to_json(v)
            wv, wc = holders[res.winner] if res.winner else next(iter(holders.values()))
            b.signal_sets[sid] = {
                "signal_id": sid,
                "state": res.state.value,
                "pos": [wc.pos.lat, wc.pos.lng],
                "vehicle_id": wv.vehicle_id,
                "service": wv.service.value,
                "lane": wc.lane.value,
                "approach_bearing": _r(wc.bearing, 1),
                "eta_s": _r(wc.eta_s, 1),
                "claimants": [
                    {
                        "vehicle_id": vid,
                        "service": v.service.value,
                        "eta_s": _r(c.eta_s, 1),
                        "state": c.state.value,
                    }
                    for vid, (v, c) in sorted(holders.items())
                ],
            }
            now_active.add(sid)
        for sid in prev - now_active:
            b.signal_dels.add(sid)
            b.signal_sets.pop(sid, None)
        self._active_signals[city_id] = now_active

    # --------------------------------------------------------------- endings
    def _arrive(self, v: Vehicle, wall: datetime) -> None:
        self._release_all(v, wall, reason="arrived")
        v.status = VehicleStatus.ON_SCENE
        v.along_m = v.route.length_m
        v.speed_mps = 0.0
        v.eta_s = 0.0
        v.travel_s = v.elapsed_s
        v.signal_etas = {}
        city = self.cities[v.city_id]
        saved = v.preempted_passed * city.traffic.avg_junction_delay_s
        p = v.pos
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
        )
        self._incident_updated(v, "ON_SCENE", wall, travel_s=_r(v.elapsed_s, 1))
        self.batch(v.city_id).vehicle_sets[v.vehicle_id] = v.live_json()

    def _end(self, v: Vehicle, how: str, wall: datetime, *, reason: str | None = None) -> None:
        """Remove a vehicle for good: cleared | cancelled | failed. Releases every claim."""
        self._release_all(v, wall, reason=how)
        vs = self.vehicles.get(v.city_id, {})
        if vs.get(v.vehicle_id) is v:
            del vs[v.vehicle_id]
        b = self.batch(v.city_id)
        b.vehicle_sets.pop(v.vehicle_id, None)
        b.route_sets.pop(v.vehicle_id, None)
        b.vehicle_dels.add(v.vehicle_id)
        b.route_dels.add(v.vehicle_id)
        b.finished[v.dispatch_id] = wall.timestamp()
        self.finished.setdefault(v.city_id, set()).add(v.dispatch_id)
        kind = {
            "cleared": HistoryEventType.CLEARED,
            "cancelled": HistoryEventType.CANCELLED,
            "failed": HistoryEventType.FAILED,
        }[how]
        prefix = {"cleared": "clr", "cancelled": "cxl", "failed": "fail"}[how]
        extra: dict[str, Any] = {"scene_s": _r(v.scene_s, 1)} if how == "cleared" else {}
        if reason:
            extra["reason"] = reason
        self._history(v, kind, event_id(prefix, v.dispatch_id), wall, **extra)
        b.publish.append(
            message(
                StreamMessageType.VEHICLE_REMOVED.value,
                city=v.city_id,
                vehicle_id=v.vehicle_id,
                dispatch_id=v.dispatch_id,
                incident_id=v.incident_id,
                reason=how,
            )
        )
        status = {"cleared": "RESOLVED", "cancelled": "CANCELLED", "failed": "FAILED"}[how]
        self._incident_updated(v, status, wall, **({"reason": reason} if reason else {}))
