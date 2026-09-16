"""Record what each tenant has uploaded, so it can be counted and collected.

Revision ID: 0016_attachment_ledger
Revises: 0015_approval_reason_and_call
Create Date: 2026-09-16

`POST /files` stored bytes under `attachments/{tenant}/{id}` and nothing recorded that it
had. Two consequences, and the security review of #239 named the first as the condition on
granting `files:write` to an untrusted tenant: `MAX_ATTACHMENT_BYTES` caps one upload and
nothing capped how many, and `jobs/retention.py` collected no objects at all, so
`attachments/` joined `artifacts/` as a prefix that only ever grew.

Both need the same thing and neither can get it from the object store: the `ObjectStore`
Protocol has no `list`, deliberately, because S3 and GCS charge for it and the filesystem
backend would have to walk a directory tree per request. So the count lives in Postgres
beside the bytes.

This is a ledger, not the system of record — the bytes are. Ordering is chosen so drift
falls the recoverable way: the row is written *before* the object and deleted *after* it, so
every interruption leaves a row whose bytes may not exist. That over-counts, which is visible
here and collectable by age. The opposite order leaves bytes with no row, and those are
invisible to the count and to the sweep alike, because both read rows.

Empty on every existing deployment, and deliberately not backfilled: a backfill would have
to list the object store, which is the operation this table exists because we do not have.
Uploads predating it are invisible to the quota and to the sweep, which is the same position
they were in before this migration and not a worse one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_attachment_ledger"
down_revision: str | None = "0015_approval_reason_and_call"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("file_id", sa.Text(), primary_key=True),
        # The decoded length, which is what the disk holds — not the base64 the caller
        # sent, which is a third larger and is a property of the request.
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.Text(), server_default="", nullable=False),
        sa.Column("filename", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )

    # The sweep asks "what has this tenant got older than X", and the quota asks for a
    # SUM over the same prefix. One index serves both.
    op.execute("CREATE INDEX IF NOT EXISTS idx_attachments_tenant_age ON attachments (tenant_id, created_at)")

    # Same policy shape as every other tenant table (`0006_tenant_rls`). Applied
    # unconditionally so the schema is reproducible; the application declares
    # `app.rls_bypass` when `FELIX_DATABASE_RLS` is off.
    op.execute("ALTER TABLE attachments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE attachments FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON attachments")
    op.execute(
        """
        CREATE POLICY felix_tenant_isolation ON attachments
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
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON attachments")
    op.execute("DROP INDEX IF EXISTS idx_attachments_tenant_age")
    op.drop_table("attachments")
