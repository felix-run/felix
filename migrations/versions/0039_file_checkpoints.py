"""Record what the model's file tools changed, so `/chat/rewind` can put it back.

Revision ID: 0039_file_checkpoints
Revises: 0038_skill_version_promoted
Create Date: 2026-10-11

`tools/workspace_checkpoints.py` captures each file before and after a write, edit, delete or
rename the workspace tools make in a thread's run, and `/chat/rewind` with `restore_files` reads
the captures anchored on the branch it abandons. The bytes are in the object store, which has no
`list`, so this ledger is what names them -- for the restore and for the retention sweep -- as
`0018_artifact_ledger` does for spills: row before bytes, bytes before row on delete.

New and empty on every deployment; nothing to backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_file_checkpoints"
down_revision: str | None = "0038_skill_version_promoted"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_checkpoints",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("thread_id", sa.Text(), primary_key=True),
        sa.Column("capture_id", sa.Text(), primary_key=True),
        sa.Column("anchor_event_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("before_state", sa.Text(), nullable=False),
        sa.Column("before_sha", sa.Text(), nullable=True),
        sa.Column("after_state", sa.Text(), nullable=False),
        sa.Column("after_sha", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    # A restore reads one thread's captures by anchor; the sweep reads by age across tenants.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_checkpoints_anchor "
        "ON file_checkpoints (tenant_id, thread_id, anchor_event_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_file_checkpoints_age ON file_checkpoints (created_at)")
    # Which captures name a digest -- the ceiling's sum and every forget's "still named" -- read
    # per tenant by content, not by thread.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_checkpoints_digest ON file_checkpoints (tenant_id, before_sha)"
    )

    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0018`).
    op.execute("ALTER TABLE file_checkpoints ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE file_checkpoints FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON file_checkpoints")
    op.execute(
        """
        CREATE POLICY felix_tenant_isolation ON file_checkpoints
        USING (
            current_setting('app.rls_bypass', true) = 'on'
            OR tenant_id = current_setting('app.tenant_id', true)
        )
        WITH CHECK (
            current_setting('app.rls_bypass', true) = 'on'
            OR tenant_id = current_setting('app.tenant_id', true)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON file_checkpoints")
    op.execute("DROP INDEX IF EXISTS idx_file_checkpoints_digest")
    op.execute("DROP INDEX IF EXISTS idx_file_checkpoints_age")
    op.execute("DROP INDEX IF EXISTS idx_file_checkpoints_anchor")
    op.drop_table("file_checkpoints")
