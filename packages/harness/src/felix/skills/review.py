"""A 0-100 quality score for a skill bundle, from weighted heuristic checks.

The score is the weight of the checks that pass over the weight of all of them. An
invalid bundle scores 0 and is not checked further. A rubric can reweight or disable any
check by id.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from felix.skills.format import SkillFrontmatter, validate_skill_bundle

_HEADING_RE = re.compile(r"^#+\s", re.MULTILINE)
_STEP_RE = re.compile(r"^\d+\.|^-\s", re.MULTILINE)
# Checks that suggest the skill changes what an agent does, not only how it reads.
_IMPACT_CHECKS = frozenset({"scripts-dir", "actionable-steps", "body-length"})


@dataclass(slots=True, frozen=True)
class ReviewCheck:
    id: str
    label: str
    passed: bool
    message: str
    weight: int


@dataclass(slots=True, frozen=True)
class SkillReviewResult:
    score: int
    checks: list[ReviewCheck]


@dataclass(slots=True, frozen=True)
class RubricCheck:
    id: str
    weight: int
    enabled: bool = True


@dataclass(slots=True, frozen=True)
class ReviewRubric:
    """``validation_weight`` is a fraction (0.3 = weight 30) for the `valid-bundle` check."""

    validation_weight: float | None = None
    checks: list[RubricCheck] = field(default_factory=list)


def _round(value: float) -> int:
    """JavaScript's `Math.round` — half up, where Python's `round` goes to even."""
    return math.floor(value + 0.5)


def review_skill_bundle(
    files: Mapping[str, str],
    expected_slug: str | None = None,
    rubric: ReviewRubric | None = None,
) -> SkillReviewResult:
    validation = validate_skill_bundle(files, expected_slug)
    overrides = {c.id: c for c in (rubric.checks if rubric else [])}
    disabled = {c.id for c in (rubric.checks if rubric else []) if not c.enabled}

    if rubric is not None and rubric.validation_weight is not None:
        validation_weight = _round(rubric.validation_weight * 100)
    else:
        validation_weight = overrides["valid-bundle"].weight if "valid-bundle" in overrides else 30

    valid_check = ReviewCheck(
        id="valid-bundle",
        label="agentskills.io bundle valid",
        passed=validation.valid,
        message="Bundle passes agentskills.io validation"
        if validation.valid
        else "; ".join(e.message for e in validation.errors),
        weight=validation_weight,
    )
    if not validation.valid or validation.frontmatter is None:
        return SkillReviewResult(score=0, checks=[valid_check])

    checks = [
        valid_check,
        *_review_frontmatter(validation.frontmatter),
        *_review_body(validation.body or ""),
        *_review_structure(files),
    ]
    weighted = [
        replace(c, weight=overrides[c.id].weight) if c.id in overrides else c
        for c in checks
        if c.id not in disabled
    ]
    total = sum(c.weight for c in weighted)
    earned = sum(c.weight for c in weighted if c.passed)
    score = _round(earned / total * 100) if total > 0 else 0
    return SkillReviewResult(score=score, checks=weighted)


def _review_frontmatter(fm: SkillFrontmatter) -> list[ReviewCheck]:
    length = len(fm.description)
    licensed = bool(fm.license or fm.metadata)
    compatible = bool(fm.compatibility)
    # A failed check's message is what to do, not what passed: `authoring._review_hint`
    # hands the failed ones to the agent as "To raise the quality score: ...".
    if length < 20:
        description_message = "Expand the description to at least 20 characters"
    elif length > 500:
        description_message = "Shorten the description to 500 characters or fewer"
    else:
        description_message = "Description is substantive"
    return [
        ReviewCheck(
            id="description-length",
            label="Description length",
            passed=20 <= length <= 500,
            message=description_message,
            weight=15,
        ),
        ReviewCheck(
            id="license-metadata",
            label="License or metadata",
            passed=licensed,
            message="Includes license or metadata for discoverability"
            if licensed
            else "Add a license or metadata for discoverability",
            weight=5,
        ),
        ReviewCheck(
            id="compatibility",
            label="Compatibility notes",
            passed=compatible,
            message="Documents agent/environment compatibility"
            if compatible
            else "Document agent/environment compatibility",
            weight=5,
        ),
    ]


def _review_body(body: str) -> list[ReviewCheck]:
    trimmed = body.strip()
    substantive = len(trimmed) >= 100
    headed = bool(_HEADING_RE.search(trimmed))
    stepped = bool(_STEP_RE.search(trimmed))
    return [
        ReviewCheck(
            id="body-length",
            label="Instruction body",
            passed=substantive,
            message="Body has sufficient instruction content"
            if substantive
            else "Expand the body to at least 100 characters of instructions",
            weight=15,
        ),
        ReviewCheck(
            id="headings",
            label="Structured headings",
            passed=headed,
            message="Uses markdown headings for structure"
            if headed
            else "Add markdown headings for structure",
            weight=10,
        ),
        ReviewCheck(
            id="actionable-steps",
            label="Actionable steps",
            passed=stepped,
            message="Includes numbered or bulleted steps" if stepped else "Add numbered or bulleted steps",
            weight=10,
        ),
    ]


def _review_structure(files: Mapping[str, str]) -> list[ReviewCheck]:
    has_scripts = any(p.startswith("scripts/") for p in files)
    has_references = any(p.startswith("references/") for p in files)
    has_plugin = "plugin.json" in files
    return [
        ReviewCheck(
            id="scripts-dir",
            label="Scripts directory",
            passed=has_scripts,
            message="Includes executable scripts" if has_scripts else "Add a scripts/ directory (optional)",
            weight=5,
        ),
        ReviewCheck(
            id="references-dir",
            label="References directory",
            passed=has_references,
            message="Includes reference docs" if has_references else "Add a references/ directory (optional)",
            weight=5,
        ),
        ReviewCheck(
            id="plugin-manifest",
            label="Plugin manifest",
            passed=has_plugin,
            message="plugin.json present for bundled context" if has_plugin else "Add plugin.json (optional)",
            weight=5,
        ),
    ]


def estimate_impact_score(review: SkillReviewResult) -> int:
    """A heuristic impact estimate from review signals alone — no agent run."""
    bonus = sum(1 for c in review.checks if c.passed and c.id in _IMPACT_CHECKS)
    return min(100, _round(review.score * 0.7 + bonus * 10))


__all__ = [
    "ReviewCheck",
    "ReviewRubric",
    "RubricCheck",
    "SkillReviewResult",
    "estimate_impact_score",
    "review_skill_bundle",
]
