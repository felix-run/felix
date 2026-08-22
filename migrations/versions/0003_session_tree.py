"""Session tree metadata helpers (event_id/parent_id live in event_metadata).

Revision ID: 0003_session_tree
Revises: 0002_a2a_tasks
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_session_tree"
down_revision: str | None = "0002_a2a_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Leaf pointers for tree navigation (fork/rewind). event_id/parent_id stay in JSONB.
    op.create_table(
        "thread_state",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("leaf_event_id", sa.Text(), nullable=True),
        sa.Column(
            "labels_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "thread_id"),
    )


def downgrade() -> None:
    op.drop_table("thread_state")
