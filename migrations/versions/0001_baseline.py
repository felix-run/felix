"""Baseline harness tables.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "audit_events",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("manifest_id", sa.Text(), server_default="", nullable=False),
        sa.Column("principal_subj", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
    )
    op.create_index("idx_audit_tenant_ts", "audit_events", ["tenant_id", "ts"])

    op.create_table(
        "plans",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("manifest_id", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.Column("plan_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
    )
    op.create_index("idx_plans_tenant_updated", "plans", ["tenant_id", "updated_at"])

    op.create_table(
        "jobs",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("schedule", sa.Text(), server_default="", nullable=False),
        sa.Column("manifest_id", sa.Text(), server_default="", nullable=False),
        sa.Column("last_run_at", sa.BigInteger(), nullable=True),
        sa.Column("next_run_at", sa.BigInteger(), nullable=True),
        sa.Column("last_status", sa.Text(), server_default="", nullable=False),
        sa.Column("last_error", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "name"),
    )

    op.create_table(
        "job_runs",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("job_name", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), server_default="ok", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "job_name", "run_id"),
    )

    op.create_table(
        "approvals",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("manifest_id", sa.Text(), server_default="", nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("call_signature", sa.Text(), nullable=False),
        sa.Column(
            "args_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("principal_subj", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("decided_at", sa.BigInteger(), nullable=True),
        sa.Column("decided_by", sa.Text(), server_default="", nullable=False),
        sa.Column("decision_note", sa.Text(), server_default="", nullable=False),
        sa.Column("edited_args_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ttl_seconds", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.Column("rule_id", sa.Text(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
    )
    op.create_index(
        "idx_approvals_tenant_status",
        "approvals",
        ["tenant_id", "status", "created_at"],
    )

    op.create_table(
        "skill_activation",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("manifest_id", sa.Text(), nullable=False),
        sa.Column(
            "active_skills",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "manifest_id"),
    )

    op.create_table(
        "oauth_token_cache",
        sa.Column("cache_key", sa.Text(), primary_key=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("scope", sa.Text(), server_default="", nullable=False),
    )

    op.create_table(
        "manifests",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.Text(), server_default="", nullable=False),
        sa.Column("comment", sa.Text(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "name", "version"),
    )

    op.create_table(
        "manifest_active",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_by", sa.Text(), server_default="", nullable=False),
        sa.Column("canary_version", sa.Integer(), nullable=True),
        sa.Column("canary_weight", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("canary_weight BETWEEN 0 AND 100", name="ck_manifest_active_canary_weight"),
        sa.PrimaryKeyConstraint("tenant_id", "name"),
    )

    op.create_table(
        "eval_datasets",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "name"),
    )

    op.create_table(
        "eval_dataset_items",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("dataset_name", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("user_input", sa.Text(), nullable=False),
        sa.Column(
            "rubric_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "dataset_name", "item_id"),
    )

    op.create_table(
        "eval_runs",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("dataset_name", sa.Text(), nullable=False),
        sa.Column("candidate_manifest", sa.Text(), nullable=False),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), server_default="in_progress", nullable=False),
        sa.Column("pass_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fail_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "scores_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("manifest_version", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
    )

    op.create_table(
        "memory_vectors",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("manifest_id", sa.Text(), server_default="", nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("origin_seq", sa.BigInteger(), nullable=True),
        sa.Column("superseded_seq", sa.BigInteger(), nullable=True),
        sa.Column("embedding_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
    )
    op.execute("ALTER TABLE memory_vectors ADD COLUMN embedding vector(768) NOT NULL")
    op.execute("CREATE INDEX idx_memvec_hnsw ON memory_vectors USING hnsw (embedding vector_cosine_ops)")

    op.create_table(
        "session_events",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.Float(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_call_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("event_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "thread_id", "seq"),
    )
    op.create_index(
        "idx_session_events_tenant_thread",
        "session_events",
        ["tenant_id", "thread_id", "seq"],
    )

    op.create_table(
        "fibers",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column(
            "state_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("wake_at", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
    )


def downgrade() -> None:
    op.drop_table("fibers")
    op.drop_table("session_events")
    op.drop_table("memory_vectors")
    op.drop_table("eval_runs")
    op.drop_table("eval_dataset_items")
    op.drop_table("eval_datasets")
    op.drop_table("manifest_active")
    op.drop_table("manifests")
    op.drop_table("oauth_token_cache")
    op.drop_table("skill_activation")
    op.drop_table("approvals")
    op.drop_table("job_runs")
    op.drop_table("jobs")
    op.drop_table("plans")
    op.drop_table("audit_events")
