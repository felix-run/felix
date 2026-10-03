"""The publish policy: the settings tightened by a tenant's row, and the gate's evaluation blocks.

Precedence is asserted through `policy.load_publish_policy`, the read every publish, rollback and
preview makes; the evaluation blocks through `library.publish` and `library.rollback`, so what is
proven is that the gate *loads* the evaluation that counts, not only that `policy_reasons` would
use one if handed it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.skills import library
from felix.skills.eval_store import get_skill_eval_store
from felix.skills.policy import delete_publish_policy, load_publish_policy, set_publish_policy
from felix.skills.publish_gate import (
    TUNABLE_FIELDS,
    Assessment,
    PublishPolicy,
    eval_counts_for_gate,
    policy_reasons,
    publish_policy,
)
from felix.skills.quality_store import get_skill_policy_store

from tests.skill_quality import NAME, TENANT, bundle, object_store

_SETTINGS = {"skill_publish_min_quality": 30, "skill_publish_block_on_advisory": True}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="memory://skill-policy", object_store="memory", data_dir=str(tmp_path), **_SETTINGS
    )


async def _draft(
    settings: Settings, source: str = "operator", parent: str | None = None, body: str | None = None
) -> str:
    files = (
        bundle(**{"evals/scenarios.json": '[{"name": "s", "prompt": "p"}]'})
        if body is None
        else bundle(body=body)
    )
    if parent is not None:
        files = {
            **(
                await library.read_version_files(
                    settings, TENANT, NAME, parent, object_store=object_store(settings)
                )
            ),
            "SKILL.md": files["SKILL.md"],
        }
    row = await library.save_draft(
        settings,
        TENANT,
        files=files,
        provenance=library.DraftProvenance(source=source, author="ops"),  # type: ignore[arg-type]
        parent=parent,
        object_store=object_store(settings),
    )
    return str(row["version"])


async def _eval(
    settings: Settings,
    version: str,
    uplift: int | None,
    n: int = 1,
    *,
    status: str = "succeeded",
    source: str = "bundle",
) -> None:
    store = get_skill_eval_store(settings)
    eval_id = f"00000000-0000-4000-8000-{n:012d}"
    await store.insert(
        TENANT, {"id": eval_id, "name": NAME, "version": version, "status": "queued", "created_at": n}
    )
    claimed = await store.claim_next(now=100 + n)
    assert claimed is not None and claimed["id"] == eval_id
    await store.finish(
        TENANT,
        eval_id,
        token=claimed["claim_token"],
        fields={"status": status, "uplift": uplift, "scenario_source": source, "finished_at": 200 + n},
    )


async def _publish(settings: Settings, version: str) -> Any:
    return await library.publish(
        settings, TENANT, NAME, version, by="ops", object_store=object_store(settings)
    )


# -- precedence: the settings tightened by the tenant -----------------------------------------


async def test_without_a_tenant_row_the_settings_decide(settings: Settings) -> None:
    state = await load_publish_policy(settings, TENANT)
    assert state.policy == PublishPolicy(min_quality=30, block_on_advisory=True, source="settings")
    assert (state.tenant, state.updated_at) == (None, None)


@pytest.mark.parametrize(
    ("row", "settings_kw", "effective", "source"),
    [
        # The tenant raises every bar: its row is the policy.
        (
            {"min_quality": 60, "block_on_advisory": True, "require_eval": True, "min_eval_uplift": 5},
            {},
            {"min_quality": 60, "block_on_advisory": True, "require_eval": True, "min_eval_uplift": 5},
            "tenant",
        ),
        # The tenant tries to lower every bar: the settings outvote it on each.
        (
            {"min_quality": 0, "block_on_advisory": False, "require_eval": False, "min_eval_uplift": None},
            {"skill_publish_require_eval": True, "skill_publish_min_eval_uplift": 3},
            {"min_quality": 30, "block_on_advisory": True, "require_eval": True, "min_eval_uplift": 3},
            "tenant+settings",
        ),
        # Uplift floors: the higher wins; a null on one side is no floor, not a zero.
        (
            {"min_quality": 30, "block_on_advisory": True, "require_eval": False, "min_eval_uplift": -10},
            {"skill_publish_min_eval_uplift": -20},
            {"min_quality": 30, "block_on_advisory": True, "require_eval": False, "min_eval_uplift": -10},
            "tenant",
        ),
        (
            {"min_quality": 30, "block_on_advisory": True, "require_eval": False, "min_eval_uplift": None},
            {"skill_publish_min_eval_uplift": -20},
            {"min_quality": 30, "block_on_advisory": True, "require_eval": False, "min_eval_uplift": -20},
            "tenant+settings",
        ),
    ],
    ids=["tenant-stricter", "tenant-looser", "uplift-higher-wins", "uplift-null-is-no-floor"],
)
def test_a_tenant_row_only_tightens_the_settings(
    tmp_path: Path, row: dict[str, Any], settings_kw: dict[str, Any], effective: dict[str, Any], source: str
) -> None:
    settings = Settings(database_url="memory://x", data_dir=str(tmp_path), **{**_SETTINGS, **settings_kw})
    policy = publish_policy(settings, row)
    assert (policy.to_row(), policy.source) == (effective, source)


async def test_a_partial_change_starts_from_the_settings_and_is_audited(settings: Settings) -> None:
    from felix.audit import store as audit_store

    after = await set_publish_policy(settings, TENANT, {"require_eval": True}, by="ops")

    assert after.policy == PublishPolicy(
        min_quality=30, block_on_advisory=True, require_eval=True, source="tenant"
    )
    assert (after.updated_by, after.tenant) == ("ops", replace(after.policy, source="tenant"))
    lowered = await set_publish_policy(settings, TENANT, {"min_quality": 0}, by="ops")
    assert (lowered.tenant.min_quality if lowered.tenant else None, lowered.policy.min_quality) == (0, 30)
    assert lowered.policy.source == "tenant+settings"
    await audit_store.flush_pending(settings)
    rows, _ = await audit_store.list_events(settings, TENANT, limit=50)
    # Two updates can share a millisecond, so the events are matched by content, not by `ts`.
    events = [r["payload_json"] for r in rows if r["event_type"] == "skill_policy_updated"]
    assert sorted(e["before"]["source"] for e in events) == ["settings", "tenant"]


async def test_deleting_the_tenant_policy_hands_back_to_the_settings(settings: Settings) -> None:
    from felix.audit import store as audit_store

    await set_publish_policy(settings, TENANT, {"min_quality": 90}, by="ops")
    after = await delete_publish_policy(settings, TENANT, by="ops")

    assert after.policy == PublishPolicy(min_quality=30, block_on_advisory=True, source="settings")
    assert await get_skill_policy_store(settings).get(TENANT) is None
    assert (await delete_publish_policy(settings, TENANT, by="ops")).policy.source == "settings"
    await audit_store.flush_pending(settings)
    rows, _ = await audit_store.list_events(settings, TENANT, limit=50)
    assert [r["event_type"] for r in rows].count("skill_policy_deleted") == 1, "only a real delete is audited"


def test_the_policy_fields_agree_everywhere() -> None:
    """One list of tunable fields: the dataclass, the PATCH body and the table must not drift."""
    from felix.db.models import SkillPolicyRow
    from felix_api.routes._skill_library_models import PolicyPatchIn, SkillPolicyValuesOut

    columns = {c.name for c in SkillPolicyRow.__table__.columns} - {"tenant_id", "updated_at", "updated_by"}
    assert (
        set(TUNABLE_FIELDS)
        == columns
        == set(PolicyPatchIn.model_fields)
        == set(SkillPolicyValuesOut.model_fields)
    )


# -- the gate's evaluation blocks --------------------------------------------------------------

_PASSING = Assessment(quality_score=90, review_checks=[], security_status="pass", security_issues=[])


@pytest.mark.parametrize(
    ("policy", "latest", "blocked"),
    [
        (PublishPolicy(require_eval=True), None, True),
        (PublishPolicy(require_eval=True), {"id": "e", "uplift": -50}, False),
        (PublishPolicy(min_eval_uplift=10), None, True),
        (PublishPolicy(min_eval_uplift=10), {"id": "e", "uplift": 9}, True),
        (PublishPolicy(min_eval_uplift=10), {"id": "e", "uplift": 10}, False),
        (PublishPolicy(min_eval_uplift=-5), {"id": "e", "uplift": -5}, False),
        (PublishPolicy(), None, False),
    ],
    ids=["require-none", "require-any", "floor-none", "floor-below", "floor-at", "negative-floor", "off"],
)
def test_policy_reasons_for_evaluations(policy: PublishPolicy, latest: Any, blocked: bool) -> None:
    assert bool(policy_reasons(policy, _PASSING, latest)) is blocked


@pytest.mark.parametrize(
    ("version_source", "status", "scenario_source", "counts"),
    [
        ("operator", "succeeded", "generated", True),
        ("operator", "succeeded", "bundle", True),
        ("agent", "succeeded", "bundle", True),
        ("agent", "succeeded", "generated", False),
        ("agent", "succeeded", "default", False),
        ("operator", "failed", "bundle", False),
        ("operator", "running", "bundle", False),
    ],
)
def test_which_evaluations_count_for_the_gate(
    version_source: str, status: str, scenario_source: str, counts: bool
) -> None:
    ok, note = eval_counts_for_gate(version_source, {"status": status, "scenario_source": scenario_source})
    assert ok is counts and note


async def test_require_eval_blocks_a_publish_until_the_version_has_a_succeeded_eval(
    settings: Settings,
) -> None:
    version = await _draft(settings)
    await set_publish_policy(settings, TENANT, {"require_eval": True}, by="ops")

    with pytest.raises(library.SkillPublishBlocked) as blocked:
        await _publish(settings, version)
    assert any("requires a succeeded evaluation" in r for r in blocked.value.reasons)

    # An evaluation of another version does not count, and nor does a failed one of this version.
    await _eval(settings, "9.9.9", uplift=20, n=7)
    await _eval(settings, version, uplift=40, n=8, status="failed")
    with pytest.raises(library.SkillPublishBlocked):
        await _publish(settings, version)

    await _eval(settings, version, uplift=-3)
    assert (await _publish(settings, version))["status"] == "published"


async def test_an_agents_version_counts_only_a_bundle_evaluation(settings: Settings) -> None:
    operator = await _draft(settings)
    await _publish(settings, operator)
    agent = await _draft(settings, source="agent", parent=operator, body="# Triage\n\nThe agent's edit.\n")
    await set_publish_policy(settings, TENANT, {"require_eval": True}, by="ops")

    await _eval(settings, agent, uplift=50, n=1, source="generated")
    with pytest.raises(library.SkillPublishBlocked):
        await _publish(settings, agent)

    await _eval(settings, agent, uplift=2, n=2, source="bundle")
    assert (await _publish(settings, agent))["status"] == "published"


async def test_min_eval_uplift_reads_the_latest_counting_eval(settings: Settings) -> None:
    version = await _draft(settings)
    await set_publish_policy(settings, TENANT, {"min_eval_uplift": 10}, by="ops")
    await _eval(settings, version, uplift=25, n=1)
    await _eval(settings, version, uplift=4, n=2)  # later, and below the floor

    verdict = await library.evaluate_version(
        settings, TENANT, NAME, version, object_store=object_store(settings)
    )
    assert verdict.reasons == [
        "evaluation 00000000-0000-4000-8000-000000000002 has uplift 4, below the minimum 10"
    ]
    with pytest.raises(library.SkillPublishBlocked):
        await _publish(settings, version)


async def test_a_rollback_skips_the_evaluation_requirement_and_nothing_else(settings: Settings) -> None:
    """A rollback returns to a version that was already live; `require_eval` set after it went
    live does not hold it back. The quality floor still does."""
    first = await _draft(settings)
    await _publish(settings, first)
    second = await _draft(settings, parent=first, body="# Triage\n\nA second version.\n")
    await _eval(settings, second, uplift=5)
    await _publish(settings, second)
    await set_publish_policy(settings, TENANT, {"require_eval": True, "min_eval_uplift": 50}, by="ops")

    rolled = await library.rollback(
        settings, TENANT, NAME, first, by="ops", object_store=object_store(settings)
    )
    assert rolled["status"] == "published"

    await set_publish_policy(settings, TENANT, {"min_quality": 100}, by="ops")
    with pytest.raises(library.SkillPublishBlocked) as blocked:
        await library.rollback(settings, TENANT, NAME, second, by="ops", object_store=object_store(settings))
    assert any("quality score" in r for r in blocked.value.reasons)
    assert not any("evaluation" in r for r in blocked.value.reasons)
