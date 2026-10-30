"""Keep a scheduled job's own state: bookmarks per source, a ledger of what it has done.

Revision ID: 0053_job_state
Revises: 0052_job_preferences
Create Date: 2026-10-30

A scheduled agent reads its sources, and the next firing needs to know where the last one stopped.
That lived in the job thread's workspace by convention, or in per-person memory -- which a firing
recalls by rank, so a bookmark could be missed. `job_state` holds a few short text values per job,
keyed by tenant, job name and key (`felix.jobs.state`), written by the job's runs through
`state_set` and read back whole by key. Deleting the job deletes its rows, and so does a re-save by
another writer (`felix.jobs.store`). Empty on every existing deployment, nothing to backfill.

`jobs.state_cleared_at` is when a re-save by another writer last cleared it, strictly increasing per
job. A firing records the value it read, and a write it makes is refused once the job's has moved:
the writer's subject alone cannot tell two holders of one shared credential apart. NULL on every
existing job, which no clear has touched. RLS is unchanged on `jobs`: its policy covers every column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053_job_state"
down_revision: str | None = "0052_job_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("state_cleared_at", sa.BigInteger(), nullable=True))
    op.create_table(
        "job_state",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("job_name", sa.Text(), primary_key=True),
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_by_thread", sa.Text(), nullable=False, server_default=""),
    )

    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0049`).
    op.execute("ALTER TABLE job_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE job_state FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON job_state")
    op.execute(
        """
        CREATE POLICY felix_tenant_isolation ON job_state
        USING (
            current_setting('app.rls_bypass', true) = 'on'
            OR tenant_id = current_setting('app.tenant_id', true)
        )
        WITH CHECK (
            current_setting('app.rls_bypass', true) = 'on'
            OR tenant_id = current_setting('app.tenant_id', true)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON job_state")
    op.drop_table("job_state")
    op.drop_column("jobs", "state_cleared_at")
