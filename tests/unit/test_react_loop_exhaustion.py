"""A react loop that runs out of steps says so.

`range(recursion_limit)` used to fall through with the model's own `tool_use` as the stop
reason and a session status of complete — a run cut off mid-thought looked like a finished
one. The first live triage run stopped at step ten with two tool calls pending and nothing
recorded it. Driven through the real `_ReactAgent.invoke` with a model that always asks for
another tool call.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns import react as react_mod
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput, ToolCall
from felix.tools.types import define_tool
from felix_ai.types import ModelChatResult, TokenUsage


class _AlwaysCalls:
    """A model that never finishes: every turn asks for the same tool again."""

    model_id = "scripted"

    def __init__(self) -> None:
        self.turns = 0

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None) -> ModelChatResult:
        self.turns += 1
        return ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content=f"step {self.turns}",
                tool_calls=[ToolCall(id=str(self.turns), name="t", args={})],
            ),
            stop_reason="tool_use",
            usage=TokenUsage(),
        )


async def _ok(_a: Any = None, _c: Any = None) -> str:
    return "ok"


def _agent(model: Any, limit: int) -> _ReactAgent:
    agent = _ReactAgent(
        tools=[define_tool(name="t", description="", handler=_ok, transport="local")],
        pattern="react",
        manifest_id="m",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=None,
        recursion_limit=limit,
    )
    agent._resolve_model = lambda _input: model  # type: ignore[method-assign]
    return agent


@pytest.mark.asyncio
async def test_running_out_of_steps_is_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    counted: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        react_mod, "record_counter", lambda name, labels: counted.append((name, dict(labels)))
    )
    model = _AlwaysCalls()
    out = await _agent(model, limit=3).invoke(InvokeInput(messages=[ChatMessage(role="user", content="go")]))
    assert model.turns == 3, "the loop took exactly recursion_limit steps"
    assert out.stop_reason == "max_turns"
    assert out.final.tool_calls, "the final message still carries the calls nothing executed"
    assert ("felix_run_stop_reason", {"manifest_id": "m", "reason": "max_turns"}) in counted


def test_the_openai_wire_reports_it_as_length() -> None:
    """`/v1` maps unknown stop reasons to `stop`; a run cut short must not read as one."""
    from felix_ai.wire.openai_completions import finish_reason_for

    assert finish_reason_for("max_turns") == "length"


@pytest.mark.asyncio
async def test_a_model_that_finishes_is_not_reported() -> None:
    class _Finishes(_AlwaysCalls):
        async def chat(
            self, messages: list[ChatMessage], tools: list[Any], opts: Any = None
        ) -> ModelChatResult:
            self.turns += 1
            return ModelChatResult(
                message=ChatMessage(role="assistant", content="done"),
                stop_reason="end_turn",
                usage=TokenUsage(),
            )

    out = await _agent(_Finishes(), limit=3).invoke(
        InvokeInput(messages=[ChatMessage(role="user", content="go")])
    )
    assert out.stop_reason == "end_turn"


def test_max_turns_on_a_react_agent_is_warned_as_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """The three bundled manifests that carried it bounded nothing; the compile step says so."""
    from felix.manifests import builder
    from felix.manifests.loader import parse_manifest

    counted: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(builder, "record_counter", lambda name, labels: counted.append((name, dict(labels))))

    def _m(spec: dict[str, Any]) -> Any:
        return parse_manifest(
            {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "x"}, "spec": spec}
        )

    builder._warn_max_turns_does_not_bound_this_loop(_m({"max_turns": 30}))
    assert counted == [
        ("felix_rule_targets_nothing", {"manifest_id": "x", "kind": "max_turns", "rule": "max_turns"})
    ]
    counted.clear()
    builder._warn_max_turns_does_not_bound_this_loop(_m({"max_turns": 30, "recursion_limit": 30}))
    builder._warn_max_turns_does_not_bound_this_loop(_m({"recursion_limit": 30}))
    builder._warn_max_turns_does_not_bound_this_loop(
        _m({"pattern": "groupchat", "sub_agents": ["a", "b"], "max_turns": 6})
    )
    assert counted == [], "set beside recursion_limit, unset, or on a multi-agent pattern: not inert"
