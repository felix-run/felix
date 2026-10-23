"""Per-person long-term memory: who the person is, and what each memory surface lets them reach.

The store's pool filter has its own contract on both backends (`tests/conformance/
test_memory_person_scope.py`) and the compile's wiring is asserted end to end
(`tests/e2e/test_memory_per_person.py`). This file holds the parts between: the person key and
`FELIX_PRINCIPAL_ALIASES`, the key carried through a recorded caller, the agent-facing tools and
capture bound to one pool, the operator's `/memory` routes, and `felix memory assign`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.auth.persons import caller_person, parse_principal_aliases, person_of
from felix.auth.recorded import record_caller, replay_caller
from felix.config import get_settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.schema import Manifest, MemoryCapture, ProceduralSpec
from felix.memory import store as memory_store
from felix.memory.assign import AssignReport, assign_to_person
from felix.memory.capture import _known_values, active_facts_prompt, capture_from_turn
from felix.memory.procedural import retrieve_procedures
from felix.memory.scope import memory_pool
from felix.memory.store import MemoryFilter, MemoryPool
from felix.memory.tools import make_memory_tools
from felix_cli.main import app
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from typer.testing import CliRunner

from tests.support.factories import make_settings

TENANT = "t-person"
MANIFEST = "assistant"
ALICE = "felix-self|github:1"
BOB = "felix-self|github:2"
OPERATOR = {"source": "management_api"}


@pytest.fixture(autouse=True)
def _clean() -> None:
    memory_store._memory_rows.clear()


# --- who the person is ----------------------------------------------------------------------


def test_a_person_is_issuer_and_subject_so_another_issuers_same_subject_is_someone_else() -> None:
    assert (
        caller_person(issuer="felix-self", subject="github:70701", anonymous=False)
        == "felix-self|github:70701"
    )
    # An API key configured with `sub: github:70701` is not the person who signed in with GitHub.
    assert caller_person(issuer="api_key", subject="github:70701", anonymous=False) == "api_key|github:70701"
    assert caller_person(issuer="felix-self", subject="github:70701", anonymous=True) == ""


@pytest.mark.parametrize(
    "subject",
    [
        "api_key",
        "anonymous",
        "cron",
        "fiber",
        "trigger:t1",
        "github-actions:123:o/r/.github/workflows/x.yml@main",
    ],
    ids=["shared-key", "anonymous", "cron", "fiber", "trigger", "github-actions"],
)
def test_a_subject_that_names_no_one_has_no_person(subject: str) -> None:
    assert caller_person(issuer="felix-self", subject=subject, anonymous=False) == ""


def test_an_alias_resolves_in_one_hop_and_a_bare_subject_is_felixs_own_issuer() -> None:
    settings = make_settings(
        principal_aliases=json.dumps({"google:9": "github:1", "jwt-iss|u-7": "github:1"})
    )
    assert parse_principal_aliases(settings) == {"felix-self|google:9": ALICE, "jwt-iss|u-7": ALICE}
    assert person_of(settings, "felix-self|google:9") == ALICE
    assert person_of(settings, "jwt-iss|u-7") == ALICE
    assert person_of(settings, ALICE) == ALICE
    assert person_of(settings, BOB) == BOB
    # The caller's key is compared exactly: nothing on that side is stripped or folded.
    assert person_of(settings, " felix-self|google:9") == " felix-self|google:9"
    assert person_of(settings, "") == ""


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ('{"google:9": "github:1", "github:1": "github:3"}', "itself an alias"),
        ('{"github:1": "github:1"}', "itself an alias"),
        ('{"google:9": "api_key"}', "not a shared credential"),
        ('{"api_key": "github:1"}', "not a shared credential"),
        ('{"google:9": "cron"}', "not a shared credential"),
        ('{"google:9": "github-actions:1:x"}', "not a shared credential"),
        ("not json", "JSON object"),
        ('["google:9"]', "JSON object"),
        ('{"google:9": 1}', "must map to a string"),
        ('{"google:9": " "}', "may not be empty"),
    ],
    ids=[
        "chain",
        "self",
        "shared-target",
        "shared-alias",
        "machine-target",
        "actions-target",
        "not-json",
        "not-an-object",
        "not-a-string",
        "empty-target",
    ],
)
def test_the_boot_refuses_an_alias_map_it_cannot_follow(raw: str, reason: str) -> None:
    settings = make_settings(principal_aliases=raw)
    with pytest.raises(ValueError, match=reason):
        parse_principal_aliases(settings)
    with pytest.raises(RuntimeError, match=f"FELIX_PRINCIPAL_ALIASES: .*{reason}"):
        settings.validate_runtime()


def test_a_recorded_caller_replays_the_same_person_key() -> None:
    live = AuthContext(
        tenant_id=TENANT, principal_sub="github:1", anonymous=False, scheme="self", person=ALICE
    )
    record = record_caller(live)
    # A durable run's resume, and a job's firing for its writer: the same key the request had.
    assert replay_caller(record, tenant_id=TENANT, actor="fiber").person == ALICE
    assert replay_caller(record, tenant_id=TENANT, actor="cron", on_behalf_of="github:1").person == ALICE
    # A key whose subject is not the person replayed names someone else's memory, and is dropped.
    assert replay_caller(record, tenant_id=TENANT, actor="cron", on_behalf_of="github:2").person == ""
    assert replay_caller({**record, "person": BOB}, tenant_id=TENANT, actor="fiber").person == ""
    assert replay_caller({**record, "person": "no-bar"}, tenant_id=TENANT, actor="fiber").person == ""
    # Same subject, another issuer: an edited key may not move the run to a different sign-in.
    assert (
        replay_caller({**record, "person": "api_key|github:1"}, tenant_id=TENANT, actor="fiber").person == ""
    )
    keyed = record_caller(
        AuthContext(
            tenant_id=TENANT,
            principal_sub="github:1",
            anonymous=False,
            scheme="api_key",
            person="api_key|github:1",
        )
    )
    assert replay_caller(keyed, tenant_id=TENANT, actor="fiber").person == "api_key|github:1"
    assert replay_caller({**keyed, "person": ALICE}, tenant_id=TENANT, actor="fiber").person == ""
    # Recorded before persons were: no key, so the run writes no one's memory.
    assert (
        replay_caller(
            {k: v for k, v in record.items() if k != "person"}, tenant_id=TENANT, actor="fiber"
        ).person
        == ""
    )


def test_the_compile_resolves_its_pool_from_the_person_it_is_given() -> None:
    settings = make_settings(principal_aliases=json.dumps({"google:9": "github:1"}))
    assert memory_pool(settings, manifest_id=MANIFEST, scope="person", person=ALICE) == MemoryPool.person(
        MANIFEST, ALICE
    )
    assert memory_pool(settings, manifest_id=MANIFEST, scope="person", person="felix-self|google:9") == (
        MemoryPool.person(MANIFEST, ALICE)
    )
    assert memory_pool(settings, manifest_id=MANIFEST, scope="person", person="") == MemoryPool.personless(
        MANIFEST
    )
    assert memory_pool(settings, manifest_id=MANIFEST, scope="agent", person=ALICE) == MemoryPool.agent(
        MANIFEST
    )


def test_the_scope_field_defaults_to_person_and_refuses_anything_else() -> None:
    def spec(memory: dict[str, Any]) -> dict[str, Any]:
        return {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "m"},
            "spec": {"memory": memory},
        }

    assert Manifest.model_validate(spec({})).spec.memory.scope == "person"
    assert Manifest.model_validate(spec({"scope": "agent"})).spec.memory.scope == "agent"
    with pytest.raises(ValidationError):
        Manifest.model_validate(spec({"scope": "principal"}))


async def test_governed_tools_bind_the_pool_of_the_auth_passed_not_the_ambient_caller() -> None:
    """`governed_tools(auth=...)` compiles for that caller. The pool must be theirs even when the
    request context installed around the call is someone else's."""
    from felix.manifests import store as manifest_store
    from felix.runtime import governed_tools, resolve_tenant_manifest
    from felix.tools.provider import InMemoryToolProvider

    settings = make_settings()
    await manifest_store.put_version(
        settings,
        TENANT,
        MANIFEST,
        Manifest.model_validate(
            {
                "apiVersion": "felix/v1",
                "kind": "Agent",
                "metadata": {"name": MANIFEST},
                "spec": {"pattern": "react", "memory": {"store": "memory", "recall": {"tools": True}}},
            }
        ),
    )
    alice = AuthContext(tenant_id=TENANT, principal_sub="github:1", anonymous=False, person=ALICE)
    bob = AuthContext(tenant_id=TENANT, principal_sub="github:2", anonymous=False, person=BOB)
    async with async_run_with_context(RequestContext(settings=settings, auth=bob)):
        resolved = await resolve_tenant_manifest(settings, TENANT, MANIFEST)
        tools = {
            t.name: t
            for t in await governed_tools(
                settings, resolved=resolved, auth=alice, tools=InMemoryToolProvider()
            )
        }
        await tools["remember"].executor.execute({"content": "Alice's own fact."})
    rows = await memory_store.list_active(settings, TENANT, scope=MemoryFilter())
    assert [(r["content"], r["principal"]) for r in rows] == [("Alice's own fact.", ALICE)]


# --- the agent's surfaces, bound to one pool -------------------------------------------------


def _tools(pool: MemoryPool) -> dict[str, Any]:
    return {t.name: t for t in make_memory_tools(settings=make_settings(), tenant_id=TENANT, pool=pool)}


async def _run(pool: MemoryPool, name: str, **args: Any) -> str:
    return await _tools(pool)[name].executor.execute(args)


A = MemoryPool.person(MANIFEST, ALICE)
B = MemoryPool.person(MANIFEST, BOB)


async def _put(content: str, pool: MemoryPool, **kw: Any) -> dict[str, Any]:
    return await memory_store.put_memory(make_settings(), TENANT, pool=pool, content=content, **kw)


async def test_one_persons_memory_never_reaches_another_through_any_surface() -> None:
    settings = make_settings()
    await _run(A, "remember", content="Alice's pin is 4242.", topic_key="user.pin")

    assert "4242" not in await _run(B, "recall", query="what is my pin")
    assert "4242" not in await _run(B, "list_memories")
    assert "4242" not in await active_facts_prompt(settings, TENANT, pool=B)
    assert await _known_values(settings, TENANT, pool=B) == []
    # Nor to a request that names no person.
    assert "4242" not in await active_facts_prompt(settings, TENANT, pool=MemoryPool.personless(MANIFEST))

    assert "4242" in await _run(A, "recall", query="what is my pin")
    assert "4242" in await active_facts_prompt(settings, TENANT, pool=A)
    assert await _known_values(settings, TENANT, pool=A) == [("user.pin", "Alice's pin is 4242.")]


async def test_a_person_sees_the_agents_curated_rows_but_not_what_the_agent_wrote_there() -> None:
    settings = make_settings()
    await _put(
        "Support hours are nine to five.", MemoryPool.agent(MANIFEST), metadata=OPERATOR, topic_key="hours"
    )
    await _put("Someone said they like nine o'clock.", MemoryPool.agent(MANIFEST), topic_key="agent.nine")

    for pool in (A, B, MemoryPool.personless(MANIFEST)):
        assert "nine to five" in await _run(pool, "recall", query="support hours nine")
        assert "nine o'clock" not in await _run(pool, "recall", query="support hours nine")
        prelude = await active_facts_prompt(settings, TENANT, pool=pool)
        assert "nine to five" in prelude and "nine o'clock" not in prelude
        listed = await _run(pool, "list_memories")
        assert "nine to five" in listed and "nine o'clock" not in listed
        assert [t for t, _ in await _known_values(settings, TENANT, pool=pool)] == ["hours"]
    # `scope: agent` keeps seeing its whole pool.
    assert "nine o'clock" in await _run(MemoryPool.agent(MANIFEST), "list_memories")


async def test_a_request_with_no_person_writes_nothing_under_person_scope() -> None:
    settings = make_settings()
    personless = MemoryPool.personless(MANIFEST)
    said = await _run(personless, "remember", content="Store this for whoever.")
    assert said.startswith("not stored:")

    class _Model:
        calls = 0

        async def chat(self, *_: Any, **__: Any) -> Any:
            _Model.calls += 1
            raise AssertionError("extraction ran for a pool that takes no writes")

    stored = await capture_from_turn(
        settings,
        TENANT,
        pool=personless,
        user_text="My name is Nobody.",
        assistant_text="Noted.",
        capture=MemoryCapture(enabled=True, min_chars=0),
        model=_Model(),
    )
    assert stored == [] and _Model.calls == 0
    assert await memory_store.list_active(settings, TENANT, scope=MemoryFilter()) == []


async def test_another_persons_row_cannot_be_forgotten_by_its_id() -> None:
    bobs = await _put("Bob's fact.", B)
    assert await _run(A, "forget", id=bobs["id"]) == f"no such memory: {bobs['id']}"
    assert "Bob's fact." in await _run(B, "list_memories")


async def test_an_agent_pool_row_is_out_of_a_persons_reach_by_pool_not_by_rank() -> None:
    # Agent-written, so the tool's own rank would allow the forget: the pool is what refuses it.
    shared = await _put("An agent-pool note.", MemoryPool.agent(MANIFEST))
    assert await _run(A, "forget", id=shared["id"]) == f"no such memory: {shared['id']}"
    assert await _run(MemoryPool.agent(MANIFEST), "forget", id=shared["id"]) == f"forgot:{shared['id']}"


async def test_remember_says_so_when_the_store_refuses_the_write() -> None:
    settings = make_settings()
    await _put("Alice likes tea.", MemoryPool.agent(MANIFEST))
    await _put("Alice likes tea.", A)
    await assign_to_person(settings, TENANT, manifest_id=MANIFEST, person=ALICE)
    # The merged duplicate kept its agent-pool id, now in Alice's pool: an agent-pool write of the
    # same sentence lands on it, and is refused rather than reported as stored.
    said = await _run(MemoryPool.agent(MANIFEST), "remember", content="Alice likes tea.")
    assert said == "not stored: that memory is held by another pool and cannot be written from here"


async def test_a_persons_remembered_topic_does_not_suppress_anothers_capture() -> None:
    settings = make_settings()
    extraction = json.dumps(
        [{"content": "Bob's timezone is Oslo time.", "kind": "fact", "topic_key": "user.tz"}]
    )

    class _Model:
        model_id = "scripted"

        async def chat(self, messages: Any, tools: Any, opts: Any = None) -> Any:
            from felix.patterns.model import ModelChatResult
            from felix.patterns.types import ChatMessage

            return ModelChatResult(
                message=ChatMessage(role="assistant", content=extraction), stop_reason="end_turn"
            )

    async with async_run_with_context(RequestContext(settings=settings, auth=AuthContext(tenant_id=TENANT))):
        await _run(A, "remember", content="Alice's timezone is Lisbon time.", topic_key="user.tz")
        stored = await capture_from_turn(
            settings,
            TENANT,
            pool=B,
            user_text="I am in Oslo",
            assistant_text="Noted.",
            capture=MemoryCapture(enabled=True, min_chars=0),
            model=_Model(),
        )
    assert stored == ["Bob's timezone is Oslo time."]
    rows = await memory_store.list_active(settings, TENANT, scope=MemoryFilter(principal=BOB))
    assert [r["content"] for r in rows] == ["Bob's timezone is Oslo time."]


async def test_two_agents_of_one_person_converge_through_the_tools() -> None:
    cowork = MemoryPool.person("cowork", ALICE)
    assert await _run(A, "remember", content="Alice likes tea.") == await _run(
        cowork, "remember", content="alice likes TEA."
    )
    listed = await _run(cowork, "list_memories")
    assert listed.lower().count("alice likes tea.") == 1, listed


async def test_procedures_stay_with_the_agent_not_the_person() -> None:
    settings = make_settings()
    spec = ProceduralSpec(enabled=True)
    await _run(A, "remember", content="Deploy steps: run make deploy.", kind="procedure")
    assert (
        await retrieve_procedures(settings, TENANT, manifest_id=MANIFEST, query="deploy steps", spec=spec)
        == ""
    )
    # The positive control: a procedure in the agent's pool is retrieved.
    await _put("Release steps: tag and push.", MemoryPool.agent(MANIFEST), kind="procedure")
    assert "Release steps" in await retrieve_procedures(
        settings, TENANT, manifest_id=MANIFEST, query="release steps", spec=spec
    )


# --- the operator's routes -------------------------------------------------------------------

KEYS = json.dumps({"sk-ops": {"tenant_id": TENANT, "sub": "ops", "scopes": ["memory:write"]}})
OPS = {"Authorization": "Bearer sk-ops"}


def _client(**settings: Any) -> AsyncClient:
    from felix_api.app import create_app

    api = create_app(settings=make_settings(auth_mode="api_key", auth_api_keys=KEYS, **settings), plugins=[])
    return AsyncClient(transport=ASGITransport(app=api), base_url="http://test")


async def test_the_routes_write_a_named_pool_and_filter_by_principal() -> None:
    async with _client(principal_aliases=json.dumps({"google:9": "github:1"})) as client:
        # A bare subject is Felix's own issuer, and an alias lands in the person it names.
        alice = await client.post(
            "/memory",
            json={"content": "Alice's fact.", "manifest_id": MANIFEST, "principal": "google:9"},
            headers=OPS,
        )
        agent = await client.post(
            "/memory", json={"content": "Agent fact.", "manifest_id": MANIFEST}, headers=OPS
        )
        assert alice.status_code == 200 and agent.status_code == 200, (alice.text, agent.text)
        assert alice.json() == {
            "id": memory_store.person_memory_id(ALICE, "Alice's fact."),
            "status": "active",
            "principal": ALICE,
        }
        assert agent.json()["id"] == memory_store.memory_id(MANIFEST, "Agent fact.")

        def contents(resp: Any) -> set[str]:
            assert resp.status_code == 200, resp.text
            return {i["content"] for i in resp.json()["items"]}

        assert contents(await client.get("/memory", headers=OPS)) == {"Alice's fact.", "Agent fact."}
        assert contents(await client.get("/memory", params={"principal": "github:1"}, headers=OPS)) == {
            "Alice's fact."
        }
        assert contents(await client.get("/memory", params={"principal": ALICE}, headers=OPS)) == {
            "Alice's fact."
        }
        assert contents(await client.get("/memory", params={"principal": ""}, headers=OPS)) == {"Agent fact."}
        assert contents(await client.get("/memory/as-of/9", params={"principal": ALICE}, headers=OPS)) == {
            "Alice's fact."
        }
        found = await client.get("/memory/search", params={"q": "fact", "principal": BOB}, headers=OPS)
        assert contents(found) == set()
        found = await client.get("/memory/search", params={"q": "fact", "principal": ALICE}, headers=OPS)
        assert [i["principal"] for i in found.json()["items"]] == [ALICE]


@pytest.mark.parametrize("principal", ["api_key", "cron", "trigger:t1", "github-actions:1:x", "iss|"])
async def test_a_write_naming_no_one_person_is_refused(principal: str) -> None:
    async with _client() as client:
        resp = await client.post(
            "/memory",
            json={"content": "A fact.", "manifest_id": MANIFEST, "principal": principal},
            headers=OPS,
        )
        assert (resp.status_code, resp.json()["detail"]) == (422, "principal_not_a_person")
        assert (await client.get("/memory", params={"principal": principal}, headers=OPS)).status_code == 422
    assert await memory_store.list_active(make_settings(), TENANT, scope=MemoryFilter()) == []


async def test_a_write_onto_another_pools_row_answers_409() -> None:
    settings = make_settings()
    await _put("Alice likes tea.", MemoryPool.agent(MANIFEST))
    await _put("Alice likes tea.", A)
    await assign_to_person(settings, TENANT, manifest_id=MANIFEST, person=ALICE)
    async with _client() as client:
        resp = await client.post(
            "/memory", json={"content": "Alice likes tea.", "manifest_id": MANIFEST}, headers=OPS
        )
    assert (resp.status_code, resp.json()["detail"]) == (409, "memory_held_by_another_pool")


# --- `felix memory assign` -------------------------------------------------------------------


@pytest.fixture
def _cli(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real-looking database URL and a recording stand-in for the store function.

    Port 1: if the substitution ever slips, the command fails to connect rather than reaching a
    developer's running Postgres.
    """
    from felix.memory import assign as assign_module

    monkeypatch.setenv("FELIX_DATABASE_URL", "postgresql+psycopg://felix:felix@127.0.0.1:1/felix")
    monkeypatch.setenv("FELIX_PRINCIPAL_ALIASES", json.dumps({"google:9": "github:1"}))
    monkeypatch.setenv("COLUMNS", "200")
    get_settings.cache_clear()
    seen: dict[str, Any] = {}

    async def fake(settings: Any, tenant_id: str, **kwargs: Any) -> AssignReport:
        seen.update(kwargs, tenant_id=tenant_id)
        return AssignReport(scanned=5, moved=4, merged=1)

    monkeypatch.setattr(assign_module, "assign_to_person", fake)
    yield seen
    get_settings.cache_clear()


def test_assign_refuses_the_in_memory_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FELIX_DATABASE_URL", "memory://cli")
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(
            app, ["memory", "assign", "--tenant", "t", "--manifest", "m", "--to", "github:1"]
        )
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 2, result.output
    assert "memory://" in result.output


def test_assign_moves_to_the_person_an_alias_names(_cli: dict[str, Any]) -> None:
    result = CliRunner().invoke(
        app,
        ["memory", "assign", "--tenant", "acme", "--manifest", "assistant", "--to", "google:9", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert _cli == {
        "tenant_id": "acme",
        "manifest_id": "assistant",
        "person": ALICE,
        "include_operator": False,
        "dry_run": True,
        "batch_size": 500,
    }
    assert f"acme/assistant -> {ALICE}: would move 4, merged 1 of 5 row(s)" in result.output


def test_assign_moves_operator_rows_only_when_asked(_cli: dict[str, Any]) -> None:
    result = CliRunner().invoke(
        app,
        [
            "memory",
            "assign",
            "--tenant",
            "acme",
            "--manifest",
            "assistant",
            "--to",
            ALICE,
            "--include-operator",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (_cli["person"], _cli["include_operator"]) == (ALICE, True)


@pytest.mark.parametrize(
    ("args", "hint"),
    [
        (["--tenant", "acme", "--manifest", "bad name!", "--to", "github:1"], "--manifest"),
        (["--tenant", "acme", "--manifest", "assistant", "--to", "api_key"], "--to"),
        (["--tenant", "acme", "--manifest", "assistant", "--to", "cron"], "--to"),
        (["--tenant", "acme", "--manifest", "assistant", "--to", "github-actions:1:x"], "--to"),
        (["--tenant", "a/b", "--manifest", "assistant", "--to", "github:1"], "--tenant"),
    ],
    ids=["manifest-name", "shared-key", "machine-actor", "actions", "tenant-delimiter"],
)
def test_assign_refuses_what_is_not_one_agent_and_one_person(
    _cli: dict[str, Any], args: list[str], hint: str
) -> None:
    result = CliRunner().invoke(app, ["memory", "assign", *args])
    assert result.exit_code == 2, result.output
    assert hint in result.output
    assert _cli == {}, "the store was reached with an argument the command should have refused"
