"""Record a job's reader-owned preferences.

Revision ID: 0052_job_preferences
Revises: 0051_job_timezone
Create Date: 2026-10-30

A scheduled agent's output has a reader, and the reader had nowhere to say what it should cover,
skip, or how long it should be, except per-person memory -- which the agent itself can write and
which a firing recalls by rank, so may miss. `preferences` is a document the reader writes over
`PUT /jobs/{name}/preferences`, never the agent, and every firing of the job reads it whole
(`felix.jobs.preferences`). `preferences_updated_at` / `preferences_updated_by` say when and by
whom it was last written. `preferences_required` is the job writer's: a firing of a job that
requires preferences and has none runs nothing and is recorded `skipped`.

Expand-only: NULL preferences and `preferences_required` false are what every existing job has,
so none fires differently. RLS is unchanged: the table's policy covers every column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_job_preferences"
down_revision: str | None = "0051_job_timezone"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("preferences", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("preferences_updated_at", sa.BigInteger(), nullable=True))
    op.add_column("jobs", sa.Column("preferences_updated_by", sa.Text(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("preferences_required", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("jobs", "preferences_required")
    op.drop_column("jobs", "preferences_updated_by")
    op.drop_column("jobs", "preferences_updated_at")
    op.drop_column("jobs", "preferences")
