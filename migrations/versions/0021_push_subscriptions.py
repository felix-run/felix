"""Store browsers that asked to be told when a run is waiting on a person.

Revision ID: 0021_push_subscriptions
Revises: 0020_ordering_indexes
Create Date: 2026-10-02

An approval or an agent's question holds a run until someone answers, and the only places
that said so were an open stream and the `/approvals` poll -- both need a page that is
running. A phone suspends a page the moment its owner switches apps, so a run could wait out
its whole deadline on a person who would have answered had anything reached them. Web Push
reaches a suspended page; this is where the harness keeps the subscriptions it pushes to.

Keyed by tenant and a hash of the endpoint rather than the endpoint itself: a re-subscribe
from the same browser replaces its row, and one tenant's browser can never overwrite
another's. Empty on every existing deployment, and nothing to backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_push_subscriptions"
down_revision: str | None = "0020_ordering_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        # sha256 of the endpoint, hex: the endpoint is a long capability URL, and a fixed-width
        # key is what an upsert and a delete both name.
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("endpoint", sa.Text(), nullable=False),
        # The browser's P-256 public key and auth secret, base64url, as `PushSubscription.toJSON()`
        # hands them over. Not secrets on their own: they let the harness encrypt *to* the browser.
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("principal_subj", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("last_ok_at", sa.BigInteger(), nullable=True),
        # Sends in a row that did not land. A subscription that never works -- keys a browser
        # did not make, a path the push service does not know -- is dropped at the limit
        # rather than holding a slot under the tenant's cap for ever.
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
    )

    # Every send reads one tenant's subscriptions, oldest first.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_push_subscriptions_tenant_age "
        "ON push_subscriptions (tenant_id, created_at)"
    )

    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0018`).
    op.execute("ALTER TABLE push_subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE push_subscriptions FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON push_subscriptions")
    op.execute(
        """
        CREATE POLICY felix_tenant_isolation ON push_subscriptions
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
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON push_subscriptions")
    op.execute("DROP INDEX IF EXISTS idx_push_subscriptions_tenant_age")
    op.drop_table("push_subscriptions")
