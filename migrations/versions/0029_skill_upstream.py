"""What an imported skill's origin held when Felix last looked.

Revision ID: 0029_skill_upstream
Revises: 0028_skill_file_digest_index
Create Date: 2026-10-04

`skills/upstream.py` checks an imported library skill against its origin -- on request, and
periodically when `FELIX_SKILL_IMPORT_CHECK_HOURS` is set. `skill_upstream` keeps one row per
imported skill: the origin it was checked against, the commit and kept-file digest found there,
when the tenant first saw that digest (the cooldown's clock), when the check ran, and the refusal
code of the last check if it failed. The upstream listing and the library detail read it, so
neither asks GitHub to say whether an update is waiting.

Backfilled from the newest `import` version of each skill with its origin and nothing else:
`checked_at` is null, which the periodic check takes first. Tenant-scoped, under the same RLS
policy as every other skill table. The index serves the sweep's cross-tenant "oldest check
first" read. A new table, small, and created empty but for the backfill: nothing here locks a
table anyone else is using.

The downgrade drops the table; every row in it can be rebuilt by checking again.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_skill_upstream"
down_revision: str | None = "0028_skill_file_digest_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "skill_upstream"
_INDEX = "idx_skill_upstream_checked"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("origin_source", sa.Text(), nullable=False),
        sa.Column("origin_ref", sa.Text(), nullable=False),
        sa.Column("upstream_commit", sa.Text(), nullable=True),
        sa.Column("upstream_tree_hash", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.BigInteger(), nullable=True),
        sa.Column("checked_at", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS {_INDEX} ON {_TABLE} (checked_at NULLS FIRST, tenant_id, name)")
    # The newest import version of each skill, rejected drafts aside. `skill_version` is under
    # forced RLS, which binds the table owner too: the bypass, for this transaction only, is what
    # lets the copy see every tenant's versions.
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.execute(
        f"""
        INSERT INTO {_TABLE} (tenant_id, name, origin_source, origin_ref)
        SELECT DISTINCT ON (tenant_id, name) tenant_id, name, origin_source, origin_ref
        FROM skill_version
        WHERE source = 'import'
          AND origin_source IS NOT NULL
          AND origin_ref IS NOT NULL
          AND NOT (status = 'archived' AND published_at IS NULL)
        ORDER BY tenant_id, name, created_at DESC, version DESC
        """
    )
    # Off again at once: nothing after the copy in this transaction may read across tenants.
    op.execute("SELECT set_config('app.rls_bypass', 'off', true)")
    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0026`).
    op.execute(f'ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(f'DROP POLICY IF EXISTS felix_tenant_isolation ON "{_TABLE}"')
    op.execute(
        f"""
        CREATE POLICY felix_tenant_isolation ON "{_TABLE}"
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
    op.execute(f'DROP POLICY IF EXISTS felix_tenant_isolation ON "{_TABLE}"')
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.drop_table(_TABLE)
