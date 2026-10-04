"""Name the conversation a plan belongs to.

Revision ID: 0025_plan_thread_id
Revises: 0024_skill_job_lease
Create Date: 2026-10-03

A plan was keyed on (tenant, id) and nothing else, so `GET /plans` could only answer for the
whole tenant: a client showing "this run's plan" beside a conversation was showing the newest
plan from *any* conversation, and the agent's own `plan_get` with no id did the same — one
thread could read, and go on to update, the plan another thread was following.

Expand-only, and empty on every historical row: `''` is the harness saying it has no thread to
name, which is a real state (a plan written outside a chat context) rather than a missing value,
the same reading `approvals.thread_id` (`0014`) gives it. The column is a catalog-only change
(a constant default, no rewrite). The index serves the thread-filtered listing in the order the
tenant-wide one already uses, and is built `CONCURRENTLY` — outside the migration's transaction,
which `CREATE INDEX CONCURRENTLY` refuses to run inside — so it holds no lock that blocks writes
while plans are being updated by live runs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_plan_thread_id"
down_revision: str | None = "0024_skill_job_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("thread_id", sa.Text(), server_default="", nullable=False),
    )
    with op.get_context().autocommit_block():
        op.create_index(
            "idx_plans_tenant_thread_updated_id",
            "plans",
            ["tenant_id", "thread_id", sa.text("updated_at DESC"), sa.text('id COLLATE "C" DESC')],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "idx_plans_tenant_thread_updated_id",
            table_name="plans",
            postgresql_concurrently=True,
            if_exists=True,
        )
    op.drop_column("plans", "thread_id")
