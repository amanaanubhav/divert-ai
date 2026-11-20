"""Live vehicles (Redis, written only by the worker leader)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from divert.api.deps import CityDep, CtxDep
from divert.store.live import read_tick, read_vehicle_routes, read_vehicles

router = APIRouter(tags=["live"])

PUBLIC_FIELDS = (
    "id", "dispatch_id", "incident_id", "service", "station_id", "incident_type", "status",
    "pos", "heading", "speed_mps", "eta_s", "predicted_eta_s", "along_m", "distance_m",
    "elapsed_s", "started_at", "destination", "claims", "preempted_passed",
    "emergency", "leg_kind", "rank", "next_movement", "speed_advisory_mps",
    # trip legs (design §5)
    "phase", "leg", "destination_info", "planning", "plan_error", "incident_location", "home",
    "hospital",
    "dwell_s", "dwell_target_s", "trip_s", "reroutes",
)  # fmt: skip


def public_vehicle(v: dict[str, Any]) -> dict[str, Any]:
    return {k: v.get(k) for k in PUBLIC_FIELDS}


@router.get("/cities/{city}/vehicles", summary="Vehicles currently live in the city")
async def list_vehicles(
    city: CityDep,
    ctx: CtxDep,
    include_route: bool = Query(default=False, description="add route + corridor per vehicle"),
) -> dict[str, Any]:
    rds, guard = ctx.redis, ctx.guard  # RedisError -> 503 live_state_unavailable
    vehicles = [public_vehicle(v) for v in await guard.call(lambda: read_vehicles(rds, city.id))]
    if include_route:
        ids = [str(v["id"]) for v in vehicles]
        routes = await guard.call(lambda: read_vehicle_routes(rds, city.id, ids))
        for v in vehicles:
            doc = routes.get(str(v["id"]), {})
            v["route"] = doc.get("route", {}).get("points", [])
            v["corridor"] = doc.get("corridor", [])
    tick = await guard.call(lambda: read_tick(rds, city.id))
    return {
        "city": city.id,
        "tick": int(float(tick.get("tick", 0))),
        "sim_ts": tick.get("sim_ts"),
        "vehicles": vehicles,
    }
