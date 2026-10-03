"""The publish gate: what a skill version is judged on before it goes live, and the policy.

Pure and synchronous -- no stores, no object store. `skills/library.py` reads a version's bytes
(digest-checked) and hands them here off the event loop; a publish, a rollback and the
management preview all reach the same `evaluate_files`, so the verdict a reviewer previews is
the verdict a publish is decided on.

A failing security scan blocks always, whatever the policy says. The policy only raises the
bar from there: a quality floor, and refusing an advisory scan.
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


@dataclass(slots=True, frozen=True)
class PublishPolicy:
    """The gate a publish passes, as configured. Settings-wide for now; per tenant later."""

    min_quality: int = 0
    block_on_advisory: bool = False
    # Not configurable: a failing scan blocks every publish, whoever asks.
    security_fail_blocks: bool = True
    source: Literal["settings"] = "settings"


def publish_policy(settings: Settings, tenant_id: str) -> PublishPolicy:
    """The policy in force for ``tenant_id``. The tenant is unused until policy rows exist;
    it is a parameter now so no caller has to learn it later."""
    del tenant_id
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


def policy_reasons(policy: PublishPolicy, assessment: Assessment) -> list[str]:
    """Why ``policy`` refuses ``assessment``; empty when it does not."""
    reasons: list[str] = []
    if assessment.security_status == "fail":
        found = [i["message"] for i in assessment.security_issues if i["severity"] in {"critical", "high"}]
        reasons.append("security scan failed: " + "; ".join(found[:5]))
    elif assessment.security_status == "advisory" and policy.block_on_advisory:
        reasons.append("security scan is advisory and FELIX_SKILL_PUBLISH_BLOCK_ON_ADVISORY is set")
    if assessment.quality_score < policy.min_quality:
        reasons.append(f"quality score {assessment.quality_score} is below the minimum {policy.min_quality}")
    return reasons


def evaluate_files(files: Mapping[str, str], name: str, policy: PublishPolicy) -> Verdict:
    """Validate, review, scan and apply the policy. Blocking: call it off the event loop."""
    validation = validate_skill_bundle(files, name)
    assessment = assess(files, name)
    issues = [{"path": i.path, "message": i.message} for i in validation.errors]
    if not validation.valid:
        reasons = [f"bundle no longer validates: {i['path']}: {i['message']}" for i in issues]
    else:
        reasons = policy_reasons(policy, assessment)
    return Verdict(valid=validation.valid, assessment=assessment, validation_issues=issues, reasons=reasons)


__all__ = [
    "Assessment",
    "PublishPolicy",
    "SecurityStatus",
    "Verdict",
    "assess",
    "evaluate_files",
    "policy_reasons",
    "publish_policy",
]
