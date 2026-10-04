"""Keep each person's GitHub refresh token, sealed, so Felix can act as them after sign-in.

Revision ID: 0027_github_connections
Revises: 0026_skill_import_origin
Create Date: 2026-10-04

GitHub login read two things with the person's GitHub token and dropped it. Per-person repo
access has to act as that person later -- clone their repository, publish their commits -- so a
sign-in through a GitHub App with expiring user tokens now keeps the refresh token and mints
short-lived access tokens from it (`felix.auth.github_connections`).

Both tokens are stored AES-GCM sealed with FELIX_GITHUB_TOKEN_KEY; the database never holds one
in the clear. Keyed by tenant and GitHub user id, since a sign-in lands in one tenant. Empty on
every existing deployment, and nothing to backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_github_connections"
down_revision: str | None = "0026_skill_import_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "github_connections",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("github_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("github_login", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("refresh_token_sealed", sa.Text(), nullable=False),
        sa.Column("refresh_expires_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("access_token_sealed", sa.Text(), nullable=True),
        sa.Column("access_expires_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("principal_subj", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )

    # The operator's listing: one tenant's connections, newest first.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_github_connections_tenant_updated "
        "ON github_connections (tenant_id, updated_at DESC, github_user_id)"
    )

    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0021`).
    op.execute("ALTER TABLE github_connections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE github_connections FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON github_connections")
    op.execute(
        """
        CREATE POLICY felix_tenant_isolation ON github_connections
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
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON github_connections")
    op.execute("DROP INDEX IF EXISTS idx_github_connections_tenant_updated")
    op.drop_table("github_connections")
