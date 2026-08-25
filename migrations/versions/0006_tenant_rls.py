"""Tenant RLS policies on every table carrying a tenant_id.

Revision ID: 0006_tenant_rls
Revises: 0005_session_fts
Create Date: 2026-08-22

This applies ENABLE *and* FORCE ROW LEVEL SECURITY unconditionally. It is not
gated on ``FELIX_DATABASE_RLS`` and must not be: a migration that produces a
different schema depending on the environment it ran in is not reproducible, and
there would then be no way to turn RLS on later without re-running DDL.

``FELIX_DATABASE_RLS`` is the *runtime* half. When it is true the application
sets ``app.tenant_id`` per transaction and the policy enforces isolation; when it
is false the application sets ``app.rls_bypass`` instead, which is what keeps a
migrated database usable by a deployment that has not opted in. The header of
this file used to say "optional ... enable with FELIX_DATABASE_RLS=true", which
described the setting while appearing to describe the migration -- and the
application did not set either GUC when the flag was off, so on any role RLS
actually applies to, every one of these tables returned zero rows.

Note FORCE: the policy binds the table owner too. Only a superuser or a
BYPASSRLS role escapes it, which is why a deployment on managed Postgres behaves
differently from the bundled compose stack.
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
