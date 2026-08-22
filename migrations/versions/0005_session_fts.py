"""Session FTS index on session_events.content.

Revision ID: 0005_session_fts
Revises: 0004_usage_events
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_session_fts"
down_revision: str | None = "0004_usage_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Generated tsvector column + GIN index for session content search.
    op.execute(
        """
        ALTER TABLE session_events
        ADD COLUMN IF NOT EXISTS content_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_session_events_content_tsv
        ON session_events USING GIN (content_tsv)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_session_events_content_tsv")
    op.execute("ALTER TABLE session_events DROP COLUMN IF EXISTS content_tsv")
