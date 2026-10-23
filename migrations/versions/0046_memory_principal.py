"""Long-term memory per person: the pool a row belongs to.

Revision ID: 0046_memory_principal
Revises: 0045_workflow_owner
Create Date: 2026-10-22

Memory was pooled per `(tenant, manifest)`. A tenant is an org and holds several people, so one
person's `user.*` facts mixed with another's, and each agent learnt the same person separately.
`principal` names the person a row belongs to (`memory.store.MemoryPool`); `''` is an agent's own
pool, which is every existing row -- so nothing moves until an operator runs
`felix memory assign`.

Expand-only: a `NOT NULL DEFAULT ''` column is catalog-only on Postgres 11+, so adding it rewrites
nothing. RLS is unchanged: the policy isolates tenants and covers every column; isolating one
person from another inside a tenant is the store's filter on this column.

Two indexes for a person pool's reads, mirroring the agent pool's (`idx_memory_active_id`,
`idx_memory_topic`) with `principal` where those have `manifest_id`. A person's read is
`principal = P OR (principal = '' AND manifest_id = M)`, which Postgres plans as a BitmapOr of
these and the existing ones. Built `CONCURRENTLY`, outside the migration's transaction, as `0035`
does: capture writes to this table on every turn.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_memory_principal"
down_revision: str | None = "0045_workflow_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE = "idx_memory_person_active_id"
_TOPIC = "idx_memory_person_topic"


def upgrade() -> None:
    op.add_column("memory_vectors", sa.Column("principal", sa.Text(), server_default="", nullable=False))
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_ACTIVE} ON memory_vectors "
            '(tenant_id, principal, status, created_at DESC, id COLLATE "C" DESC)'
        )
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_TOPIC} ON memory_vectors "
            "(tenant_id, principal, topic_key, status) WHERE topic_key IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_TOPIC}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_ACTIVE}")
    op.drop_column("memory_vectors", "principal")
