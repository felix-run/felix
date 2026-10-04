"""One contract for the skill library store, run against both backends.

The properties a dict and a table can quietly disagree on: that a version is written once (the
primary key is what settles two saves racing to one version), that publishing moves the live
pointer and archives what it replaced in one step, that a status change lands only on the state
it was decided against, and that one tenant's library is invisible to another.
"""

from __future__ import annotations

import asyncio
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


def _columns(model: Any) -> set[str]:
    return {c.key for c in model.__table__.columns}


@parametrized
async def test_rows_carry_exactly_the_table_columns(store_settings: Any) -> None:
    from felix.db.models import SkillFileRow, SkillRow, SkillVersionRow

    store = get_skill_library_store(store_settings)
    await _save(store, "0.1.0", at=1)
    assert set(await store.get_version("acme", "invoice-triage", "0.1.0") or {}) == _columns(SkillVersionRow)
    assert set(await store.get_skill("acme", "invoice-triage") or {}) == _columns(SkillRow)
    for row in await store.list_files("acme", "invoice-triage", "0.1.0"):
        assert set(row) == _columns(SkillFileRow)


@parametrized
async def test_republishing_an_archived_version_keeps_its_first_publish_time(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _save(store, "0.1.0", at=1)
    await _save(store, "0.1.1", at=2)
    await store.publish("acme", "invoice-triage", "0.1.0", from_statuses={"draft"}, by="ops", at=10)
    await store.publish("acme", "invoice-triage", "0.1.1", from_statuses={"draft"}, by="ops", at=20)

    previous = await store.publish(
        "acme", "invoice-triage", "0.1.0", from_statuses={"archived", "published"}, by="ops", at=30
    )
    assert previous == "0.1.1"
    row = await store.get_version("acme", "invoice-triage", "0.1.0")
    assert row is not None and (row["status"], row["published_at"], row["decided_at"]) == (
        "published",
        10,
        30,
    )
    assert [r["version"] for r in await store.list_live("acme")] == ["0.1.0"]


@parametrized
async def test_list_live_carries_the_skill_md_digest(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _save(store, "0.1.0", at=1)
    assert await store.list_live("acme") == []
    await store.publish("acme", "invoice-triage", "0.1.0", from_statuses={"draft"}, by="ops", at=2)
    assert await store.list_live("acme") == [
        {"name": "invoice-triage", "version": "0.1.0", "sha256": "a" * 64, "lineage_import": False}
    ]
    assert await store.list_live("globex") == []


@parametrized
async def test_deleting_a_draft_keeps_a_skill_with_other_versions(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _save(store, "0.1.0", at=1)
    await _save(store, "0.1.1", at=2)
    await store.delete_draft("acme", "invoice-triage", "0.1.1")
    assert await store.get_skill("acme", "invoice-triage") is not None
    assert await store.version_ids("acme", "invoice-triage") == ["0.1.0"]


@parametrized
async def test_skills_list_in_codepoint_order_and_honour_the_limit(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    for i, name in enumerate(["ab", "a-c", "b"]):
        row = {**_row("0.1.0", at=i), "name": name}
        await store.insert_version("acme", row, [], created_by="ops", at=i)
    # "-" (0x2d) sorts before "b" (0x62) by codepoint, whatever the database's collation.
    assert [s["name"] for s in await store.list_skills("acme")] == ["a-c", "ab", "b"]
    assert [s["name"] for s in await store.list_skills("acme", limit=2)] == ["a-c", "ab"]


@parametrized
async def test_a_publish_racing_a_reject_has_one_winner(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _save(store, "0.1.0", at=1)
    results = await asyncio.gather(
        store.publish("acme", "invoice-triage", "0.1.0", from_statuses={"draft"}, by="ops", at=5),
        store.reject("acme", "invoice-triage", "0.1.0", by="ops", note="no", at=5),
        return_exceptions=True,
    )
    conflicts = [r for r in results if isinstance(r, SkillStateConflict)]
    assert len(conflicts) == 1, results
    row = await store.get_version("acme", "invoice-triage", "0.1.0")
    skill = await store.get_skill("acme", "invoice-triage")
    assert row is not None and skill is not None
    if row["status"] == "published":
        assert skill["live_version"] == "0.1.0"
    else:
        assert row["status"] == "archived" and skill["live_version"] is None


@parametrized
async def test_two_publishes_leave_exactly_one_published_version(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _save(store, "0.1.0", at=1)
    await _save(store, "0.1.1", at=2)
    await asyncio.gather(
        store.publish("acme", "invoice-triage", "0.1.0", from_statuses={"draft"}, by="ops", at=5),
        store.publish("acme", "invoice-triage", "0.1.1", from_statuses={"draft"}, by="ops", at=6),
    )
    versions = await store.list_versions("acme", "invoice-triage")
    published = [v["version"] for v in versions if v["status"] == "published"]
    skill = await store.get_skill("acme", "invoice-triage")
    assert len(published) == 1 and skill is not None and skill["live_version"] == published[0]


@parametrized
async def test_concurrent_agent_saves_stay_within_the_pending_cap(store_settings: Any) -> None:
    from felix.skills import library
    from felix.skills.format import serialize_skill_md
    from felix.storage import MemoryObjectStore

    files = {
        "SKILL.md": serialize_skill_md({"name": "race-skill", "description": "d"}, "\n# Race\n\nSteps.\n")
    }
    who = library.DraftProvenance(source="agent", author="m", origin_manifest_id="m")
    objects = MemoryObjectStore()
    results = await asyncio.gather(
        *(
            library.save_draft(
                store_settings, "acme", files=files, provenance=who, max_pending=1, object_store=objects
            )
            for _ in range(3)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, library.SkillPendingCapReached) for r in results) == 2, results
    assert all(isinstance(r, dict | library.SkillLibraryError) for r in results), results
    assert await get_skill_library_store(store_settings).count_pending("acme", "m") == 1


async def _named(store: Any, name: str, version: str, *, at: int, tenant: str = "acme") -> None:
    await store.insert_version(tenant, {**_row(version, at=at), "name": name}, [], created_by="ops", at=at)


@parametrized
async def test_skills_page_after_a_name_in_codepoint_order(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    for i, name in enumerate(["ab", "a-c", "b"]):
        await _named(store, name, "0.1.0", at=i)
    assert [s["name"] for s in await store.list_skills("acme", after="a-c")] == ["ab", "b"]
    assert [s["name"] for s in await store.list_skills("acme", limit=1, after="ab")] == ["b"]
    assert await store.list_skills("acme", after="b") == []


@parametrized
async def test_the_review_queue_is_every_draft_oldest_first_and_pages(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    # Two drafts in one millisecond across skills, a tie on name broken by version, a
    # published version that must not appear, and another tenant's draft.
    await _named(store, "b-skill", "0.1.0", at=5)
    await _named(store, "a-skill", "0.1.0", at=5)
    await _named(store, "a-skill", "0.1.1", at=5)
    await _named(store, "c-skill", "0.1.0", at=1)
    await _named(store, "c-skill", "0.1.1", at=9)
    await store.publish("acme", "c-skill", "0.1.0", from_statuses={"draft"}, by="ops", at=10)
    await _named(store, "a-skill", "0.1.0", at=0, tenant="globex")

    queue = await store.list_drafts("acme")
    keys = [(d["created_at"], d["name"], d["version"]) for d in queue]
    assert keys == [
        (5, "a-skill", "0.1.0"),
        (5, "a-skill", "0.1.1"),
        (5, "b-skill", "0.1.0"),
        (9, "c-skill", "0.1.1"),
    ]
    page = await store.list_drafts("acme", limit=2)
    rest = await store.list_drafts(
        "acme", after=(page[-1]["created_at"], page[-1]["name"], page[-1]["version"])
    )
    assert [(d["name"], d["version"]) for d in page + rest] == [(k[1], k[2]) for k in keys]
    assert all(d["tenant_id"] == "acme" and d["status"] == "draft" for d in queue)


@parametrized
async def test_summaries_name_the_newest_version_and_count_drafts(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _named(store, "a-skill", "0.1.0", at=1)
    await _named(store, "a-skill", "0.1.1", at=2)
    await _named(store, "a-skill", "0.1.2", at=2)  # a tie: the higher version is the newer
    await store.publish("acme", "a-skill", "0.1.0", from_statuses={"draft"}, by="ops", at=3)
    await _named(store, "b-skill", "0.1.0", at=1)
    await _named(store, "b-skill", "0.1.0", at=1, tenant="globex")

    summary = await store.summarize("acme", ["a-skill", "b-skill", "missing"])
    assert set(summary) == {"a-skill", "b-skill"}
    assert summary["a-skill"]["pending"] == 2
    assert summary["a-skill"]["latest"] == {
        "version": "0.1.2",
        "status": "draft",
        "source": "agent",
        "quality_score": 70,
        "security_status": "pass",
        "created_at": 2,
    }
    assert summary["b-skill"]["pending"] == 1
    assert await store.summarize("acme", []) == {}
    assert await store.summarize("initech", ["a-skill"]) == {}


@parametrized
async def test_skills_are_fetched_by_name_in_one_call_and_tenant_scoped(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _named(store, "a-skill", "0.1.0", at=1)
    await _named(store, "b-skill", "0.1.0", at=1)
    await _named(store, "c-skill", "0.1.0", at=1, tenant="globex")
    await store.publish("acme", "b-skill", "0.1.0", from_statuses={"draft"}, by="ops", at=2)

    found = await store.get_skills("acme", ["a-skill", "b-skill", "c-skill", "missing", "a-skill"])
    assert sorted(found) == ["a-skill", "b-skill"]
    assert found["b-skill"]["live_version"] == "0.1.0" and found["a-skill"]["live_version"] is None
    assert found["a-skill"] == await store.get_skill("acme", "a-skill")
    assert await store.get_skills("acme", []) == {}


@parametrized
async def test_the_review_queue_pages_one_at_a_time_across_a_millisecond(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    await _named(store, "a-skill", "0.1.1", at=5)
    await _named(store, "b-skill", "0.1.0", at=5)
    await _named(store, "a-skill", "0.1.0", at=5)
    # Rows the queue must skip, placed after the cursor's position rather than before it.
    await _named(store, "c-skill", "0.1.0", at=7)
    await store.publish("acme", "c-skill", "0.1.0", from_statuses={"draft"}, by="ops", at=8)
    await _named(store, "a-skill", "0.1.0", at=7, tenant="globex")
    await _named(store, "d-skill", "0.1.0", at=9)

    seen: list[tuple[str, str]] = []
    after = None
    for _ in range(10):
        page = await store.list_drafts("acme", limit=1, after=after)
        if not page:
            break
        (row,) = page
        seen.append((row["name"], row["version"]))
        after = (row["created_at"], row["name"], row["version"])
    assert seen == [("a-skill", "0.1.0"), ("a-skill", "0.1.1"), ("b-skill", "0.1.0"), ("d-skill", "0.1.0")]


@parametrized
async def test_a_publish_can_require_the_live_version_it_saw(store_settings: Any) -> None:
    from felix.skills.library_store import SkillLiveMismatch

    store = get_skill_library_store(store_settings)
    for n, version in enumerate(("0.1.0", "0.1.1", "0.1.2"), start=1):
        await _save(store, version, at=n)

    # Expected "nothing live", and nothing is: it lands.
    assert (
        await store.publish(
            "acme", "invoice-triage", "0.1.0", from_statuses={"draft"}, by="a", at=10, expected_live=None
        )
        is None
    )
    # Expected nothing live, but 0.1.0 is: refused, and nothing moved.
    with pytest.raises(SkillLiveMismatch):
        await store.publish(
            "acme", "invoice-triage", "0.1.1", from_statuses={"draft"}, by="b", at=11, expected_live=None
        )
    with pytest.raises(SkillLiveMismatch):
        await store.publish(
            "acme", "invoice-triage", "0.1.1", from_statuses={"draft"}, by="b", at=11, expected_live="0.1.2"
        )
    skill = await store.get_skill("acme", "invoice-triage")
    assert skill is not None and skill["live_version"] == "0.1.0"
    assert (await store.get_version("acme", "invoice-triage", "0.1.1") or {})["status"] == "draft"
    # The right expectation lands; saying nothing lands whatever is live.
    assert (
        await store.publish(
            "acme", "invoice-triage", "0.1.1", from_statuses={"draft"}, by="b", at=12, expected_live="0.1.0"
        )
        == "0.1.0"
    )
    assert (
        await store.publish("acme", "invoice-triage", "0.1.2", from_statuses={"draft"}, by="c", at=13)
        == "0.1.1"
    )


@parametrized
async def test_buildable_versions_leave_out_rejected_drafts_only(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    for n, version in enumerate(("0.1.0", "0.1.1", "0.1.2", "0.1.3"), start=1):
        await _save(store, version, at=n)
    await store.publish("acme", "invoice-triage", "0.1.0", from_statuses={"draft"}, by="o", at=10)
    await store.publish(
        "acme", "invoice-triage", "0.1.1", from_statuses={"draft"}, by="o", at=11
    )  # 0.1.0 archived, once live
    await store.reject("acme", "invoice-triage", "0.1.2", by="o", note="bad script", at=12)
    await _save(store, "0.1.0", at=1, tenant="globex")

    assert await store.buildable_versions("acme", ["invoice-triage", "nope"]) == {
        "invoice-triage": ["0.1.0", "0.1.1", "0.1.3"]
    }
    assert await store.buildable_versions("globex", ["invoice-triage"]) == {"invoice-triage": ["0.1.0"]}
    assert await store.buildable_versions("acme", []) == {}


ORIGIN = {
    "origin_source": "github:acme/skills/skills/invoice-triage",
    "origin_ref": "main",
    "origin_commit": "c" * 40,
    "origin_tree_hash": "d" * 64,
    "origin_license": "MIT",
    # Past 2**31: a BigInteger column, as every epoch-ms column here is.
    "origin_committed_at": 1_750_000_000_000,
}


@parametrized
async def test_an_imported_version_keeps_its_origin_and_others_read_back_null(store_settings: Any) -> None:
    """The `import` source and its five origin columns: written and read back alike on both arms
    (the check constraint admits the source), and null -- not missing -- on a version that is
    not an import, as Postgres returns them."""
    store = get_skill_library_store(store_settings)
    row = {**_row("0.1.0", at=1, source="import", origin=None), **ORIGIN}
    await store.insert_version("acme", row, FILES, created_by="ops", at=1)
    await _save(store, "0.1.1", at=2)

    imported = await store.get_version("acme", "invoice-triage", "0.1.0")
    assert imported is not None and imported["source"] == "import"
    assert {k: imported[k] for k in ORIGIN} == ORIGIN
    agent = await store.get_version("acme", "invoice-triage", "0.1.1")
    assert agent is not None and {k: agent[k] for k in ORIGIN} == dict.fromkeys(ORIGIN)
    newest, oldest = await store.list_versions("acme", "invoice-triage")
    assert oldest["origin_commit"] == "c" * 40 and newest["origin_commit"] is None
    assert await store.count_pending("acme", "contributor") == 1, "an import is not an agent draft"


@parametrized
async def test_lineage_reads_back_on_the_version_and_on_the_live_listing(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    imported = {**_row("0.1.0", at=1, source="import", origin=None), **ORIGIN, "lineage_import": True}
    await store.insert_version("acme", imported, FILES, created_by="ops", at=1)
    await _save(store, "0.1.1", at=2)
    await store.publish("acme", "invoice-triage", "0.1.0", from_statuses={"draft"}, by="ops", at=3)

    assert (await store.get_version("acme", "invoice-triage", "0.1.0") or {})["lineage_import"] is True
    assert (await store.get_version("acme", "invoice-triage", "0.1.1") or {})["lineage_import"] is False
    (live,) = await store.list_live("acme")
    assert (live["version"], live["lineage_import"]) == ("0.1.0", True)


@parametrized
async def test_a_sighting_keeps_its_first_stamp_and_is_tenant_scoped(store_settings: Any) -> None:
    from felix.skills.sighting_store import get_sighting_store

    sightings = get_sighting_store(store_settings)
    src = "github:acme/skills/skills/invoice-triage"
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    first = await sightings.first_seen("acme", [(src, a), (src, b)], at=1_750_000_000_000)
    assert first == {(src, a): 1_750_000_000_000, (src, b): 1_750_000_000_000}
    again = await sightings.first_seen("acme", [(src, a), (src, c)], at=1_760_000_000_000)
    assert again == {(src, a): 1_750_000_000_000, (src, c): 1_760_000_000_000}
    assert await sightings.first_seen("globex", [(src, a)], at=1_770_000_000_000) == {
        (src, a): 1_770_000_000_000
    }
    assert await sightings.first_seen("acme", [], at=1) == {}


@parametrized
async def test_a_file_of_an_import_lineage_version_is_found_by_its_digest(store_settings: Any) -> None:
    store = get_skill_library_store(store_settings)
    imported = {**_row("0.1.0", at=1, source="import", origin=None), **ORIGIN, "lineage_import": True}
    await store.insert_version("acme", imported, FILES, created_by="ops", at=1)
    own = [{"path": "SKILL.md", "sha256": "e" * 64, "size": 4}]
    await store.insert_version(
        "acme", {**_row("0.1.0", at=2), "name": "own-notes"}, own, created_by="c", at=2
    )

    assert await store.holds_imported_file("acme", ["b" * 64, "f" * 64]) is True
    assert await store.holds_imported_file("acme", ["e" * 64]) is False, "a file of a version not imported"
    assert await store.holds_imported_file("globex", ["b" * 64]) is False
    assert await store.holds_imported_file("acme", []) is False


@parametrized
async def test_pruning_sightings_drops_every_tenants_old_rows_only(store_settings: Any) -> None:
    from felix.skills.sighting_store import get_sighting_store

    sightings = get_sighting_store(store_settings)
    src = "github:acme/skills/skills/invoice-triage"
    await sightings.first_seen("acme", [(src, "a" * 64)], at=1_000)
    await sightings.first_seen("globex", [(src, "a" * 64)], at=2_000)
    await sightings.first_seen("acme", [(src, "b" * 64)], at=9_000)

    assert await sightings.prune(before=5_000) == 2
    again = await sightings.first_seen("acme", [(src, "a" * 64), (src, "b" * 64)], at=10_000)
    assert again == {(src, "a" * 64): 10_000, (src, "b" * 64): 9_000}
