"""Add a2a_tasks for durable A2A task persistence.

Revision ID: 0002_a2a_tasks
Revises: 0001_baseline
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_a2a_tasks"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "a2a_tasks",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("manifest_id", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "status_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "artifacts_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "task_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
    )
    op.create_index("idx_a2a_tasks_tenant_updated", "a2a_tasks", ["tenant_id", "updated_at"])


def downgrade() -> None:
    op.drop_index("idx_a2a_tasks_tenant_updated", table_name="a2a_tasks")
    op.drop_table("a2a_tasks")
