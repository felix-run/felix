"""The publish policy: a tenant's row ahead of the settings, and the gate's two evaluation blocks.

Precedence is asserted through `library.load_publish_policy`, the read every publish, rollback
and preview makes; the evaluation blocks through `library.publish`, so what is proven is that
the gate *loads* the evaluation it needs, not only that `policy_reasons` would use one if handed
it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.skills import library
from felix.skills.publish_gate import Assessment, PublishPolicy, policy_reasons, publish_policy
from felix.skills.quality_store import get_skill_eval_store, get_skill_policy_store

from tests.skill_quality import NAME, TENANT, bundle, object_store


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="memory://skill-policy",
        object_store="memory",
        data_dir=str(tmp_path),
        skill_publish_min_quality=30,
        skill_publish_block_on_advisory=True,
    )


async def _draft(settings: Settings) -> str:
    row = await library.save_draft(
        settings,
        TENANT,
        files=bundle(),
        provenance=library.DraftProvenance(source="operator", author="ops"),
        object_store=object_store(settings),
    )
    return str(row["version"])


async def _succeeded(settings: Settings, version: str, uplift: int, n: int = 1) -> None:
    store = get_skill_eval_store(settings)
    eval_id = f"00000000-0000-4000-8000-{n:012d}"
    await store.insert(
        TENANT,
        {"id": eval_id, "name": NAME, "version": version, "status": "queued", "created_at": n},
    )
    await store.claim(TENANT, eval_id, now=100 + n)
    await store.finish(
        TENANT,
        eval_id,
        started_at=100 + n,
        fields={"status": "succeeded", "uplift": uplift, "finished_at": 200 + n},
    )


async def _publish(settings: Settings, version: str) -> Any:
    return await library.publish(
        settings, TENANT, NAME, version, by="ops", object_store=object_store(settings)
    )


# -- precedence ------------------------------------------------------------------------------


async def test_without_a_tenant_row_the_settings_decide(settings: Settings) -> None:
    policy = await library.load_publish_policy(settings, TENANT)
    assert policy == PublishPolicy(min_quality=30, block_on_advisory=True, source="settings")


async def test_a_tenant_row_replaces_the_settings_whole(settings: Settings) -> None:
    await get_skill_policy_store(settings).put(
        TENANT,
        {
            "min_quality": 0,
            "block_on_advisory": False,
            "require_eval": True,
            "min_eval_uplift": 5,
            "updated_at": 1,
            "updated_by": "ops",
        },
    )

    policy = await library.load_publish_policy(settings, TENANT)

    # Lower than the settings on two fields: the row is the tenant's whole policy, not an overlay.
    assert policy == PublishPolicy(
        min_quality=0, block_on_advisory=False, require_eval=True, min_eval_uplift=5, source="tenant"
    )
    assert (await library.load_publish_policy(settings, "globex")).source == "settings"


async def test_a_partial_change_copies_the_settings_into_the_tenant_row(settings: Settings) -> None:
    from felix.audit import store as audit_store

    after = await library.set_publish_policy(settings, TENANT, {"require_eval": True}, by="ops")

    assert after == PublishPolicy(min_quality=30, block_on_advisory=True, require_eval=True, source="tenant")
    cleared = await library.set_publish_policy(settings, TENANT, {"min_eval_uplift": 3}, by="ops")
    assert (cleared.require_eval, cleared.min_eval_uplift) == (True, 3)
    again = await library.set_publish_policy(settings, TENANT, {"min_eval_uplift": None}, by="ops")
    assert again.min_eval_uplift is None and again.require_eval is True
    await audit_store.flush_pending(settings)
    rows, _ = await audit_store.list_events(settings, TENANT, limit=50)
    # Three updates can share a millisecond, so the events are matched by content, not by `ts`.
    events = [r["payload_json"] for r in rows if r["event_type"] == "skill_policy_updated"]
    assert sorted(e["before"]["source"] for e in events) == ["settings", "tenant", "tenant"]
    (first,) = [e for e in events if e["before"]["source"] == "settings"]
    assert first["after"] == {
        "min_quality": 30,
        "block_on_advisory": True,
        "require_eval": True,
        "min_eval_uplift": None,
    }


def test_the_settings_alone_never_require_an_evaluation(settings: Settings) -> None:
    assert publish_policy(settings, None).needs_eval is False


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


async def test_require_eval_blocks_a_publish_until_the_version_has_a_succeeded_eval(
    settings: Settings,
) -> None:
    version = await _draft(settings)
    await library.set_publish_policy(settings, TENANT, {"require_eval": True}, by="ops")

    with pytest.raises(library.SkillPublishBlocked) as blocked:
        await _publish(settings, version)
    assert any("requires a succeeded evaluation" in r for r in blocked.value.reasons)

    # An evaluation of another version does not count.
    await _succeeded(settings, "9.9.9", uplift=20, n=7)
    with pytest.raises(library.SkillPublishBlocked):
        await _publish(settings, version)

    await _succeeded(settings, version, uplift=-3)
    assert (await _publish(settings, version))["status"] == "published"


async def test_min_eval_uplift_reads_the_latest_succeeded_eval(settings: Settings) -> None:
    version = await _draft(settings)
    await library.set_publish_policy(settings, TENANT, {"min_eval_uplift": 10}, by="ops")
    await _succeeded(settings, version, uplift=25, n=1)
    await _succeeded(settings, version, uplift=4, n=2)  # later, and below the floor

    verdict = await library.evaluate_version(
        settings, TENANT, NAME, version, object_store=object_store(settings)
    )
    assert verdict.reasons == [
        "evaluation 00000000-0000-4000-8000-000000000002 has uplift 4, below the minimum 10"
    ]
    with pytest.raises(library.SkillPublishBlocked):
        await _publish(settings, version)
