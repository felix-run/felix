"""One contract for the skill library store, run against both backends.

The properties a dict and a table can quietly disagree on: that a version is written once (the
primary key is what settles two saves racing to one version), that publishing moves the live
pointer and archives what it replaced in one step, that a status change lands only on the state
it was decided against, and that one tenant's library is invisible to another.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.skills.library_store import SkillStateConflict, SkillVersionExists, get_skill_library_store

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)


def _row(
    version: str, *, at: int, source: str = "agent", origin: str | None = "contributor"
) -> dict[str, Any]:
    return {
        "name": "invoice-triage",
        "version": version,
        "status": "draft",
        "source": source,
        "author": "contributor",
        "origin_manifest_id": origin,
        "reason": "seen twice",
        "description": "Triage invoices.",
        "quality_score": 70,
        "security_status": "pass",
        "security_issues": [],
        "review_checks": [{"id": "valid-bundle", "passed": True}],
        "created_at": at,
    }


FILES = [
    {"path": "SKILL.md", "sha256": "a" * 64, "size": 10},
    {"path": "references/x.md", "sha256": "b" * 64, "size": 3},
]


async def _save(store: Any, version: str, *, at: int, tenant: str = "acme", **kw: Any) -> None:
    await store.insert_version(tenant, _row(version, at=at, **kw), FILES, created_by="contributor", at=at)


@parametrized
async def test_a_version_is_written_once(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _save(store, "0.1.0", at=1)
    with pytest.raises(SkillVersionExists):
        await _save(store, "0.1.0", at=2)

    (version,) = await store.list_versions("acme", "invoice-triage")
    assert version["created_at"] == 1
    assert version["review_checks"] == [{"id": "valid-bundle", "passed": True}]
    assert version["decided_by"] is None and version["published_at"] is None
    assert [f["path"] for f in await store.list_files("acme", "invoice-triage", "0.1.0")] == [
        "SKILL.md",
        "references/x.md",
    ]
    skill = await store.get_skill("acme", "invoice-triage")
    assert skill is not None and skill["live_version"] is None and skill["created_by"] == "contributor"


@parametrized
async def test_publishing_moves_live_and_archives_the_previous_version(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _save(store, "0.1.0", at=1)
    await _save(store, "0.1.1", at=2)

    assert (
        await store.publish("acme", "invoice-triage", "0.1.0", from_statuses={"draft"}, by="ops", at=10)
        is None
    )
    previous = await store.publish(
        "acme", "invoice-triage", "0.1.1", from_statuses={"draft"}, by="ops", at=20
    )

    assert previous == "0.1.0"
    skill = await store.get_skill("acme", "invoice-triage")
    assert skill is not None and skill["live_version"] == "0.1.1"
    first = await store.get_version("acme", "invoice-triage", "0.1.0")
    second = await store.get_version("acme", "invoice-triage", "0.1.1")
    assert first is not None and first["status"] == "archived" and first["published_at"] == 10
    assert second is not None and second["status"] == "published" and second["decided_by"] == "ops"


@parametrized
async def test_a_publish_from_the_wrong_state_changes_nothing(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _save(store, "0.1.0", at=1)
    await store.reject("acme", "invoice-triage", "0.1.0", by="ops", note="no", at=5)

    with pytest.raises(SkillStateConflict):
        await store.publish("acme", "invoice-triage", "0.1.0", from_statuses={"draft"}, by="ops", at=6)
    with pytest.raises(SkillStateConflict):
        await store.reject("acme", "invoice-triage", "0.1.0", by="ops", note="again", at=7)

    row = await store.get_version("acme", "invoice-triage", "0.1.0")
    assert row is not None and row["status"] == "archived" and row["decision_note"] == "no"
    skill = await store.get_skill("acme", "invoice-triage")
    assert skill is not None and skill["live_version"] is None


@parametrized
async def test_archiving_clears_live_and_keeps_history(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _save(store, "0.1.0", at=1)
    await store.publish("acme", "invoice-triage", "0.1.0", from_statuses={"draft"}, by="ops", at=10)

    assert await store.archive_skill("acme", "invoice-triage", by="ops", at=20) == "0.1.0"
    skill = await store.get_skill("acme", "invoice-triage")
    assert skill is not None and skill["live_version"] is None
    row = await store.get_version("acme", "invoice-triage", "0.1.0")
    assert row is not None and row["status"] == "archived" and row["published_at"] == 10
    with pytest.raises(SkillStateConflict):
        await store.archive_skill("acme", "nothing-here", by="ops", at=21)


@parametrized
async def test_pending_counts_one_manifests_agent_drafts(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _save(store, "0.1.0", at=1)
    await _save(store, "0.1.1", at=2)
    await _save(store, "0.1.2", at=3, source="operator")
    await _save(store, "0.1.3", at=4, origin="other")
    await store.reject("acme", "invoice-triage", "0.1.1", by="ops", note="", at=5)

    assert await store.count_pending("acme", "contributor") == 1
    assert await store.count_pending("globex", "contributor") == 0


@parametrized
async def test_listings_are_ordered_and_tenant_scoped(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    # Same millisecond: the order must still be one order, newest version first on the tie.
    await _save(store, "0.1.0", at=7)
    await _save(store, "0.1.1", at=7)
    await _save(store, "0.2.0", at=9)
    await _save(store, "0.1.0", at=1, tenant="globex")

    versions = await store.list_versions("acme", "invoice-triage")
    assert [v["version"] for v in versions] == ["0.2.0", "0.1.1", "0.1.0"]
    assert [v["version"] for v in await store.list_versions("acme", "invoice-triage", limit=2)] == [
        "0.2.0",
        "0.1.1",
    ]
    assert await store.version_ids("acme", "invoice-triage") == ["0.1.0", "0.1.1", "0.2.0"]
    assert [s["name"] for s in await store.list_skills("acme")] == ["invoice-triage"]
    assert await store.list_skills("initech") == []
    assert await store.get_version("initech", "invoice-triage", "0.1.0") is None


@parametrized
async def test_deleting_a_draft_removes_an_otherwise_empty_skill(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _save(store, "0.1.0", at=1)
    await store.delete_draft("acme", "invoice-triage", "0.1.0")

    assert await store.get_skill("acme", "invoice-triage") is None
    assert await store.list_files("acme", "invoice-triage", "0.1.0") == []

    await _save(store, "0.1.0", at=2)
    await store.publish("acme", "invoice-triage", "0.1.0", from_statuses={"draft"}, by="ops", at=3)
    # Only a draft may be taken back; a published version is history.
    await store.delete_draft("acme", "invoice-triage", "0.1.0")
    assert await store.get_version("acme", "invoice-triage", "0.1.0") is not None
