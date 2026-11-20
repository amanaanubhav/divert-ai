"""vehicle_positions.elapsed_s - simulated seconds since dispatch at each sample.

``ts`` is the wall clock of the sample; simulated time runs DIVERT_SIM_TIME_SCALE times
faster and is frozen while no worker leads. The worker rebuilds a lost vehicle's elapsed
time from this column (wall-clock differences inflated actual_travel_s after a failover).
Nullable: rows written before this revision fall back to the wall-clock estimate.

Revision ID: 0003_position_elapsed
Revises: 0002_dispatch_rebuild
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_position_elapsed"
down_revision: str | None = "0002_dispatch_rebuild"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("vehicle_positions", sa.Column("elapsed_s", sa.Double(), nullable=True))


def downgrade() -> None:
    op.drop_column("vehicle_positions", "elapsed_s")
