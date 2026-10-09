"""Whose skills a compile loads, and that a personal skill's files are its own.

The e2e file (`tests/e2e/test_personal_skills.py`) shows the catalog through the production app;
this one holds the rules beneath it that a system prompt cannot show: the manifest field's
bounds, the owner taken from a verified principal, a fiber's recorded owner read back with
suspicion, the catalog's order, and the skill tools reading from the library a skill came from
rather than whichever one holds its name -- a personal skill and the tenant's may share a name and
a version, so a lookup by name serves someone else's bytes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.config import Settings
from felix.manifests.loader import ManifestParseError, parse_manifest
from felix.skills.copy_rule import digest
from felix.skills.library_keys import ORG_OWNER
from felix.skills.library_store import get_skill_library_store
from felix.skills.loader import load_manifest_skills
from felix.skills.store import InMemorySkillActivationStore
from felix.skills.tools import make_skill_tools
from felix.storage import MemoryObjectStore
from felix.tools.types import ToolInvocationCtx, tool_output_content

ALICE, BOB = "iss|alice", "iss|bob"


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="memory://personal-skills")


@pytest.fixture
def store() -> MemoryObjectStore:
    return MemoryObjectStore()


async def _publish(
    settings: Settings,
    store: MemoryObjectStore,
    owner: str,
    name: str,
    description: str,
    *,
    version: str = "0.1.0",
    live: bool = True,
    **extra: str,
) -> None:
    lib = get_skill_library_store(settings, owner=owner)
    files = {"SKILL.md": f"---\nname: {name}\ndescription: {description}\n---\n\nSteps.\n", **extra}
    rows = []
    for path, text in files.items():
        data = text.encode()
        await store.put(lib.object_key("acme", name, version, path), data)
        rows.append({"path": path, "sha256": digest(data), "size": len(data)})
    row = {
        "name": name,
        "version": version,
        "status": "draft",
        "source": "operator",
        "security_status": "pass",
        "created_at": 1,
    }
    await lib.insert_version("acme", row, rows, created_by="x", at=1)
    if live:
        await lib.publish("acme", name, version, from_statuses={"draft"}, by="x", at=2)


async def _catalog(settings: Settings, store: MemoryObjectStore, owner: str | None) -> Any:
    return await load_manifest_skills(
        [], tenant_id="acme", object_store=store, settings=settings, owner=owner
    )


def _spec(**spec: Any) -> Any:
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "m"}, "spec": spec}
    ).spec


# -- the manifest field ---------------------------------------------------------------------------


def test_personal_skills_are_off_unless_asked_for() -> None:
    assert _spec().personal_skills == "off"
    assert _spec(personal_skills="read").personal_skills == "read"
    with pytest.raises(ManifestParseError):
        _spec(personal_skills="always")


def test_a_declared_catalog_refuses_personal_skills() -> None:
    with pytest.raises(ManifestParseError, match="skills_declared_only"):
        _spec(personal_skills="read", skills_declared_only=True)
    assert _spec(personal_skills="off", skills_declared_only=True).skills_declared_only


# -- whose library a caller is ------------------------------------------------------------------


def test_a_verified_caller_owns_issuer_and_subject_and_an_anonymous_one_nothing() -> None:
    from felix.auth.context import ANONYMOUS, AuthContext, Principal
    from felix.auth.middleware import caller_skill_owner

    principal = Principal(subject="alice", tenant_id="acme", issuer="https://id.example", scheme="access")
    assert (
        caller_skill_owner(
            AuthContext(principal=principal, outbound_token=ANONYMOUS.outbound_token, anonymous=False)
        )
        == "https://id.example|alice"
    )
    assert caller_skill_owner(ANONYMOUS) is None
    keyed = Principal(subject="api_key", tenant_id="acme", issuer="api_key", scheme="api_key")
    no_sub = AuthContext(
        principal=keyed, outbound_token=ANONYMOUS.outbound_token, anonymous=False, raw_claims={}
    )
    assert caller_skill_owner(no_sub) is None, "every key without its own sub would share one library"
    own = Principal(subject="alice", tenant_id="acme", issuer="api_key", scheme="api_key")
    named = AuthContext(
        principal=own, outbound_token=ANONYMOUS.outbound_token, anonymous=False, raw_claims={"sub": "alice"}
    )
    assert caller_skill_owner(named) == "api_key|alice"
    assert (
        caller_skill_owner(
            AuthContext(principal=principal, outbound_token=ANONYMOUS.outbound_token, anonymous=True)
        )
        is None
    )


@pytest.mark.parametrize(
    ("stored", "owner"),
    [
        ({"skill_owner": ALICE, "principal_sub": "alice"}, ALICE),
        ({"skill_owner": ALICE, "principal_sub": "mallory"}, None),
        ({"skill_owner": ALICE}, None),
        ({}, None),
        ({"skill_owner": ""}, None),
        ({"skill_owner": "not-an-owner"}, None),
        ({"skill_owner": ["iss|alice"]}, None),
        ({"skill_owner": "iss|\x00"}, None),
        (None, None),
    ],
    ids=[
        "recorded",
        "another-callers",
        "no-caller-recorded",
        "before-owners",
        "org",
        "malformed",
        "not-a-string",
        "unprintable",
        "no-record",
    ],
)
def test_a_fibers_recorded_owner_is_used_only_when_it_is_one(stored: Any, owner: str | None) -> None:
    from felix.durability.fibers import _stored_skill_owner

    assert _stored_skill_owner(stored) == owner


# -- the catalog ------------------------------------------------------------------------------------


async def test_a_catalog_offers_its_callers_skills_ahead_of_the_tenants_and_no_one_elses(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
    await _publish(settings, store, ALICE, "notes", "Alice's notes")
    await _publish(settings, store, ALICE, "drafts", "Alice's drafts")
    await _publish(settings, store, BOB, "ledger", "Bob's ledger")

    org = await _catalog(settings, store, None)
    alice = await _catalog(settings, store, ALICE)
    bob = await _catalog(settings, store, BOB)

    assert org.get("notes").description == "The tenant's notes" and org.get("drafts") is None
    assert alice.get("notes").description == "Alice's notes", "hers shadows the tenant's"
    assert alice.get("notes").library_owner == ALICE and alice.get("drafts") is not None
    assert alice.get("ledger") is None
    assert bob.get("notes").description == "The tenant's notes" and bob.get("notes").library_owner == ""
    assert bob.get("drafts") is None and bob.get("ledger") is not None


async def test_the_host_still_wins_over_a_personal_skill_of_its_name(
    settings: Settings, store: MemoryObjectStore, tmp_path: Any
) -> None:
    host = tmp_path / "host" / "notes"
    host.mkdir(parents=True)
    (host / "SKILL.md").write_text("---\nname: notes\ndescription: The host's notes\n---\n\nHost.\n")
    await _publish(settings, store, ALICE, "notes", "Alice's notes")
    catalog = await load_manifest_skills(
        [],
        tenant_id="acme",
        object_store=store,
        settings=settings,
        bundled_dir=tmp_path / "host",
        owner=ALICE,
    )
    assert catalog.get("notes").description == "The host's notes"


# -- the skill tools --------------------------------------------------------------------------------


def _tools(catalog: Any, settings: Settings, store: MemoryObjectStore, activation: Any) -> dict[str, Any]:
    tools = make_skill_tools(
        catalog,
        activation_store=activation,
        tenant_id="acme",
        manifest_id="m",
        settings=settings,
        object_store=store,
    )
    return {t.name: t for t in tools}


async def _call(tool: Any, args: dict[str, Any]) -> Any:
    out = await tool.executor.execute(args, ToolInvocationCtx(thread_id="acme:t", tool_call_id="c"))
    return tool_output_content(out)


async def test_a_personal_skills_files_are_read_from_its_own_library(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """Same name, same version, both libraries. Looked up by name, Alice's skill would list and
    serve the tenant's reference file; it must serve her own."""
    await _publish(
        settings,
        store,
        ORG_OWNER,
        "notes",
        "The tenant's notes",
        **{"references/how.md": "Tenant way.", "references/tenant-only.md": "Tenant only."},
    )
    await _publish(settings, store, ALICE, "notes", "Alice's notes", **{"references/how.md": "Alice's way."})
    alice = _tools(await _catalog(settings, store, ALICE), settings, store, InMemorySkillActivationStore())
    org = _tools(await _catalog(settings, store, None), settings, store, InMemorySkillActivationStore())

    activated = json.loads(await _call(alice["activate_skill"], {"name": "notes"}))
    assert activated["files"] == ["references/how.md"], "her listing, not the tenant's"
    org_files = json.loads(await _call(org["activate_skill"], {"name": "notes"}))["files"]
    assert org_files == ["references/how.md", "references/tenant-only.md"]
    assert "Alice's way." in await _call(
        alice["read_skill_file"], {"name": "notes", "path": "references/how.md"}
    )
    assert "Tenant way." in await _call(
        org["read_skill_file"], {"name": "notes", "path": "references/how.md"}
    )


async def test_activation_tells_a_caller_only_their_own_catalogs_names(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """Activation is stored per manifest, shared by its callers; what one activated must not
    come back to another as a name only the first one's catalog holds."""
    await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
    await _publish(settings, store, ALICE, "alice-secret-plan", "Alice's plan")
    shared = InMemorySkillActivationStore()
    alice = _tools(await _catalog(settings, store, ALICE), settings, store, shared)
    bob = _tools(await _catalog(settings, store, BOB), settings, store, shared)

    await _call(alice["activate_skill"], {"name": "alice-secret-plan"})
    activated = json.loads(await _call(bob["activate_skill"], {"name": "notes"}))
    assert activated["active_skills"] == ["notes"]
    deactivated = json.loads(await _call(bob["deactivate_skill"], {"name": "notes"}))
    assert deactivated["active_skills"] == []


async def test_a_skills_newest_version_is_its_own_librarys(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """`update_skill` must name the newest version as its parent; read in the wrong library, the
    model is told the tenant's 0.2.0 draft is the newest of Alice's skill."""
    await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
    await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes", version="0.2.0", live=False)
    await _publish(settings, store, ALICE, "notes", "Alice's notes")
    alice = _tools(await _catalog(settings, store, ALICE), settings, store, InMemorySkillActivationStore())
    org = _tools(await _catalog(settings, store, None), settings, store, InMemorySkillActivationStore())

    assert json.loads(await _call(alice["activate_skill"], {"name": "notes"}))["newest_version"] == "0.1.0"
    assert json.loads(await _call(org["activate_skill"], {"name": "notes"}))["newest_version"] == "0.2.0"
    [listed] = [s for s in json.loads(await _call(alice["list_skills"], {})) if s["name"] == "notes"]
    assert listed["newest_version"] == "0.1.0"


async def test_a_skill_the_manifest_names_is_never_a_callers_own(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """A name in `spec.skills` is the author's reviewed choice; a caller's skill of that name
    shadows only what the catalog picked up without naming it."""
    await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
    await _publish(settings, store, ORG_OWNER, "drafts", "The tenant's drafts")
    await _publish(settings, store, ALICE, "notes", "Alice's notes")
    await _publish(settings, store, ALICE, "drafts", "Alice's drafts")
    catalog = await load_manifest_skills(
        [{"name": "notes"}], tenant_id="acme", object_store=store, settings=settings, owner=ALICE
    )
    assert catalog.get("notes").description == "The tenant's notes"
    assert catalog.get("drafts").description == "Alice's drafts"


async def test_authoring_and_feedback_refuse_a_callers_own_skill(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """These tools write the tenant's library. Aimed at Alice's `notes`, they would act on the
    tenant's `notes` -- an edit built on the tenant's files, feedback that rewrites the tenant's
    skill -- so a name that is hers in this catalog is refused, and the tenant's is untouched."""
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.skills.feedback_store import get_skill_feedback_store
    from felix.tools.provider import InMemoryToolProvider

    await _publish(settings, store, ORG_OWNER, "notes", "The tenant's notes")
    await _publish(settings, store, ORG_OWNER, "shared", "The tenant's shared skill")
    await _publish(settings, store, ALICE, "notes", "Alice's notes")
    agent = await build_agent(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "author-test"},
            "spec": {"pattern": "react", "personal_skills": "read", "skill_authoring": {"enabled": True}},
        },
        deps=BuildDeps(
            tools=InMemoryToolProvider(),
            settings=settings,
            tenant_id="acme",
            object_store=store,
            skill_owner=ALICE,
        ),
        settings=settings,
    )
    tools = {t.name: t for t in agent.tools}
    edit = {"name": "notes", "parent_version": "0.1.0", "body": "Mine.", "reason": "r"}
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="acme"), manifest_id="author-test")
    async with async_run_with_context(ctx):
        updated = json.loads(await _call(tools["update_skill"], edit))
        created = json.loads(
            await _call(
                tools["create_skill"], {"name": "notes", "description": "d", "body": "b", "reason": "r"}
            )
        )
        filed = json.loads(await _call(tools["submit_skill_feedback"], {"name": "notes", "body": "Better."}))
        shared = json.loads(
            await _call(tools["submit_skill_feedback"], {"name": "shared", "body": "Better."})
        )

    assert updated["error"] == created["error"] == filed["error"] == "personal_skill"
    assert shared["status"] == "pending", "a tenant skill in the same catalog still takes feedback"
    assert await get_skill_library_store(settings, owner=ORG_OWNER).version_ids("acme", "notes") == ["0.1.0"]
    pending = await get_skill_feedback_store(settings).list_by_status("acme", "pending")
    assert [r["name"] for r in pending] == ["shared"]
