"""PostgreSQL 16 + PostGIS 3.4 schema (system of record).

Reference data: cities, stations, units, signals.
History: incidents, dispatches, signal_preemptions, incident_events, vehicle_positions.
Delivery: outbox (transactional outbox for worker commands).

Spatial columns are ``geography(..., 4326)`` so distances/buffers are in metres; every
spatial column has an explicit GiST index (declared here and in the migration).
Keep this file and ``migrations/versions`` in sync: an integration test diffs them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import Geography
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from divert.db.base import Base, str_enum, utc_now_column
from divert.domain.enums import (
    DestinationKind,
    DispatchStatus,
    IncidentStatus,
    IncidentType,
    Lane,
    LegEndReason,
    LegKind,
    Movement,
    PhaseStateAtClaim,
    PreemptOutcome,
    ServiceType,
    SignalState,
    UnitStatus,
)

POINT = Geography(geometry_type="POINT", srid=4326, spatial_index=False)
LINESTRING = Geography(geometry_type="LINESTRING", srid=4326, spatial_index=False)
TSTZ = sa.DateTime(timezone=True)

# --------------------------------------------------------------------------- reference


class City(Base):
    __tablename__ = "cities"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(100))
    timezone: Mapped[str] = mapped_column(sa.String(64))
    center: Mapped[Any] = mapped_column(POINT)
    bbox_south: Mapped[float] = mapped_column(sa.Double)
    bbox_west: Mapped[float] = mapped_column(sa.Double)
    bbox_north: Mapped[float] = mapped_column(sa.Double)
    bbox_east: Mapped[float] = mapped_column(sa.Double)
    zoom: Mapped[int] = mapped_column(sa.SmallInteger)
    hourly_congestion: Mapped[list[float]] = mapped_column(ARRAY(sa.Double))
    avg_junction_delay_s: Mapped[float] = mapped_column(sa.Double)
    data_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    created_at: Mapped[datetime] = utc_now_column()
    updated_at: Mapped[datetime] = utc_now_column(onupdate=True)

    __table_args__ = (
        sa.CheckConstraint("array_length(hourly_congestion, 1) = 24", name="hourly_24"),
        sa.CheckConstraint("bbox_south < bbox_north AND bbox_west < bbox_east", name="bbox"),
        sa.CheckConstraint("avg_junction_delay_s >= 0", name="junction_delay"),
    )


class Station(Base):
    __tablename__ = "stations"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    city_id: Mapped[str] = mapped_column(sa.ForeignKey("cities.id", ondelete="RESTRICT"))
    service: Mapped[ServiceType] = mapped_column(str_enum(ServiceType, "service"))
    name: Mapped[str | None] = mapped_column(sa.String(255))
    osm_type: Mapped[str] = mapped_column(sa.String(8))
    osm_id: Mapped[int] = mapped_column(sa.BigInteger)
    geom: Mapped[Any] = mapped_column(POINT)
    active: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())
    # hospitals (AMBULANCE bases) only: OSM emergency=yes|no (NULL = not tagged) and the
    # healthcare=* tag - hospital selection skips emergency=no and non-acute facilities
    emergency: Mapped[str | None] = mapped_column(sa.String(3))
    healthcare: Mapped[str | None] = mapped_column(sa.String(32))
    # hospitals only: OSM healthcare:speciality (raw, ';'-separated) - TRANSPORT skips
    # hospitals offering only specialities that cannot take an emergency patient
    speciality: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = utc_now_column()
    updated_at: Mapped[datetime] = utc_now_column(onupdate=True)

    __table_args__ = (
        sa.UniqueConstraint("osm_type", "osm_id"),
        sa.CheckConstraint("osm_type IN ('node', 'way', 'relation')", name="osm_type"),
        sa.CheckConstraint("emergency IN ('yes', 'no')", name="emergency"),
        sa.Index("ix_stations_geom", "geom", postgresql_using="gist"),
        sa.Index("ix_stations_city_service", "city_id", "service"),
    )


class Unit(Base):
    """One row per vehicle. ``current_dispatch_id`` is set iff the unit is busy."""

    __tablename__ = "units"

    id: Mapped[str] = mapped_column(sa.String(48), primary_key=True)  # callsign
    station_id: Mapped[str] = mapped_column(sa.ForeignKey("stations.id", ondelete="RESTRICT"))
    city_id: Mapped[str] = mapped_column(sa.ForeignKey("cities.id", ondelete="RESTRICT"))
    service: Mapped[ServiceType] = mapped_column(str_enum(ServiceType, "service"))
    ordinal: Mapped[int] = mapped_column(sa.SmallInteger)
    status: Mapped[UnitStatus] = mapped_column(
        str_enum(UnitStatus, "status"), server_default=UnitStatus.AVAILABLE.value
    )
    current_dispatch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey(
            "dispatches.id",
            ondelete="SET NULL",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    status_changed_at: Mapped[datetime] = utc_now_column()
    updated_at: Mapped[datetime] = utc_now_column(onupdate=True)

    __table_args__ = (
        sa.UniqueConstraint("station_id", "ordinal"),
        sa.CheckConstraint("ordinal >= 1", name="ordinal"),
        sa.CheckConstraint(
            "(status IN ('DISPATCHED', 'ON_SCENE', 'TRANSPORTING', 'AT_HOSPITAL', 'RETURNING'))"
            " = (current_dispatch_id IS NOT NULL)",
            name="busy_has_dispatch",
        ),
        sa.Index(
            "ix_units_free_by_station",
            "station_id",
            postgresql_where=sa.text("status = 'AVAILABLE'"),
        ),
        sa.Index("ix_units_city_status", "city_id", "status"),
    )


class Signal(Base):
    """A signalised junction (OSM traffic_signals nodes clustered within 40 m)."""

    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    city_id: Mapped[str] = mapped_column(sa.ForeignKey("cities.id", ondelete="RESTRICT"))
    name: Mapped[str | None] = mapped_column(sa.String(255))
    geom: Mapped[Any] = mapped_column(POINT)
    osm_node_ids: Mapped[list[int]] = mapped_column(ARRAY(sa.BigInteger))
    active: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())
    created_at: Mapped[datetime] = utc_now_column()
    updated_at: Mapped[datetime] = utc_now_column(onupdate=True)

    __table_args__ = (
        sa.Index("ix_signals_geom", "geom", postgresql_using="gist"),
        sa.Index("ix_signals_city_id", "city_id"),
    )


# ----------------------------------------------------------------------------- history


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    city_id: Mapped[str] = mapped_column(sa.ForeignKey("cities.id", ondelete="RESTRICT"))
    type: Mapped[IncidentType] = mapped_column(str_enum(IncidentType, "type"))
    reported_type: Mapped[str] = mapped_column(sa.String(32))  # raw input, e.g. "POLICE"
    geom: Mapped[Any] = mapped_column(POINT)
    description: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[IncidentStatus] = mapped_column(
        str_enum(IncidentStatus, "status"), server_default=IncidentStatus.OPEN.value
    )
    reported_at: Mapped[datetime] = utc_now_column()
    dispatched_at: Mapped[datetime | None] = mapped_column(TSTZ)
    en_route_at: Mapped[datetime | None] = mapped_column(TSTZ)
    on_scene_at: Mapped[datetime | None] = mapped_column(TSTZ)
    resolved_at: Mapped[datetime | None] = mapped_column(TSTZ)
    cancelled_at: Mapped[datetime | None] = mapped_column(TSTZ)
    failed_at: Mapped[datetime | None] = mapped_column(TSTZ)
    failure_reason: Mapped[str | None] = mapped_column(sa.Text)
    # MEDICAL: the hospital the patient was taken to (design §1.1) and when
    hospital_station_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("stations.id", ondelete="RESTRICT")
    )
    transport_started_at: Mapped[datetime | None] = mapped_column(TSTZ)
    at_hospital_at: Mapped[datetime | None] = mapped_column(TSTZ)
    updated_at: Mapped[datetime] = utc_now_column(onupdate=True)

    __table_args__ = (
        # keyset pagination: WHERE city_id = :c AND (reported_at, id) < (:t, :id)
        sa.Index(
            "ix_incidents_city_reported", "city_id", sa.text("reported_at DESC"), sa.text("id DESC")
        ),
        sa.Index("ix_incidents_city_status", "city_id", "status"),
        sa.Index("ix_incidents_geom", "geom", postgresql_using="gist"),
    )


class Dispatch(Base):
    """One vehicle sent to one incident: the whole trip (design §5) until the unit is back
    in service. ``route`` / ``predicted_eta_s`` / ``corridor`` / ``actual_travel_s`` are
    the RESPONSE leg (kept for compatibility); every leg is in ``dispatch_legs``."""

    __tablename__ = "dispatches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE")
    )
    unit_id: Mapped[str] = mapped_column(sa.ForeignKey("units.id", ondelete="RESTRICT"))
    station_id: Mapped[str] = mapped_column(sa.ForeignKey("stations.id", ondelete="RESTRICT"))
    city_id: Mapped[str] = mapped_column(sa.ForeignKey("cities.id", ondelete="RESTRICT"))
    service: Mapped[ServiceType] = mapped_column(str_enum(ServiceType, "service"))
    status: Mapped[DispatchStatus] = mapped_column(
        str_enum(DispatchStatus, "status"), server_default=DispatchStatus.DISPATCHED.value
    )
    route: Mapped[Any] = mapped_column(LINESTRING)
    distance_m: Mapped[float] = mapped_column(sa.Double)
    predicted_eta_s: Mapped[float] = mapped_column(sa.Double)
    actual_travel_s: Mapped[float | None] = mapped_column(sa.Double)
    est_time_saved_s: Mapped[float | None] = mapped_column(sa.Double)
    corridor: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=sa.text("'[]'"))
    # OSRM free-flow speed per route segment (lets the worker rebuild a vehicle from PG)
    segment_speeds_mps: Mapped[list[float]] = mapped_column(
        ARRAY(sa.Double), server_default=sa.text("'{}'")
    )
    eta_model: Mapped[str | None] = mapped_column(sa.String(32))
    dispatched_at: Mapped[datetime] = utc_now_column()
    arrived_at: Mapped[datetime | None] = mapped_column(TSTZ)
    ended_at: Mapped[datetime | None] = mapped_column(TSTZ)  # back in service / failed
    end_reason: Mapped[str | None] = mapped_column(sa.String(16))

    __table_args__ = (
        sa.CheckConstraint("distance_m >= 0 AND predicted_eta_s >= 0", name="non_negative"),
        sa.CheckConstraint(
            "end_reason IS NULL OR end_reason IN ('CLEARED', 'CANCELLED', 'FAILED')",
            name="end_reason",
        ),
        sa.Index("ix_dispatches_incident_id", "incident_id"),
        sa.Index("ix_dispatches_route", "route", postgresql_using="gist"),
        # a unit can be on at most one active dispatch (belt and braces for SKIP LOCKED)
        sa.Index(
            "uq_dispatches_active_unit",
            "unit_id",
            unique=True,
            postgresql_where=sa.text("ended_at IS NULL"),
        ),
        # worker rebuild + live queries: active dispatches per city
        sa.Index(
            "ix_dispatches_active_city", "city_id", postgresql_where=sa.text("ended_at IS NULL")
        ),
        # analytics windows per city
        sa.Index("ix_dispatches_city_dispatched", "city_id", "dispatched_at"),
    )


class DispatchLeg(Base):
    """One leg of a dispatch (design §1, §5): RESPONSE (seq 0, written with the dispatch),
    then TRANSPORT and/or RETURN, written by the LegPlanner together with the outbox row
    that carries the ``leg_planned`` command. Times: ``started_trip_s`` / ``ended_trip_s``
    are simulated seconds since the dispatch started (wall timestamps run slower than the
    simulation when DIVERT_SIM_TIME_SCALE > 1)."""

    __tablename__ = "dispatch_legs"

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    dispatch_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("dispatches.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(sa.SmallInteger)
    kind: Mapped[LegKind] = mapped_column(str_enum(LegKind, "kind"))
    emergency: Mapped[bool] = mapped_column(sa.Boolean)
    origin: Mapped[Any] = mapped_column(POINT)
    destination: Mapped[Any] = mapped_column(POINT)
    destination_kind: Mapped[DestinationKind] = mapped_column(
        str_enum(DestinationKind, "destination_kind")
    )
    destination_station_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("stations.id", ondelete="RESTRICT")
    )
    route: Mapped[Any] = mapped_column(LINESTRING)
    distance_m: Mapped[float] = mapped_column(sa.Double)
    predicted_eta_s: Mapped[float] = mapped_column(sa.Double)
    segment_speeds_mps: Mapped[list[float]] = mapped_column(
        ARRAY(sa.Double), server_default=sa.text("'{}'")
    )
    corridor: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=sa.text("'[]'"))
    route_choice: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    hospital_choice: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    eta_model: Mapped[str | None] = mapped_column(sa.String(32))
    planned_at: Mapped[datetime] = mapped_column(TSTZ)
    started_at: Mapped[datetime | None] = mapped_column(TSTZ)
    ended_at: Mapped[datetime | None] = mapped_column(TSTZ)
    actual_travel_s: Mapped[float | None] = mapped_column(sa.Double)
    end_reason: Mapped[LegEndReason | None] = mapped_column(str_enum(LegEndReason, "end_reason"))
    reroutes: Mapped[int] = mapped_column(sa.SmallInteger, server_default=sa.text("0"))
    started_trip_s: Mapped[float | None] = mapped_column(sa.Double)
    ended_trip_s: Mapped[float | None] = mapped_column(sa.Double)

    __table_args__ = (
        sa.UniqueConstraint("dispatch_id", "seq"),
        sa.CheckConstraint("seq >= 0", name="seq"),
        sa.CheckConstraint("distance_m >= 0 AND predicted_eta_s >= 0", name="non_negative"),
        sa.Index("ix_dispatch_legs_route", "route", postgresql_using="gist"),
        sa.Index("ix_dispatch_legs_destination_station", "destination_station_id"),
    )


class SignalPreemption(Base):
    """One claim of one junction pass by one vehicle (design §4.6)."""

    __tablename__ = "signal_preemptions"

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    event_id: Mapped[str] = mapped_column(sa.String(64), unique=True)
    dispatch_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("dispatches.id", ondelete="CASCADE")
    )
    signal_id: Mapped[str] = mapped_column(sa.ForeignKey("signals.id", ondelete="RESTRICT"))
    city_id: Mapped[str] = mapped_column(sa.ForeignKey("cities.id", ondelete="RESTRICT"))
    state: Mapped[SignalState] = mapped_column(str_enum(SignalState, "state"))
    lane: Mapped[Lane | None] = mapped_column(str_enum(Lane, "lane", length=1))
    approach_bearing: Mapped[float | None] = mapped_column(sa.Double)
    eta_at_claim_s: Mapped[float | None] = mapped_column(sa.Double)
    claimed_at: Mapped[datetime] = mapped_column(TSTZ)
    released_at: Mapped[datetime | None] = mapped_column(TSTZ)
    leg_seq: Mapped[int] = mapped_column(sa.SmallInteger, server_default=sa.text("0"))
    movement: Mapped[Movement | None] = mapped_column(str_enum(Movement, "movement"))
    lane_index: Mapped[int | None] = mapped_column(sa.SmallInteger)
    lanes_total: Mapped[int | None] = mapped_column(sa.SmallInteger)
    free_left: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    required_lead_s: Mapped[float | None] = mapped_column(sa.Double)
    transition_s: Mapped[float | None] = mapped_column(sa.Double)
    queue_clear_s: Mapped[float | None] = mapped_column(sa.Double)
    green_at: Mapped[datetime | None] = mapped_column(TSTZ)
    crossed_at: Mapped[datetime | None] = mapped_column(TSTZ)
    waited_s: Mapped[float | None] = mapped_column(sa.Double)
    outcome: Mapped[PreemptOutcome | None] = mapped_column(str_enum(PreemptOutcome, "outcome"))
    phase_state_at_claim: Mapped[PhaseStateAtClaim | None] = mapped_column(
        str_enum(PhaseStateAtClaim, "phase_state_at_claim")
    )
    # times the claim was queued behind another phase: ``state`` is the final state (a
    # queued claim that was served ends PREEMPTED), this keeps that it was contested
    conflicts: Mapped[int] = mapped_column(sa.SmallInteger, server_default=sa.text("0"))

    __table_args__ = (
        sa.CheckConstraint("state <> 'NORMAL'", name="not_normal"),
        sa.CheckConstraint("conflicts >= 0", name="conflicts"),
        sa.Index("ix_signal_preemptions_dispatch_id", "dispatch_id"),
        sa.Index("ix_signal_preemptions_signal_claimed", "signal_id", "claimed_at"),
        sa.Index("ix_signal_preemptions_city_claimed", "city_id", "claimed_at"),
    )


class IncidentEvent(Base):
    """Append-only timeline / audit log; ``event_id`` makes persisting idempotent."""

    __tablename__ = "incident_events"

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    event_id: Mapped[str] = mapped_column(sa.String(64), unique=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="CASCADE")
    )
    dispatch_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("dispatches.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(sa.String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sa.text("'{}'"))
    ts: Mapped[datetime] = mapped_column(TSTZ)
    recorded_at: Mapped[datetime] = utc_now_column()

    __table_args__ = (sa.Index("ix_incident_events_incident_ts", "incident_id", "ts"),)


class VehiclePosition(Base):
    """Sampled track (every DIVERT_POSITION_SAMPLE_S sim seconds); retention-pruned."""

    __tablename__ = "vehicle_positions"

    dispatch_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("dispatches.id", ondelete="CASCADE"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(TSTZ, primary_key=True)
    geom: Mapped[Any] = mapped_column(POINT)
    speed_mps: Mapped[float] = mapped_column(sa.Double)
    along_m: Mapped[float] = mapped_column(sa.Double)
    # simulated seconds since the LEG started (ts is wall clock; sim time can run faster
    # and is frozen while no worker leads) - the worker rebuilds elapsed time from this
    elapsed_s: Mapped[float | None] = mapped_column(sa.Double)
    leg_seq: Mapped[int] = mapped_column(sa.SmallInteger, server_default=sa.text("0"))

    __table_args__ = (sa.Index("ix_vehicle_positions_ts_brin", "ts", postgresql_using="brin"),)


# ---------------------------------------------------------------------------- delivery


class Outbox(Base):
    """Transactional outbox: written in the dispatch transaction, relayed to Redis."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    topic: Mapped[str] = mapped_column(sa.String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = utc_now_column()
    sent_at: Mapped[datetime | None] = mapped_column(TSTZ)
    attempts: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    last_error: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.Index("ix_outbox_unsent", "id", postgresql_where=sa.text("sent_at IS NULL")),
        # retention: delete relayed rows older than DIVERT_OUTBOX_RETENTION_DAYS
        sa.Index("ix_outbox_sent_at", "sent_at", postgresql_where=sa.text("sent_at IS NOT NULL")),
    )
