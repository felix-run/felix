"""A lease row for the `skill_jobs` sweep, replacing a session advisory lock.

Revision ID: 0024_skill_job_lease
Revises: 0023_skill_feedback_evals
Create Date: 2026-10-03

`0023` kept one sweep running at a time with `pg_try_advisory_lock`, which is held by a server
session. Behind PgBouncer in transaction mode (`deploy/docker/compose.pgbouncer.yml`) the unlock
usually runs on a different server session, returns false, and the lock leaks: workers handed
the leaked session take it again (advisory locks are reentrant per session), so two sweeps run
at once, and every other worker skips until PgBouncer recycles the connection.

A row lives in the database rather than on a connection, so it holds whichever server session
each statement lands on. `holder` is a random token per sweep, `until_ms` the epoch-ms the
lease lapses at; taking it, renewing it and releasing it are each one statement in a
transaction of its own.

Deliberately not a tenant table: one row is the whole sweep across every tenant, so there is
no tenant to scope it to, and like `memory_vector_config` (`0009`) it has no `tenant_id` and no
RLS. It holds no tenant data -- a name, a random token and a time. Empty on every existing
deployment; the first sweep after the upgrade inserts its row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_skill_job_lease"
down_revision: str | None = "0023_skill_feedback_evals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_job_lease",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("holder", sa.Text(), nullable=False),
        sa.Column("until_ms", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("skill_job_lease")
