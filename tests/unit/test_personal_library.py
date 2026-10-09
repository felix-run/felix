"""The library layer writing a personal library: the same gate, a few refusals of its own.

Everything a save, publish, rollback, reject or archive does to the tenant's library it does to a
person's, through the same gate -- these pin that it happens in the right library and nowhere
else, and the differences: an import or an adopt is the tenant's alone, a person's library has a
size, an evaluation of the tenant's skill of a name never counts for a person's, a personal skill
splits no operator upload's name, and the audit trail says whose library changed.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.skills import library
from felix.skills.format import serialize_skill_md
from felix.skills.library_keys import ORG_OWNER, library_label
from felix.skills.library_store import get_skill_library_store
from felix.storage import MemoryObjectStore

ALICE = "https://id.example|alice"
BODY = "\n# Notes\n\nUse this when taking notes.\n\n## Steps\n\n1. Write it down.\n2. File it.\n"


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="memory://personal-library")


@pytest.fixture
def store() -> MemoryObjectStore:
    return MemoryObjectStore()


def _bundle(name: str = "notes", body: str = BODY) -> dict[str, str]:
    return {"SKILL.md": serialize_skill_md({"name": name, "description": "Take notes."}, body)}


async def _save(
    settings: Settings, store: MemoryObjectStore, owner: str, name: str = "notes", **kw: Any
) -> Any:
    provenance = kw.pop("provenance", library.DraftProvenance(source="operator", author="someone"))
    return await library.save_draft(
        settings,
        "acme",
        files=kw.pop("files", _bundle(name)),
        provenance=provenance,
        object_store=store,
        owner=owner,
        **kw,
    )


async def _events(settings: Settings) -> list[dict[str, Any]]:
    from felix.audit import store as audit_store

    await audit_store.flush_pending(settings)
    events, _ = await audit_store.list_events(settings, "acme", limit=100)
    return sorted(events, key=lambda e: e["ts"])


async def test_a_personal_skill_lives_its_whole_life_in_its_owners_library(
    settings: Settings, store: MemoryObjectStore
) -> None:
    org, alice = (
        get_skill_library_store(settings, owner=ORG_OWNER),
        get_skill_library_store(settings, owner=ALICE),
    )
    first = await _save(settings, store, ALICE)
    await library.publish(
        settings, "acme", "notes", first["version"], by=ALICE, object_store=store, owner=ALICE
    )
    second = await _save(settings, store, ALICE, files=_bundle(body=BODY + "\n3. Re-read it.\n"))
    await library.reject(settings, "acme", "notes", second["version"], by=ALICE, note="no", owner=ALICE)
    third = await _save(settings, store, ALICE, files=_bundle(body=BODY + "\n3. Share it.\n"))
    await library.publish(
        settings, "acme", "notes", third["version"], by=ALICE, object_store=store, owner=ALICE
    )
    await library.rollback(
        settings, "acme", "notes", first["version"], by=ALICE, object_store=store, owner=ALICE
    )
    assert (await alice.get_skill("acme", "notes") or {})["live_version"] == first["version"]
    await library.archive_skill(settings, "acme", "notes", by=ALICE, owner=ALICE)

    assert (await alice.get_skill("acme", "notes") or {})["live_version"] is None
    assert len(await alice.version_ids("acme", "notes")) == 3
    assert await org.get_skill("acme", "notes") is None, "nothing reached the tenant's library"
    assert await store.get(alice.object_key("acme", "notes", first["version"], "SKILL.md"))
    assert not await store.exists(org.object_key("acme", "notes", first["version"], "SKILL.md"))


async def test_the_publish_gate_judges_a_personal_version_as_it_judges_the_tenants(
    settings: Settings, store: MemoryObjectStore
) -> None:
    gated = settings.model_copy(update={"skill_publish_min_quality": 100})
    saved = await _save(gated, store, ALICE)
    with pytest.raises(library.SkillPublishBlocked):
        await library.publish(
            gated, "acme", "notes", saved["version"], by=ALICE, object_store=store, owner=ALICE
        )


async def test_an_evaluation_of_the_tenants_skill_never_counts_for_a_persons(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """Evaluations are kept by skill name for the tenant's library. A person's version is judged
    without them -- not by one that happens to share its name and version."""
    needs_eval = settings.model_copy(update={"skill_publish_require_eval": True})
    org = await _save(needs_eval, store, ORG_OWNER)
    with pytest.raises(library.SkillPublishBlocked):
        await library.publish(
            needs_eval, "acme", "notes", org["version"], by="ops", object_store=store, owner=ORG_OWNER
        )
    mine = await _save(needs_eval, store, ALICE)
    verdict = await library.evaluate_version(
        needs_eval, "acme", "notes", mine["version"], object_store=store, owner=ALICE
    )
    assert verdict.passes, verdict.reasons


@pytest.mark.parametrize(
    "provenance",
    [
        library.DraftProvenance(
            source="import",
            author="ops",
            origin=library.ImportOrigin(
                source="github:acme/skills/notes", ref="main", commit="a" * 40, tree_hash="b" * 40
            ),
        ),
        library.DraftProvenance(source="operator", author="ops", reason="vouched", adopted_from="0.1.0"),
    ],
    ids=["import", "adopt"],
)
async def test_imports_and_adopts_are_the_tenants_alone(
    settings: Settings, store: MemoryObjectStore, provenance: Any
) -> None:
    with pytest.raises(library.SkillOrgOnly):
        await _save(settings, store, ALICE, provenance=provenance)
    assert await get_skill_library_store(settings, owner=ALICE).get_skill("acme", "notes") is None


async def test_a_personal_library_has_a_size_and_editing_a_skill_in_it_does_not_count(
    settings: Settings, store: MemoryObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(library, "MAX_PERSONAL_SKILLS", 2)
    await _save(settings, store, ALICE, "one", files=_bundle("one"))
    await _save(settings, store, ALICE, "two", files=_bundle("two"))
    with pytest.raises(library.SkillPersonalLibraryFull):
        await _save(settings, store, ALICE, "three", files=_bundle("three"))
    await _save(settings, store, ALICE, "two", files=_bundle("two", BODY + "\n3. Again.\n"))
    for name in ("three", "four"):
        await _save(settings, store, ORG_OWNER, name, files=_bundle(name))  # the tenant's has no such bound


async def test_a_personal_skill_splits_no_operator_uploads_name(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """A personal skill answers no ref, so an upload of its name is never split by it."""
    await store.put(
        "skills/acme/notes/SKILL.md", serialize_skill_md({"name": "notes", "description": "d"}, BODY).encode()
    )
    assert await library.shadows_operator_upload(
        settings, "acme", "notes", object_store=store, owner=ORG_OWNER
    )
    assert not await library.shadows_operator_upload(
        settings, "acme", "notes", object_store=store, owner=ALICE
    )


async def test_the_audit_trail_says_whose_library_changed(
    settings: Settings, store: MemoryObjectStore
) -> None:
    mine = await _save(settings, store, ALICE)
    await library.publish(
        settings, "acme", "notes", mine["version"], by=ALICE, object_store=store, owner=ALICE
    )
    await _save(settings, store, ORG_OWNER, "shared", files=_bundle("shared"))

    events = [(e["event_type"], e["payload_json"].get("library")) for e in await _events(settings)]
    personal = library_label(ALICE)
    assert ("skill_draft_saved", personal) in events and ("skill_published", personal) in events
    assert ("skill_draft_saved", "org") in events
    assert all("alice" not in str(label) for _, label in events)
