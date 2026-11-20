"""Incident history and analytics - the read side over PostgreSQL.

Who writes the history tables (by design, there is no single "history writer" process):

* the API's dispatch transaction (``services/dispatch.py``): incident, dispatch, the first
  timeline events and the outbox row - atomically with locking the unit;
* the persister (``persister.py`` -> ``db/repositories/history.HistoryWriter``): every
  event the worker reports on ``divert:history`` (positions, preemptions, arrival,
  clearance, failures), idempotently by ``event_id``.

This module is what the history / analytics API uses: filtering, keyset pagination,
incident detail + timeline, sampled tracks and the per-city summary, with the query
validation that belongs to the domain rather than to HTTP.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from divert.db.repositories import analytics
from divert.db.repositories.incidents import IncidentRepository
from divert.domain.enums import IncidentStatus, IncidentType

MAX_ANALYTICS_RANGE: Final = timedelta(days=366)
DEFAULT_ANALYTICS_RANGE: Final = timedelta(days=1)


class HistoryQueryError(ValueError):
    """The query itself is invalid (bad cursor, inverted or oversized range) -> 422."""


def as_utc(ts: datetime | None) -> datetime | None:
    """Naive timestamps from query strings are taken as UTC."""
    if ts is None:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


@dataclass(frozen=True, slots=True)
class IncidentFilter:
    statuses: Sequence[IncidentStatus] = ()
    types: Sequence[IncidentType] = ()
    start: datetime | None = None  # reported_at >= start
    end: datetime | None = None  # reported_at < end
    limit: int = 50
    cursor: str | None = None


class HistoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.incidents = IncidentRepository(session)
        self.session = session

    async def list_incidents(self, city_id: str, f: IncidentFilter) -> dict[str, Any]:
        """Newest first; ``next_cursor`` continues the keyset (None on the last page)."""
        try:
            items, nxt = await self.incidents.list_incidents(
                city_id,
                statuses=f.statuses,
                types=f.types,
                start=as_utc(f.start),
                end=as_utc(f.end),
                limit=f.limit,
                cursor=f.cursor,
            )
        except ValueError as exc:  # undecodable cursor
            raise HistoryQueryError(str(exc)) from None
        return {"items": items, "next_cursor": nxt}

    async def incident_detail(self, incident_id: uuid.UUID) -> dict[str, Any] | None:
        """Incident + dispatch + route + preemptions + timeline (None if unknown)."""
        return await self.incidents.incident_detail(incident_id)

    async def track(self, incident_id: uuid.UUID, max_points: int) -> dict[str, Any] | None:
        """Sampled recorded positions (None if the incident is unknown)."""
        if await self.incidents.get_incident(incident_id) is None:
            return None
        points = await self.incidents.track(incident_id, max_points)
        return {"incident_id": str(incident_id), "points": points}

    async def analytics_summary(
        self,
        city_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Counts, response-time percentiles, preemptions and estimated time saved for
        incidents reported in [start, end) - default: the last 24 h."""
        stop = as_utc(end) or now or datetime.now(UTC)
        begin = as_utc(start) or stop - DEFAULT_ANALYTICS_RANGE
        if begin >= stop:
            raise HistoryQueryError("'from' must be before 'to'")
        if stop - begin > MAX_ANALYTICS_RANGE:
            raise HistoryQueryError(f"range is limited to {MAX_ANALYTICS_RANGE.days} days")
        return await analytics.summary(self.session, city_id, begin, stop)
