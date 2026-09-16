"""Name the conversation an approval is blocking.

Revision ID: 0014_approval_thread_id
Revises: 0013_drop_oauth_token_cache
Create Date: 2026-09-12

The `approval_required` stream frame has carried `thread_id` all along; the row behind it
never did. That split matters because the two channels do not cover the same runs: side
events are an in-process queue keyed by thread, so a **durable** run — agent in the worker,
stream served by the API — can only be seen through `GET /approvals`. The channel that is
the whole story for an unwatched run was the one with no thread on it.

Expand-only, and empty on every historical row: `''` is the harness saying it has no thread
to name, which is a real state (a gated tool called outside a chat context) rather than a
missing value. `create_pending` still reuses a pending row across threads, so this is the
*originating* thread — attribution, not ownership. See the column comment in `db/models.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_approval_thread_id"
down_revision: str | None = "0013_drop_oauth_token_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("thread_id", sa.Text(), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("approvals", "thread_id")
