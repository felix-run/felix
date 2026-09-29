"""Indexes that match the listings' orderings, tiebreak included, so no listing sorts.

Revision ID: 0020_ordering_indexes
Revises: 0019_fiber_webhooks
Create Date: 2026-09-28

Every listing that pages or cuts now orders all the way down to a unique key — the timestamp,
then the id, compared byte for byte (`COLLATE "C"`) so Postgres and the memory twin agree
(#364-#366). The indexes stopped at the timestamp, so each plan was an index scan under an
Incremental Sort of the tie group, or, for the job run history and the fiber claim, a full sort
of everything the filter matched before the `LIMIT` applied. An index can only serve an
`ORDER BY` whose expressions it matches, collation included, so each one here spells the
listing's ordering exactly.

Five replace an index that is a prefix of theirs; keeping both would pay the write twice for a
read the new one already serves. The fiber claim's is partial on the claim's own status filter.

Built with plain `CREATE INDEX`, as every migration here is: each takes a write lock on its
table for as long as it builds. On a large `audit_events` or `usage_events`, run the upgrade in a
quiet window.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_ordering_indexes"
down_revision: str | None = "0019_fiber_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (new index, table, definition, the index it replaces or None, that index's definition)
INDEXES = [
    (
        "idx_audit_tenant_ts_id",
        "audit_events",
        '(tenant_id, ts DESC, id COLLATE "C" DESC)',
        "idx_audit_tenant_ts",
        "(tenant_id, ts)",
    ),
    (
        "idx_usage_tenant_ts_id",
        "usage_events",
        '(tenant_id, ts DESC, id COLLATE "C" DESC)',
        "idx_usage_tenant_ts",
        "(tenant_id, ts)",
    ),
    (
        "idx_approvals_tenant_status_created_id",
        "approvals",
        '(tenant_id, status, created_at DESC, id COLLATE "C" DESC)',
        "idx_approvals_tenant_status",
        "(tenant_id, status, created_at)",
    ),
    (
        "idx_plans_tenant_updated_id",
        "plans",
        '(tenant_id, updated_at DESC, id COLLATE "C" DESC)',
        "idx_plans_tenant_updated",
        "(tenant_id, updated_at)",
    ),
    (
        "idx_memory_active_id",
        "memory_vectors",
        '(tenant_id, manifest_id, status, created_at DESC, id COLLATE "C" DESC)',
        "idx_memory_active",
        "(tenant_id, manifest_id, status, created_at DESC)",
    ),
    (
        "idx_job_runs_history",
        "job_runs",
        "(tenant_id, job_name, started_at DESC, run_id DESC)",
        None,
        None,
    ),
]

FIBER_CLAIM = (
    "CREATE INDEX IF NOT EXISTS idx_fibers_claim ON fibers (updated_at) "
    "WHERE status IN ('running', 'pending', 'sleeping')"
)


def upgrade() -> None:
    for name, table, columns, old, _old_columns in INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {columns}")
        if old:
            op.execute(f"DROP INDEX IF EXISTS {old}")
    op.execute(FIBER_CLAIM)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_fibers_claim")
    for name, table, _columns, old, old_columns in reversed(INDEXES):
        if old:
            op.execute(f"CREATE INDEX IF NOT EXISTS {old} ON {table} {old_columns}")
        op.execute(f"DROP INDEX IF EXISTS {name}")
