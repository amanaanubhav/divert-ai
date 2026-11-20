"""Contested claims and hospital specialities (stage 5 fix-2 B5, B6).

* signal_preemptions.conflicts - how often the claim was queued behind another phase
  (``preemption_conflict`` events). The row's ``state`` is the claim's final state
  (PREEMPTED once a queued claim is served), so without this count a contested claim
  that was later served looked uncontested; backfilled from the conflict events;
* stations.speciality - OSM ``healthcare:speciality`` of a hospital (seeded from the
  snapshots): TRANSPORT skips hospitals that only offer specialities which cannot take
  an emergency patient (dentistry, ophthalmology, ...), see domain/hospitals.py.

Revision ID: 0005_contested_claims_speciality
Revises: 0004_trip_legs
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_contested_claims_speciality"
down_revision: str | None = "0004_trip_legs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "signal_preemptions",
        sa.Column("conflicts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.execute(  # (exact convention name, like 0004)
        "ALTER TABLE signal_preemptions "
        "ADD CONSTRAINT ck_signal_preemptions_conflicts CHECK (conflicts >= 0)"
    )
    # one preemption_conflict event per time the claim was queued (deterministic ids)
    op.execute(
        """
        UPDATE signal_preemptions sp
           SET conflicts = LEAST(c.n, 32767)
          FROM (SELECT e.payload ->> 'claim_event_id' AS claim_event_id, count(*) AS n
                  FROM incident_events e
                 WHERE e.type = 'preemption_conflict'
                 GROUP BY 1) c
         WHERE c.claim_event_id = sp.event_id
        """
    )
    # a row still in CONFLICT was queued at least once, even if its event is missing
    op.execute(
        "UPDATE signal_preemptions SET conflicts = 1 WHERE state = 'CONFLICT' AND conflicts = 0"
    )
    op.add_column("stations", sa.Column("speciality", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("stations", "speciality")
    op.execute(
        "ALTER TABLE signal_preemptions DROP CONSTRAINT IF EXISTS ck_signal_preemptions_conflicts"
    )
    op.drop_column("signal_preemptions", "conflicts")
