"""Thread state: an index for paging a tenant's sessions newest first.

Revision ID: 0037_thread_state_listing_index
Revises: 0036_session_events_one_index
Create Date: 2026-10-09

`GET /chat/sessions` read every `thread_state` row a tenant had, unordered, on each call. It now
returns one page ordered by `updated_at DESC, thread_id COLLATE "C" DESC` -- the keyset order
`felix.cursors` pages every time-ordered listing on -- and this index serves that order, so a page
reads only its own rows rather than sorting the tenant's whole history first.

Built `CONCURRENTLY`, outside the migration's transaction, as `0035` and `0036` are: every turn
writes the thread's row, and a plain `CREATE INDEX` blocks those writes for as long as it runs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_thread_state_listing_index"
down_revision: str | None = "0036_session_events_one_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "idx_thread_state_tenant_updated_thread"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX,
            "thread_state",
            ["tenant_id", sa.text("updated_at DESC"), sa.text('thread_id COLLATE "C" DESC')],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(_INDEX, table_name="thread_state", postgresql_concurrently=True, if_exists=True)
