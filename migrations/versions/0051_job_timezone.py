"""Record the timezone a job's cron is read in.

Revision ID: 0051_job_timezone
Revises: 0050_job_health
Create Date: 2026-10-30

A job's cron was evaluated in UTC only, so "08:00 every day" for someone in America/Phoenix was
written `0 15 * * *`, and in a zone with daylight saving it moved by an hour twice a year.
`timezone` is the IANA name its cron and macros are read in (`felix.jobs.schedule`); it moves
nothing else, and the store refuses one beside an interval or an `at:`.

Expand-only: NULL is UTC, so every existing job keeps firing exactly when it did. RLS is
unchanged: the table's policy covers every column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_job_timezone"
down_revision: str | None = "0050_job_health"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("timezone", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "timezone")
