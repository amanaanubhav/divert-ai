"""Corridor: every pass of a route through a signalised junction, with the movement the
vehicle makes there and the lane advice for it (design §3). Pure.

The PostGIS query (``ST_DWithin`` on the GiST index) finds the candidate signals; here
each candidate is projected onto the polyline once per *pass* - a route that loops back
through the same junction gets two corridor entries (``pass_no`` 0 and 1), which the
worker claims separately (design §4.2 claim key ``(vehicle, junction, pass_no)``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from divert.domain.geo import LatLng, Projection, haversine_m, project_onto_polyline
from divert.domain.lanes import LaneInput, plan_lanes
from divert.domain.models import CorridorCandidate
from divert.domain.movement import approach_speed_mps, junction_movement
from divert.domain.route import CorridorSignal, Route

DEFAULT_CORRIDOR_M: Final = 30.0
# a second pass must be at least this far along the route from the first (assumption:
# shorter gaps are the same pass wiggling in and out of the corridor buffer)
MIN_REPASS_M: Final = 200.0


@dataclass(slots=True)
class _Run:
    best: Projection
    last_along: float


def project_passes(p: LatLng, route: Route, within_m: float) -> list[Projection]:
    """The closest point of each separate pass of the route within ``within_m`` of ``p``."""
    pts, cum = route.points, route.cumulative
    runs: list[_Run] = []
    current: _Run | None = None
    for i in range(len(pts) - 1):
        proj = project_onto_polyline(p, pts[i : i + 2], (0.0, cum[i + 1] - cum[i]))
        if proj.offset_m > within_m:
            current = None
            continue
        along = cum[i] + proj.along_m
        hit = Projection(proj.point, along, proj.offset_m, i, proj.fraction)
        if current is None and runs and along - runs[-1].last_along < MIN_REPASS_M:
            current = runs[-1]  # same pass, briefly outside the buffer
        if current is None:
            current = _Run(hit, along)
            runs.append(current)
        elif hit.offset_m < current.best.offset_m - 1e-9:
            current.best = hit
        current.last_along = along
    return [r.best for r in runs]


def build_corridor(
    route: Route,
    candidates: Sequence[CorridorCandidate],
    *,
    within_m: float = DEFAULT_CORRIDOR_M,
    emergency: bool = True,
) -> list[CorridorSignal]:
    """Corridor entries in route order, with movement and lane advice per pass."""
    found: list[tuple[CorridorCandidate, int, Projection]] = []
    for cand in candidates:
        passes = project_passes(cand.signal.pos, route, within_m)
        if not passes:  # PostGIS (spheroid) said within, our sphere disagrees by cm
            passes = [project_onto_polyline(cand.signal.pos, route.points, route.cumulative)]
        found.extend((cand, k, proj) for k, proj in enumerate(passes))
    found.sort(key=lambda f: (f[2].along_m, f[0].signal.id, f[1]))
    moves = [
        junction_movement(route, proj.along_m, cand.signal.pos, proj.offset_m)
        for cand, _, proj in found
    ]
    advice = plan_lanes(
        [
            LaneInput(
                along_m=proj.along_m,
                movement=mv.movement,
                approach_speed_mps=approach_speed_mps(route, proj.along_m),
                free_left=mv.free_left,
                roundabout_exit=mv.roundabout_exit,
                turn_deg=mv.turn_deg,
            )
            for (_, _, proj), mv in zip(found, moves, strict=True)
        ],
        emergency=emergency,
    )
    out: list[CorridorSignal] = []
    for (cand, k, proj), mv, adv in zip(found, moves, advice, strict=True):
        out.append(
            CorridorSignal(
                signal_id=cand.signal.id,
                name=cand.signal.name,
                pos=cand.signal.pos,
                along_m=proj.along_m,
                offset_m=haversine_m(cand.signal.pos, proj.point),
                lane=adv.side,
                approach_bearing=mv.approach_bearing,
                pass_no=k,
                movement=mv.movement,
                turn_deg=mv.turn_deg,
                exit_bearing=mv.exit_bearing,
                roundabout_exit=mv.roundabout_exit,
                free_left=mv.free_left,
                legs=mv.legs,
                junction_bearings=mv.bearings,
                advice=adv,
            )
        )
    return out
