"""Fibers carry the delivery state of their completion webhooks.

Revision ID: 0019_fiber_webhooks
Revises: 0018_artifact_ledger
Create Date: 2026-09-27

A durable run that names `spec.execution.webhooks` is announced to those operator-registered
endpoints when it reaches a terminal status, by a worker sweep rather than the API replica
that accepted it — which may be gone by then. The delivery state lives on the run's own row
(no second store): `webhook_status` is null for a run with no webhooks, `pending` until every
endpoint has answered, then `delivered` or `dead`. `webhook_due_at` is when the sweep may try
next, and doubles as its claim. `webhook_state` holds each endpoint's attempts and last result,
in its own column so a delivery write never contends with the fiber's versioned `state_json`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_fiber_webhooks"
down_revision: str | None = "0018_artifact_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fibers", sa.Column("webhook_status", sa.Text(), nullable=True))
    op.add_column("fibers", sa.Column("webhook_due_at", sa.BigInteger(), nullable=True))
    op.add_column(
        "fibers",
        sa.Column(
            "webhook_state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    # Partial: only rows with a delivery outstanding, which is a sliver of the table.
    op.create_index(
        "idx_fibers_webhook_due",
        "fibers",
        ["webhook_due_at"],
        postgresql_where=sa.text("webhook_status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("idx_fibers_webhook_due", table_name="fibers")
    op.drop_column("fibers", "webhook_state")
    op.drop_column("fibers", "webhook_due_at")
    op.drop_column("fibers", "webhook_status")
