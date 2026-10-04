"""Request and response models for `routes/skill_library.py`. No routes.

Typed so the OpenAPI document describes what the library API returns -- the chat UI's library,
editor and review queue are generated against it.
"""

from __future__ import annotations

from typing import Literal

from felix.skills.feedback import MAX_BODY_CHARS, MAX_PATCH_CHARS, NOTE_LIMIT
from felix.skills.format import MAX_BUNDLE_FILES
from felix.skills.library import SkillSourceKind
from felix.skills.library_store import SkillStatus
from felix.skills.publish_gate import PolicySource, SecurityStatus
from felix.skills.quality_store import EvalStatus, FeedbackSource, FeedbackStatus, ScenarioSource
from felix.skills.security import Severity
from pydantic import BaseModel, ConfigDict, Field, model_validator

REASON_LIMIT = 2000
# A `major.minor.patch` version in a request body. `$` rather than `library.VERSION_RE`'s `\Z`:
# pydantic's regex engine is Rust's, where `$` is end of text.
VERSION_PATTERN = r"^\d{1,6}\.\d{1,6}\.\d{1,6}$"


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
    # Where an imported version came from (`source: import`); null on every other version. The
    # canonical source (`github:owner/repo/path`), the ref asked for, the commit it resolved to,
    # a digest of the skill folder's tree there, and the repository's SPDX license, if declared.
    origin_source: str | None = None
    origin_ref: str | None = None
    origin_commit: str | None = None
    origin_tree_hash: str | None = None
    origin_license: str | None = None
    # When the skill's folder last changed at `origin_commit` (epoch ms): what the minimum import
    # age is measured on.
    origin_committed_at: int | None = None


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


class SkillPolicyValuesOut(BaseModel):
    """The fields a tenant sets, as it set them."""

    min_quality: int
    block_on_advisory: bool
    require_eval: bool
    min_eval_uplift: int | None
    import_min_age_days: int = 0


class SkillPolicyOut(BaseModel):
    """The publish gate in force: the deployment's `FELIX_SKILL_PUBLISH_*` settings, tightened by
    the tenant's own policy (`PATCH /-/policy`) when it has one. A tenant can raise the bar and
    never lower it. `source`: `settings` (no tenant policy), `tenant` (the tenant's policy is at
    least as strict as the settings everywhere), or `tenant+settings` (a setting outvoted a
    looser tenant value; `tenant_values` shows what the tenant set)."""

    min_quality: int
    block_on_advisory: bool
    # Always true: a failing security scan blocks every publish, whatever the policy says.
    security_fail_blocks: bool
    # Block a publish unless the version has a succeeded evaluation.
    require_eval: bool
    # Block a publish unless the version's latest succeeded evaluation has at least this uplift.
    # Null is no floor. Set without `require_eval`, a version with no evaluation is blocked too.
    min_eval_uplift: int | None
    # Refuse an import whose skill folder on GitHub changed fewer than this many days ago
    # (`FELIX_SKILL_IMPORT_MIN_AGE_DAYS`, raised by the tenant): 403 `too_recent`, and nothing is
    # saved. 0 is off.
    import_min_age_days: int = 0
    source: PolicySource
    # What the tenant itself set; null while `source` is `settings`.
    tenant_values: SkillPolicyValuesOut | None = None
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
    # When the worker last took the improvement, its last heartbeat, and how many times it has
    # been taken (it fails `attempts_exhausted` after 3); null and 0 until then.
    claimed_at: int | None
    heartbeat_at: int | None
    attempts: int
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
    scenario_source: ScenarioSource | None
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
    # When the last claim started it, its last heartbeat, and how many times it has been claimed
    # (it fails `attempts_exhausted` after 3).
    started_at: int | None
    heartbeat_at: int | None
    attempts: int
    finished_at: int | None
    # Whether this evaluation can satisfy `require_eval` / `min_eval_uplift` for its version, and
    # why not. Only a succeeded one can, and for an agent's version only one on the bundle's own
    # `evals/` scenarios: generated and default scenarios are written from the agent's text.
    counts_for_gate: bool
    gate_note: str


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


class SkillImportOut(SkillWriteOut):
    # True when the library's newest version already holds exactly these files (the skill
    # folder's tree is unchanged): nothing was saved, and this is that version.
    unchanged: bool
    # Files of the skill folder the import left out: anything outside `SKILL.md`, `plugin.json`,
    # `scripts/`, `references/` and `assets/` (an `evals/` included), dot-paths, a binary file
    # outside `assets/`, and text that is not UTF-8.
    dropped_files: list[str]


class BrowseItemOut(BaseModel):
    # From the SKILL.md frontmatter; the directory name when it names none.
    name: str
    description: str
    # The skill's directory in the repository.
    path: str
    # What to import it by: `github:owner/repo/path`.
    source: str
    # Under a minimum import age only (null otherwise): when the skill's folder last changed at
    # `commit`, and when it becomes old enough to import (epoch ms).
    committed_at: int | None = None
    eligible_at: int | None = None
    # Whether an import now would pass the minimum age; always true when there is none.
    eligible: bool = True


class SkillBrowseOut(BaseModel):
    """The skills a repository offers at one commit."""

    source: str
    # The ref asked for, or the repository's default branch.
    ref: str
    # The commit `ref` resolved to; every listed SKILL.md was read at it.
    commit: str
    license: str | None
    # The minimum import age in force for the caller's tenant; 0 is none.
    min_age_days: int = 0
    items: list[BrowseItemOut]
    # More skills were found than one browse lists; name a path to narrow it.
    truncated: bool


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
    parent_version: str = Field(pattern=VERSION_PATTERN, max_length=32)
    version: str | None = Field(default=None, max_length=32)
    bump: Literal["patch", "minor", "major"] | None = None

    @model_validator(mode="after")
    def _one_way_to_number(self) -> NewVersionIn:
        if self.version is not None and self.bump is not None:
            raise ValueError("give `version` or `bump`, not both")
        return self


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `github:owner/repo[/path]`: the directory holding the skill's SKILL.md.
    source: str = Field(min_length=1, max_length=512)
    # A branch, tag or commit; the repository's default branch when omitted. Resolved to a
    # commit once, and every file is read at that commit.
    ref: str | None = Field(default=None, min_length=1, max_length=200)
    # Publish in the same request, through the same gate as `POST .../publish` -- where an
    # imported version's advisory scan blocks too. The draft is saved either way.
    publish: bool = False


class MakeLiveIn(BaseModel):
    """Optional body of a publish or rollback. Send `expected_live_version` -- the live version
    the page showed, or null for "nothing was live" -- and the move is refused with 409
    `live_changed` if another version is live by the time it lands. Leave it out to move
    whatever is live."""

    model_config = ConfigDict(extra="forbid")

    expected_live_version: str | None = Field(default=None, pattern=VERSION_PATTERN, max_length=32)


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=REASON_LIMIT)


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    suggested_patch: str | None = Field(default=None, max_length=MAX_PATCH_CHARS)
    # The version this is about; the live version (else the newest) when omitted.
    target_version: str | None = Field(default=None, pattern=VERSION_PATTERN, max_length=32)


class AcceptFeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Queue an AI rewrite of the skill from this feedback. It lands as a draft in the review
    # queue; nothing is published.
    improve: bool = True
    note: str | None = Field(default=None, max_length=NOTE_LIMIT)


class PolicyPatchIn(BaseModel):
    """The fields of the tenant's own policy to change; the rest keep the tenant's value (or, on
    the first PATCH, the settings'). Only the fields sent are applied, so the defaults here are
    never written. Only `min_eval_uplift` takes null, which removes the tenant's floor; the
    others must be a value."""

    model_config = ConfigDict(extra="forbid")

    min_quality: int = Field(default=0, ge=0, le=100)
    block_on_advisory: bool = False
    require_eval: bool = False
    min_eval_uplift: int | None = Field(default=None, ge=-100, le=100)
    import_min_age_days: int = Field(default=0, ge=0, le=365)


__all__ = [
    "REASON_LIMIT",
    "VERSION_PATTERN",
    "AcceptFeedbackIn",
    "BrowseItemOut",
    "BundleIn",
    "BundleIssueOut",
    "CreateSkillIn",
    "EvalScenarioOut",
    "EvalScenarioResultOut",
    "FeedbackIn",
    "ImportIn",
    "MakeLiveIn",
    "NewVersionIn",
    "PolicyPatchIn",
    "RejectIn",
    "ReviewCheckOut",
    "ReviewQueueItemOut",
    "ReviewQueueOut",
    "SecurityIssueOut",
    "SkillArchivedOut",
    "SkillBrowseOut",
    "SkillDetailOut",
    "SkillEvalListOut",
    "SkillEvalOut",
    "SkillFeedbackListOut",
    "SkillFeedbackOut",
    "SkillFileMetaOut",
    "SkillFileOut",
    "SkillImportOut",
    "SkillLibraryErrorOut",
    "SkillListOut",
    "SkillPolicyOut",
    "SkillPolicyValuesOut",
    "SkillPreviewOut",
    "SkillSummaryOut",
    "SkillVersionDetailOut",
    "SkillVersionOut",
    "SkillWriteOut",
    "VersionHeadOut",
]
