"""Record each spilled tool output, so retention can collect it.

Revision ID: 0018_artifact_ledger
Revises: 0017_eval_run_error_count
Create Date: 2026-09-26

`spec.artifacts` writes oversized tool results under `artifacts/{tenant}/{manifest}/` and
nothing ever collected that prefix: `jobs/retention.py` sweeps rows, and the `ObjectStore`
Protocol has no `list` to find objects with. That was an opt-in cost until #319 enabled the
spill in five bundled manifests, at which point every default deployment of them grew.

The same answer `0016_attachment_ledger` gave uploads: a ledger beside the bytes, row written
before the object and deleted after it, so any drift is a row whose objects may be absent --
which the sweep clears by age -- and never objects no row names.

Empty on every existing deployment and not backfilled, for the reason `0016` gives: a
backfill would have to list the object store. Spills predating this migration stay where they
were, uncollected, which is no worse than before it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_artifact_ledger"
down_revision: str | None = "0017_eval_run_error_count"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("manifest_id", sa.Text(), primary_key=True),
        sa.Column("artifact_id", sa.Text(), primary_key=True),
        # UTF-8 bytes of the spilled text, which is what the store holds.
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )

    # The sweep reads by age across tenants; a per-tenant question would lead with tenant_id.
    op.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_tenant_age ON artifacts (tenant_id, created_at)")

    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0016`).
    op.execute("ALTER TABLE artifacts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE artifacts FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON artifacts")
    op.execute(
        """
        CREATE POLICY felix_tenant_isolation ON artifacts
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
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON artifacts")
    op.execute("DROP INDEX IF EXISTS idx_artifacts_tenant_age")
    op.drop_table("artifacts")
