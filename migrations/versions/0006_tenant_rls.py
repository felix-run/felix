"""Optional tenant RLS policies (enable with FELIX_DATABASE_RLS=true).

Revision ID: 0006_tenant_rls
Revises: 0005_session_fts
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_tenant_rls"
down_revision: str | None = "0005_session_fts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables with a tenant_id column (see felix.db.models).
_TENANT_TABLES = (
    "audit_events",
    "plans",
    "jobs",
    "job_runs",
    "approvals",
    "skill_activation",
    "manifests",
    "manifest_active",
    "eval_datasets",
    "eval_dataset_items",
    "eval_runs",
    "memory_vectors",
    "session_events",
    "thread_state",
    "fibers",
    "usage_events",
    "a2a_tasks",
)


def upgrade() -> None:
    for table in _TENANT_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS felix_tenant_isolation ON "{table}"')
        op.execute(
            f"""
            CREATE POLICY felix_tenant_isolation ON "{table}"
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
    for table in _TENANT_TABLES:
        op.execute(f'DROP POLICY IF EXISTS felix_tenant_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
