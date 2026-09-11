"""`spec.memory.checkpointer` selects where session state lives.

It shipped as a closed `Literal` that no code read, so every value silently meant
"whatever FELIX_DATABASE_URL points at" — including `do`, which named Cloudflare
Durable Objects, compute this stack deliberately does not run.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.session.store import (
    InMemorySessionStore,
    PostgresSessionStore,
    build_checkpointer,
    list_checkpointers,
    register_checkpointer,
    validate_checkpointer_config,
)

MEMORY = Settings(database_url="memory://ci")


@pytest.fixture
def restore_checkpointers() -> Any:
    from felix.session import store as store_mod

    saved = dict(store_mod._checkpointers)
    yield
    store_mod._checkpointers.clear()
    store_mod._checkpointers.update(saved)


def test_postgres_follows_the_configured_database_url() -> None:
    """Pins "follows the URL", not "returns an object".

    Asserting only the in-memory type under `memory://` passed just as well for an
    implementation that ignored settings entirely — which is the pre-change bug.
    `PostgresSessionStore` constructs lazily, so the real arm needs no database.
    """
    assert isinstance(build_checkpointer("postgres", MEMORY, tenant_id="t"), InMemorySessionStore)

    real_db = Settings(database_url="postgresql+psycopg://u:p@127.0.0.1:5999/nope")
    assert isinstance(build_checkpointer("postgres", real_db, tenant_id="t"), PostgresSessionStore)


def test_there_is_no_in_process_builtin() -> None:
    """Deliberate: a thread is not manifest-scoped.

    Fifteen `/chat` routes address a thread by id with no manifest in hand, so a
    manifest choosing a different *backend* would split-brain — the agent reading
    one log while `/history`, `/continue` and `/compact` read another. `none` is
    exempt because it is a claim about the agent, not a competing backend.
    """
    assert set(list_checkpointers()) >= {"none", "postgres"}
    with pytest.raises(ValueError, match="unknown checkpointer"):
        build_checkpointer("memory", MEMORY, tenant_id="t")


def test_none_means_no_store_at_all() -> None:
    """`None` and not a null store: the react loop already guards every session
    call on `session_store is None`, so this cannot half-persist."""
    assert build_checkpointer("none", MEMORY, tenant_id="t") is None


@pytest.mark.parametrize("name", ["do", "agentcore", "sqlite"])
def test_the_unimplementable_values_are_now_errors(name: str) -> None:
    """They used to be accepted and silently mean `postgres`."""
    with pytest.raises(ValueError, match="unknown checkpointer"):
        build_checkpointer(name, MEMORY, tenant_id="t")
    with pytest.raises(ValueError, match="unknown checkpointer"):
        validate_checkpointer_config(name, session_strategy="full_replay")


@pytest.mark.parametrize("strategy", ["full_replay", "compacting", "windowed:20", "semantic:5"])
def test_a_stateful_checkpointer_accepts_any_strategy(strategy: str) -> None:
    validate_checkpointer_config("postgres", session_strategy=strategy)


def test_a_plugin_can_register_a_checkpointer(restore_checkpointers: Any) -> None:
    class _Redis:
        """Stands in for a plugin-supplied store; never opened here."""

        def open(self, thread_id: str) -> Any:
            raise NotImplementedError

    register_checkpointer("acme-redis", lambda settings, tenant: _Redis())

    assert "acme-redis" in list_checkpointers()
    assert isinstance(build_checkpointer("acme-redis", MEMORY, tenant_id="t"), _Redis)
    validate_checkpointer_config("acme-redis", session_strategy="compacting")

    # The field must still accept the plugin's name — reverting it to a Literal
    # would otherwise only show up as generated-schema drift.
    from felix.manifests.schema import Manifest

    m = Manifest.model_validate(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "ck"},
            "spec": {"pattern": "react", "memory": {"checkpointer": "acme-redis"}},
        }
    )
    assert m.spec.memory.checkpointer == "acme-redis"


def test_a_plugin_cannot_replace_a_builtin(restore_checkpointers: Any) -> None:
    """A checkpointer decides where every conversation lands."""
    with pytest.raises(ValueError, match="cannot be overridden"):
        register_checkpointer("postgres", lambda settings, tenant: None)


@pytest.mark.asyncio
async def test_checkpointer_none_builds_a_stateless_agent() -> None:
    """End to end: the manifest field must reach the agent, not just the factory."""
    from felix.runtime import build_tenant_agent
    from felix.tools.provider import InMemoryToolProvider

    def _manifest(checkpointer: str) -> dict[str, Any]:
        return {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "ck"},
            "spec": {"pattern": "react", "memory": {"checkpointer": checkpointer}},
        }

    from felix.manifests.schema import Manifest

    stateless = await build_tenant_agent(
        MEMORY,
        manifest=Manifest.model_validate(_manifest("none")),
        tools=InMemoryToolProvider(),
        tenant_id="t",
    )
    stateful = await build_tenant_agent(
        MEMORY,
        manifest=Manifest.model_validate(_manifest("postgres")),
        tools=InMemoryToolProvider(),
        tenant_id="t",
    )

    assert stateless.session_store is None
    assert stateful.session_store is not None


@pytest.mark.parametrize(
    "spec_bit",
    [
        {"session": {"strategy": "compacting"}},
        {"session": {"compact_after_turn": True}},
        {"memory": {"checkpointer": "none", "capture": {"enabled": True}}},
    ],
)
def test_none_refuses_everything_the_loop_would_drop(spec_bit: dict[str, Any]) -> None:
    """Each of these is guarded on `session_store is None`, so with `none` it is a
    silent no-op — including memory capture, where `_turn_seq` returns None and every
    fact lands at genesis, collapsing supersession ordering rather than erroring."""
    from felix.manifests.schema import Manifest

    spec: dict[str, Any] = {"pattern": "react", "memory": {"checkpointer": "none"}}
    for key, value in spec_bit.items():
        spec[key] = {**spec.get(key, {}), **value}
    m = Manifest.model_validate(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "ck"}, "spec": spec}
    )
    with pytest.raises(ValueError, match="silently drops"):
        validate_checkpointer_config(
            m.spec.memory.checkpointer,
            session_strategy=m.spec.session.strategy,
            compact_after_turn=m.spec.session.compact_after_turn,
            memory_capture=m.spec.memory.capture.enabled,
        )


def test_none_with_nothing_stateful_is_fine() -> None:
    validate_checkpointer_config(
        "none", session_strategy="full_replay", compact_after_turn=False, memory_capture=False
    )


# --- the wiring ---------------------------------------------------------------
#
# Every test above calls `validate_checkpointer_config` directly, which proves the
# function's logic and nothing about whether anyone calls it. All three enforcement
# sites could be deleted with the suite still green. These pin the callers.


@pytest.mark.asyncio
async def test_runtime_enforces_the_cross_check() -> None:
    """`build_tenant_agent` must refuse, not just the validator in isolation."""
    from felix.manifests.schema import Manifest
    from felix.runtime import build_tenant_agent
    from felix.tools.provider import InMemoryToolProvider

    manifest = Manifest.model_validate(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "ck"},
            "spec": {
                "pattern": "react",
                "memory": {"checkpointer": "none"},
                "session": {"strategy": "compacting"},
            },
        }
    )
    with pytest.raises(ValueError, match="silently drops"):
        await build_tenant_agent(MEMORY, manifest=manifest, tools=InMemoryToolProvider(), tenant_id="t")


@pytest.mark.asyncio
async def test_runtime_rejects_an_unknown_checkpointer() -> None:
    from felix.manifests.schema import Manifest
    from felix.runtime import build_tenant_agent
    from felix.tools.provider import InMemoryToolProvider

    manifest = Manifest.model_validate(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "ck"},
            "spec": {"pattern": "react", "memory": {"checkpointer": "do"}},
        }
    )
    with pytest.raises(ValueError, match="unknown checkpointer"):
        await build_tenant_agent(MEMORY, manifest=manifest, tools=InMemoryToolProvider(), tenant_id="t")


def test_the_cli_rejects_a_bad_checkpointer(tmp_path: Any) -> None:
    """`validate-manifest` is documented as a GitOps CI gate."""
    from felix_cli.main import app
    from typer.testing import CliRunner

    path = tmp_path / "m.yaml"
    path.write_text(
        "apiVersion: felix/v1\nkind: Agent\nmetadata:\n  name: ck\n"
        "spec:\n  pattern: react\n  memory:\n    checkpointer: do\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate-manifest", str(path)])

    assert result.exit_code == 1
    # Rich wraps the line at the runner's width, which differs between a terminal and CI.
    assert "unknown checkpointer" in " ".join(result.output.split())


async def _put_manifest(checkpointer: str, *, strategy: str = "full_replay") -> tuple[int, str]:
    from felix.config import Settings as S
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    settings = S(
        allow_insecure=True,
        auth_mode="none",
        host="127.0.0.1",
        environment="development",
        object_store="memory",
        database_url="memory://ck-put",
    )
    manifest = {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": "ck"},
        "spec": {
            "pattern": "react",
            "memory": {"checkpointer": checkpointer},
            "session": {"strategy": strategy},
        },
    }
    app = create_app(settings=settings, plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.put("/manifests/ck", json={"manifest": manifest})
        return resp.status_code, resp.text


@pytest.mark.asyncio
async def test_put_manifest_rejects_a_bad_checkpointer_at_write_time() -> None:
    """Stored, it would raise inside every build — a 500 per request instead."""
    status, body = await _put_manifest("do")
    assert status == 400
    assert "unknown checkpointer" in body


@pytest.mark.asyncio
async def test_put_manifest_rejects_a_silently_dropped_combination() -> None:
    status, body = await _put_manifest("none", strategy="compacting")
    assert status == 400
    assert "silently drops" in body


@pytest.mark.asyncio
async def test_put_manifest_accepts_a_valid_checkpointer() -> None:
    status, _ = await _put_manifest("none")
    assert status == 200


@pytest.mark.asyncio
async def test_none_is_stateless_across_turns_and_postgres_is_not() -> None:
    """The behavioural claim, not the structural one.

    `assert agent.session_store is None` pins an attribute; the CHANGELOG promises
    "every turn starts from the messages it was given". This runs two turns on one
    thread and checks what actually landed in the tenant's log.
    """
    from felix.manifests.schema import Manifest
    from felix.patterns.model import ModelChatResult, TokenUsage
    from felix.patterns.types import ChatMessage, InvokeInput
    from felix.runtime import build_tenant_agent
    from felix.session.store import get_session_store
    from felix.tools.provider import InMemoryToolProvider

    class _Model:
        model_id = "claude-sonnet-4-5"

        async def chat(self, messages: list[Any], tools: list[Any], opts: Any = None) -> Any:
            return ModelChatResult(
                message=ChatMessage(role="assistant", content="ok"),
                stop_reason="end_turn",
                usage=TokenUsage(input=1, output=1),
            )

    async def _two_turns(checkpointer: str, thread: str) -> int:
        settings = Settings(database_url=f"memory://ck-{checkpointer}")
        manifest = Manifest.model_validate(
            {
                "apiVersion": "felix/v1",
                "kind": "Agent",
                "metadata": {"name": "ck"},
                "spec": {"pattern": "react", "memory": {"checkpointer": checkpointer}},
            }
        )
        agent = await build_tenant_agent(
            settings, manifest=manifest, tools=InMemoryToolProvider(), tenant_id="t"
        )
        agent._resolve_model = lambda _i: _Model()  # type: ignore[attr-defined]
        for text in ("first", "second"):
            await agent.invoke(
                InvokeInput(
                    messages=[ChatMessage(role="user", content=text)],
                    thread_id=thread,
                    model_id="claude-sonnet-4-5",
                    tenant_id="t",
                )
            )
        events = await get_session_store(settings, tenant_id="t").open(thread).get_events()
        return len(events)

    assert await _two_turns("none", "th-none") == 0
    assert await _two_turns("postgres", "th-pg") > 0
