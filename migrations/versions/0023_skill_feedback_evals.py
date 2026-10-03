"""Feedback, evaluations and a per-tenant publish policy for the skill library.

Revision ID: 0023_skill_feedback_evals
Revises: 0022_skill_library
Create Date: 2026-10-03

`skill_feedback` holds what a person or an agent said about one version of a library skill.
An agent's feedback waits for a person; only a person's accept with `improve` lets the worker
rewrite the skill from it, and what the worker writes is a draft for review, never a publish.

`skill_eval` is one baseline-versus-with-skill evaluation of a version. The partial unique index
is what keeps a version to one queued or running evaluation, so two requests racing to queue
one collide on the index rather than both spending model calls.

`skill_policy` is a tenant's publish gate, read ahead of `FELIX_SKILL_PUBLISH_*`. Empty on every
existing deployment, and nothing to backfill: no row means the settings decide, as before.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_skill_feedback_evals"
down_revision: str | None = "0022_skill_library"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("skill_feedback", "skill_eval", "skill_policy")


def _jsonb_list(name: str) -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")
    )


def upgrade() -> None:
    op.create_table(
        "skill_feedback",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("target_version", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        # The manifest id for an agent's feedback, the principal for a person's.
        sa.Column("author", sa.Text(), nullable=False, server_default=""),
        sa.Column("principal", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("suggested_patch", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        # Set by a person's accept: the worker may rewrite the skill from this feedback.
        sa.Column("improve", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("result_version", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        # When a worker took the improvement; a claim older than the lease is taken again.
        sa.Column("claimed_at", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.BigInteger(), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'applied', 'failed')",
            name="ck_skill_feedback_status",
        ),
        sa.CheckConstraint("source IN ('human', 'agent')", name="ck_skill_feedback_source"),
    )
    op.create_table(
        "skill_eval",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("scenario_source", sa.Text(), nullable=True),
        _jsonb_list("scenarios"),
        sa.Column("baseline_score", sa.Integer(), nullable=True),
        sa.Column("with_skill_score", sa.Integer(), nullable=True),
        sa.Column("uplift", sa.Integer(), nullable=True),
        _jsonb_list("results"),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("judge_model", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.BigInteger(), nullable=True),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_skill_eval_status"
        ),
    )
    op.create_table(
        "skill_policy",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("min_quality", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("block_on_advisory", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("require_eval", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("min_eval_uplift", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False, server_default=""),
    )

    # A skill's feedback, newest first; the inbox, oldest first, by status; the agent cap,
    # which counts one manifest's pending feedback on every submit.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_feedback_name ON skill_feedback (tenant_id, name, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_feedback_status_age "
        "ON skill_feedback (tenant_id, status, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_feedback_pending_author "
        "ON skill_feedback (tenant_id, author) WHERE status = 'pending' AND source = 'agent'"
    )
    # The worker's sweep, across tenants: accepted improvements waiting for a claim.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_feedback_improve "
        "ON skill_feedback (created_at) WHERE status = 'accepted' AND improve"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_eval_version "
        "ON skill_eval (tenant_id, name, version, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_eval_due "
        "ON skill_eval (created_at) WHERE status IN ('queued', 'running')"
    )
    # One evaluation in flight per version.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_eval_in_flight "
        "ON skill_eval (tenant_id, name, version) WHERE status IN ('queued', 'running')"
    )

    # Same policy shape as every other tenant table (`0006_tenant_rls`, `0022`).
    for table in _TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS felix_tenant_isolation ON "{table}"')
        op.execute(
            f"""
            CREATE POLICY felix_tenant_isolation ON "{table}"
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
    for table in _TABLES:
        op.execute(f'DROP POLICY IF EXISTS felix_tenant_isolation ON "{table}"')
    for index in (
        "uq_skill_eval_in_flight",
        "idx_skill_eval_due",
        "idx_skill_eval_version",
        "idx_skill_feedback_improve",
        "idx_skill_feedback_pending_author",
        "idx_skill_feedback_status_age",
        "idx_skill_feedback_name",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index}")
    op.drop_table("skill_policy")
    op.drop_table("skill_eval")
    op.drop_table("skill_feedback")
