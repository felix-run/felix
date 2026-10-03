"""The publish gate: what a skill version is judged on before it goes live, and the policy.

Pure and synchronous -- no stores, no object store. `skills/library.py` reads a version's bytes
(digest-checked) and hands them here off the event loop; a publish, a rollback and the
management preview all reach the same `evaluate_files`, so the verdict a reviewer previews is
the verdict a publish is decided on.

A failing security scan blocks always, whatever the policy says. The policy only raises the
bar from there: a quality floor, refusing an advisory scan, requiring a succeeded evaluation of
the version, and a floor on that evaluation's uplift. The policy is a tenant's `skill_policy`
row when it has one, else `FELIX_SKILL_PUBLISH_*`; the caller reads both and hands them here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from felix.config import Settings
from felix.skills.format import validate_skill_bundle
from felix.skills.review import review_skill_bundle
from felix.skills.security import ScanStatus, scan_skill_security

# What a security scan concludes; the `skill_version.security_status` column holds one.
SecurityStatus = ScanStatus


PolicySource = Literal["settings", "tenant"]


@dataclass(slots=True, frozen=True)
class PublishPolicy:
    """The gate a publish passes: a tenant's `skill_policy` row, or the settings without one."""

    min_quality: int = 0
    block_on_advisory: bool = False
    # Not configurable: a failing scan blocks every publish, whoever asks.
    security_fail_blocks: bool = True
    # Block unless the version has a succeeded evaluation.
    require_eval: bool = False
    # Block unless the version's latest succeeded evaluation has at least this uplift (with-skill
    # score minus baseline, -100..100). Set without `require_eval`, a version with no evaluation
    # is blocked too: there is no uplift to compare.
    min_eval_uplift: int | None = None
    source: PolicySource = "settings"

    @property
    def needs_eval(self) -> bool:
        """Whether a verdict under this policy depends on the version's evaluation."""
        return self.require_eval or self.min_eval_uplift is not None


def publish_policy(settings: Settings, row: Mapping[str, Any] | None) -> PublishPolicy:
    """The policy in force: ``row`` -- the tenant's `skill_policy` row -- when there is one,
    whole, else the settings. A row replaces the settings rather than overlaying them, so what
    `GET /skill-library/-/policy` reports is every field the gate reads, from one source."""
    if row is not None:
        uplift = row.get("min_eval_uplift")
        return PublishPolicy(
            min_quality=int(row.get("min_quality") or 0),
            block_on_advisory=bool(row.get("block_on_advisory")),
            require_eval=bool(row.get("require_eval")),
            min_eval_uplift=None if uplift is None else int(uplift),
            source="tenant",
        )
    return PublishPolicy(
        min_quality=int(settings.skill_publish_min_quality or 0),
        block_on_advisory=bool(settings.skill_publish_block_on_advisory),
    )


@dataclass(slots=True, frozen=True)
class Assessment:
    """Review and scan of one bundle. ``review_checks`` and ``security_issues`` are the rows
    a `skill_version` stores."""

    quality_score: int
    review_checks: list[dict[str, Any]]
    security_status: SecurityStatus
    security_issues: list[dict[str, Any]]

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class Verdict:
    """What a publish of a version would be decided on. ``reasons`` empty means the gate
    passes these bytes -- not that the version is in a state to be published."""

    valid: bool
    assessment: Assessment | None = None
    validation_issues: list[dict[str, str]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return not self.reasons


def assess(files: Mapping[str, str], name: str) -> Assessment:
    """Review and scan in one call, so one `to_thread` hop covers both."""
    review = review_skill_bundle(files, name)
    scan = scan_skill_security(files)
    return Assessment(
        quality_score=review.score,
        review_checks=[asdict(c) for c in review.checks],
        security_status=scan.status,
        security_issues=[asdict(i) for i in scan.issues],
    )


def _eval_reasons(policy: PublishPolicy, latest_eval: Mapping[str, Any] | None) -> list[str]:
    if not policy.needs_eval:
        return []
    if latest_eval is None:
        return ["the publish policy requires a succeeded evaluation of this version, and it has none"]
    uplift = latest_eval.get("uplift")
    if policy.min_eval_uplift is not None and (uplift is None or int(uplift) < policy.min_eval_uplift):
        return [
            f"evaluation {latest_eval.get('id')} has uplift {uplift}, below the minimum "
            f"{policy.min_eval_uplift}"
        ]
    return []


def policy_reasons(
    policy: PublishPolicy, assessment: Assessment, latest_eval: Mapping[str, Any] | None = None
) -> list[str]:
    """Why ``policy`` refuses ``assessment``; empty when it does not. ``latest_eval`` is the
    version's latest succeeded evaluation, which only a policy that `needs_eval` reads."""
    reasons: list[str] = []
    if assessment.security_status == "fail":
        found = [i["message"] for i in assessment.security_issues if i["severity"] in {"critical", "high"}]
        reasons.append("security scan failed: " + "; ".join(found[:5]))
    elif assessment.security_status == "advisory" and policy.block_on_advisory:
        reasons.append("security scan is advisory and the publish policy blocks advisory scans")
    if assessment.quality_score < policy.min_quality:
        reasons.append(f"quality score {assessment.quality_score} is below the minimum {policy.min_quality}")
    return reasons + _eval_reasons(policy, latest_eval)


def evaluate_files(
    files: Mapping[str, str],
    name: str,
    policy: PublishPolicy,
    latest_eval: Mapping[str, Any] | None = None,
) -> Verdict:
    """Validate, review, scan and apply the policy. Blocking: call it off the event loop."""
    validation = validate_skill_bundle(files, name)
    assessment = assess(files, name)
    issues = [{"path": i.path, "message": i.message} for i in validation.errors]
    if not validation.valid:
        reasons = [f"bundle no longer validates: {i['path']}: {i['message']}" for i in issues]
    else:
        reasons = policy_reasons(policy, assessment, latest_eval)
    return Verdict(valid=validation.valid, assessment=assessment, validation_issues=issues, reasons=reasons)


__all__ = [
    "Assessment",
    "PolicySource",
    "PublishPolicy",
    "SecurityStatus",
    "Verdict",
    "assess",
    "evaluate_files",
    "policy_reasons",
    "publish_policy",
]
