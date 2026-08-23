"""Fiber claiming — stop durable steps from running twice.

``resume_due_fibers`` selected every fiber in ``('running','pending')`` with no lock, no
limit, and no claim, while a fiber stayed ``running`` for the duration of its step. The
scheduler fires every minute, so a step still running at the next tick was picked up and
invoked again — concurrently, on a single node, and guaranteed with two workers. The
``invoke`` op runs a full agent with tools, so that means duplicated side effects.

Revision ID: 0008_fiber_leases
Revises: 0007_approval_consumed_at
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_fiber_leases"
down_revision: str | None = "0007_approval_consumed_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fibers", sa.Column("lease_owner", sa.Text(), server_default="", nullable=False))
    op.add_column("fibers", sa.Column("lease_until", sa.BigInteger(), nullable=True))
    op.add_column(
        "fibers",
        sa.Column("version", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index("idx_fibers_due", "fibers", ["status", "wake_at", "lease_until"])


def downgrade() -> None:
    op.drop_index("idx_fibers_due", table_name="fibers")
    op.drop_column("fibers", "version")
    op.drop_column("fibers", "lease_until")
    op.drop_column("fibers", "lease_owner")
