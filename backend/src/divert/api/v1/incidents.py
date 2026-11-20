"""Dispatch (create) and cancel incidents."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Path, status

from divert.api.deps import CityDep, CtxDep, require_api_key
from divert.api.ratelimit import rate_limit_mutations
from divert.domain.geo import LatLng
from divert.schemas.incidents import IncidentCreate

router = APIRouter(tags=["incidents"])
MUTATING = [Depends(require_api_key), Depends(rate_limit_mutations)]


@router.post(
    "/cities/{city}/incidents",
    status_code=status.HTTP_201_CREATED,
    dependencies=MUTATING,
    summary="Report an incident and dispatch the fastest free unit",
    responses={
        409: {"description": "no unit of the service is free"},
        422: {"description": "unknown type / outside the city / invalid body"},
        429: {"description": "rate limited"},
        503: {"description": "routing (OSRM) unavailable - nothing was written"},
    },
)
async def create_incident(body: IncidentCreate, city: CityDep, ctx: CtxDep) -> dict[str, Any]:
    return await ctx.dispatch.create_incident(
        city.id, body.type, LatLng(*body.location), body.description
    )


@router.delete(
    "/cities/{city}/incidents/{incident_id}",
    dependencies=MUTATING,
    summary="Cancel an incident (the worker recalls its vehicle, which drives back)",
    responses={
        404: {"description": "not found"},
        409: {
            "description": "cannot_cancel_transport (patient on board: TRANSPORTING / "
            "AT_HOSPITAL) or invalid_state (FAILED)"
        },
    },
)
async def cancel_incident(
    city: CityDep, ctx: CtxDep, incident_id: uuid.UUID = Path()
) -> dict[str, Any]:
    return {"incident": await ctx.dispatch.cancel_incident(city.id, incident_id)}
