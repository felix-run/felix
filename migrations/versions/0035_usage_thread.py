"""Usage: record the thread a model call was made on.

Revision ID: 0035_usage_thread
Revises: 0034_fiber_thread
Create Date: 2026-10-08

`usage_events` carried a tenant, a manifest and a model, and no thread, so "what did this
conversation cost" had no answer short of joining the audit log by timestamp. `thread_id` is the
`{tenant}:{suffix}` id the call ran on -- the spelling the audit payload's `thread_id` uses, so
the two join -- and `''` for a call made outside any thread, which is a real state rather than a
missing value, the reading `approvals.thread_id` (`0014`) and `plans.thread_id` (`0025`) give it.

Expand-only. The column is a catalog-only change (a constant default, no rewrite on PG >= 11), and
every row written before it reads `''`: there is nothing to backfill it from. The index serves
`GET /usage/threads` and the thread-filtered `GET /usage`, and is built `CONCURRENTLY` -- outside
the migration's transaction, which `CREATE INDEX CONCURRENTLY` refuses to run inside -- because
`usage_events` is the largest append-only table here and the usage flush writes to it every few
seconds.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_usage_thread"
down_revision: str | None = "0034_fiber_thread"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "idx_usage_tenant_thread_ts"


def upgrade() -> None:
    op.add_column(
        "usage_events",
        sa.Column("thread_id", sa.Text(), server_default="", nullable=False),
    )
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX,
            "usage_events",
            ["tenant_id", "thread_id", sa.text("ts DESC")],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(_INDEX, table_name="usage_events", postgresql_concurrently=True, if_exists=True)
    op.drop_column("usage_events", "thread_id")
