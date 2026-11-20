"""Aggregates over the incident history."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query

from divert.api.deps import CityDep, SessionDep
from divert.api.errors import ApiError
from divert.services.history import HistoryQueryError, HistoryService

router = APIRouter(tags=["analytics"])


@router.get("/cities/{city}/analytics/summary", summary="Counts, response times, preemptions")
async def summary(
    city: CityDep,
    session: SessionDep,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
) -> dict[str, Any]:
    try:
        return await HistoryService(session).analytics_summary(city.id, from_, to)
    except HistoryQueryError as exc:
        raise ApiError(422, "validation_error", str(exc)) from None
