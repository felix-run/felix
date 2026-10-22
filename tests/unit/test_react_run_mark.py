"""A react run's opening: the run mark goes up before the first append, and comes down -- with the
run queue -- however the opening ends.

`workspace_notes.run_active` is what `/chat/workspace/edited` and `notify` read to keep out of a run
in flight. The turn loop's `finally` has always cleared it; these cover the stretch before that
loop, which now holds the mark too.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix import steer, workspace_notes
from felix.patterns.model import ModelChatResult, TokenUsage
from felix.patterns.types import ChatMessage, InvokeInput

from tests.support.factories import make_settings

TENANT = "acme"
THREAD = f"{TENANT}:opening"


class _Model:
    model_id = "claude-sonnet-4-5"

    async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="ok"),
            stop_reason="end_turn",
            usage=TokenUsage(input=1, output=1),
        )


def _agent() -> Any:
    from felix.patterns.react import _ReactAgent
    from felix.session.store import get_session_store
    from felix.session.strategies import FullReplaySessionStrategy

    settings = make_settings()
    agent = _ReactAgent(
        tools=[],
        pattern="react",
        manifest_id="opening",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=settings,
        recursion_limit=3,
        session_store=get_session_store(settings, tenant_id=TENANT),
        session_strategy=FullReplaySessionStrategy(),
        tenant_id=TENANT,
    )
    agent._resolve_model = lambda _i: _Model()  # type: ignore[method-assign]
    return agent


def _input() -> InvokeInput:
    return InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id=THREAD, tenant_id=TENANT)


class AssemblyFailed(Exception):
    pass


async def test_an_opening_that_fails_clears_the_run_mark_and_releases_the_run_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent()
    seen: list[bool] = []

    async def failing(*_a: Any, **_k: Any) -> Any:
        seen.append(await workspace_notes.run_active(TENANT, THREAD))
        raise AssemblyFailed

    monkeypatch.setattr(agent, "_assemble_messages", failing)
    with pytest.raises(AssemblyFailed):
        await agent.invoke(_input())

    assert seen == [True], "the run was marked while it opened"
    assert not await workspace_notes.run_active(TENANT, THREAD)
    assert steer._key(TENANT, THREAD) not in steer._queues


async def test_the_run_mark_is_refreshed_once_the_history_is_assembled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assembly can summarise a long thread with a model call, longer than the mark's Redis TTL."""
    from felix.patterns import react

    agent = _agent()
    order: list[str] = []
    assemble = agent._assemble_messages

    async def assembled(*a: Any, **k: Any) -> Any:
        order.append("assembled")
        return await assemble(*a, **k)

    async def refreshed(tenant_id: str, thread_id: str) -> None:
        order.append(f"refreshed {thread_id}")

    monkeypatch.setattr(agent, "_assemble_messages", assembled)
    monkeypatch.setattr(react, "refresh_run_active", refreshed)
    await agent.invoke(_input())

    assert order[:2] == ["assembled", f"refreshed {THREAD}"], order
