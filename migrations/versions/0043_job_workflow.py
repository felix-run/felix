"""Let a scheduled job start a workflow run.

Revision ID: 0043_job_workflow
Revises: 0042_job_creator
Create Date: 2026-10-13

A job names either a manifest, whose agent it invokes, or a workflow (`felix/workflows/`), whose
run it starts with `payload.inputs` as the run's inputs (`felix.jobs.scheduler`). Never both: the
store refuses that.

`workflow_revision` is the revision the writer saw when they wrote the job. A firing runs the
workflow only at that revision: the run carries the writer's scopes, so a definition someone else
saved since must not run with them until the writer re-saves the job.

Expand-only. Both columns default to `''`, which is every existing job: a manifest job or a no-op,
as it was. RLS is unchanged: the table's policy covers every column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_job_workflow"
down_revision: str | None = "0042_job_creator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("workflow_id", sa.Text(), server_default="", nullable=False))
    op.add_column("jobs", sa.Column("workflow_revision", sa.Text(), server_default="", nullable=False))


def downgrade() -> None:
    op.drop_column("jobs", "workflow_revision")
    op.drop_column("jobs", "workflow_id")
