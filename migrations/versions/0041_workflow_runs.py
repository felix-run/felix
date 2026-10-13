"""Record workflow runs.

Revision ID: 0041_workflow_runs
Revises: 0040_workflows
Create Date: 2026-10-12

`felix/workflows/runs.py` keeps one row per run: the definition and revision it started on (a run
never reads the workflow again, so an edit mid-run changes nothing it does), its inputs, each
step's state, who started it, and a pin of every manifest its steps name, checked at start. The
orchestrator fiber advances it under a compare-and-set on `version`.

New and empty on every deployment; nothing to backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041_workflow_runs"
down_revision: str | None = "0040_workflows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("definition_json", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("inputs_json", postgresql.JSONB(), nullable=False),
        sa.Column("steps_json", postgresql.JSONB(), nullable=False),
        sa.Column("auth_json", postgresql.JSONB(), nullable=False),
        sa.Column("pins_json", postgresql.JSONB(), nullable=False),
        sa.Column("trigger", sa.Text(), server_default="", nullable=False),
        sa.Column("started_by", sa.Text(), server_default="", nullable=False),
        sa.Column("fiber_id", sa.Text(), server_default="", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
    )
    # One workflow's runs, newest first, ending on the id in byte order (`test_ordering_rule.py`).
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_created_id "
        'ON workflow_runs (tenant_id, workflow_id, created_at DESC, id COLLATE "C" DESC)'
    )

    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0040`).
    op.execute("ALTER TABLE workflow_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workflow_runs FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON workflow_runs")
    op.execute(
        """
        CREATE POLICY felix_tenant_isolation ON workflow_runs
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
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON workflow_runs")
    op.execute("DROP INDEX IF EXISTS idx_workflow_runs_workflow_created_id")
    op.drop_table("workflow_runs")
