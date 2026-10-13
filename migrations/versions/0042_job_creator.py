"""Record who wrote a scheduled job, so it runs as them.

Revision ID: 0042_job_creator
Revises: 0041_workflow_runs
Create Date: 2026-10-13

A job fired as principal `cron` with no scopes, compiled its manifest directly and never passed
through the manifest's inbound auth: a holder of `jobs:write` could schedule a manifest whose
`required_scopes` they lacked, or one their credential's `felix_manifests` pin does not name, and
the worker ran it. `PUT /jobs/{name}` now records the writer (`created_by`) and the authority the
run may carry (`created_auth`: their scopes, scheme, anonymity and manifest pin), and the scheduler
runs the job as `cron` on their behalf, checked against the manifest like any other caller
(`felix.jobs.scheduler`).

Expand-only. Both columns are nullable and null on every existing row, so adding them is a
catalogue change that rewrites nothing. A row with no `created_by` is a job written before this
revision; the scheduler runs it as a principal holding no scopes, which is what it always was,
except that the manifest's inbound rules now apply to it. RLS is unchanged: the table's policy
covers every column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042_job_creator"
down_revision: str | None = "0041_workflow_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("created_by", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("created_auth", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "created_auth")
    op.drop_column("jobs", "created_by")
