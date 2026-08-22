"""Usage events — token meters flushed from the worker.

Revision ID: 0004_usage_events
Revises: 0003_session_tree
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_usage_events"
down_revision: str | None = "0003_session_tree"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.Column("manifest_id", sa.Text(), server_default="", nullable=False),
        sa.Column("model_id", sa.Text(), server_default="", nullable=False),
        sa.Column("kind", sa.Text(), server_default="tokens", nullable=False),
        sa.Column("tokens_input", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tokens_output", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_creation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_read", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "meta_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
    )
    op.create_index("idx_usage_tenant_ts", "usage_events", ["tenant_id", "ts"])


def downgrade() -> None:
    op.drop_index("idx_usage_tenant_ts", table_name="usage_events")
    op.drop_table("usage_events")
