"""Analytics read model (all aggregation happens in PostgreSQL).

Response time of an incident = (dispatch time - report time) + simulated travel time
of its dispatch to the scene (``dispatches.actual_travel_s``, in simulated seconds), so
it is meaningful at any DIVERT_SIM_TIME_SCALE. Percentiles use ``percentile_cont``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from divert.db.models import Dispatch, DispatchLeg, Incident, IncidentEvent, SignalPreemption
from divert.db.models import Station as StationRow
from divert.domain.enums import (
    DispatchStatus,
    IncidentStatus,
    IncidentType,
    LegEndReason,
    LegKind,
    PhaseStateAtClaim,
    PreemptOutcome,
    SignalState,
)

FREE_LEFT_PATH: Final = "$[*] ? (@.free_left == true)"  # corridor entries that are free lefts


def _contested() -> sa.ColumnElement[bool]:
    """A claim queued behind another phase at least once (served later or not)."""
    return sa.or_(SignalPreemption.conflicts > 0, SignalPreemption.state == SignalState.CONFLICT)


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
                sa.func.count().filter(_contested()),
                sa.func.count(sa.distinct(SignalPreemption.signal_id)),
                sa.func.count().filter(
                    _contested(), SignalPreemption.state == SignalState.PREEMPTED
                ),
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

    legs = await _leg_metrics(session, in_window)
    outcomes = await session.execute(
        sa.select(SignalPreemption.outcome, sa.func.count())
        .join(Dispatch, Dispatch.id == SignalPreemption.dispatch_id)
        .join(Incident, Incident.id == Dispatch.incident_id)
        .where(in_window, SignalPreemption.outcome.is_not(None))
        .group_by(SignalPreemption.outcome)
    )
    by_outcome: dict[str, int] = {o.value: 0 for o in PreemptOutcome}
    for o, n in outcomes:
        by_outcome[str(o)] = n
    phase_rows = await session.execute(
        sa.select(SignalPreemption.phase_state_at_claim, sa.func.count())
        .join(Dispatch, Dispatch.id == SignalPreemption.dispatch_id)
        .join(Incident, Incident.id == Dispatch.incident_id)
        .where(in_window, SignalPreemption.phase_state_at_claim.is_not(None))
        .group_by(SignalPreemption.phase_state_at_claim)
    )
    by_phase: dict[str, int] = {p.value: 0 for p in PhaseStateAtClaim}
    for p, n in phase_rows:
        by_phase[str(p)] = n
    status_rows = await session.execute(
        sa.select(Dispatch.status, sa.func.count())
        .join(Incident, Incident.id == Dispatch.incident_id)
        .where(in_window)
        .group_by(Dispatch.status)
    )
    by_dispatch_status: dict[str, int] = {s.value: 0 for s in DispatchStatus}
    for ds, n in status_rows:
        by_dispatch_status[DispatchStatus(ds).value] = n

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
            "by_status": by_dispatch_status,
        },
        **legs,
        "preemptions": {
            "total": pre[0],
            "conflicts": pre[1],  # contested claims, whether served later or not
            "conflicts_served": pre[3],  # contested claims whose phase was served later
            "distinct_signals": pre[2],
            "top_signals": [{"signal_id": s, "count": n} for s, n in top],
            "outcomes": by_outcome,
            "green_extension": by_phase[PhaseStateAtClaim.GREEN_EXTEND.value],
            "red_truncation": by_phase[PhaseStateAtClaim.RED_TRUNCATE.value],
        },
        "est_time_saved_s": {
            "total": _f(disp[2]),
            "note": "estimate: pre-empted junctions passed x city average junction delay",
        },
    }


def _stats(n: Any, p50: Any, p90: Any, avg: Any) -> dict[str, Any]:
    return {"count": int(n or 0), "p50_s": _f(p50), "p90_s": _f(p90), "mean_s": _f(avg)}


async def _distribution(session: AsyncSession, value: Any, *where: Any) -> dict[str, Any]:
    """count / p50 / p90 / mean of ``value`` over the rows selected by ``where``."""
    row = (
        await session.execute(
            sa.select(
                sa.func.count(value),
                sa.func.percentile_cont(0.5).within_group(value),
                sa.func.percentile_cont(0.9).within_group(value),
                sa.func.avg(value),
            ).where(*where)
        )
    ).one()
    return _stats(*row)


def _trip_s_of(event_type: str) -> Any:
    """(dispatch id, simulated trip seconds) of every ``event_type`` timeline event."""
    return (
        sa.select(
            IncidentEvent.dispatch_id.label("did"),
            IncidentEvent.payload["trip_s"].as_float().label("trip_s"),
        )
        .where(IncidentEvent.type == event_type)
        .subquery()
    )


async def _leg_metrics(session: AsyncSession, in_window: Any) -> dict[str, Any]:
    """Trip-leg analytics (design §5), all in simulated seconds:

    * transport_time_s - the TRANSPORT leg's travel time;
    * call_to_hospital_s - report -> dispatch (wall) + sim time from dispatch to arrival
      at the hospital (``ended_trip_s`` of the TRANSPORT leg);
    * handover_to_back_in_service_s - handover complete -> back in service (events);
    * hospitals - where MEDICAL patients were taken;
    * legs - predicted vs actual per leg kind (count, mean absolute error share);
    * free_left_passed - free-left junction passes on driven legs (never claimed);
    * reroutes - route_replaced applied.
    """
    base = sa.and_(in_window, Dispatch.incident_id == Incident.id)
    leg_of = sa.and_(base, DispatchLeg.dispatch_id == Dispatch.id)
    arrived = DispatchLeg.end_reason == LegEndReason.ARRIVED
    transport = sa.and_(leg_of, DispatchLeg.kind == LegKind.TRANSPORT, arrived)
    transport_time = await _distribution(session, DispatchLeg.actual_travel_s, transport)
    call_to_hospital = await _distribution(
        session,
        sa.extract("epoch", Dispatch.dispatched_at - Incident.reported_at)
        + DispatchLeg.ended_trip_s,
        transport,
    )
    handover, back_ev = (_trip_s_of(name) for name in ("handover_complete", "back_in_service"))
    back = await _distribution(  # (also when no RETURN leg was needed: home hospital)
        session,
        back_ev.c.trip_s - handover.c.trip_s,
        base,
        handover.c.did == Dispatch.id,
        back_ev.c.did == Dispatch.id,
    )
    hospitals = await session.execute(
        sa.select(Incident.hospital_station_id, StationRow.name, sa.func.count())
        .join(StationRow, StationRow.id == Incident.hospital_station_id)
        .where(in_window)
        .group_by(Incident.hospital_station_id, StationRow.name)
        .order_by(sa.func.count().desc(), Incident.hospital_station_id)
        .limit(20)
    )
    accuracy = await session.execute(
        sa.select(
            DispatchLeg.kind,
            sa.func.count(),
            sa.func.avg(
                sa.func.abs(DispatchLeg.actual_travel_s - DispatchLeg.predicted_eta_s)
                / sa.func.nullif(DispatchLeg.actual_travel_s, 0)
            ),
            sa.func.max(
                sa.func.abs(DispatchLeg.actual_travel_s - DispatchLeg.predicted_eta_s)
                / sa.func.nullif(DispatchLeg.actual_travel_s, 0)
            ),
        )
        .where(leg_of, arrived, DispatchLeg.reroutes == 0)
        .group_by(DispatchLeg.kind)
    )
    by_kind: dict[str, dict[str, Any]] = {
        k.value: {"count": 0, "eta_mape": None, "eta_max_error": None} for k in LegKind
    }
    for kind, n, mape, worst in accuracy:
        by_kind[LegKind(kind).value] = {
            "count": n,
            "eta_mape": _f(mape, 4),
            "eta_max_error": _f(worst, 4),
        }
    free_lefts = sa.func.jsonb_array_length(  # free-left passes of the leg's corridor
        sa.func.jsonb_path_query_array(
            DispatchLeg.corridor, sa.text(f"'{FREE_LEFT_PATH}'::jsonpath")
        )
    )
    free_left = await session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(free_lefts), 0)).where(
            leg_of, DispatchLeg.started_at.is_not(None)
        )
    )
    reroutes = await session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(DispatchLeg.reroutes), 0)).where(leg_of)
    )
    return {
        "transport_time_s": transport_time,
        "call_to_hospital_s": call_to_hospital,
        "handover_to_back_in_service_s": back,
        "hospitals": [{"station_id": sid, "name": name, "count": n} for sid, name, n in hospitals],
        "legs": by_kind,
        "free_left_passed": int(free_left or 0),
        "reroutes": int(reroutes or 0),
    }
