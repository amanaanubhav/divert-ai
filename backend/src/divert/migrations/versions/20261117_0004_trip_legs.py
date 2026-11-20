"""Trip legs (design §1, §5): dispatch_legs, the leg statuses, hospital, pre-emption detail.

* dispatch_legs - one row per leg (RESPONSE / TRANSPORT / RETURN), unique (dispatch_id,
  seq); existing dispatches get their RESPONSE leg (seq 0) from the dispatches columns;
* units.status += TRANSPORTING, AT_HOSPITAL, RETURNING (busy: they keep the dispatch);
* incidents.status += TRANSPORTING, AT_HOSPITAL; incidents.hospital_station_id,
  transport_started_at, at_hospital_at;
* dispatches.status (the whole trip; COMPLETED when the unit is back) - backfilled
  COMPLETED / FAILED for ended dispatches, ON_SCENE when arrived, else DISPATCHED;
* signal_preemptions: the design §4.6 columns and lane 'C' - backfilled from the stage A
  event payloads where present, ``outcome`` from the release reason otherwise;
* vehicle_positions.leg_seq; stations.emergency / healthcare (hospitals, seeded).

Downgrade maps the new statuses back (TRANSPORTING / AT_HOSPITAL / RETURNING -> ON_SCENE,
lane C -> NULL) so the old CHECK constraints hold, then drops everything added here.

Revision ID: 0004_trip_legs
Revises: 0003_position_elapsed
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql as pg

revision: str = "0004_trip_legs"
down_revision: str | None = "0003_position_elapsed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POINT = Geography(geometry_type="POINT", srid=4326, spatial_index=False)
LINESTRING = Geography(geometry_type="LINESTRING", srid=4326, spatial_index=False)
TSTZ = sa.DateTime(timezone=True)

UNIT_STATUSES_OLD = ("AVAILABLE", "DISPATCHED", "ON_SCENE", "OUT_OF_SERVICE")
UNIT_STATUSES = (
    "AVAILABLE",
    "DISPATCHED",
    "ON_SCENE",
    "TRANSPORTING",
    "AT_HOSPITAL",
    "RETURNING",
    "OUT_OF_SERVICE",
)
BUSY_OLD = ("DISPATCHED", "ON_SCENE")
BUSY = ("DISPATCHED", "ON_SCENE", "TRANSPORTING", "AT_HOSPITAL", "RETURNING")
INCIDENT_STATUSES_OLD = (
    "OPEN",
    "DISPATCHED",
    "EN_ROUTE",
    "ON_SCENE",
    "RESOLVED",
    "CANCELLED",
    "FAILED",
)
INCIDENT_STATUSES = (
    "OPEN",
    "DISPATCHED",
    "EN_ROUTE",
    "ON_SCENE",
    "TRANSPORTING",
    "AT_HOSPITAL",
    "RESOLVED",
    "CANCELLED",
    "FAILED",
)
DISPATCH_STATUSES = (
    "DISPATCHED",
    "ON_SCENE",
    "TRANSPORTING",
    "AT_HOSPITAL",
    "RETURNING",
    "COMPLETED",
    "FAILED",
)
LEG_KINDS = ("RESPONSE", "TRANSPORT", "RETURN")
DESTINATION_KINDS = ("SCENE", "HOSPITAL", "STATION")
LEG_END_REASONS = ("ARRIVED", "CANCELLED", "FAILED", "SUPERSEDED")
LANES_OLD = ("L", "R")
LANES = ("L", "R", "C")
MOVEMENTS = (
    "STRAIGHT",
    "SLIGHT_LEFT",
    "LEFT",
    "SHARP_LEFT",
    "SLIGHT_RIGHT",
    "RIGHT",
    "SHARP_RIGHT",
    "U_TURN",
    "ROUNDABOUT",
)
OUTCOMES = ("CLEARED", "TIMEOUT", "CANCELLED", "REROUTED")
PHASE_STATES = ("green_extend", "red_truncate")
# signal_preemptions backfill: the stage A release payloads carry the §4.6 fields; older
# rows only a release reason ({movements} = the allowed Movement values)
_PREEMPTION_BACKFILL = """
UPDATE signal_preemptions sp SET
    lane = COALESCE(sp.lane,
        CASE WHEN e.payload ->> 'lane' IN ('L', 'R', 'C') THEN e.payload ->> 'lane' END),
    leg_seq = COALESCE((e.payload ->> 'leg_seq')::smallint, 0),
    movement = CASE WHEN e.payload ->> 'movement' IN ({movements})
                    THEN e.payload ->> 'movement' END,
    lane_index = (e.payload ->> 'lane_index')::smallint,
    lanes_total = (e.payload ->> 'lanes_total')::smallint,
    free_left = COALESCE((e.payload ->> 'free_left')::boolean, false),
    required_lead_s = (e.payload ->> 'required_lead_s')::double precision,
    transition_s = (e.payload ->> 'transition_s')::double precision,
    queue_clear_s = (e.payload ->> 'queue_clear_s')::double precision,
    green_at = (e.payload ->> 'green_at')::timestamptz,
    crossed_at = (e.payload ->> 'crossed_at')::timestamptz,
    waited_s = (e.payload ->> 'waited_s')::double precision,
    outcome = CASE
        WHEN e.payload ->> 'outcome' IN ('CLEARED', 'TIMEOUT', 'CANCELLED', 'REROUTED')
            THEN e.payload ->> 'outcome'
        WHEN e.payload ->> 'reason' IN ('passed', 'arrived', 'cleared') THEN 'CLEARED'
        WHEN e.payload ->> 'reason' = 'timeout' THEN 'TIMEOUT'
        WHEN e.payload ->> 'reason' = 'rerouted' THEN 'REROUTED'
        ELSE 'CANCELLED'
    END,
    phase_state_at_claim = CASE
        WHEN e.payload ->> 'phase_state_at_claim' IN ('green_extend', 'red_truncate')
            THEN e.payload ->> 'phase_state_at_claim' END
FROM incident_events e
WHERE e.type = 'preemption_released' AND e.payload ->> 'claim_event_id' = sp.event_id
"""


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _set_check(table: str, name: str, condition: str) -> None:
    """(Re)create a CHECK constraint under its exact (convention) name."""
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition})")


def _enum(values: tuple[str, ...], name: str, length: int = 16) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def upgrade() -> None:
    # ------------------------------------------------------------------ stations
    op.add_column("stations", sa.Column("emergency", sa.String(3), nullable=True))
    op.add_column("stations", sa.Column("healthcare", sa.String(32), nullable=True))
    _set_check("stations", "ck_stations_emergency", "emergency IN ('yes', 'no')")

    # --------------------------------------------------------------------- units
    _set_check("units", "ck_units_status", _in("status", UNIT_STATUSES))
    _set_check(
        "units",
        "ck_units_busy_has_dispatch",
        f"({_in('status', BUSY)}) = (current_dispatch_id IS NOT NULL)",
    )

    # ----------------------------------------------------------------- incidents
    _set_check("incidents", "ck_incidents_status", _in("status", INCIDENT_STATUSES))
    op.add_column(
        "incidents",
        sa.Column(
            "hospital_station_id",
            sa.String(32),
            sa.ForeignKey(
                "stations.id",
                name="fk_incidents_hospital_station_id_stations",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
    )
    op.add_column("incidents", sa.Column("transport_started_at", TSTZ, nullable=True))
    op.add_column("incidents", sa.Column("at_hospital_at", TSTZ, nullable=True))

    # ---------------------------------------------------------------- dispatches
    op.add_column(
        "dispatches",
        sa.Column("status", sa.String(16), nullable=False, server_default="DISPATCHED"),
    )
    _set_check("dispatches", "ck_dispatches_status", _in("status", DISPATCH_STATUSES))
    op.execute(
        "UPDATE dispatches SET status = CASE "
        "WHEN end_reason = 'FAILED' THEN 'FAILED' "
        "WHEN ended_at IS NOT NULL THEN 'COMPLETED' "
        "WHEN arrived_at IS NOT NULL THEN 'ON_SCENE' "
        "ELSE 'DISPATCHED' END"
    )

    # ------------------------------------------------------------- dispatch_legs
    op.create_table(
        "dispatch_legs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "dispatch_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dispatches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.SmallInteger(), nullable=False),
        sa.Column("kind", _enum(LEG_KINDS, "kind"), nullable=False),
        sa.Column("emergency", sa.Boolean(), nullable=False),
        sa.Column("origin", POINT, nullable=False),
        sa.Column("destination", POINT, nullable=False),
        sa.Column("destination_kind", _enum(DESTINATION_KINDS, "destination_kind"), nullable=False),
        sa.Column(
            "destination_station_id",
            sa.String(32),
            sa.ForeignKey("stations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("route", LINESTRING, nullable=False),
        sa.Column("distance_m", sa.Double(), nullable=False),
        sa.Column("predicted_eta_s", sa.Double(), nullable=False),
        sa.Column(
            "segment_speeds_mps",
            pg.ARRAY(sa.Double()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("corridor", pg.JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("route_choice", pg.JSONB(), nullable=True),
        sa.Column("hospital_choice", pg.JSONB(), nullable=True),
        sa.Column("eta_model", sa.String(32), nullable=True),
        sa.Column("planned_at", TSTZ, nullable=False),
        sa.Column("started_at", TSTZ, nullable=True),
        sa.Column("ended_at", TSTZ, nullable=True),
        sa.Column("actual_travel_s", sa.Double(), nullable=True),
        sa.Column("end_reason", _enum(LEG_END_REASONS, "end_reason"), nullable=True),
        sa.Column("reroutes", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("started_trip_s", sa.Double(), nullable=True),
        sa.Column("ended_trip_s", sa.Double(), nullable=True),
        sa.UniqueConstraint("dispatch_id", "seq"),
        sa.CheckConstraint("seq >= 0", name="seq"),
        sa.CheckConstraint("distance_m >= 0 AND predicted_eta_s >= 0", name="non_negative"),
    )
    op.create_index("ix_dispatch_legs_route", "dispatch_legs", ["route"], postgresql_using="gist")
    op.create_index(
        "ix_dispatch_legs_destination_station", "dispatch_legs", ["destination_station_id"]
    )
    # every existing dispatch: its RESPONSE leg (seq 0) from the dispatches columns
    op.execute(
        """
        INSERT INTO dispatch_legs (
            dispatch_id, seq, kind, emergency, origin, destination, destination_kind,
            destination_station_id, route, distance_m, predicted_eta_s, segment_speeds_mps,
            corridor, route_choice, eta_model, planned_at, started_at, ended_at,
            actual_travel_s, end_reason, started_trip_s, ended_trip_s
        )
        SELECT
            d.id, 0, 'RESPONSE', true,
            ST_StartPoint(d.route::geometry)::geography, i.geom, 'SCENE', NULL,
            d.route, d.distance_m, d.predicted_eta_s, d.segment_speeds_mps, d.corridor,
            (SELECT NULLIF(e.payload -> 'route_choice', 'null'::jsonb)
               FROM incident_events e
              WHERE e.dispatch_id = d.id AND e.type = 'dispatched'
              LIMIT 1),
            d.eta_model, d.dispatched_at, d.dispatched_at,
            COALESCE(d.arrived_at, d.ended_at),
            d.actual_travel_s,
            CASE
                WHEN d.arrived_at IS NOT NULL THEN 'ARRIVED'
                WHEN d.end_reason = 'CANCELLED' THEN 'CANCELLED'
                WHEN d.end_reason = 'FAILED' THEN 'FAILED'
            END,
            0, CASE WHEN d.arrived_at IS NOT NULL THEN d.actual_travel_s END
        FROM dispatches d JOIN incidents i ON i.id = d.incident_id
        """
    )

    # -------------------------------------------------------- signal_preemptions
    _set_check("signal_preemptions", "ck_signal_preemptions_lane", _in("lane", LANES))
    for col in (
        sa.Column("leg_seq", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("movement", sa.String(16), nullable=True),
        sa.Column("lane_index", sa.SmallInteger(), nullable=True),
        sa.Column("lanes_total", sa.SmallInteger(), nullable=True),
        sa.Column("free_left", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("required_lead_s", sa.Double(), nullable=True),
        sa.Column("transition_s", sa.Double(), nullable=True),
        sa.Column("queue_clear_s", sa.Double(), nullable=True),
        sa.Column("green_at", TSTZ, nullable=True),
        sa.Column("crossed_at", TSTZ, nullable=True),
        sa.Column("waited_s", sa.Double(), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("phase_state_at_claim", sa.String(16), nullable=True),
    ):
        op.add_column("signal_preemptions", col)
    _set_check("signal_preemptions", "ck_signal_preemptions_movement", _in("movement", MOVEMENTS))
    _set_check("signal_preemptions", "ck_signal_preemptions_outcome", _in("outcome", OUTCOMES))
    _set_check(
        "signal_preemptions",
        "ck_signal_preemptions_phase_state_at_claim",
        _in("phase_state_at_claim", PHASE_STATES),
    )
    # backfill: the stage A payloads carry the §4.6 fields; older rows only a reason
    op.execute(_PREEMPTION_BACKFILL.replace("{movements}", ", ".join(repr(m) for m in MOVEMENTS)))

    # --------------------------------------------------------- vehicle_positions
    op.add_column(
        "vehicle_positions",
        sa.Column("leg_seq", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("vehicle_positions", "leg_seq")

    op.execute("UPDATE signal_preemptions SET lane = NULL WHERE lane = 'C'")
    for name in (
        "ck_signal_preemptions_movement",
        "ck_signal_preemptions_outcome",
        "ck_signal_preemptions_phase_state_at_claim",
    ):
        op.execute(f"ALTER TABLE signal_preemptions DROP CONSTRAINT IF EXISTS {name}")
    for col in (
        "phase_state_at_claim",
        "outcome",
        "waited_s",
        "crossed_at",
        "green_at",
        "queue_clear_s",
        "transition_s",
        "required_lead_s",
        "free_left",
        "lanes_total",
        "lane_index",
        "movement",
        "leg_seq",
    ):
        op.drop_column("signal_preemptions", col)
    _set_check("signal_preemptions", "ck_signal_preemptions_lane", _in("lane", LANES_OLD))

    op.drop_index("ix_dispatch_legs_destination_station", table_name="dispatch_legs")
    op.drop_index("ix_dispatch_legs_route", table_name="dispatch_legs")
    op.drop_table("dispatch_legs")

    op.execute("ALTER TABLE dispatches DROP CONSTRAINT IF EXISTS ck_dispatches_status")
    op.drop_column("dispatches", "status")

    op.execute(
        "UPDATE incidents SET status = 'ON_SCENE' WHERE status IN ('TRANSPORTING', 'AT_HOSPITAL')"
    )
    op.drop_column("incidents", "at_hospital_at")
    op.drop_column("incidents", "transport_started_at")
    op.drop_constraint("fk_incidents_hospital_station_id_stations", "incidents", type_="foreignkey")
    op.drop_column("incidents", "hospital_station_id")
    _set_check("incidents", "ck_incidents_status", _in("status", INCIDENT_STATUSES_OLD))

    op.execute(
        "UPDATE units SET status = 'ON_SCENE' "
        "WHERE status IN ('TRANSPORTING', 'AT_HOSPITAL', 'RETURNING')"
    )
    _set_check(
        "units",
        "ck_units_busy_has_dispatch",
        f"({_in('status', BUSY_OLD)}) = (current_dispatch_id IS NOT NULL)",
    )
    _set_check("units", "ck_units_status", _in("status", UNIT_STATUSES_OLD))

    op.execute("ALTER TABLE stations DROP CONSTRAINT IF EXISTS ck_stations_emergency")
    op.drop_column("stations", "healthcare")
    op.drop_column("stations", "emergency")
