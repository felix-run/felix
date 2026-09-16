"""Making the skills subsystem reachable, and asserting what it reports is true.

`grep -rn skill apps/api/src/felix_api/routes/` returned zero: a loader, a catalog, an
activation store with its own table and Postgres arm, and three model-facing tools, none of
it answerable to an operator. A subsystem nothing can reach is inert by this repo's own
rule, so the tests that matter are the ones that would fail if the routes reported something
other than what a turn would actually see.

Through `create_app` rather than against the module, because the scope gate and where the
tenant comes from are exactly what a direct call skips.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from httpx import ASGITransport, AsyncClient

KEYS = (
    '{"sk-skills":{"tenant_id":"acme","sub":"ops","scopes":["skills:read"]},'
    '"sk-none":{"tenant_id":"acme","sub":"ops","scopes":["chat:write"]},'
    '"sk-other":{"tenant_id":"globex","sub":"ops","scopes":["skills:read"]}}'
)

SKILL_MD = """---
name: invoice-triage
description: Sort an invoice into a category.
---

Read the invoice. Decide whether it is a duplicate.
"""

HIDDEN_MD = """---
name: internal-only
description: Not offered to the model.
disable-model-invocation: "true"
---

Operator-run only.
"""


@pytest.fixture
def skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A skills directory, exported the way production supplies one.

    `FELIX_SKILLS_DIR` rather than the `Settings` object handed to `create_app`, because
    `loader._configured_skills_dir` reads `get_settings()` -- the process-wide settings --
    not whatever the app was constructed with. Setting only the field would leave the
    loader looking at the real environment, and every body would come back empty while the
    names still listed, which is exactly what it did before this fixture was corrected.
    """
    from felix.config import get_settings

    for name, body in (("invoice-triage", SKILL_MD), ("internal-only", HIDDEN_MD)):
        pkg = tmp_path / name
        pkg.mkdir()
        (pkg / "SKILL.md").write_text(body)
    monkeypatch.setenv("FELIX_SKILLS_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _settings(skills_dir: Path, **kw: object) -> Settings:
    base: dict[str, object] = {
        "allow_insecure": True,
        "auth_mode": "api_key",
        "auth_api_keys": KEYS,
        "environment": "development",
        "object_store": "memory",
        "database_url": "memory://skills",
        "skills_dir": str(skills_dir),
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


async def _client(skills_dir: Path, **kw: object) -> tuple[AsyncClient, Settings]:
    from felix_api.app import create_app

    settings = _settings(skills_dir, **kw)
    app = create_app(settings=settings, plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), settings


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _store_manifest(settings: Settings, name: str, skills: list[str]) -> None:
    from felix.manifests.loader import parse_manifest
    from felix.manifests.store import put_version

    await put_version(
        settings,
        "acme",
        name,
        parse_manifest(
            {
                "apiVersion": "felix/v1",
                "kind": "Agent",
                "metadata": {"name": name},
                "spec": {
                    "pattern": "react",
                    "tools": [],
                    "skills": [{"name": s} for s in skills],
                },
            }
        ),
    )


@pytest.mark.asyncio
async def test_listing_names_every_skill_the_manifest_can_reach(skills_dir: Path) -> None:
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        resp = await client.get("/skills/triage", headers=_auth("sk-skills"))

    assert resp.status_code == 200, resp.text
    names = [item["name"] for item in resp.json()["items"]]
    assert "invoice-triage" in names


@pytest.mark.asyncio
async def test_listing_says_which_skills_the_model_is_never_offered(skills_dir: Path) -> None:
    """`disable_model_invocation` is invisible from the model's side by construction —
    `catalog.list_public()` filters it out before the tool ever sees it — so "why does this
    skill never fire" is the question an operator cannot otherwise answer."""
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage", "internal-only"])
        resp = await client.get("/skills/triage", headers=_auth("sk-skills"))

    by_name = {item["name"]: item for item in resp.json()["items"]}
    assert by_name["internal-only"]["model_invocable"] is False
    assert by_name["invoice-triage"]["model_invocable"] is True


@pytest.mark.asyncio
async def test_listing_withholds_the_bodies(skills_dir: Path) -> None:
    """Progressive disclosure is the design: the model pays for instructions only on
    activation. Returning every body here would answer a question nobody asked with the
    largest payload on the surface."""
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        resp = await client.get("/skills/triage", headers=_auth("sk-skills"))

    item = next(i for i in resp.json()["items"] if i["name"] == "invoice-triage")
    assert item["has_body"] is True
    assert "body" not in item
    assert "duplicate" not in resp.text


@pytest.mark.asyncio
async def test_one_skill_returns_the_instructions_activation_would_hand_the_model(
    skills_dir: Path,
) -> None:
    """The body is the point: it is appended to the system prompt, so it is prompt content
    an operator is accountable for and could not otherwise read without unpacking the
    object store or the bundled directory by hand."""
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        resp = await client.get("/skills/triage/invoice-triage", headers=_auth("sk-skills"))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "Read the invoice" in body["body"]
    assert body["description"] == "Sort an invoice into a category."


@pytest.mark.asyncio
async def test_a_skill_the_manifest_never_declared_is_still_reachable(skills_dir: Path) -> None:
    """`spec.skills` adds to a deployment-wide library rather than restricting one.

    `load_manifest_skills` seeds every skill in the bundled directory and in
    `FELIX_SKILLS_DIR` before it resolves a single ref, so a manifest declaring one skill
    compiles a catalog holding every skill on the host — and `make_skill_tools` offers all
    of them to the model. Verified directly: a manifest naming only `invoice-triage` reaches
    `internal-only` and the repo's own bundled skills too.

    Asserted rather than fixed, because it may well be the intent — a host-wide skill
    library is a reasonable design — but it is not what "declared skills" reads like, and
    nothing said so anywhere. `declared` is how the route makes the difference legible.
    """
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        resp = await client.get("/skills/triage/internal-only", headers=_auth("sk-skills"))

    assert resp.status_code == 200, resp.text
    assert resp.json()["declared"] is False


@pytest.mark.asyncio
async def test_declared_separates_what_the_manifest_asked_for(skills_dir: Path) -> None:
    """The one field an operator needs to tell "we chose this" from "it was lying around"."""
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        resp = await client.get("/skills/triage", headers=_auth("sk-skills"))

    by_name = {item["name"]: item for item in resp.json()["items"]}
    assert by_name["invoice-triage"]["declared"] is True
    assert by_name["internal-only"]["declared"] is False


@pytest.mark.asyncio
async def test_a_name_no_skill_anywhere_defines_is_not_found(skills_dir: Path) -> None:
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        resp = await client.get("/skills/triage/no-such-skill", headers=_auth("sk-skills"))

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_a_declared_skill_with_no_body_still_lists(skills_dir: Path) -> None:
    """The loader substitutes a placeholder rather than failing the compile, so the manifest
    works and the ref is visible. Reporting it as an error here would hide the useful fact:
    `has_body` is false, and activating it hands the model nothing."""
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["never-written"])
        resp = await client.get("/skills/triage/never-written", headers=_auth("sk-skills"))

    assert resp.status_code == 200, resp.text
    assert resp.json()["body"] == ""


@pytest.mark.asyncio
async def test_active_state_is_reported_from_the_store_a_turn_writes(skills_dir: Path) -> None:
    """Otherwise the route reports a default rather than the truth, and the one question it
    exists to answer is the one it gets wrong."""
    from felix.skills.store import get_skill_activation_store

    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        await get_skill_activation_store(settings).activate("acme", "triage", "invoice-triage")
        resp = await client.get("/skills/triage", headers=_auth("sk-skills"))

    by_name = {i["name"]: i for i in resp.json()["items"]}
    assert by_name["invoice-triage"]["active"] is True
    # Both sides. Asserting only the activated one passes just as well against a route that
    # reports everything as active, which is the shape this file's mutation run caught.
    assert by_name["internal-only"]["active"] is False
    assert resp.json()["active"] == ["invoice-triage"]


@pytest.mark.asyncio
async def test_another_tenants_activation_is_not_reported_as_ours(skills_dir: Path) -> None:
    """The store is keyed by `(tenant, manifest)` and the tenant comes from the credential,
    never the path — so a manifest name shared between tenants must not leak either way."""
    from felix.skills.store import get_skill_activation_store

    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        await get_skill_activation_store(settings).activate("globex", "triage", "invoice-triage")
        resp = await client.get("/skills/triage", headers=_auth("sk-skills"))

    assert resp.json()["active"] == []


@pytest.mark.asyncio
async def test_reading_skills_needs_the_scope(skills_dir: Path) -> None:
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        denied = await client.get("/skills/triage", headers=_auth("sk-none"))
        one = await client.get("/skills/triage/invoice-triage", headers=_auth("sk-none"))
        recent = await client.get("/skills/triage/activations/recent", headers=_auth("sk-none"))

    assert denied.status_code == 403, denied.text
    assert one.status_code == 403
    assert recent.status_code == 403
    assert "skills:read" in denied.json()["detail"]


@pytest.mark.asyncio
async def test_an_unknown_manifest_is_not_found(skills_dir: Path) -> None:
    client, _ = await _client(skills_dir)
    async with client:
        resp = await client.get("/skills/no-such-manifest", headers=_auth("sk-skills"))

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_recent_activations_name_the_skill_not_just_the_tool(skills_dir: Path) -> None:
    """The gap that made this route possible.

    `tool_runner` already audited every `activate_skill` call, but its payload carries the
    tool's *name* and not its arguments — deliberately, since arguments are arbitrary model
    text and a credential in a retained row is how that goes wrong. So the trail recorded
    that a skill activated and never which one, and the roadmap's ask was unanswerable from
    existing data. `skills/tools.py` now emits a `skill_activation` event naming the skill it
    resolved against the catalog.
    """
    from felix.context import AuthContext, RequestContext, run_with_context
    from felix.skills.loader import load_manifest_skills
    from felix.skills.store import get_skill_activation_store
    from felix.skills.tools import make_skill_tools
    from felix.tools.types import ToolInvocationCtx

    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        catalog = await load_manifest_skills([{"name": "invoice-triage"}], tenant_id="acme")
        tools = make_skill_tools(
            catalog,
            activation_store=get_skill_activation_store(settings),
            tenant_id="acme",
            manifest_id="triage",
        )
        activate = next(t for t in tools if t.name == "activate_skill")

        ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="acme"))
        with run_with_context(ctx):
            await activate.executor.execute(
                {"name": "invoice-triage"}, ToolInvocationCtx(thread_id="acme:t1")
            )

        # `record_event` buffers; the route reads what has been written.
        from felix.audit import store as audit_store

        await audit_store.flush_pending(settings)

        resp = await client.get("/skills/triage/activations/recent", headers=_auth("sk-skills"))

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["skill"] for i in items] == ["invoice-triage"]
    assert items[0]["action"] == "activate"
    assert items[0]["thread_id"] == "acme:t1"


@pytest.mark.asyncio
async def test_recent_activations_are_scoped_to_the_manifest_asked_about(skills_dir: Path) -> None:
    """One tenant's busy manifest must not push another manifest's activations out of view,
    which is what an unfiltered scan over a shared audit trail would do."""
    from felix.audit import store as audit_store

    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        for manifest in ("triage", "other"):
            audit_store.record_event(
                settings,
                "acme",
                "skill_activation",
                manifest_id=manifest,
                status="ok",
                payload={"action": "activate", "skill": f"{manifest}-skill", "thread_id": "t"},
            )
        await audit_store.flush_pending(settings)

        resp = await client.get("/skills/triage/activations/recent", headers=_auth("sk-skills"))

    assert [i["skill"] for i in resp.json()["items"]] == ["triage-skill"]


@pytest.mark.asyncio
async def test_activations_do_not_cross_tenants(skills_dir: Path) -> None:
    from felix.audit import store as audit_store

    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        audit_store.record_event(
            settings,
            "globex",
            "skill_activation",
            manifest_id="triage",
            status="ok",
            payload={"action": "activate", "skill": "theirs", "thread_id": "t"},
        )
        await audit_store.flush_pending(settings)

        resp = await client.get("/skills/triage/activations/recent", headers=_auth("sk-skills"))

    assert resp.json()["items"] == []


async def _drive_skill_tool(settings: Settings, tool_name: str, skill_name: str) -> None:
    """Run one skill tool the way a turn runs it, then flush what it audited."""
    from felix.audit import store as audit_store
    from felix.context import AuthContext, RequestContext, run_with_context
    from felix.skills.loader import load_manifest_skills
    from felix.skills.store import get_skill_activation_store
    from felix.skills.tools import make_skill_tools
    from felix.tools.types import ToolInvocationCtx

    catalog = await load_manifest_skills([{"name": "invoice-triage"}], tenant_id="acme")
    tools = make_skill_tools(
        catalog,
        activation_store=get_skill_activation_store(settings),
        tenant_id="acme",
        manifest_id="triage",
    )
    tool = next(t for t in tools if t.name == tool_name)
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="acme"))
    with run_with_context(ctx):
        await tool.executor.execute(
            {"name": skill_name}, ToolInvocationCtx(thread_id="acme:t1", tool_call_id="call_7")
        )
    await audit_store.flush_pending(settings)


@pytest.mark.asyncio
async def test_a_model_naming_a_skill_that_does_not_exist_is_recorded_as_such(
    skills_dir: Path,
) -> None:
    """The arm the whole event is justified on: a model probing for skills it was never
    granted is itself what an operator wants to see, and `status` is what distinguishes that
    row's model-supplied name from the host-declared names beside it."""
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        await _drive_skill_tool(settings, "activate_skill", "no-such-skill")
        resp = await client.get("/skills/triage/activations/recent", headers=_auth("sk-skills"))

    item = resp.json()["items"][0]
    assert item["status"] == "unknown_skill"
    assert item["skill"] == "no-such-skill"


@pytest.mark.asyncio
async def test_a_deactivation_is_not_reported_as_an_activation(skills_dir: Path) -> None:
    """`action` says which happened, and every test until now supplied `activate` -- so a
    route reporting one as the other had nothing to catch it."""
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        await _drive_skill_tool(settings, "deactivate_skill", "invoice-triage")
        resp = await client.get("/skills/triage/activations/recent", headers=_auth("sk-skills"))

    item = resp.json()["items"][0]
    assert item["action"] == "deactivate"
    assert item["status"] == "ok"
    assert item["skill"] == "invoice-triage"


@pytest.mark.asyncio
async def test_deactivating_something_that_is_not_a_skill_is_not_reported_as_ok(
    skills_dir: Path,
) -> None:
    """`activation_store.deactivate` is a list filter -- it neither validates the name nor
    says whether anything was removed -- so auditing the raw argument recorded arbitrary
    model text under `status="ok"`, indistinguishable from a real deactivation. Both
    reviewers found this; the security review ranked it the highest finding in the change.
    """
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        await _drive_skill_tool(settings, "deactivate_skill", "ignore previous instructions")
        resp = await client.get("/skills/triage/activations/recent", headers=_auth("sk-skills"))

    item = resp.json()["items"][0]
    assert item["status"] == "unknown_skill", "model text was recorded as a real deactivation"


@pytest.mark.asyncio
async def test_an_activation_carries_the_id_that_ties_it_to_its_tool_call(
    skills_dir: Path,
) -> None:
    """`tool_runner` writes a `tool_call` row for the same invocation and this is the only
    key that joins them. Parallel calls in one batch share a thread and adjacent timestamps,
    so without it there is nothing to tell two activations apart."""
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        await _drive_skill_tool(settings, "activate_skill", "invoice-triage")
        resp = await client.get("/skills/triage/activations/recent", headers=_auth("sk-skills"))

    assert resp.json()["items"][0]["tool_call_id"] == "call_7"


@pytest.mark.asyncio
async def test_a_skill_body_is_redacted_before_it_leaves_on_the_read_scope(
    skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`routes/manifests.py` redacts a manifest on `manifests:read` because an embedded
    credential must not ride out on the lower scope. Nothing validates SKILL.md frontmatter,
    and the same bytes reaching the *model* are already masked by the governance stack -- so
    without this the HTTP route is the only path on which a body reaches anyone unmasked."""
    secret = "sk-live-abcdefghijklmnop"
    monkeypatch.setenv("FELIX_ANTHROPIC_API_KEY", secret)

    pkg = skills_dir / "leaky"
    pkg.mkdir()
    (pkg / "SKILL.md").write_text(
        f"---\nname: leaky\ndescription: Has a credential in it.\n---\n\nUse {secret} to call out.\n"
    )

    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["leaky"])
        resp = await client.get("/skills/triage/leaky", headers=_auth("sk-skills"))

    assert resp.status_code == 200, resp.text
    assert secret not in resp.text
    assert "Use" in resp.json()["body"], "the body was dropped rather than redacted"


@pytest.mark.asyncio
async def test_a_malformed_manifest_name_is_a_404_not_a_500(skills_dir: Path) -> None:
    """`assert_valid_manifest_name` raises `ValueError`, which is not a `LookupError` -- so
    this was a 500 with the caller's path segment reflected into the server log.

    A space rather than `%2F`: an encoded slash is decoded and path-normalised before the
    router sees it, so that name never reaches the validator at all and the test would 404
    for the wrong reason -- which is exactly what it did until the mutation run said so.
    """
    client, _ = await _client(skills_dir)
    async with client:
        resp = await client.get("/skills/not%20a%20name", headers=_auth("sk-skills"))

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_the_absolute_install_path_is_not_handed_to_a_tenant(skills_dir: Path) -> None:
    """`Skill.path` is absolute and derived from the install prefix, so returning it tells a
    tenant-scoped caller the container's filesystem layout."""
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        resp = await client.get("/skills/triage/invoice-triage", headers=_auth("sk-skills"))

    body = resp.json()
    assert body["filename"] == "SKILL.md"
    assert "path" not in body
    assert str(skills_dir) not in resp.text


async def _store_manifest_only(settings: Settings, name: str, skills: list[str]) -> None:
    """A manifest that declares `skills_declared_only`."""
    from felix.manifests.loader import parse_manifest
    from felix.manifests.store import put_version

    await put_version(
        settings,
        "acme",
        name,
        parse_manifest(
            {
                "apiVersion": "felix/v1",
                "kind": "Agent",
                "metadata": {"name": name},
                "spec": {
                    "pattern": "react",
                    "tools": [],
                    "skills": [{"name": s} for s in skills],
                    "skills_declared_only": True,
                },
            }
        ),
    )


@pytest.mark.asyncio
async def test_declared_only_keeps_the_host_library_out_of_the_catalogue(
    skills_dir: Path,
) -> None:
    """The point of the flag: what the manifest names is what the agent can load.

    A skill body is appended to the system prompt, so an ambient skill is a prompt fragment
    the manifest never named -- the one prompt-shaping input `pin_compile` cannot cover,
    because the hash is over the manifest and the drift is on the host's disk.
    """
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest_only(settings, "locked", ["invoice-triage"])
        resp = await client.get("/skills/locked", headers=_auth("sk-skills"))

    body = resp.json()
    assert body["declared_only"] is True
    assert [i["name"] for i in body["items"]] == ["invoice-triage"]
    assert all(i["declared"] for i in body["items"])


@pytest.mark.asyncio
async def test_declared_only_still_finds_the_body_of_what_it_declares(
    skills_dir: Path,
) -> None:
    """Restricting the catalogue must not stop a declared skill resolving.

    The seeding is also how a bundled name is resolved cheaply, so dropping it naively
    would leave every declared skill as an empty placeholder -- the manifest would compile
    and the agent would activate a skill that hands the model nothing.
    """
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest_only(settings, "locked", ["invoice-triage"])
        resp = await client.get("/skills/locked/invoice-triage", headers=_auth("sk-skills"))

    assert resp.status_code == 200, resp.text
    assert "Read the invoice" in resp.json()["body"]


@pytest.mark.asyncio
async def test_a_host_skill_is_not_reachable_under_declared_only(skills_dir: Path) -> None:
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest_only(settings, "locked", ["invoice-triage"])
        resp = await client.get("/skills/locked/internal-only", headers=_auth("sk-skills"))

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_the_default_is_unchanged(skills_dir: Path) -> None:
    """Opt-in, because narrowing silently would be a behaviour change to every manifest
    already in Postgres -- which this repo's own rule says needs a migration rather than a
    reinterpretation. A manifest that says nothing keeps the host library."""
    client, settings = await _client(skills_dir)
    async with client:
        await _store_manifest(settings, "triage", ["invoice-triage"])
        resp = await client.get("/skills/triage", headers=_auth("sk-skills"))

    body = resp.json()
    assert body["declared_only"] is False
    assert "internal-only" in [i["name"] for i in body["items"]]


async def _catalogue_in_the_system_prompt(skills_dir: Path, *, declared_only: bool) -> str:
    """Compile through `build_agent` and return the prompt the model would be given.

    Through the builder rather than by calling `load_manifest_skills` directly, because the
    thing worth pinning is that the *compile* reads the flag. Calling the loader with the
    flag proves only that the loader honours an argument I passed it, and stays green if
    `builder.py` never passes one -- this repo's named defect shape, and exactly what the
    mutation run caught the first time this test was written.

    The system prompt rather than the `list_skills` tool, because the catalogue XML appended
    to the prompt is how the model actually learns which skills exist: progressive
    disclosure means it sees names and descriptions there and pays for a body only on
    activation. Executing the tool instead would run it through the governance stack, which
    refuses outside a request context -- a real behaviour, and not the one under test.
    """
    from felix.config import Settings
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.storage import MemoryObjectStore
    from felix.tools.provider import InMemoryToolProvider

    _ = skills_dir  # the fixture exports FELIX_SKILLS_DIR
    settings = Settings(database_url="memory://compile", object_store="memory", allow_insecure=True)
    agent = await build_agent(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "locked"},
            "spec": {
                "pattern": "react",
                "tools": [],
                "skills": [{"name": "invoice-triage"}],
                "skills_declared_only": declared_only,
            },
        },
        deps=BuildDeps(
            tools=InMemoryToolProvider(),
            settings=settings,
            tenant_id="acme",
            object_store=MemoryObjectStore(),
        ),
        settings=settings,
    )
    return agent.system_prompt or ""


#: A skill this repo ships in `skills/`, which no test manifest declares. Its presence in a
#: compiled prompt is the host library leaking in; its absence under `skills_declared_only`
#: is the flag working. `internal-only` cannot play this role -- `skill_catalog_xml` renders
#: `list_public()`, which filters `disable_model_invocation` out before the prompt is built.
BUNDLED_SKILL = "felix-architecture"


@pytest.mark.asyncio
async def test_the_compile_shows_the_model_only_what_the_manifest_declared(
    skills_dir: Path,
) -> None:
    """The property the flag exists for, asserted where the model actually meets it."""
    prompt = await _catalogue_in_the_system_prompt(skills_dir, declared_only=True)

    assert "invoice-triage" in prompt
    assert BUNDLED_SKILL not in prompt, "a skill the manifest never named reached the prompt"


@pytest.mark.asyncio
async def test_the_compile_shows_the_host_library_by_default(skills_dir: Path) -> None:
    """The other half, and the behaviour this whole flag was written to make optional: a
    manifest naming one skill has the repo's own bundled skills in its prompt.

    Without this assertion the test above passes against a compile that puts no catalogue in
    the prompt at all, and "restricted" is indistinguishable from "broken"."""
    prompt = await _catalogue_in_the_system_prompt(skills_dir, declared_only=False)

    assert "invoice-triage" in prompt
    assert BUNDLED_SKILL in prompt, "the host library was not seeded"


@pytest.mark.asyncio
async def test_skills_is_bounded_like_every_other_ref_list() -> None:
    """`spec.skills` was the one ref list with no `max_length`, and each ref can cost an
    object-store lookup at compile -- so an unbounded list is an unbounded fan-out."""
    from felix.manifests.loader import ManifestParseError, parse_manifest
    from felix.manifests.schema import MAX_REFS

    # Matched on the message, not just the type: `parse_manifest` refuses a manifest for
    # plenty of reasons, and a bare `raises` would pass on any of them.
    with pytest.raises(ManifestParseError, match="at most 64"):
        parse_manifest(
            {
                "apiVersion": "felix/v1",
                "kind": "Agent",
                "metadata": {"name": "toomany"},
                "spec": {"skills": [{"name": f"s{i}"} for i in range(MAX_REFS + 1)]},
            }
        )
