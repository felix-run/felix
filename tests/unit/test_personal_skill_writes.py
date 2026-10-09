"""`personal_skills: write`: an agent saving skills into the caller's own library.

`create_skill` saves into the caller's library and `update_skill` edits a skill where its catalog
entry came from. Both need a caller with a library who holds `skills:personal`, and neither ever
falls back to the tenant's library when one is missing -- a save the caller meant for themselves,
landing where every session in the tenant would load it, is the failure this file is about.
Compiled through `build_agent`, so the builder's wiring is what is under test, not a hand-made
tool.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.loader import ManifestParseError, parse_manifest
from felix.skills import library
from felix.skills.format import serialize_skill_md
from felix.skills.library_keys import ORG_OWNER
from felix.skills.library_store import get_skill_library_store
from felix.storage import MemoryObjectStore
from felix.tools.types import Tool, ToolInvocationCtx, tool_output_content

ALICE, BOB = "iss|alice", "iss|bob"
BODY = """# Notes

Use this when taking notes.

## Steps

1. Write it down.
2. File it.
"""
NEW = {"name": "notes", "description": "Take notes the way I do", "body": BODY, "reason": "I repeat it"}


@pytest.fixture
def settings() -> Settings:
    # Not `none`: under it no scope is checked, and `skills:personal` is the point here.
    return Settings(database_url="memory://personal-writes", auth_mode="api_key")


@pytest.fixture
def store() -> MemoryObjectStore:
    return MemoryObjectStore()


def _manifest(personal_skills: str = "write", mode: str = "draft", **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": "writer"},
        "spec": {
            "pattern": "react",
            "personal_skills": personal_skills,
            "skill_authoring": {"enabled": True, "mode": mode},
            **spec,
        },
    }


async def _tools(
    settings: Settings, store: MemoryObjectStore, owner: str | None, **manifest: Any
) -> dict[str, Tool]:
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.provider import InMemoryToolProvider

    agent = await build_agent(
        _manifest(**manifest),
        deps=BuildDeps(
            tools=InMemoryToolProvider(),
            settings=settings,
            tenant_id="acme",
            object_store=store,
            skill_owner=owner,
        ),
        settings=settings,
    )
    return {t.name: t for t in agent.tools}


def _ctx(settings: Settings, owner: str | None, *scopes: str) -> RequestContext:
    auth = AuthContext(
        principal_sub="alice", tenant_id="acme", scopes=frozenset(scopes), anonymous=False, skill_owner=owner
    )
    return RequestContext(settings=settings, auth=auth, manifest_id="writer")


async def _call(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    out = await tool.executor.execute(args, ToolInvocationCtx(thread_id="acme:t1", tool_call_id="c1"))
    return json.loads(tool_output_content(out))


async def _publish(
    settings: Settings, store: MemoryObjectStore, owner: str, name: str, description: str
) -> str:
    files = {"SKILL.md": serialize_skill_md({"name": name, "description": description}, f"\n{BODY}")}
    row = await library.save_draft(
        settings,
        "acme",
        files=files,
        provenance=library.DraftProvenance(source="agent", author="seed"),
        object_store=store,
        owner=owner,
    )
    await library.publish(settings, "acme", name, row["version"], by="seed", object_store=store, owner=owner)
    return str(row["version"])


async def _versions(settings: Settings, owner: str, name: str) -> list[str]:
    return await get_skill_library_store(settings, owner=owner).version_ids("acme", name)


# -- the manifest field ---------------------------------------------------------------------------


def test_write_points_skill_authoring_and_needs_it() -> None:
    def spec(**s: Any) -> Any:
        return parse_manifest(
            {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "m"}, "spec": s}
        )

    assert spec(personal_skills="write", skill_authoring={"enabled": True}).spec.personal_skills == "write"
    with pytest.raises(ManifestParseError, match="skill_authoring"):
        spec(personal_skills="write")
    with pytest.raises(ManifestParseError, match="skills_declared_only"):
        spec(personal_skills="write", skill_authoring={"enabled": True}, skills_declared_only=True)


# -- create_skill ---------------------------------------------------------------------------------


async def test_create_saves_into_the_callers_own_library(
    settings: Settings, store: MemoryObjectStore
) -> None:
    tools = await _tools(settings, store, ALICE)
    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        preview = await tools["create_skill"].approval_preview(NEW)
        result = await _call(tools["create_skill"], NEW)

    assert result["status"] == "draft" and result["library"] == "personal", result
    assert await _versions(settings, ALICE, "notes") == [result["version"]]
    assert await _versions(settings, ORG_OWNER, "notes") == [], "the tenant's library is untouched"
    assert "caller's own skill library" in preview, "an approver is told where it saves"


async def test_a_caller_without_skills_personal_saves_nothing(
    settings: Settings, store: MemoryObjectStore
) -> None:
    tools = await _tools(settings, store, ALICE)
    async with async_run_with_context(_ctx(settings, ALICE, "skills:write")):
        result = await _call(tools["create_skill"], NEW)
    assert result["error"] == "missing_scope", result
    assert await _versions(settings, ALICE, "notes") == []
    assert await _versions(settings, ORG_OWNER, "notes") == []


async def test_a_caller_with_no_library_is_refused_not_redirected(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """An anonymous caller, or a key without its own subject, has no library; their save does
    not become the tenant's."""
    tools = await _tools(settings, store, None)
    async with async_run_with_context(_ctx(settings, None, "skills:personal", "skills:write")):
        result = await _call(tools["create_skill"], NEW)
    assert result["error"] == "no_personal_library", result
    assert await _versions(settings, ORG_OWNER, "notes") == []


async def test_a_call_under_someone_elses_context_saves_nothing(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """The tools were compiled for Alice's library; a call running as Bob holds no grant to it."""
    tools = await _tools(settings, store, ALICE)
    async with async_run_with_context(_ctx(settings, BOB, "skills:personal")):
        result = await _call(tools["create_skill"], NEW)
    assert result["error"] == "missing_scope", result
    assert await _versions(settings, ALICE, "notes") == []


async def test_publish_mode_publishes_into_the_callers_library(
    settings: Settings, store: MemoryObjectStore
) -> None:
    from felix.skills.authoring import make_skill_authoring_tools

    tools = {
        t.name: t
        for t in make_skill_authoring_tools(
            settings,
            tenant_id="acme",
            manifest_id="writer",
            mode="publish",
            object_store=store,
            write_personal=True,
            owner=ALICE,
        )
    }
    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        result = await _call(tools["create_skill"], NEW)
    assert result["status"] == "published" and result["library"] == "personal", result
    mine = await get_skill_library_store(settings, owner=ALICE).get_skill("acme", "notes")
    assert mine is not None and mine["live_version"] == result["version"]
    assert await get_skill_library_store(settings, owner=ORG_OWNER).get_skill("acme", "notes") is None


# -- update_skill ---------------------------------------------------------------------------------


async def test_update_edits_a_skill_where_it_lives(settings: Settings, store: MemoryObjectStore) -> None:
    """Alice's `notes` is hers; the tenant's `shared` is the tenant's. Each edit lands beside its
    parent, and the other library is untouched."""
    mine = await _publish(settings, store, ALICE, "notes", "Alice's notes")
    org_notes = await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
    shared = await _publish(settings, store, ORG_OWNER, "shared", "The tenant's shared skill")
    tools = await _tools(settings, store, ALICE)
    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        own = await _call(
            tools["update_skill"],
            {"name": "notes", "parent_version": mine, "body": BODY + "3. Mine.\n", "reason": "r"},
        )
        tenants = await _call(
            tools["update_skill"],
            {"name": "shared", "parent_version": shared, "body": BODY + "3. Ours.\n", "reason": "r"},
        )

    assert own["library"] == "personal", own
    assert await _versions(settings, ALICE, "notes") == sorted([mine, own["version"]])
    assert await _versions(settings, ORG_OWNER, "notes") == [org_notes]
    assert "library" not in tenants and tenants["status"] == "draft", tenants
    assert sorted(await _versions(settings, ORG_OWNER, "shared")) == sorted([shared, tenants["version"]])
    assert await _versions(settings, ALICE, "shared") == []


async def test_update_reaches_a_personal_draft_the_catalog_does_not_hold(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """A skill the agent just created is a draft, so no catalog holds it; its next edit still
    goes to the caller's library, where it is."""
    tools = await _tools(settings, store, ALICE)
    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        created = await _call(tools["create_skill"], NEW)
        edited = await _call(
            tools["update_skill"],
            {
                "name": "notes",
                "parent_version": created["version"],
                "body": BODY + "3. More.\n",
                "reason": "r",
            },
        )
    assert edited["library"] == "personal" and edited["status"] == "draft", edited
    assert len(await _versions(settings, ALICE, "notes")) == 2


async def test_a_tenant_skill_is_edited_as_before_without_skills_personal(
    settings: Settings, store: MemoryObjectStore
) -> None:
    shared = await _publish(settings, store, ORG_OWNER, "shared", "The tenant's shared skill")
    tools = await _tools(settings, store, ALICE)
    async with async_run_with_context(_ctx(settings, ALICE)):
        result = await _call(
            tools["update_skill"],
            {"name": "shared", "parent_version": shared, "body": BODY + "3. Ours.\n", "reason": "r"},
        )
    assert result["status"] == "draft", result


async def test_feedback_still_refuses_a_callers_own_skill(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """Feedback is the tenant's review loop, filed against and accepted into the tenant's skill."""
    await _publish(settings, store, ALICE, "notes", "Alice's notes")
    tools = await _tools(settings, store, ALICE)
    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        filed = await _call(tools["submit_skill_feedback"], {"name": "notes", "body": "Better."})
    assert filed["error"] == "personal_skill"


async def test_a_declared_skill_is_edited_in_the_tenants_library_though_the_caller_has_one(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """The manifest names `notes`, so the catalog holds the tenant's (a caller's skill never
    shadows a named one) and the model read the tenant's: its edit is the tenant's, even though
    Alice's library holds a `notes` too."""
    await _publish(settings, store, ALICE, "notes", "Alice's notes")
    org_notes = await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
    tools = await _tools(settings, store, ALICE, skills=[{"name": "notes"}])
    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        result = await _call(
            tools["update_skill"],
            {"name": "notes", "parent_version": org_notes, "body": BODY + "3. Ours.\n", "reason": "r"},
        )
    assert "library" not in result and result["status"] == "draft", result
    assert len(await _versions(settings, ORG_OWNER, "notes")) == 2
    assert len(await _versions(settings, ALICE, "notes")) == 1
