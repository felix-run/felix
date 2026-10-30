"""Record a job's open health incidents on the job.

Revision ID: 0050_job_health
Revises: 0049_destination_posts
Create Date: 2026-10-29

The `job_health_watch` sweep (`felix.jobs.watch`) flags a job that is overdue or whose last runs
all failed, and reports each incident once. `health_json` holds the open ones by kind, with when
each began and what became of its alert, so the next sweep finds it already reported and a success
clears it. Only the watch writes it; `put_job` leaves it alone, so re-saving a job does not re-alert.

Expand-only: `'{}'` is every existing job, which has no incident open. RLS is unchanged: the table's
policy covers every column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0050_job_health"
down_revision: str | None = "0049_destination_posts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "health_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "health_json")
