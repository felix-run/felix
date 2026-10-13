"""Store authored workflow definitions.

Revision ID: 0040_workflows
Revises: 0039_file_checkpoints
Create Date: 2026-10-12

`felix/workflows/store.py` keeps one row per workflow, replaced in place, its definition as JSON,
a `version` counting saves and a `generation` fixed at creation. A conditional write compares the
pair, so two editors saving the same workflow find out instead of one erasing the other, and an
editor holding a revision of a deleted workflow cannot save over its re-creation. Runs are a later
table; a run will snapshot the definition it started on rather than read this row again.

New and empty on every deployment; nothing to backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_workflows"
down_revision: str | None = "0039_file_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Text(), nullable=False),
        sa.Column("definition_json", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    # The listing: newest first, ending on the id in byte order (`test_ordering_rule.py`).
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_workflows_tenant_updated_id "
        'ON workflows (tenant_id, updated_at DESC, id COLLATE "C" DESC)'
    )

    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0039`).
    op.execute("ALTER TABLE workflows ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workflows FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON workflows")
    op.execute(
        """
        CREATE POLICY felix_tenant_isolation ON workflows
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
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON workflows")
    op.execute("DROP INDEX IF EXISTS idx_workflows_tenant_updated_id")
    op.drop_table("workflows")
