"""Dispatch rebuild data + analytics/retention indexes.

* dispatches.segment_speeds_mps - OSRM per-segment speeds, so the worker can rebuild a
  live vehicle from PostgreSQL alone if Redis lost it;
* dispatches.eta_model - which ETA model produced predicted_eta_s;
* ix_dispatches_city_dispatched - analytics windows per city;
* ix_outbox_sent_at - outbox retention sweep.

Revision ID: 0002_dispatch_rebuild
Revises: 0001_initial
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002_dispatch_rebuild"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dispatches",
        sa.Column(
            "segment_speeds_mps",
            pg.ARRAY(sa.Double()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column("dispatches", sa.Column("eta_model", sa.String(32), nullable=True))
    op.create_index("ix_dispatches_city_dispatched", "dispatches", ["city_id", "dispatched_at"])
    op.create_index(
        "ix_outbox_sent_at",
        "outbox",
        ["sent_at"],
        postgresql_where=sa.text("sent_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_sent_at", table_name="outbox")
    op.drop_index("ix_dispatches_city_dispatched", table_name="dispatches")
    op.drop_column("dispatches", "eta_model")
    op.drop_column("dispatches", "segment_speeds_mps")
