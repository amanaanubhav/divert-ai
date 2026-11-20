"""Trip legs (design §1, §2): the data the simulation and the LegPlanner exchange. Pure.

* :class:`PendingLeg` - the next leg a vehicle has asked the planner for (in the vehicle's
  live state, so a new leader re-issues it);
* :class:`LegPlan` - a planned leg as carried by the ``leg_planned`` command (applied
  idempotently, keyed by ``(dispatch_id, seq)``);
* :func:`leg_planned_command` / :func:`route_replaced_command` - those commands;
* :func:`splice` - a re-route: the current route up to the re-route point, then the new
  route, with the corridor re-keyed so passed junctions keep their keys;
* :func:`behind_plan_s` / :func:`reroute_origin_m` - the re-route triggers' arithmetic.
"""

from __future__ import annotations

import bisect
import dataclasses
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from divert.domain.enums import DestinationKind, LegKind
from divert.domain.geo import LatLng, haversine_m, interpolate_by_distance
from divert.domain.route import CorridorSignal, Route
from divert.store.events import CommandType, message

# Re-routing (design §2) - assumptions where the design gives no number:
REROUTE_LOOKAHEAD_S: Final = 15.0  # re-route from where the vehicle will be in ~15 s ...
REROUTE_LOOKAHEAD_MIN_M: Final = 100.0  # ... but at least this far ahead (planning time)
REROUTE_MIN_REMAINING_M: Final = 500.0  # not worth re-routing the last 500 m
REROUTE_COOLDOWN_S: Final = 60.0  # sim seconds between two re-route evaluations of a leg
SPLICE_MARGIN_M: Final = 5.0  # a vehicle this close to the re-route point is too late for it
REROUTE_ZONE_TYPES: Final = frozenset({"FIRE", "CRIME"})
# A unit that ends a leg this close to its own station is home: no RETURN leg is planned
# (e.g. a hospital ambulance that took the patient to its own ED). Assumption: a station
# forecourt / hospital campus entrance is within 30 m of its OSM point - or of its road
# point (where OSRM snaps the station; see is_home).
AT_HOME_M: Final = 30.0


def is_home(pos: LatLng, home: LatLng, home_road: LatLng | None = None) -> bool:
    """The unit is at its own station: a RETURN leg would have no length.

    ``home`` is the station's OSM point; ``home_road`` its road point - where OSRM snaps
    the station, i.e. the first point of the unit's RESPONSE route. A station mapped
    more than AT_HOME_M off the road (a hospital campus) is only ever reached at its road
    point: every leg to it ends there, and a RETURN from there has no length.
    """
    if haversine_m(pos, home) <= AT_HOME_M:
        return True
    return home_road is not None and haversine_m(pos, home_road) <= AT_HOME_M


def _ll(raw: Any) -> LatLng:
    return LatLng(float(raw[0]), float(raw[1]))


@dataclass(slots=True)
class PendingLeg:
    """A leg the vehicle is waiting for (requested from the LegPlanner)."""

    seq: int
    kind: LegKind
    origin: LatLng
    requested_t: float  # sim epoch of the request
    start_at: float  # sim epoch the leg is expected to start (end of the dwell)
    attempts: int = 0  # failed planning attempts so far (leg_plan_failed)
    error: str | None = None  # last planning error (shown as "planning route (retrying)")

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind.value,
            "origin": [round(self.origin.lat, 7), round(self.origin.lng, 7)],
            "requested_t": round(self.requested_t, 3),
            "start_at": round(self.start_at, 3),
            "attempts": self.attempts,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> PendingLeg:
        return cls(
            seq=int(d["seq"]),
            kind=LegKind(d["kind"]),
            origin=_ll(d["origin"]),
            requested_t=float(d.get("requested_t", 0.0)),
            start_at=float(d.get("start_at", d.get("requested_t", 0.0))),
            attempts=int(d.get("attempts", 0)),
            error=d.get("error"),
        )


@dataclass(frozen=True, slots=True)
class LegPlan:
    """A planned leg (from the ``leg_planned`` command)."""

    seq: int
    kind: LegKind
    emergency: bool
    route: Route
    corridor: tuple[CorridorSignal, ...]
    destination: LatLng
    dest_kind: DestinationKind
    dest_station_id: str | None
    dest_name: str | None
    predicted_eta_s: float
    hospital: dict[str, Any] | None = None  # {id, name, location, emergency} (TRANSPORT)

    @classmethod
    def from_command(cls, cmd: Mapping[str, Any]) -> LegPlan:
        dest = cmd.get("destination") or {}
        return cls(
            seq=int(cmd["seq"]),
            kind=LegKind(cmd["kind"]),
            emergency=bool(cmd.get("emergency", cmd["kind"] != LegKind.RETURN.value)),
            route=Route.from_json(cmd["route"]),
            corridor=tuple(
                sorted(
                    (CorridorSignal.from_json(c) for c in cmd.get("corridor", [])),
                    key=lambda c: (c.along_m, c.signal_id, c.pass_no),
                )
            ),
            destination=_ll(dest["location"]),
            dest_kind=DestinationKind(dest.get("kind", DestinationKind.STATION.value)),
            dest_station_id=dest.get("station_id"),
            dest_name=dest.get("name"),
            predicted_eta_s=float(cmd.get("predicted_eta_s", 0.0)),
            hospital=cmd.get("hospital"),
        )


def destination_json(
    kind: DestinationKind, location: LatLng, station_id: str | None, name: str | None
) -> dict[str, Any]:
    return {
        "kind": kind.value,
        "station_id": station_id,
        "name": name,
        "location": [round(location.lat, 7), round(location.lng, 7)],
    }


def leg_planned_command(
    *,
    city_id: str,
    dispatch_id: str,
    incident_id: str,
    vehicle_id: str,
    seq: int,
    kind: LegKind,
    route: Route,
    corridor: Sequence[CorridorSignal],
    destination: dict[str, Any],
    predicted_eta_s: float,
    route_choice: dict[str, Any] | None,
    hospital: dict[str, Any] | None,
    planned_at: datetime,
) -> dict[str, Any]:
    """The ``leg_planned`` worker command (also the outbox payload, so it is in PG)."""
    return message(
        CommandType.LEG_PLANNED.value,
        command_id=f"leg:{dispatch_id.replace('-', '')}:{seq}",
        city=city_id,
        dispatch_id=dispatch_id,
        incident_id=incident_id,
        vehicle_id=vehicle_id,
        seq=seq,
        kind=kind.value,
        emergency=kind is not LegKind.RETURN,
        route=route.to_json(),
        corridor=[c.to_json() for c in corridor],
        destination=destination,
        predicted_eta_s=round(predicted_eta_s, 1),
        route_choice=route_choice,
        hospital=hospital,
        planned_at=planned_at.isoformat(),
    )


def route_replaced_command(
    *,
    city_id: str,
    dispatch_id: str,
    vehicle_id: str,
    seq: int,
    reroute_no: int,
    origin_along_m: float,
    route: Route,
    corridor: Sequence[CorridorSignal],
    saved_s: float,
    old_remaining_s: float,
    new_remaining_s: float,
    reason: str,
    route_choice: dict[str, Any] | None,
) -> dict[str, Any]:
    """The ``route_replaced`` worker command, keyed by (dispatch_id, seq, reroute_no)."""
    return message(
        CommandType.ROUTE_REPLACED.value,
        command_id=f"rrt:{dispatch_id.replace('-', '')}:{seq}:{reroute_no}",
        city=city_id,
        dispatch_id=dispatch_id,
        vehicle_id=vehicle_id,
        seq=seq,
        reroute_no=reroute_no,
        origin_along_m=round(origin_along_m, 2),
        route=route.to_json(),
        corridor=[c.to_json() for c in corridor],
        saved_s=round(saved_s, 1),
        old_remaining_s=round(old_remaining_s, 1),
        new_remaining_s=round(new_remaining_s, 1),
        reason=reason,
        route_choice=route_choice,
    )


# ------------------------------------------------------------------------- re-routing


def behind_plan_s(elapsed_s: float, eta_s: float, predicted_eta_s: float) -> float:
    """Projected lateness: elapsed + remaining ETA - the leg's predicted travel time."""
    return elapsed_s + eta_s - predicted_eta_s


def is_behind_plan(
    elapsed_s: float, eta_s: float, predicted_eta_s: float, *, share: float, min_s: float
) -> bool:
    """Trigger (b): more than ``share`` of the plan AND more than ``min_s`` behind it."""
    late = behind_plan_s(elapsed_s, eta_s, predicted_eta_s)
    return late > min_s and late > share * predicted_eta_s


def reroute_origin_m(along_m: float, speed_mps: float, length_m: float) -> float | None:
    """Where a re-route starts: ~15 s ahead (planning happens meanwhile), at least 100 m;
    None when less than 500 m would be left after it."""
    at = along_m + max(REROUTE_LOOKAHEAD_MIN_M, speed_mps * REROUTE_LOOKAHEAD_S)
    if length_m - at < REROUTE_MIN_REMAINING_M:
        return None
    return at


def _shift(c: CorridorSignal, offset: float, pass_no: int) -> CorridorSignal:
    adv = c.advice
    return dataclasses.replace(
        c,
        along_m=c.along_m + offset,
        pass_no=pass_no,
        advice=None
        if adv is None
        else dataclasses.replace(adv, advise_at_m=adv.advise_at_m + offset),
    )


def splice(
    old: Route,
    old_corridor: Sequence[CorridorSignal],
    at_m: float,
    new: Route,
    new_corridor: Sequence[CorridorSignal],
) -> tuple[Route, tuple[CorridorSignal, ...]]:
    """``old`` up to ``at_m``, then ``new`` (which starts at, or snapped next to, that
    point). The vehicle's ``along_m`` (behind ``at_m``) stays valid on the result; the
    junctions up to ``at_m`` keep their corridor keys, the new ones are numbered after
    them (a junction crossed twice gets the next pass number)."""
    at_m = min(max(0.0, at_m), old.length_m)
    i = max(0, bisect.bisect_right(old.cumulative, at_m) - 1)
    pts = list(old.points[: i + 1])
    speeds = list(old.segment_speeds_mps[:i])
    if at_m > old.cumulative[i] and i < len(old.segment_speeds_mps):
        p, _ = interpolate_by_distance(old.points, old.cumulative, at_m)
        pts.append(p)
        speeds.append(old.segment_speeds_mps[i])
    join = new.segment_speeds_mps[0] if new.segment_speeds_mps else speeds[-1]
    pts.append(new.points[0])
    speeds.append(join)
    pts.extend(new.points[1:])
    speeds.extend(new.segment_speeds_mps)
    route = Route.build(pts, speeds)
    offset = route.length_m - new.length_m
    kept = [c for c in old_corridor if c.along_m <= at_m + 1e-6]
    passes = Counter(c.signal_id for c in kept)
    out = list(kept)
    for c in sorted(new_corridor, key=lambda c: (c.along_m, c.signal_id, c.pass_no)):
        out.append(_shift(c, offset, passes[c.signal_id]))
        passes[c.signal_id] += 1
    if not math.isfinite(offset) or offset < -1e-6:
        raise ValueError("splice produced a route shorter than its new part")
    return route, tuple(out)
