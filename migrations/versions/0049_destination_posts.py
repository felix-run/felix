"""Record each edition an agent posts to an operator-registered destination.

Revision ID: 0049_destination_posts
Revises: 0048_connections
Create Date: 2026-10-29

`post` (`felix.destinations.tool`) claims a row before it delivers, so a retried call, a resumed
run or a second firing of the same job naming the same edition finds the row and does not post
twice (`felix.destinations.ledger`). One row per tenant, destination, manifest and edition. Empty on every
existing deployment, nothing to backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_destination_posts"
down_revision: str | None = "0048_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "destination_posts",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("destination", sa.Text(), primary_key=True),
        sa.Column("manifest_id", sa.Text(), primary_key=True),
        sa.Column("edition", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=True),
        sa.Column("thread_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "status IN ('posting', 'posted', 'maybe_posted')", name="ck_destination_posts_status"
        ),
    )

    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0048`).
    op.execute("ALTER TABLE destination_posts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE destination_posts FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON destination_posts")
    op.execute(
        """
        CREATE POLICY felix_tenant_isolation ON destination_posts
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
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON destination_posts")
    op.drop_table("destination_posts")
