"""Incident history (PostgreSQL): list with keyset pagination, detail, track.

HTTP only: parsing and error mapping. Queries live in ``services/history.py``."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from divert.api.deps import CityDep, SessionDep
from divert.api.errors import ApiError
from divert.domain.enums import IncidentStatus, IncidentType, normalize_incident_type
from divert.services.history import HistoryQueryError, HistoryService, IncidentFilter

router = APIRouter(tags=["history"])


def _csv(values: list[str] | None) -> list[str]:
    return [p.strip() for v in values or [] for p in v.split(",") if p.strip()]


def parse_statuses(values: list[str] | None) -> list[IncidentStatus]:
    try:
        return [IncidentStatus(v.upper()) for v in _csv(values)]
    except ValueError:
        raise ApiError(
            422, "validation_error", "unknown status", {"valid": [s.value for s in IncidentStatus]}
        ) from None


def parse_types(values: list[str] | None) -> list[IncidentType]:
    try:
        return sorted({normalize_incident_type(v) for v in _csv(values)})
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from None


@router.get("/cities/{city}/incidents", summary="Incident history (newest first)")
async def list_incidents(
    *,
    city: CityDep,
    session: SessionDep,
    status: Annotated[
        list[str] | None, Query(description="filter; repeat or comma-separate")
    ] = None,
    type: Annotated[list[str] | None, Query(description="filter; aliases accepted")] = None,
    from_: Annotated[
        datetime | None, Query(alias="from", description="reported_at >= (ISO 8601)")
    ] = None,
    to: Annotated[datetime | None, Query(description="reported_at < (ISO 8601)")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, Any]:
    query = IncidentFilter(
        statuses=parse_statuses(status),
        types=parse_types(type),
        start=from_,
        end=to,
        limit=limit,
        cursor=cursor,
    )
    try:
        return await HistoryService(session).list_incidents(city.id, query)
    except HistoryQueryError as exc:
        raise ApiError(422, "validation_error", str(exc)) from None


@router.get(
    "/incidents/{incident_id}", summary="Incident + dispatch + route + preemptions + timeline"
)
async def get_incident(session: SessionDep, incident_id: uuid.UUID = Path()) -> dict[str, Any]:
    detail = await HistoryService(session).incident_detail(incident_id)
    if detail is None:
        raise ApiError(404, "not_found", f"incident {incident_id} not found")
    return detail


@router.get("/incidents/{incident_id}/track", summary="Sampled vehicle positions")
async def get_track(
    session: SessionDep,
    incident_id: uuid.UUID = Path(),
    max_points: Annotated[int, Query(ge=2, le=5000)] = 500,
) -> dict[str, Any]:
    track = await HistoryService(session).track(incident_id, max_points)
    if track is None:
        raise ApiError(404, "not_found", f"incident {incident_id} not found")
    return track
