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
    Numeric,
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

    # Matches `keyset_order` exactly, collation included, or the planner cannot use it to sort
    # (migration 0020).
    __table_args__ = (
        Index("idx_audit_tenant_ts_id", "tenant_id", text("ts DESC"), text('id COLLATE "C" DESC')),
    )


class Plan(Base):
    __tablename__ = "plans"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    manifest_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    # The conversation the plan was written in, as `{tenant}:{suffix}`; `''` when it was
    # written outside a chat context or before the column existed (`0025`).
    thread_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index(
            "idx_plans_tenant_updated_id", "tenant_id", text("updated_at DESC"), text('id COLLATE "C" DESC')
        ),
        Index(
            "idx_plans_tenant_thread_updated_id",
            "tenant_id",
            "thread_id",
            text("updated_at DESC"),
            text('id COLLATE "C" DESC'),
        ),
    )


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
    # Why the gate fired, in the operator's own words -- a rule sends its `description`, a
    # command-screening gate sends the finding. The frame has carried it since #210 and the
    # row did not, so an approval found by polling named a rule and explained nothing.
    reason: Mapped[str] = mapped_column(Text, server_default="", default="")
    # Attribution, not ownership: `create_pending` reuses a pending row keyed on
    # (tenant, manifest, tool, call_signature), so this names whichever thread asked first.
    thread_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    # The tool call this approval is blocking, so a polled approval can be attached to the
    # card on screen rather than floating free. Empty for a gated tool called outside a
    # tool loop, and -- like `thread_id` -- it names whichever call created the row.
    tool_call_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    # Set when a one_shot grant is spent, so it cannot authorize a second identical call.
    consumed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        Index(
            "idx_approvals_tenant_status_created_id",
            "tenant_id",
            "status",
            text("created_at DESC"),
            text('id COLLATE "C" DESC'),
        ),
    )


class SkillActivation(Base):
    __tablename__ = "skill_activation"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    manifest_id: Mapped[str] = mapped_column(Text, primary_key=True)
    active_skills: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SkillRow(Base):
    """A skill in a tenant's library. `live_version` is what catalogs load; null is archived."""

    __tablename__ = "skill"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Whose library the skill is in: `""` is the tenant's own (every skill before `0033`), anything
    # else is one principal's personal namespace (`library_keys.personal_owner`). Part of the key, so a
    # personal skill and an org skill may share a name.
    owner: Mapped[str] = mapped_column(Text, primary_key=True, server_default="")
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    live_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SkillVersionRow(Base):
    """One immutable version of a library skill. Content lives in the object store at
    `library_keys.library_object_key`; this row is its review record."""

    __tablename__ = "skill_version"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner: Mapped[str] = mapped_column(Text, primary_key=True, server_default="")
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    parent_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    origin_manifest_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    quality_score: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    security_status: Mapped[str] = mapped_column(Text, nullable=False)
    security_issues: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    review_checks: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decided_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Set the first time this version goes live, and never cleared. What separates a version
    # a rollback may return to from a draft that was rejected: both are `archived`.
    published_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Where an imported version came from (`skills/importer.py`); null unless `source` is
    # `import`. The tree hash is what a re-import compares to decide nothing changed.
    origin_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_commit: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_tree_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_license: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the newest commit touching the skill's folder at `origin_commit` was committed (ms).
    origin_committed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # True for an import and for every version built on one: third-party text is still in it, so
    # the publish gate judges it as an import and activation screens it as untrusted output.
    lineage_import: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    # Set on the version an operator's adopt saved (`library.adopt`): the import-lineage version
    # whose files it carries byte for byte, vouched for with the row's `reason` by its `author`.
    # Its own `lineage_import` is false; the copy rule still counts its files as imported.
    adopted_from: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'published', 'archived')", name="ck_skill_version_status"),
        CheckConstraint("source IN ('agent', 'operator', 'import')", name="ck_skill_version_source"),
    )


class SkillFileRow(Base):
    """One file of a skill version: its digest and size. The bytes are in the object store."""

    __tablename__ = "skill_file"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner: Mapped[str] = mapped_column(Text, primary_key=True, server_default="")
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The sha256 of a text file's normalized text (`skills/copy_rule.py`, a stored format: NFKC,
    # format characters removed, casefolded, whitespace collapsed; a SKILL.md's body only). Null
    # for a binary asset, and for every row written before `0032`, which stays null.
    normalized_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)

    # `holds_imported_file`: an agent's save looks its files' digests up across the tenant, by
    # bytes (0028) and normalized text (0032).
    __table_args__ = (
        Index("idx_skill_file_tenant_sha256", "tenant_id", "sha256"),
        Index("idx_skill_file_tenant_normalized_sha256", "tenant_id", "normalized_sha256"),
    )


class SkillFeedbackRow(Base):
    """Feedback on one version of a library skill, from a person or an agent.

    Nothing rewrites a skill from feedback until a person accepts it with `improve`; the
    worker then claims the row (`claim_token`, kept alive by `heartbeat_at`) and records the draft it produced
    (`result_version`) or why it could not (`error`)."""

    __tablename__ = "skill_feedback"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    target_version: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    principal: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_patch: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    improve: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false(), default=False)
    result_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claimed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    decided_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'applied', 'failed')",
            name="ck_skill_feedback_status",
        ),
        CheckConstraint("source IN ('human', 'agent')", name="ck_skill_feedback_source"),
    )


class SkillEvalRow(Base):
    """One baseline-versus-with-skill evaluation of a library skill version."""

    __tablename__ = "skill_eval"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    scenario_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    scenarios: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    baseline_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    with_skill_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uplift: Mapped[int | None] = mapped_column(Integer, nullable=True)
    results: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), default=list)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    judge_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_skill_eval_status"
        ),
    )


class SkillPolicyRow(Base):
    """A tenant's skill publish policy. Absent means `FELIX_SKILL_PUBLISH_*` decides."""

    __tablename__ = "skill_policy"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    min_quality: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    block_on_advisory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    require_eval: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    min_eval_uplift: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Days a skill's folder must have been unchanged on GitHub before it may be imported.
    import_min_age_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False, server_default="")


class SkillImportSightingRow(Base):
    """When this tenant first saw a source's skill folder with this tree digest: the import
    cooldown's clock (`skills/sighting_store.py`), which an upstream committer cannot set."""

    __tablename__ = "skill_import_sighting"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    origin_source: Mapped[str] = mapped_column(Text, primary_key=True)
    tree_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    first_seen_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SkillUpstreamRow(Base):
    """What an imported library skill's origin held when Felix last looked
    (`skills/upstream_store.py`): the upstream listing and the library detail read it rather than
    asking GitHub."""

    __tablename__ = "skill_upstream"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    origin_source: Mapped[str] = mapped_column(Text, nullable=False)
    origin_ref: Mapped[str] = mapped_column(Text, nullable=False)
    upstream_commit: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_tree_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The `skill.update_available` notification (`skills/update_notify.py`, migration 0030): the
    # newest upstream digest one was queued for, and its delivery, kept beside the check it is
    # about as a run's completion webhook is kept on the run.
    notified_tree_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    notify_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    notify_due_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    notify_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    notify_claim_until: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Bumped on every queue: part of the `webhook-id`, and what a delivery's save is guarded on.
    notify_generation: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    # When the check that queued it ran: an older check never replaces it.
    notify_checked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    notify_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (
        Index(
            "idx_skill_upstream_notify_due",
            "notify_due_at",
            "tenant_id",
            "name",
            postgresql_where=text("notify_status = 'pending'"),
        ),
    )


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
    # The subset of `fail_count` that never reached the scorer — the item raised. A run
    # whose failures are all errors is a broken dataset, not a model regression.
    error_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
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
    # No secondary index: the primary key is `(tenant_id, thread_id, seq)`, which is every read
    # this table serves. The baseline built a second btree on the same columns; 0036 dropped it.


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

    # `GET /chat/sessions` pages a tenant's threads newest first (migration 0037).
    __table_args__ = (
        Index(
            "idx_thread_state_tenant_updated_thread",
            "tenant_id",
            text("updated_at DESC"),
            text('thread_id COLLATE "C" DESC'),
        ),
    )


class Fiber(Base):
    __tablename__ = "fibers"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="step")
    status: Mapped[str] = mapped_column(Text, server_default="pending", default="pending")
    # The thread a durable chat writes to, so a thread's run can be found and a second one
    # refused (felix-run/felix#529). Null for a fiber with no thread of its own. Migration 0034.
    thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    # Consecutive failed steps. Reset by a step that completes; at the ceiling the fiber is `dead`.
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    # Completion webhooks (`spec.execution.webhooks`): null when the run names none, else
    # `pending` → `delivered` | `dead`. `webhook_due_at` is the sweep's next try and its claim;
    # `webhook_state` is per endpoint, kept out of the versioned `state_json`. Migration 0019.
    webhook_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_due_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    webhook_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (
        Index("idx_fibers_due", "status", "wake_at", "lease_until"),
        # The claim orders by `updated_at` under a status filter; partial on that filter.
        Index(
            "idx_fibers_claim",
            "updated_at",
            postgresql_where=text("status IN ('running', 'pending', 'sleeping')"),
        ),
        Index(
            "idx_fibers_webhook_due",
            "webhook_due_at",
            postgresql_where=text("webhook_status = 'pending'"),
        ),
        # A thread's run in flight (`active_fiber_for_thread`): read on every send and snapshot.
        Index(
            "idx_fibers_thread_active",
            "tenant_id",
            "thread_id",
            postgresql_where=text(
                "thread_id IS NOT NULL AND status NOT IN ('completed', 'failed', 'expired', 'dead')"
            ),
        ),
    )


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
    # `model_id` is the logical route name the operator configured; this is the provider's
    # id the row was priced by. Cost is fixed at write time — the only moment the wire id,
    # the rates and any manifest price override are all in hand.
    wire_model_id: Mapped[str] = mapped_column(Text, server_default="", default="")
    cost_usd: Mapped[float] = mapped_column(Numeric(14, 8, asdecimal=False), server_default="0", default=0)
    meta_json: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)
    # The `{tenant}:{suffix}` thread the call was made on — the spelling the audit payload's
    # `thread_id` uses, so the two join — or `''` for a call outside any thread.
    thread_id: Mapped[str] = mapped_column(Text, server_default="", default="")

    __table_args__ = (
        Index("idx_usage_tenant_ts_id", "tenant_id", text("ts DESC"), text('id COLLATE "C" DESC')),
        Index("idx_usage_tenant_thread_ts", "tenant_id", "thread_id", text("ts DESC")),
    )


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

    # `list_runs` orders a job's history newest first; without this it sorted all of it.
    __table_args__ = (
        Index("idx_job_runs_history", "tenant_id", "job_name", text("started_at DESC"), text("run_id DESC")),
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


class AttachmentRow(Base):
    """One stored upload, recorded so the object store can be counted and collected.

    The object store is the system of record for the *bytes*; this is a ledger beside it,
    and it exists because the `ObjectStore` Protocol has no `list`. Two things need one:
    a per-tenant quota has to know the current total before admitting the next upload, and
    retention has to find what is old enough to drop -- `attachments/` was a prefix nothing
    ever collected, so on the default `fs` backend one tenant filling the disk degraded
    artifact spill and manifest storage for every tenant on the host.

    `size_bytes` is the decoded length, which is what the disk actually holds -- not the
    base64 the caller sent, which is a third larger and is a property of the request rather
    than of the stored object.

    A ledger beside a store can drift, and the direction is chosen rather than accidental:
    the row is written *before* the object and deleted *after* it, so every interruption
    leaves the same shape -- a row whose bytes may not exist. That over-counts, which an
    operator can see here and the sweep collects by age, and re-deleting absent bytes is a
    no-op on every backend. The opposite order is unrecoverable in both directions: bytes
    with no row are invisible to `tenant_attachment_bytes` *and* to `expired_attachments`,
    because both read rows, on a store whose Protocol has no `list`.
    """

    __tablename__ = "attachments"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    file_id: Mapped[str] = mapped_column(Text, primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, server_default="", default="")
    filename: Mapped[str] = mapped_column(Text, server_default="", default="")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ArtifactRow(Base):
    """One spilled tool output, recorded so retention can find it.

    The artifact twin of `AttachmentRow`, for the same reason: the `ObjectStore` Protocol
    has no `list`, so an object nothing records is an object nothing can ever collect. The
    ordering rule is the same too -- row before bytes on write, bytes before row on delete --
    so drift is always a row whose objects may be absent, which the sweep clears by age.

    One row covers both objects a spill writes: `{id}.txt` and the `{id}.owner` record that
    holds `read_artifact` to the conversation that spilled it.
    """

    __tablename__ = "artifacts"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    manifest_id: Mapped[str] = mapped_column(Text, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(Text, primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PushSubscriptionRow(Base):
    """A browser that asked to be told when a run is waiting on a person.

    `id` is the sha256 of `endpoint`, so a browser re-subscribing replaces its own row and
    never another tenant's. `p256dh` and `auth` are what `PushSubscription.toJSON()` hands
    over: the browser's key and secret, which let the harness encrypt *to* it.
    """

    __tablename__ = "push_subscriptions"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    principal_subj: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_ok_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class GitHubConnectionRow(Base):
    """One person's GitHub connection in a tenant: their refresh token, sealed.

    Both tokens are AES-GCM sealed with FELIX_GITHUB_TOKEN_KEY, the tenant, the user id and the
    column bound in, so a value copied to another row or column does not open
    (`felix.auth.github_connections`). `status` is `active` or `revoked` (GitHub refused a
    refresh); a removed connection is a deleted row.
    """

    __tablename__ = "github_connections"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    github_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    github_login: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_sealed: Mapped[str] = mapped_column(Text, nullable=False)
    # Epoch ms; 0 when GitHub did not say.
    refresh_expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    access_token_sealed: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    principal_subj: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = [
    "A2ATask",
    "Approval",
    "ArtifactRow",
    "AttachmentRow",
    "AuditEvent",
    "Base",
    "DocumentChunk",
    "EvalDataset",
    "EvalDatasetItem",
    "EvalRun",
    "Fiber",
    "GitHubConnectionRow",
    "Job",
    "JobRun",
    "ManifestActive",
    "ManifestRow",
    "MemoryVector",
    "Plan",
    "PushSubscriptionRow",
    "SessionEventRow",
    "SkillActivation",
    "SkillFileRow",
    "SkillRow",
    "SkillVersionRow",
    "ThreadState",
    "UsageEvent",
]
