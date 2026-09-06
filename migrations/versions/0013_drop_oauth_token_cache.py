"""Drop `oauth_token_cache`: never written, never read, and the one table without a tenant.

Revision ID: 0013_drop_oauth_token_cache
Revises: 0012_fiber_attempts
Create Date: 2026-09-04

The baseline created it for an OAuth client-credentials cache that was never built: no
code path inserted into or selected from it, `FELIX_OAUTH_CACHE_KEY` was read by nothing,
and the AES helper that named it had no caller. It was also a table with no `tenant_id`
column and therefore no RLS policy — an exception the coverage invariant
(`tests/unit/test_rls_coverage.py`) would otherwise have to carry, and unlike
`memory_vector_config` (one deployment-wide row) it held per-caller data. When outbound
OAuth lands it gets a tenant-keyed table and a policy in the same migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_drop_oauth_token_cache"
down_revision: str | None = "0012_fiber_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF EXISTS: an operator who stamped past 0001 or pre-dropped the table must not get
    # a failed pre-upgrade hook over a table that held nothing.
    op.execute("DROP TABLE IF EXISTS oauth_token_cache")


def downgrade() -> None:
    op.create_table(
        "oauth_token_cache",
        sa.Column("cache_key", sa.Text(), primary_key=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("scope", sa.Text(), server_default="", nullable=False),
    )
