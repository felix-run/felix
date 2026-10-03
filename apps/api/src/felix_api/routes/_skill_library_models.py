"""Request and response models for `routes/skill_library.py`. No routes.

Typed so the OpenAPI document describes what the library API returns -- the chat UI's library,
editor and review queue are generated against it.
"""

from __future__ import annotations

from typing import Literal

from felix.skills.format import MAX_BUNDLE_FILES
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
    severity: str
    path: str
    message: str
    rule_id: str | None = None


class BundleIssueOut(BaseModel):
    path: str
    message: str


class VersionHeadOut(BaseModel):
    """A skill's newest version, as a listing shows it."""

    version: str
    status: Literal["draft", "published", "archived"]
    source: Literal["agent", "operator"]
    quality_score: int
    security_status: str
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
    status: Literal["draft", "published", "archived"]
    source: Literal["agent", "operator"]
    author: str
    origin_manifest_id: str | None
    session_id: str | None
    reason: str
    description: str
    quality_score: int
    security_status: str
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
    status: Literal["draft", "published", "archived"]
    valid: bool
    validation_issues: list[BundleIssueOut]
    quality_score: int | None
    review_checks: list[ReviewCheckOut]
    security_status: str | None
    security_issues: list[SecurityIssueOut]
    # True when the publish gate would let these bytes through. Not whether the version's
    # state allows a publish: only a draft publishes, and only a once-published version rolls back.
    policy_passes: bool
    reasons: list[str]


class SkillPolicyOut(BaseModel):
    min_quality: int
    block_on_advisory: bool
    security_fail_blocks: bool
    source: Literal["settings"]


class SkillWriteOut(SkillVersionDetailOut):
    published: bool
    # Why the publish asked for in the same request did not happen; the draft is saved either way.
    publish_blocked: list[str] | None = None


class SkillArchivedOut(BaseModel):
    name: str
    live_version: str | None


class SkillLibraryErrorOut(BaseModel):
    """Every refusal. `issues` accompanies `invalid_bundle`; `reasons` accompanies `publish_blocked`."""

    error: str
    message: str
    issues: list[BundleIssueOut] | None = None
    reasons: list[str] | None = None


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


__all__ = [
    "REASON_LIMIT",
    "BundleIn",
    "BundleIssueOut",
    "CreateSkillIn",
    "NewVersionIn",
    "RejectIn",
    "ReviewCheckOut",
    "ReviewQueueItemOut",
    "ReviewQueueOut",
    "SecurityIssueOut",
    "SkillArchivedOut",
    "SkillDetailOut",
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
