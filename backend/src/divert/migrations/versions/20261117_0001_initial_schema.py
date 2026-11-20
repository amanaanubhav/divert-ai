"""Initial schema: reference data, incident history, outbox (PostGIS).

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POINT = Geography(geometry_type="POINT", srid=4326, spatial_index=False)
LINESTRING = Geography(geometry_type="LINESTRING", srid=4326, spatial_index=False)
TSTZ = sa.DateTime(timezone=True)

SERVICES = ("AMBULANCE", "FIRE_ENGINE", "POLICE")
UNIT_STATUSES = ("AVAILABLE", "DISPATCHED", "ON_SCENE", "OUT_OF_SERVICE")
INCIDENT_TYPES = ("MEDICAL", "FIRE", "CRIME")
INCIDENT_STATUSES = (
    "OPEN",
    "DISPATCHED",
    "EN_ROUTE",
    "ON_SCENE",
    "RESOLVED",
    "CANCELLED",
    "FAILED",
)
SIGNAL_STATES = ("NORMAL", "PREEMPTED", "CONFLICT")
LANES = ("L", "R")


def _enum(values: tuple[str, ...], name: str, length: int = 16) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    # ------------------------------------------------------------------ reference
    op.create_table(
        "cities",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("center", POINT, nullable=False),
        sa.Column("bbox_south", sa.Double(), nullable=False),
        sa.Column("bbox_west", sa.Double(), nullable=False),
        sa.Column("bbox_north", sa.Double(), nullable=False),
        sa.Column("bbox_east", sa.Double(), nullable=False),
        sa.Column("zoom", sa.SmallInteger(), nullable=False),
        sa.Column("hourly_congestion", pg.ARRAY(sa.Double()), nullable=False),
        sa.Column("avg_junction_delay_s", sa.Double(), nullable=False),
        sa.Column("data_meta", pg.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", TSTZ, nullable=False, server_default=_now()),
        sa.Column("updated_at", TSTZ, nullable=False, server_default=_now()),
        sa.CheckConstraint("array_length(hourly_congestion, 1) = 24", name="hourly_24"),
        sa.CheckConstraint("bbox_south < bbox_north AND bbox_west < bbox_east", name="bbox"),
        sa.CheckConstraint("avg_junction_delay_s >= 0", name="junction_delay"),
    )

    op.create_table(
        "stations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "city_id",
            sa.String(32),
            sa.ForeignKey("cities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("service", _enum(SERVICES, "service"), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("osm_type", sa.String(8), nullable=False),
        sa.Column("osm_id", sa.BigInteger(), nullable=False),
        sa.Column("geom", POINT, nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", TSTZ, nullable=False, server_default=_now()),
        sa.Column("updated_at", TSTZ, nullable=False, server_default=_now()),
        sa.UniqueConstraint("osm_type", "osm_id"),
        sa.CheckConstraint("osm_type IN ('node', 'way', 'relation')", name="osm_type"),
    )
    op.create_index("ix_stations_geom", "stations", ["geom"], postgresql_using="gist")
    op.create_index("ix_stations_city_service", "stations", ["city_id", "service"])

    op.create_table(
        "units",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column(
            "station_id",
            sa.String(32),
            sa.ForeignKey("stations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "city_id",
            sa.String(32),
            sa.ForeignKey("cities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("service", _enum(SERVICES, "service"), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column(
            "status", _enum(UNIT_STATUSES, "status"), nullable=False, server_default="AVAILABLE"
        ),
        sa.Column("current_dispatch_id", pg.UUID(as_uuid=True)),  # FK added after dispatches
        sa.Column("status_changed_at", TSTZ, nullable=False, server_default=_now()),
        sa.Column("updated_at", TSTZ, nullable=False, server_default=_now()),
        sa.UniqueConstraint("station_id", "ordinal"),
        sa.CheckConstraint("ordinal >= 1", name="ordinal"),
        sa.CheckConstraint(
            "(status IN ('DISPATCHED', 'ON_SCENE')) = (current_dispatch_id IS NOT NULL)",
            name="busy_has_dispatch",
        ),
    )
    op.create_index(
        "ix_units_free_by_station",
        "units",
        ["station_id"],
        postgresql_where=sa.text("status = 'AVAILABLE'"),
    )
    op.create_index("ix_units_city_status", "units", ["city_id", "status"])

    op.create_table(
        "signals",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "city_id",
            sa.String(32),
            sa.ForeignKey("cities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255)),
        sa.Column("geom", POINT, nullable=False),
        sa.Column("osm_node_ids", pg.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", TSTZ, nullable=False, server_default=_now()),
        sa.Column("updated_at", TSTZ, nullable=False, server_default=_now()),
    )
    op.create_index("ix_signals_geom", "signals", ["geom"], postgresql_using="gist")
    op.create_index("ix_signals_city_id", "signals", ["city_id"])

    # -------------------------------------------------------------------- history
    op.create_table(
        "incidents",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "city_id",
            sa.String(32),
            sa.ForeignKey("cities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("type", _enum(INCIDENT_TYPES, "type"), nullable=False),
        sa.Column("reported_type", sa.String(32), nullable=False),
        sa.Column("geom", POINT, nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "status", _enum(INCIDENT_STATUSES, "status"), nullable=False, server_default="OPEN"
        ),
        sa.Column("reported_at", TSTZ, nullable=False, server_default=_now()),
        sa.Column("dispatched_at", TSTZ),
        sa.Column("en_route_at", TSTZ),
        sa.Column("on_scene_at", TSTZ),
        sa.Column("resolved_at", TSTZ),
        sa.Column("cancelled_at", TSTZ),
        sa.Column("failed_at", TSTZ),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("updated_at", TSTZ, nullable=False, server_default=_now()),
    )
    op.create_index(
        "ix_incidents_city_reported",
        "incidents",
        ["city_id", sa.text("reported_at DESC"), sa.text("id DESC")],
    )
    op.create_index("ix_incidents_city_status", "incidents", ["city_id", "status"])
    op.create_index("ix_incidents_geom", "incidents", ["geom"], postgresql_using="gist")

    op.create_table(
        "dispatches",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "incident_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "unit_id", sa.String(48), sa.ForeignKey("units.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "station_id",
            sa.String(32),
            sa.ForeignKey("stations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "city_id",
            sa.String(32),
            sa.ForeignKey("cities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("service", _enum(SERVICES, "service"), nullable=False),
        sa.Column("route", LINESTRING, nullable=False),
        sa.Column("distance_m", sa.Double(), nullable=False),
        sa.Column("predicted_eta_s", sa.Double(), nullable=False),
        sa.Column("actual_travel_s", sa.Double()),
        sa.Column("est_time_saved_s", sa.Double()),
        sa.Column("corridor", pg.JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("dispatched_at", TSTZ, nullable=False, server_default=_now()),
        sa.Column("arrived_at", TSTZ),
        sa.Column("ended_at", TSTZ),
        sa.Column("end_reason", sa.String(16)),
        sa.CheckConstraint("distance_m >= 0 AND predicted_eta_s >= 0", name="non_negative"),
        sa.CheckConstraint(
            "end_reason IS NULL OR end_reason IN ('CLEARED', 'CANCELLED', 'FAILED')",
            name="end_reason",
        ),
    )
    op.create_index("ix_dispatches_incident_id", "dispatches", ["incident_id"])
    op.create_index("ix_dispatches_route", "dispatches", ["route"], postgresql_using="gist")
    op.create_index(
        "uq_dispatches_active_unit",
        "dispatches",
        ["unit_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_dispatches_active_city",
        "dispatches",
        ["city_id"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_foreign_key(
        "fk_units_current_dispatch_id_dispatches",
        "units",
        "dispatches",
        ["current_dispatch_id"],
        ["id"],
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "signal_preemptions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "dispatch_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dispatches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "signal_id",
            sa.String(32),
            sa.ForeignKey("signals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "city_id",
            sa.String(32),
            sa.ForeignKey("cities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", _enum(SIGNAL_STATES, "state"), nullable=False),
        sa.Column("lane", _enum(LANES, "lane", length=1)),
        sa.Column("approach_bearing", sa.Double()),
        sa.Column("eta_at_claim_s", sa.Double()),
        sa.Column("claimed_at", TSTZ, nullable=False),
        sa.Column("released_at", TSTZ),
        sa.CheckConstraint("state <> 'NORMAL'", name="not_normal"),
    )
    op.create_index("ix_signal_preemptions_dispatch_id", "signal_preemptions", ["dispatch_id"])
    op.create_index(
        "ix_signal_preemptions_signal_claimed", "signal_preemptions", ["signal_id", "claimed_at"]
    )
    op.create_index(
        "ix_signal_preemptions_city_claimed", "signal_preemptions", ["city_id", "claimed_at"]
    )

    op.create_table(
        "incident_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "incident_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dispatch_id", pg.UUID(as_uuid=True), sa.ForeignKey("dispatches.id", ondelete="CASCADE")
        ),
        sa.Column("type", sa.String(48), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("ts", TSTZ, nullable=False),
        sa.Column("recorded_at", TSTZ, nullable=False, server_default=_now()),
    )
    op.create_index("ix_incident_events_incident_ts", "incident_events", ["incident_id", "ts"])

    op.create_table(
        "vehicle_positions",
        sa.Column(
            "dispatch_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dispatches.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ts", TSTZ, primary_key=True),
        sa.Column("geom", POINT, nullable=False),
        sa.Column("speed_mps", sa.Double(), nullable=False),
        sa.Column("along_m", sa.Double(), nullable=False),
    )
    op.create_index(
        "ix_vehicle_positions_ts_brin", "vehicle_positions", ["ts"], postgresql_using="brin"
    )

    # ------------------------------------------------------------------- delivery
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("created_at", TSTZ, nullable=False, server_default=_now()),
        sa.Column("sent_at", TSTZ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text()),
    )
    op.create_index(
        "ix_outbox_unsent", "outbox", ["id"], postgresql_where=sa.text("sent_at IS NULL")
    )


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("vehicle_positions")
    op.drop_table("incident_events")
    op.drop_table("signal_preemptions")
    op.drop_constraint("fk_units_current_dispatch_id_dispatches", "units", type_="foreignkey")
    op.drop_table("dispatches")
    op.drop_table("incidents")
    op.drop_table("signals")
    op.drop_table("units")
    op.drop_table("stations")
    op.drop_table("cities")
    # the postgis extension is left installed on purpose (shared, possibly pre-existing)
