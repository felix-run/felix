"""Session events: drop the secondary index that duplicates the primary key.

Revision ID: 0036_session_events_one_index
Revises: 0035_usage_thread
Create Date: 2026-10-08

`session_events` has a primary key on `(tenant_id, thread_id, seq)` and the baseline also built
`idx_session_events_tenant_thread` on the same three columns, in the same order. Postgres backs a
primary key with a unique btree, so every read the secondary index could serve the primary key
already serves, and every append -- several per turn -- maintained the same btree twice.

Both directions run `CONCURRENTLY`, outside the migration's transaction, as `0035`'s index does:
`session_events` takes several appends per turn, and a plain `DROP INDEX` queues for an ACCESS
EXCLUSIVE lock behind any long read, stalling every append behind it while it waits.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0036_session_events_one_index"
down_revision: str | None = "0035_usage_thread"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "idx_session_events_tenant_thread"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(_INDEX, table_name="session_events", postgresql_concurrently=True, if_exists=True)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX,
            "session_events",
            ["tenant_id", "thread_id", "seq"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
