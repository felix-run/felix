"""SQLAlchemy models aligned with Alembic baseline + API route shapes."""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    Index,
    Integer,
    Text,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AuditEvent(Base):
    __tablename__ = "audit_events"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    principal_subj: Mapped[str] = mapped_column(Text, server_default="", default="")
    status: Mapped[str] = mapped_column(Text, server_default="", default="")
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (Index("idx_audit_tenant_ts", "tenant_id", "ts"),)


class Plan(Base):
    __tablename__ = "plans"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    manifest_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (Index("idx_plans_tenant_updated", "tenant_id", "updated_at"),)


class Job(Base):
    __tablename__ = "jobs"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    schedule: Mapped[str] = mapped_column(Text, server_default="", default="")
    manifest_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    last_run_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    next_run_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_status: Mapped[str] = mapped_column(Text, server_default="", default="")
    last_error: Mapped[str] = mapped_column(Text, server_default="", default="")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)


class Approval(Base):
    __tablename__ = "approvals"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    manifest_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    call_signature: Mapped[str] = mapped_column(Text, nullable=False)
    args_json: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)
    principal_subj: Mapped[str] = mapped_column(Text, server_default="", default="")
    status: Mapped[str] = mapped_column(Text, server_default="pending", default="pending")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decided_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decided_by: Mapped[str] = mapped_column(Text, server_default="", default="")
    decision_note: Mapped[str] = mapped_column(Text, server_default="", default="")
    edited_args_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rule_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    # Set when a one_shot grant is spent, so it cannot authorize a second identical call.
    consumed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (Index("idx_approvals_tenant_status", "tenant_id", "status", "created_at"),)


class SkillActivation(Base):
    __tablename__ = "skill_activation"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    manifest_id: Mapped[str] = mapped_column(Text, primary_key=True)
    active_skills: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class OAuthTokenCache(Base):
    __tablename__ = "oauth_token_cache"

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scope: Mapped[str] = mapped_column(Text, server_default="", default="")


class ManifestRow(Base):
    __tablename__ = "manifests"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, server_default="", default="")
    comment: Mapped[str] = mapped_column(Text, server_default="", default="")


class ManifestActive(Base):
    __tablename__ = "manifest_active"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, server_default="", default="")
    canary_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canary_weight: Mapped[int] = mapped_column(Integer, server_default="0", default=0)

    __table_args__ = (
        CheckConstraint("canary_weight BETWEEN 0 AND 100", name="ck_manifest_active_canary_weight"),
    )


class EvalDataset(Base):
    __tablename__ = "eval_datasets"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str] = mapped_column(Text, server_default="", default="")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class EvalDatasetItem(Base):
    __tablename__ = "eval_dataset_items"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    dataset_name: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_input: Mapped[str] = mapped_column(Text, nullable=False)
    rubric_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    dataset_name: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_manifest: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(Text, server_default="in_progress", default="in_progress")
    pass_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    fail_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    scores_json: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    manifest_version: Mapped[int | None] = mapped_column(Integer, nullable=True)


class MemoryVector(Base):
    __tablename__ = "memory_vectors"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    content: Mapped[str] = mapped_column(Text, server_default="", default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), default=0)
    last_used_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thread_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    # Supersession has two axes and they must never disagree. `status` is current
    # state and is the only one that can express "forgotten", which has no turn
    # endpoint; `superseded_seq` closes the row's validity interval in turn time,
    # which is what makes an as-of query possible. Every write that closes a memory
    # sets both in one statement.
    topic_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, server_default="active", default="active")
    superseded_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    importance: Mapped[float] = mapped_column(Float, server_default=text("0.5"), default=0.5)
    origin_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    superseded_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_model: Mapped[str] = mapped_column(Text, server_default="", default="")
    # Deprecated: never populated, and superseded by the pgvector `embedding` column.
    # Dropped once the backfill in the follow-up has run everywhere.
    embedding_json: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    # `embedding vector(N)`, `content_tsv` and `topic_tsv` are deliberately absent.
    # They are created by migration 0009 and reached through `text()`, the same way
    # `session_events.content_tsv` is: a generated column has no writable ORM
    # representation, and pinning `vector(N)` here would hardcode a deploy-time
    # dimension into Python.


class SessionEventRow(Base):
    __tablename__ = "session_events"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[float] = mapped_column(Float, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (Index("idx_session_events_tenant_thread", "tenant_id", "thread_id", "seq"),)


class ThreadState(Base):
    """Leaf pointer + labels for tree-structured sessions."""

    __tablename__ = "thread_state"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    leaf_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    labels_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Fiber(Base):
    __tablename__ = "fibers"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="step")
    status: Mapped[str] = mapped_column(Text, server_default="pending", default="pending")
    state_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    wake_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Claim: who is stepping this fiber and until when. A step that outlives its lease
    # is reclaimable, so a crashed worker does not strand the fiber forever.
    lease_owner: Mapped[str] = mapped_column(Text, server_default="", default="")
    lease_until: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Optimistic concurrency: _save_fiber is a read-modify-write, and a lost update
    # can rewind `cursor` and replay a step that already ran.
    version: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), default=0)

    __table_args__ = (Index("idx_fibers_due", "status", "wake_at", "lease_until"),)


class UsageEvent(Base):
    __tablename__ = "usage_events"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    manifest_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    model_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    kind: Mapped[str] = mapped_column(Text, server_default="tokens", default="tokens")
    tokens_input: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    cache_creation: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    cache_read: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    meta_json: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)

    __table_args__ = (Index("idx_usage_tenant_ts", "tenant_id", "ts"),)


class A2ATask(Base):
    """Persisted A2A task (message/send → tasks/get across api/worker)."""

    __tablename__ = "a2a_tasks"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    manifest_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    status_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    artifacts_json: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    task_json: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (Index("idx_a2a_tasks_tenant_updated", "tenant_id", "updated_at"),)


class JobRun(Base):
    __tablename__ = "job_runs"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_name: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(Text, server_default="ok", default="ok")
    error: Mapped[str] = mapped_column(Text, server_default="", default="")
    result_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )


class DocumentChunk(Base):
    """One retrievable slice of an ingested document.

    Chunks rather than documents, in one table rather than two. Retrieval returns chunks, so
    a `documents` parent would exist only to hold a title and a source — both of which are
    denormalised here instead, because every read path wants them alongside the chunk and no
    write path updates a document without rewriting all of its chunks anyway. `doc_id` groups
    them: listing is `DISTINCT doc_id`, replacing is delete-then-insert under one transaction.

    The `embedding vector(...)` column and `content_tsv` are added by migration `0010`, the
    way `memory_vectors` does it — SQLAlchemy cannot express either a pgvector type or a
    generated column, and the in-memory twin needs neither.
    """

    __tablename__ = "document_chunks"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    doc_id: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    title: Mapped[str] = mapped_column(Text, server_default="", default="")
    # Where the text came from — a URL, a path, an object-store key. Opaque to the harness
    # and shown to whoever reads a hit, so a claim can be traced to its origin.
    source: Mapped[str] = mapped_column(Text, server_default="", default="")
    content: Mapped[str] = mapped_column(Text, server_default="", default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_model: Mapped[str] = mapped_column(Text, server_default="", default="")


__all__ = [
    "A2ATask",
    "Approval",
    "AuditEvent",
    "Base",
    "DocumentChunk",
    "EvalDataset",
    "EvalDatasetItem",
    "EvalRun",
    "Fiber",
    "Job",
    "JobRun",
    "ManifestActive",
    "ManifestRow",
    "MemoryVector",
    "OAuthTokenCache",
    "Plan",
    "SessionEventRow",
    "SkillActivation",
    "ThreadState",
    "UsageEvent",
]
