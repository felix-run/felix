"""Durable runs: record the thread a fiber writes to.

Revision ID: 0034_fiber_thread
Revises: 0033_skill_owner
Create Date: 2026-10-08

A durable chat's thread lived only inside `state_json.steps[0].thread_id`, so nothing could ask
"does this thread already have a run?" -- and the harness started a second run on a thread whose
first was still going. The two appended to one log, each re-doing what it could not see the
other doing (felix-run/felix#529). `fibers.thread_id` is that question's column, with a partial
index over the runs that can still be in flight, which is all the send path and the snapshot ask.

The backfill covers rows a deployment has in flight while it rolls, so a send arriving during
the deploy is refused against them too; finished rows are filled as well, which costs nothing
and keeps the column honest. It runs under the RLS bypass, as `0029`'s does: the table is under
forced RLS, so a migration role that is not a superuser would otherwise update nothing.

Adding a nullable column is catalog-only. The index is built without CONCURRENTLY, under the
same SHARE lock every index here is built under, and `fibers` holds one row per durable run.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_fiber_thread"
down_revision: str | None = "0033_skill_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "idx_fibers_thread_active"


def upgrade() -> None:
    op.add_column("fibers", sa.Column("thread_id", sa.Text(), nullable=True))
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.execute(
        """
        UPDATE fibers
           SET thread_id = state_json -> 'steps' -> 0 ->> 'thread_id'
         WHERE thread_id IS NULL
           AND kind = 'durable_chat'
           AND COALESCE(state_json -> 'steps' -> 0 ->> 'thread_id', '') <> ''
        """
    )
    # Off again at once: nothing after the backfill in this transaction may write across tenants.
    op.execute("SELECT set_config('app.rls_bypass', 'off', true)")
    op.create_index(
        _INDEX,
        "fibers",
        ["tenant_id", "thread_id"],
        postgresql_where=sa.text(
            "thread_id IS NOT NULL AND status NOT IN ('completed', 'failed', 'expired', 'dead')"
        ),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="fibers")
    op.drop_column("fibers", "thread_id")
