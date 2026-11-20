"""Analytics read model (all aggregation happens in PostgreSQL).

Response time of an incident = (dispatch time - report time) + simulated travel time
of its dispatch to the scene (``dispatches.actual_travel_s``, in simulated seconds), so
it is meaningful at any DIVERT_SIM_TIME_SCALE. Percentiles use ``percentile_cont``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from divert.db.models import Dispatch, Incident, SignalPreemption
from divert.domain.enums import IncidentStatus, IncidentType, SignalState


def _f(x: Any, nd: int = 1) -> float | None:
    return None if x is None else round(float(x), nd)


async def summary(
    session: AsyncSession, city_id: str, start: datetime, end: datetime
) -> dict[str, Any]:
    in_window = sa.and_(
        Incident.city_id == city_id, Incident.reported_at >= start, Incident.reported_at < end
    )

    counts = await session.execute(
        sa.select(Incident.type, Incident.status, sa.func.count())
        .where(in_window)
        .group_by(Incident.type, Incident.status)
    )
    by_type: dict[str, int] = {t.value: 0 for t in IncidentType}
    by_status: dict[str, int] = {s.value: 0 for s in IncidentStatus}
    total = 0
    for t, s, n in counts:
        by_type[IncidentType(t).value] += n
        by_status[IncidentStatus(s).value] += n
        total += n

    rt = (
        sa.extract("epoch", Dispatch.dispatched_at - Incident.reported_at)
        + Dispatch.actual_travel_s
    ).label("rt")
    rt_rows = (
        sa.select(Incident.type.label("type"), rt)
        .join(Dispatch, Dispatch.incident_id == Incident.id)
        .where(in_window, Dispatch.actual_travel_s.is_not(None))
        .subquery()
    )
    resp = await session.execute(
        sa.select(
            rt_rows.c.type,
            sa.func.count(),
            sa.func.percentile_cont(0.5).within_group(rt_rows.c.rt),
            sa.func.percentile_cont(0.9).within_group(rt_rows.c.rt),
            sa.func.avg(rt_rows.c.rt),
        ).group_by(rt_rows.c.type)
    )
    response_time = {
        IncidentType(t).value: {"count": n, "p50_s": _f(p50), "p90_s": _f(p90), "mean_s": _f(avg)}
        for t, n, p50, p90, avg in resp
    }

    disp = (
        await session.execute(
            sa.select(
                sa.func.count(),
                sa.func.count(Dispatch.arrived_at),
                sa.func.coalesce(sa.func.sum(Dispatch.est_time_saved_s), 0.0),
                sa.func.avg(Dispatch.predicted_eta_s),
                sa.func.avg(Dispatch.actual_travel_s),
                sa.func.avg(
                    sa.func.abs(Dispatch.actual_travel_s - Dispatch.predicted_eta_s)
                    / sa.func.nullif(Dispatch.actual_travel_s, 0)
                ),
            )
            .join(Incident, Incident.id == Dispatch.incident_id)
            .where(in_window)
        )
    ).one()

    pre = (
        await session.execute(
            sa.select(
                sa.func.count(),
                sa.func.count().filter(SignalPreemption.state == SignalState.CONFLICT),
                sa.func.count(sa.distinct(SignalPreemption.signal_id)),
            )
            .join(Dispatch, Dispatch.id == SignalPreemption.dispatch_id)
            .join(Incident, Incident.id == Dispatch.incident_id)
            .where(in_window)
        )
    ).one()

    top = await session.execute(
        sa.select(SignalPreemption.signal_id, sa.func.count().label("n"))
        .join(Dispatch, Dispatch.id == SignalPreemption.dispatch_id)
        .join(Incident, Incident.id == Dispatch.incident_id)
        .where(in_window)
        .group_by(SignalPreemption.signal_id)
        .order_by(sa.text("n DESC"), SignalPreemption.signal_id)
        .limit(10)
    )

    return {
        "city": city_id,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "incidents": {"total": total, "by_type": by_type, "by_status": by_status},
        "response_time_s": response_time,
        "dispatches": {
            "total": disp[0],
            "arrived": disp[1],
            "avg_predicted_eta_s": _f(disp[3]),
            "avg_actual_travel_s": _f(disp[4]),
            "eta_mape": _f(disp[5], 4),
        },
        "preemptions": {
            "total": pre[0],
            "conflicts": pre[1],
            "distinct_signals": pre[2],
            "top_signals": [{"signal_id": s, "count": n} for s, n in top],
        },
        "est_time_saved_s": {
            "total": _f(disp[2]),
            "note": "estimate: pre-empted junctions passed x city average junction delay",
        },
    }
