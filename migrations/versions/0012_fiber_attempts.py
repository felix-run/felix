"""Fibers count consecutive failed steps.

Revision ID: 0012_fiber_attempts
Revises: 0011_usage_cost
Create Date: 2026-09-05

A step that raised outside the invoke's own handler (a save, a lease write) was released
and picked up again on the next tick, forever: no counter, no backoff, no terminal state,
so a deterministically failing fiber ran once a minute until `expires_at`. `attempts` is
the counter; the scheduler backs off on it and marks the fiber `dead` at the ceiling.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_fiber_attempts"
down_revision: str | None = "0011_usage_cost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fibers", sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False))


def downgrade() -> None:
    op.drop_column("fibers", "attempts")
