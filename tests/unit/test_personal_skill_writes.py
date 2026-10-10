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
from felix.skills.authoring import _INTO_OWN_LIBRARY
from felix.skills.format import serialize_skill_md
from felix.skills.library_keys import ORG_OWNER
from felix.skills.library_store import get_skill_library_store
from felix.storage import MemoryObjectStore
from felix.tools.types import Tool, ToolInvocationCtx, tool_output_content

from tests.support.factories import make_settings

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
    return make_settings(auth_mode="api_key")


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
    assert _INTO_OWN_LIBRARY in preview, "an approver is told where it saves"


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
    assert result["status"] == "draft" and "library" not in result, result
    assert len(await _versions(settings, ORG_OWNER, "shared")) == 2


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


# -- approvals bind the library -------------------------------------------------------------------


async def _approve_next(settings: Settings, call: Any, *, decision: Any = "approved") -> dict[str, Any]:
    """Run ``call`` until it waits on a fresh pending approval, decide that row, and return the
    call's result -- with the id of the row it waited on, or None if it waited on none."""
    import asyncio

    from felix.approvals import store as approvals_store
    from felix.approvals.interrupt import signal_decision

    before = {r["id"] for r in await approvals_store.list_approvals(settings, "acme", status=None)}
    task = asyncio.create_task(call)
    row = None
    for _ in range(200):
        if task.done():
            break
        fresh = [r for r in await approvals_store.list_approvals(settings, "acme") if r["id"] not in before]
        if fresh:
            row = fresh[0]
            await approvals_store.decide(settings, "acme", row["id"], decision=decision, decided_by="ops")
            await signal_decision(row["id"], decision)  # what `/approvals/{id}/decide` does next
            break
        await asyncio.sleep(0.01)
    out = await asyncio.wait_for(task, 5)
    return {"out": tool_output_content(out), "approval": row}


async def test_a_grant_for_one_library_authorizes_no_save_into_another(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """Both libraries hold `notes@0.1.0`, so an edit's arguments are the same whoever sends them.
    Alice's approved edit goes into her library; Bob, sending the identical call, would edit the
    tenant's -- and her grant must not be his."""
    from felix.approvals import store as approvals_store

    await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
    await _publish(settings, store, ALICE, "notes", "Alice's notes")
    rule = [{"id": "author", "tools": ["create_skill", "update_skill"], "ttl_seconds": 60}]
    edit = {"name": "notes", "parent_version": "0.1.0", "body": BODY + "3. Mine.\n", "reason": "r"}
    alice = await _tools(settings, store, ALICE, approvals=rule)
    bob = await _tools(settings, store, BOB, approvals=rule)
    ctx = ToolInvocationCtx(thread_id="acme:t1", tool_call_id="c1")

    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        first = await _approve_next(settings, alice["update_skill"].executor.execute(edit, ctx))
    async with async_run_with_context(_ctx(settings, BOB, "skills:personal")):
        bobs = await _approve_next(
            settings, bob["update_skill"].executor.execute(edit, ctx), decision="denied"
        )
    granted = await approvals_store.list_approvals(settings, "acme", status="approved")

    assert first["approval"] is not None and json.loads(first["out"])["library"] == "personal", first
    assert bobs["approval"] is not None, "Bob's call asked for its own approval rather than using Alice's"
    assert "denied" in bobs["out"]
    assert [r["id"] for r in granted] == [first["approval"]["id"]]
    assert await _versions(settings, ORG_OWNER, "notes") == ["0.1.0"], "the tenant's notes is untouched"
    assert len(await _versions(settings, ALICE, "notes")) == 2


async def test_a_grant_for_one_callers_new_skill_is_not_anothers(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """The same `create_skill` call, injected into Bob's session after Alice's was approved,
    would save into Bob's library: it asks for its own approval."""
    rule = [{"id": "author", "tools": ["create_skill", "update_skill"], "ttl_seconds": 60}]
    alice = await _tools(settings, store, ALICE, approvals=rule)
    bob = await _tools(settings, store, BOB, approvals=rule)
    ctx = ToolInvocationCtx(thread_id="acme:t1", tool_call_id="c1")
    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        first = await _approve_next(settings, alice["create_skill"].executor.execute(NEW, ctx))
    async with async_run_with_context(_ctx(settings, BOB, "skills:personal")):
        bobs = await _approve_next(
            settings, bob["create_skill"].executor.execute(NEW, ctx), decision="denied"
        )
    assert first["approval"] is not None and json.loads(first["out"])["library"] == "personal", first
    assert bobs["approval"] is not None and "denied" in bobs["out"], bobs
    assert await _versions(settings, BOB, "notes") == []


# -- what the reviewers asked to pin ---------------------------------------------------------------


async def test_an_edit_of_a_new_personal_draft_stays_personal_though_the_tenant_has_the_name(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """Every library numbers from 0.1.0: the agent's draft `notes@0.1.0` and the tenant's live
    `notes@0.1.0` share a parent_version, so the edit goes to the narrower library."""
    await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
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
    assert created["library"] == edited["library"] == "personal", (created, edited)
    assert len(await _versions(settings, ALICE, "notes")) == 2
    assert await _versions(settings, ORG_OWNER, "notes") == ["0.1.0"]


async def test_a_personal_save_queues_no_evaluation(settings: Settings, store: MemoryObjectStore) -> None:
    """Evaluations run on the tenant's library: queued for a personal `notes`, one would score
    the tenant's `notes` and hand its id back as this save's."""
    from felix.skills.eval_store import get_skill_eval_store

    await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
    tools = await _tools(settings, store, ALICE, skill_authoring={"enabled": True, "auto_eval": True})
    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        result = await _call(tools["create_skill"], NEW)
    assert result["library"] == "personal" and "eval_id" not in result, result
    assert await get_skill_eval_store(settings).list_for_skill("acme", "notes") == []


async def test_publish_mode_holds_a_personal_skill_that_would_replace_a_tenant_one(
    settings: Settings, store: MemoryObjectStore
) -> None:
    from felix.skills.authoring import make_skill_authoring_tools

    await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
    tools = {
        t.name: t
        for t in make_skill_authoring_tools(
            settings,
            tenant_id="acme",
            manifest_id="writer",
            mode="publish",
            object_store=store,
            tenant=frozenset({"notes"}),
            write_personal=True,
            owner=ALICE,
        )
    }
    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        held = await _call(tools["create_skill"], NEW)
        other = await _call(tools["create_skill"], {**NEW, "name": "journal"})
    assert held["status"] == "draft" and "replace the tenant's skill notes" in held["review_required"], held
    assert other["status"] == "published", "a name the tenant does not have publishes as before"
    mine = await get_skill_library_store(settings, owner=ALICE).get_skill("acme", "notes")
    assert mine is not None and mine["live_version"] is None


async def test_publish_mode_holds_a_shadow_of_a_tenant_skill_the_catalog_did_not_hold(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """Published after this agent compiled, the tenant's `notes` is in no catalog yet; the
    library itself says a personal `notes` would replace it."""
    from felix.skills.authoring import make_skill_authoring_tools

    await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
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
        held = await _call(tools["create_skill"], NEW)
    assert held["status"] == "draft" and "review_required" in held, held


async def test_publish_mode_holds_an_edit_of_a_skill_its_owner_wrote(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """A person's own `~me` save is source `operator`: the agent's edit of it waits for them."""
    from felix.skills.authoring import make_skill_authoring_tools

    files = {"SKILL.md": serialize_skill_md({"name": "notes", "description": "Alice's notes"}, f"\n{BODY}")}
    row = await library.save_draft(
        settings,
        "acme",
        files=files,
        provenance=library.DraftProvenance(source="operator", author="alice"),
        object_store=store,
        owner=ALICE,
    )
    await library.publish(
        settings, "acme", "notes", row["version"], by="alice", object_store=store, owner=ALICE
    )
    tools = {
        t.name: t
        for t in make_skill_authoring_tools(
            settings,
            tenant_id="acme",
            manifest_id="writer",
            mode="publish",
            object_store=store,
            personal=frozenset({"notes"}),
            write_personal=True,
            owner=ALICE,
        )
    }
    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        result = await _call(
            tools["update_skill"],
            {"name": "notes", "parent_version": row["version"], "body": BODY + "3. More.\n", "reason": "r"},
        )
    assert result["status"] == "draft" and "by its owner" in result["review_required"], result
    mine = await get_skill_library_store(settings, owner=ALICE).get_skill("acme", "notes")
    assert mine is not None and mine["live_version"] == row["version"]


async def test_an_edit_preview_says_which_library_it_saves_into(
    settings: Settings, store: MemoryObjectStore
) -> None:
    mine = await _publish(settings, store, ALICE, "notes", "Alice's notes")
    shared = await _publish(settings, store, ORG_OWNER, "shared", "The tenant's shared skill")
    tools = await _tools(settings, store, ALICE)
    preview = tools["update_skill"].approval_preview
    assert preview is not None
    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        own = await preview({"name": "notes", "parent_version": mine, "body": "B.", "reason": "r"})
        tenants = await preview({"name": "shared", "parent_version": shared, "body": "B.", "reason": "r"})
    assert _INTO_OWN_LIBRARY in own
    assert _INTO_OWN_LIBRARY not in tenants


async def test_editing_ones_own_skill_also_needs_skills_personal(
    settings: Settings, store: MemoryObjectStore
) -> None:
    mine = await _publish(settings, store, ALICE, "notes", "Alice's notes")
    tools = await _tools(settings, store, ALICE)
    async with async_run_with_context(_ctx(settings, ALICE, "skills:write")):
        result = await _call(
            tools["update_skill"], {"name": "notes", "parent_version": mine, "body": BODY, "reason": "r"}
        )
    assert result["error"] == "missing_scope", result
    assert await _versions(settings, ALICE, "notes") == [mine]


async def test_a_caller_with_no_library_still_edits_a_tenant_skill(
    settings: Settings, store: MemoryObjectStore
) -> None:
    shared = await _publish(settings, store, ORG_OWNER, "shared", "The tenant's shared skill")
    tools = await _tools(settings, store, None)
    async with async_run_with_context(_ctx(settings, None)):
        result = await _call(
            tools["update_skill"],
            {"name": "shared", "parent_version": shared, "body": BODY + "3. Ours.\n", "reason": "r"},
        )
    assert result["status"] == "draft" and "library" not in result, result
    assert len(await _versions(settings, ORG_OWNER, "shared")) == 2


async def test_a_personal_create_checks_only_the_callers_library_for_the_name(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
    await _publish(settings, store, ALICE, "journal", "Alice's journal")
    tools = await _tools(settings, store, ALICE)
    async with async_run_with_context(_ctx(settings, ALICE, "skills:personal")):
        shadow = await _call(tools["create_skill"], NEW)
        again = await _call(tools["create_skill"], {**NEW, "name": "journal"})
    assert shadow["library"] == "personal" and shadow["status"] == "draft", shadow
    assert again["error"] == "skill_exists", again
