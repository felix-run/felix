"""Request and response models for `routes/skill_library.py`. No routes.

Typed so the OpenAPI document describes what the library API returns -- the chat UI's library,
editor and review queue are generated against it.
"""

from __future__ import annotations

from typing import Literal

from felix.skills.format import MAX_BUNDLE_FILES
from felix.skills.library import SkillSourceKind
from felix.skills.library_store import SkillStatus
from felix.skills.publish_gate import PolicySource, SecurityStatus
from felix.skills.quality_store import EvalStatus, FeedbackSource, FeedbackStatus
from felix.skills.security import Severity
from pydantic import BaseModel, ConfigDict, Field, model_validator

REASON_LIMIT = 2000


# -- response models -------------------------------------------------------------------------


class ReviewCheckOut(BaseModel):
    id: str
    label: str = ""
    passed: bool
    message: str = ""
    weight: int = 0


class SecurityIssueOut(BaseModel):
    severity: Severity
    path: str
    message: str
    rule_id: str | None = None


class BundleIssueOut(BaseModel):
    path: str
    message: str


class VersionHeadOut(BaseModel):
    """A skill's newest version, as a listing shows it."""

    version: str
    status: SkillStatus
    source: SkillSourceKind
    quality_score: int
    security_status: SecurityStatus
    created_at: int


class SkillSummaryOut(BaseModel):
    name: str
    live_version: str | None
    latest: VersionHeadOut | None
    pending_drafts: int
    # An operator upload under this name at a key a ref reads (`library.shadows_operator_upload`):
    # an unpinned ref gets the library's live version, a ref pinning the upload's version gets
    # the upload.
    shadows_operator_upload: bool
    created_by: str
    created_at: int
    updated_at: int


class SkillListOut(BaseModel):
    items: list[SkillSummaryOut]
    # Pass back as `cursor` for the next page; null on the last. A filtered page may hold
    # fewer than `limit` items and still have a next one.
    next_cursor: str | None


class SkillVersionOut(BaseModel):
    """One version's review record, without its review detail or files."""

    name: str
    version: str
    parent_version: str | None
    status: SkillStatus
    source: SkillSourceKind
    author: str
    origin_manifest_id: str | None
    session_id: str | None
    reason: str
    description: str
    quality_score: int
    security_status: SecurityStatus
    decided_by: str | None
    decision_note: str | None
    created_at: int
    decided_at: int | None
    published_at: int | None


class SkillFileMetaOut(BaseModel):
    path: str
    sha256: str
    size: int


class SkillVersionDetailOut(SkillVersionOut):
    review_checks: list[ReviewCheckOut]
    security_issues: list[SecurityIssueOut]
    files: list[SkillFileMetaOut]
    shadows_operator_upload: bool


class SkillDetailOut(BaseModel):
    name: str
    live_version: str | None
    created_by: str
    created_at: int
    updated_at: int
    shadows_operator_upload: bool
    # Newest first.
    versions: list[SkillVersionOut]


class ReviewQueueItemOut(SkillVersionOut):
    # The skill's live version, to diff the draft against; null when nothing is live.
    live_version: str | None


class ReviewQueueOut(BaseModel):
    # Oldest first: the draft that has waited longest is the one to read next.
    items: list[ReviewQueueItemOut]
    next_cursor: str | None


class SkillFileOut(BaseModel):
    path: str
    # `base64` for a binary asset under `assets/`; `utf-8` text otherwise, secret-redacted.
    encoding: Literal["utf-8", "base64"]
    content_type: str
    content: str
    sha256: str
    size: int


class SkillPreviewOut(BaseModel):
    """What a publish of these bytes would be decided on today. Changes nothing."""

    name: str
    version: str
    status: SkillStatus
    valid: bool
    validation_issues: list[BundleIssueOut]
    quality_score: int | None
    review_checks: list[ReviewCheckOut]
    security_status: SecurityStatus | None
    security_issues: list[SecurityIssueOut]
    # True when the publish gate would let these bytes through. Not whether the version's
    # state allows a publish: only a draft publishes, and only a once-published version rolls back.
    policy_passes: bool
    reasons: list[str]


class SkillPolicyOut(BaseModel):
    """The publish gate in force. `source` says where it came from: the tenant's own policy
    (`PATCH /-/policy`), or the deployment's `FELIX_SKILL_PUBLISH_*` settings without one."""

    min_quality: int
    block_on_advisory: bool
    # Always true: a failing security scan blocks every publish, whatever the policy says.
    security_fail_blocks: bool
    # Block a publish unless the version has a succeeded evaluation.
    require_eval: bool
    # Block a publish unless the version's latest succeeded evaluation has at least this uplift.
    # Null is no floor. Set without `require_eval`, a version with no evaluation is blocked too.
    min_eval_uplift: int | None
    source: PolicySource
    # Who last changed the tenant's policy, and when; null while `source` is `settings`.
    updated_at: int | None = None
    updated_by: str | None = None


class SkillFeedbackOut(BaseModel):
    """Feedback on one version of a skill. Every text field is secret-redacted."""

    id: str
    name: str
    target_version: str
    source: FeedbackSource
    # The manifest id for an agent's feedback, the principal for a person's.
    author: str
    principal: str | None
    body: str
    suggested_patch: str | None
    # `pending` until a person decides. `accepted` with `improve` is waiting for the worker,
    # which moves it to `applied` (with `result_version`, a draft in the review queue) or
    # `failed` (with `error`). `accepted` without `improve` and `rejected` are final.
    status: FeedbackStatus
    improve: bool
    result_version: str | None
    # The model route the improvement used.
    model: str | None
    error: str | None
    created_at: int
    # When the worker took the improvement; null until then.
    claimed_at: int | None
    decided_at: int | None
    decided_by: str | None
    decision_note: str | None


class SkillFeedbackListOut(BaseModel):
    items: list[SkillFeedbackOut]
    # Pass back as `cursor` for the next page; null on the last.
    next_cursor: str | None


class EvalScenarioOut(BaseModel):
    name: str
    prompt: str
    criteria: str


class EvalScenarioResultOut(BaseModel):
    name: str
    baseline_score: int
    with_skill_score: int
    uplift: int
    baseline_reason: str = ""
    with_skill_reason: str = ""


class SkillEvalOut(BaseModel):
    """One baseline-versus-with-skill evaluation. Scores are 0-100 from the judge; uplift is
    `with_skill_score - baseline_score`. Null until it has `succeeded`."""

    id: str
    name: str
    version: str
    status: EvalStatus
    # Where the scenarios came from: the bundle's `evals/`, generated by the model, or the
    # defaults derived from the skill's description. Null until it runs.
    scenario_source: Literal["bundle", "generated", "default"] | None
    scenarios: list[EvalScenarioOut]
    baseline_score: int | None
    with_skill_score: int | None
    uplift: int | None
    results: list[EvalScenarioResultOut]
    # The answering and judging model routes.
    model: str | None
    judge_model: str | None
    error: str | None
    requested_by: str
    created_at: int
    started_at: int | None
    finished_at: int | None


class SkillEvalListOut(BaseModel):
    # Newest first.
    items: list[SkillEvalOut]
    next_cursor: str | None


class SkillLibraryErrorOut(BaseModel):
    """Every refusal. `issues` accompanies `invalid_bundle`; `reasons` accompanies `publish_blocked`."""

    error: str
    message: str
    issues: list[BundleIssueOut] | None = None
    reasons: list[str] | None = None


class SkillWriteOut(SkillVersionDetailOut):
    published: bool
    # The refusal of the publish asked for in the same request, with its code (`publish_blocked`
    # with the gate's reasons, or a state conflict); null when none was asked for or it went
    # live. The draft is saved either way.
    publish_blocked: SkillLibraryErrorOut | None = None


class SkillArchivedOut(BaseModel):
    name: str
    live_version: str | None


# -- request models --------------------------------------------------------------------------


class BundleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Path → content; a binary asset under `assets/` is base64. Bounded by count here, and by
    # bytes in `validate_skill_bundle`, which measures the text before anything is decoded.
    files: dict[str, str] = Field(min_length=1, max_length=MAX_BUNDLE_FILES)
    reason: str = Field(default="", max_length=REASON_LIMIT)
    # Publish in the same request, through the same gate as `POST .../publish`.
    publish: bool = False


class CreateSkillIn(BundleIn):
    pass


class NewVersionIn(BundleIn):
    # The newest version the editor loaded. The save is refused with 409 `parent_changed` when
    # anything newer has been saved since, so two editors cannot silently overwrite each other.
    # `$` rather than `VERSION_RE`'s `\Z`: pydantic's regex engine is Rust's, where `$` is end of text.
    parent_version: str = Field(pattern=r"^\d{1,6}\.\d{1,6}\.\d{1,6}$", max_length=32)
    version: str | None = Field(default=None, max_length=32)
    bump: Literal["patch", "minor", "major"] | None = None

    @model_validator(mode="after")
    def _one_way_to_number(self) -> NewVersionIn:
        if self.version is not None and self.bump is not None:
            raise ValueError("give `version` or `bump`, not both")
        return self


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=REASON_LIMIT)


_VERSION_PATTERN = r"^\d{1,6}\.\d{1,6}\.\d{1,6}$"


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=8000)
    suggested_patch: str | None = Field(default=None, max_length=32000)
    # The version this is about; the live version (else the newest) when omitted.
    target_version: str | None = Field(default=None, pattern=_VERSION_PATTERN, max_length=32)


class AcceptFeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Queue an AI rewrite of the skill from this feedback. It lands as a draft in the review
    # queue; nothing is published.
    improve: bool = True
    note: str | None = Field(default=None, max_length=REASON_LIMIT)


class PolicyPatchIn(BaseModel):
    """The fields to change; the rest keep their effective value. The first PATCH copies the
    settings' values into a tenant policy, which from then on replaces them whole. An explicit
    null `min_eval_uplift` removes the floor."""

    model_config = ConfigDict(extra="forbid")

    min_quality: int | None = Field(default=None, ge=0, le=100)
    block_on_advisory: bool | None = None
    require_eval: bool | None = None
    min_eval_uplift: int | None = Field(default=None, ge=-100, le=100)


__all__ = [
    "REASON_LIMIT",
    "AcceptFeedbackIn",
    "BundleIn",
    "BundleIssueOut",
    "CreateSkillIn",
    "EvalScenarioOut",
    "EvalScenarioResultOut",
    "FeedbackIn",
    "NewVersionIn",
    "PolicyPatchIn",
    "RejectIn",
    "ReviewCheckOut",
    "ReviewQueueItemOut",
    "ReviewQueueOut",
    "SecurityIssueOut",
    "SkillArchivedOut",
    "SkillDetailOut",
    "SkillEvalListOut",
    "SkillEvalOut",
    "SkillFeedbackListOut",
    "SkillFeedbackOut",
    "SkillFileMetaOut",
    "SkillFileOut",
    "SkillLibraryErrorOut",
    "SkillListOut",
    "SkillPolicyOut",
    "SkillPreviewOut",
    "SkillSummaryOut",
    "SkillVersionDetailOut",
    "SkillVersionOut",
    "SkillWriteOut",
    "VersionHeadOut",
]
