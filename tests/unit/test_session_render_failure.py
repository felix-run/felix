"""A thread whose history cannot be read is answered without it, and that is said out loud."""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns import react
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput
from felix.session.store import InMemorySessionStore
from felix.session.strategies import FullReplaySessionStrategy


class _UnreadableHistory(FullReplaySessionStrategy):
    async def render(self, session: Any, incoming: Any, opts: Any) -> list[ChatMessage]:
        raise ConnectionError("the database went away")


async def test_a_failed_render_is_counted_and_the_turn_starts_from_the_incoming_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """It was logged at debug: the model answered as if the thread were new, the reply was
    appended to the real thread, and nothing anywhere recorded that it had happened."""
    counted: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(react, "record_counter", lambda name, labels: counted.append((name, dict(labels))))
    agent = _ReactAgent(
        tools=[],
        pattern="react",
        manifest_id="quick",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=None,
        recursion_limit=3,
        session_store=InMemorySessionStore(tenant_id="default"),
        session_strategy=_UnreadableHistory(),
    )
    incoming = [ChatMessage(role="user", content="and the second thing?")]

    with caplog.at_level("WARNING", logger="felix.patterns.react"):
        messages = await agent._assemble_messages(
            InvokeInput(messages=incoming, thread_id="default:t-render"), model=object(), tenant_id="default"
        )

    assert [(m.role, m.content) for m in messages] == [("system", "s"), ("user", "and the second thing?")]
    assert counted == [("felix_session_render_failed", {"manifest_id": "quick"})]
    assert "answering without history" in caplog.text
