"""The publish gate: what a skill version is judged on before it goes live, and the policy.

Pure and synchronous -- no stores, no object store. `skills/library.py` reads a version's bytes
(digest-checked) and hands them here off the event loop; a publish, a rollback and the
management preview all reach the same `evaluate_files`, so the verdict a reviewer previews is
the verdict a publish is decided on.

A failing security scan blocks always, whatever the policy says. The policy only raises the
bar from there: a quality floor, refusing an advisory scan, requiring a succeeded evaluation of
the version, and a floor on that evaluation's uplift.

The deployment's `FELIX_SKILL_PUBLISH_*` settings are a floor a tenant cannot go under: a
tenant's `skill_policy` row can only tighten them (`publish_policy`). Which evaluation counts is
decided here too (`eval_counts_for_gate`), so the gate and the API report it the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Literal

from felix.config import Settings
from felix.skills.format import validate_skill_bundle
from felix.skills.review import review_skill_bundle
from felix.skills.security import ScanStatus, scan_skill_security
from felix.skills.sources import source_traits

# What a security scan concludes; the `skill_version.security_status` column holds one.
SecurityStatus = ScanStatus


# Where the policy in force came from: the settings alone (no tenant row); the tenant's row, which
# is at least as strict as the settings everywhere; or the row, tightened by a setting it was
# looser than.
PolicySource = Literal["settings", "tenant", "tenant+settings"]


@dataclass(slots=True, frozen=True)
class PublishPolicy:
    """The gate a publish passes: the settings, tightened by a tenant's `skill_policy` row."""

    min_quality: int = 0
    block_on_advisory: bool = False
    # Not configurable: a failing scan blocks every publish, whoever asks.
    security_fail_blocks: bool = True
    # Block unless the version has a succeeded evaluation that counts (`eval_counts_for_gate`).
    require_eval: bool = False
    # Block unless the version's latest counting evaluation has at least this uplift (with-skill
    # score minus baseline, -100..100). Set without `require_eval`, a version with no evaluation
    # is blocked too: there is no uplift to compare.
    min_eval_uplift: int | None = None
    # Refuse an import (`skills/importer.py`) of files the tenant first saw fewer than this many
    # days ago (`sighting_store`): a supply-chain cooldown. Not a publish rule -- an import it
    # refuses saves nothing -- but it lives on the same row, under the same tighten-only rule.
    import_min_age_days: int = 0
    source: PolicySource = "settings"

    @property
    def needs_eval(self) -> bool:
        """Whether a verdict under this policy depends on the version's evaluation."""
        return self.require_eval or self.min_eval_uplift is not None

    @classmethod
    def from_row(cls, row: Mapping[str, Any], *, source: PolicySource = "tenant") -> PublishPolicy:
        uplift = row.get("min_eval_uplift")
        return cls(
            min_quality=int(row.get("min_quality") or 0),
            block_on_advisory=bool(row.get("block_on_advisory")),
            require_eval=bool(row.get("require_eval")),
            min_eval_uplift=None if uplift is None else int(uplift),
            import_min_age_days=int(row.get("import_min_age_days") or 0),
            source=source,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> PublishPolicy:
        uplift = settings.skill_publish_min_eval_uplift
        return cls(
            min_quality=int(settings.skill_publish_min_quality or 0),
            block_on_advisory=bool(settings.skill_publish_block_on_advisory),
            require_eval=bool(settings.skill_publish_require_eval),
            min_eval_uplift=None if uplift is None else int(uplift),
            import_min_age_days=int(settings.skill_import_min_age_days or 0),
        )

    def to_row(self) -> dict[str, Any]:
        """The tunable fields, as a `skill_policy` row holds them."""
        return {f: getattr(self, f) for f in TUNABLE_FIELDS}

    def tightened_by(self, other: PublishPolicy) -> PublishPolicy:
        """The stricter of the two on every field. A null uplift floor is no floor."""
        floors = [u for u in (self.min_eval_uplift, other.min_eval_uplift) if u is not None]
        return replace(
            self,
            min_quality=max(self.min_quality, other.min_quality),
            block_on_advisory=self.block_on_advisory or other.block_on_advisory,
            require_eval=self.require_eval or other.require_eval,
            min_eval_uplift=max(floors) if floors else None,
            import_min_age_days=max(self.import_min_age_days, other.import_min_age_days),
        )

    def without_eval(self) -> PublishPolicy:
        """This policy with no evaluation requirement: what a rollback is judged on."""
        return replace(self, require_eval=False, min_eval_uplift=None)


# The fields a tenant may set: everything but the scan rule nobody may turn off and the source.
TUNABLE_FIELDS: tuple[str, ...] = tuple(
    f.name for f in fields(PublishPolicy) if f.name not in {"security_fail_blocks", "source"}
)


def publish_policy(settings: Settings, row: Mapping[str, Any] | None) -> PublishPolicy:
    """The policy in force: the settings, tightened by ``row`` -- the tenant's `skill_policy`
    row -- when there is one. A tenant can raise the deployment's bar and never lower it."""
    floor = PublishPolicy.from_settings(settings)
    if row is None:
        return floor
    tenant = PublishPolicy.from_row(row)
    effective = tenant.tightened_by(floor)
    return replace(effective, source="tenant" if effective == tenant else "tenant+settings")


def _untrusted_eval(version_source: str | None) -> str | None:
    """Why only the bundle's own scenarios count for a version by ``version_source``
    (`sources.SourceTraits.untrusted_eval`: an agent, an import, a promotion), or None when any
    do. A source the table does not know is held to the bundle's own, never let through."""
    if version_source is None:
        return None
    traits = source_traits(version_source)
    return (
        traits.untrusted_eval
        if traits is not None
        else f"this version's source {version_source!r} is unknown"
    )


def eval_counts_for_gate(version_source: str | None, evaluation: Mapping[str, Any]) -> tuple[bool, str]:
    """Whether ``evaluation`` can satisfy `require_eval` and `min_eval_uplift` for a version
    written by ``version_source``, and why not when it cannot.

    For an agent's, an imported or a promoted version only an evaluation on the bundle's own
    scenarios counts. Generated and default scenarios are written from the skill's text -- the
    text the agent, the third party or the promoter wrote -- so its author could steer the test it
    is graded on. A bundle's `evals/` files come only from an operator's save: an agent's save and
    a promotion may only carry them unchanged from the tenant's parent (`library.save_draft`), and
    an import drops them (`importer.sanitize_bundle`).
    """
    if evaluation.get("status") != "succeeded":
        return False, f"the evaluation has not succeeded (it is {evaluation.get('status')})"
    who = _untrusted_eval(version_source)
    if who is not None and evaluation.get("scenario_source") != "bundle":
        return False, (
            f"{who}, and these scenarios were {evaluation.get('scenario_source')}: "
            "only the bundle's own evals/ scenarios count for it"
        )
    return True, "counts toward the publish policy"


def gate_scenario_source(version_source: str | None) -> str | None:
    """The scenario source an evaluation must have to count for this version, or None for any."""
    return "bundle" if _untrusted_eval(version_source) is not None else None


def carries_imported_text(row: Mapping[str, Any] | None) -> bool:
    """Whether a version is judged and served as third-party text: an import, or a version that
    inherited one's lineage (`lineage_import`). The gate (`gate_source`) and the catalog
    (`loader._library_skill`, which marks such a skill untrusted) both ask this, so they cannot
    disagree about which versions it covers. An adopted version (`adopted_from`) does not."""
    if row is None:
        return False
    return row.get("source") == "import" or bool(row.get("lineage_import"))


def holds_third_party_bytes(row: Mapping[str, Any] | None) -> bool:
    """Whether a version's files count as imported text to the copy rule
    (`library_store.holds_imported_file`): every version that `carries_imported_text`, and also a
    version an operator adopted from one (`adopted_from`). Adoption vouches for the text in that
    skill; an agent copying it into another skill is still copying a third party's words. Wider
    than `carries_imported_text` on purpose."""
    return carries_imported_text(row) or bool(row and row.get("adopted_from"))


def gate_source(row: Mapping[str, Any] | None) -> str | None:
    """Who the gate judges a version as having been written by: `import` for every version that
    `carries_imported_text` -- an agent's or an operator's edit of third-party text still carries
    it -- else the version's own source."""
    if row is None:
        return None
    if carries_imported_text(row):
        return "import"
    return row.get("source")


def policy_for_source(policy: PublishPolicy, version_source: str | None) -> PublishPolicy:
    """``policy`` as it applies to a version written by ``version_source``: only ever tighter.

    An imported version is third-party instructions nobody in the tenant wrote, so an advisory
    scan (an executable link, remote content piped to a shell) blocks it whatever the tenant's
    policy says about advisories -- the bar a public registry holds mirrored skills to.
    """
    if version_source == "import" and not policy.block_on_advisory:
        return replace(policy, block_on_advisory=True)
    return policy


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
        return [
            "the publish policy requires a succeeded evaluation of this version that counts, and it has none"
        ]
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
    version's latest evaluation that counts (`eval_counts_for_gate`), which only a policy that
    `needs_eval` reads."""
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
    "TUNABLE_FIELDS",
    "Assessment",
    "PolicySource",
    "PublishPolicy",
    "SecurityStatus",
    "Verdict",
    "assess",
    "carries_imported_text",
    "eval_counts_for_gate",
    "evaluate_files",
    "gate_scenario_source",
    "gate_source",
    "holds_third_party_bytes",
    "policy_for_source",
    "policy_reasons",
    "publish_policy",
]
