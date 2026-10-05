"""Notify an operator's webhook when an imported skill's upstream content changes.

Revision ID: 0030_skill_update_notify
Revises: 0029_skill_upstream
Create Date: 2026-10-04

`skills/update_notify.py` queues a signed `skill.update_available` event when a recorded check
finds upstream files the skill's newest version does not hold, once per new digest, and a worker
sweep delivers it to the endpoints `FELIX_SKILL_UPDATE_WEBHOOKS` binds to the tenant. The delivery
lives on the `skill_upstream` row it is about, as a run's completion webhook lives on the run
(`0019`): the digest last queued (`notified_tree_hash`, what "once per digest" compares with), the
delivery's status (`pending`, `delivered`, `dead`, or `superseded` when the skill or its origin
moved past it first), when it is next due, how many tries it has had, a claim a crashed worker's
sweep lets lapse, a generation bumped on every queue (part of the event's id, and what a
delivery's save is guarded on), when the check that queued it ran (an older check never replaces
it), and per-endpoint progress with the event body (`notify_state`).

Every column is nullable or has a constant default, so adding them rewrites nothing; the table is
`0029`'s, one row per imported skill. The partial index serves the sweep's cross-tenant "pending,
due first" read and holds only pending rows. RLS is unchanged: the table's policy already covers
every column.

The downgrade drops the columns and the index; a queued notification is lost with them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_skill_update_notify"
down_revision: str | None = "0029_skill_upstream"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "skill_upstream"
_INDEX = "idx_skill_upstream_notify_due"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("notified_tree_hash", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("notify_status", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("notify_due_at", sa.BigInteger(), nullable=True))
    op.add_column(
        _TABLE, sa.Column("notify_attempts", sa.Integer(), nullable=False, server_default=sa.text("0"))
    )
    op.add_column(_TABLE, sa.Column("notify_claim_until", sa.BigInteger(), nullable=True))
    op.add_column(
        _TABLE, sa.Column("notify_generation", sa.Integer(), nullable=False, server_default=sa.text("0"))
    )
    op.add_column(_TABLE, sa.Column("notify_checked_at", sa.BigInteger(), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column(
            "notify_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} ON {_TABLE} (notify_due_at, tenant_id, name) "
        "WHERE notify_status = 'pending'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    for column in (
        "notify_state",
        "notify_checked_at",
        "notify_generation",
        "notify_claim_until",
        "notify_attempts",
        "notify_due_at",
        "notify_status",
        "notified_tree_hash",
    ):
        op.drop_column(_TABLE, column)
