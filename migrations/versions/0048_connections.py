"""Keep each person's connections to other services, sealed, so Felix can act as them later.

Revision ID: 0048_connections
Revises: 0047_drop_skill_activation
Create Date: 2026-10-29

GitHub's connection has its own table (`0027`); every other service a person connects -- Google's
calendar and mail, an iCloud app password -- lands here, one row per person, provider and account
(`felix.auth.connections`). Every credential is AES-GCM sealed with FELIX_CONNECTION_TOKEN_KEY;
the database never holds one in the clear. Empty on every existing deployment, nothing to
backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_connections"
down_revision: str | None = "0047_drop_skill_activation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        # `issuer|subject` (`felix.auth.persons`): a subject is only unique within its issuer.
        sa.Column("person", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), primary_key=True),
        sa.Column("account", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("scopes", sa.Text(), nullable=False, server_default=""),
        sa.Column("secret_sealed", sa.Text(), nullable=False),
        sa.Column("secret_expires_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("access_sealed", sa.Text(), nullable=True),
        sa.Column("access_expires_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )

    # The operator's listing: one tenant's connections, newest first, ending on the key.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_connections_tenant_updated "
        "ON connections (tenant_id, updated_at DESC, person, provider, account)"
    )

    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0027`).
    op.execute("ALTER TABLE connections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connections FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON connections")
    op.execute(
        """
        CREATE POLICY felix_tenant_isolation ON connections
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
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON connections")
    op.execute("DROP INDEX IF EXISTS idx_connections_tenant_updated")
    op.drop_table("connections")
