"""Reference data: cities, stations (with live unit availability), signals."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from divert.api.deps import CityDep, CtxDep, SessionDep
from divert.db.repositories.reference import ReferenceRepository
from divert.domain.models import City

router = APIRouter(tags=["cities"])


def city_summary(c: City) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "timezone": c.timezone,
        "center": [c.center.lat, c.center.lng],
        "bbox": [c.bbox.south, c.bbox.west, c.bbox.north, c.bbox.east],
        "zoom": c.zoom,
    }


@router.get("/cities", summary="Cities served")
async def list_cities(ctx: CtxDep) -> dict[str, Any]:
    cities = [c for c in await ctx.reference.cities() if c.id in ctx.settings.cities]
    return {"cities": [city_summary(c) for c in cities]}


@router.get("/cities/{city}", summary="City detail: stations with free units, signals")
async def get_city(city: CityDep, ctx: CtxDep, session: SessionDep) -> dict[str, Any]:
    ref = await ctx.reference.reference(city.id)
    assert ref is not None
    availability = await ReferenceRepository(session).station_availability(city.id)
    return {
        **city_summary(city),
        "traffic": {
            "hourly_congestion": list(city.traffic.hourly_congestion),
            "avg_junction_delay_s": city.traffic.avg_junction_delay_s,
        },
        "stations": [
            {
                "id": a.station.id,
                "name": a.station.name,
                "service": a.station.service.value,
                "location": [a.station.pos.lat, a.station.pos.lng],
                "osm": a.station.osm_ref,
                "total_units": a.total_units,
                "free_units": a.free_units,
            }
            for a in availability
        ],
        "signals": [
            {
                "id": s.id,
                "name": s.name,
                "location": [s.pos.lat, s.pos.lng],
                "osm_node_ids": list(s.osm_node_ids),
            }
            for s in ref.signals
        ],
    }
