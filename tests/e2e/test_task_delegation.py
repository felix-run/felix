"""`spec.delegation`: the model hands a job to a child agent through the `task` tool.

Driven through the real stack -- the route, the compile, the governance wrappers, the react
loop -- with a scripted model whose one queue serves the parent and the child in call order.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall


def _agent(name: str, **spec: Any) -> Any:
    base: dict[str, Any] = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": True}}}
    base.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": base}
    )


def _lead(**spec: Any) -> Any:
    return _agent(
        "e2e-lead",
        system_prompt={"inline": "You lead."},
        delegation={"agents": [{"name": "e2e-researcher", "description": "Looks things up."}]},
        **spec,
    )


RESEARCHER = _agent("e2e-researcher", system_prompt={"inline": "You research."})


def _task(call_id: str, prompt: str) -> ToolCall:
    return ToolCall(id=call_id, name="task", args={"agent": "e2e-researcher", "prompt": prompt})


async def _chat(app: Any, name: str = "e2e-lead", text: str = "how tall is the tower?") -> Any:
    return await app.client.post(
        "/chat",
        json={"manifest": name, "thread_id": "e2e-task", "messages": [{"role": "user", "content": text}]},
    )


def _text(messages: list[Any]) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in messages)


async def test_the_child_answers_from_a_fresh_context_and_the_parent_reads_it(boot: Any) -> None:
    script = [
        ScriptedTurn(tool_calls=[_task("c1", "Find the height of the Eiffel Tower.")]),
        ScriptedTurn(content="It is 330 metres."),
        ScriptedTurn(content="The tower is 330 m."),
    ]
    async with boot(script, manifests={"e2e-lead": _lead(), "e2e-researcher": RESEARCHER}) as app:
        resp = await _chat(app)
        assert resp.status_code == 200, resp.text
        assert "task" in app.spy.tools[0]

        _parent_first, child, parent_second = app.spy.prompts
        # The child sees its own system prompt and the job -- not the parent's conversation.
        assert "You research." in _text(child)
        assert "Find the height of the Eiffel Tower." in _text(child)
        assert "how tall is the tower?" not in _text(child)
        assert "You lead." not in _text(child)
        # And the parent reads the child's answer back as the tool result.
        assert "It is 330 metres." in _text(parent_second)
        assert "The tower is 330 m." in resp.text


async def test_task_calls_count_as_peer_hops(boot: Any) -> None:
    """The run-time bound on delegation: the second call is refused by `limits`, and the
    refused one never reaches a model."""
    script = [
        ScriptedTurn(tool_calls=[_task("c1", "first job"), _task("c2", "second job")]),
        ScriptedTurn(content="first answer"),
        ScriptedTurn(content="done"),
    ]
    manifests = {"e2e-lead": _lead(limits={"max_peer_hops": 1}), "e2e-researcher": RESEARCHER}
    async with boot(script, manifests=manifests) as app:
        resp = await _chat(app)
        assert resp.status_code == 200, resp.text
        assert len(app.spy.prompts) == 3
        assert "second job" not in _text(app.spy.prompts[1])
        assert "max_peer_hops (1) exceeded" in _text(app.spy.prompts[2])


async def test_a_policy_on_task_governs_it_like_any_tool(boot: Any) -> None:
    """Bound before the governance pipeline: a scope policy naming `task` refuses the call."""
    script = [ScriptedTurn(tool_calls=[_task("c1", "job")]), ScriptedTurn(content="could not")]
    manifests = {
        "e2e-lead": _lead(
            policies=[{"id": "no-delegating", "tools": ["task"], "required_scopes": ["delegate"]}]
        ),
        "e2e-researcher": RESEARCHER,
    }
    async with boot(script, manifests=manifests) as app:
        resp = await _chat(app)
        assert resp.status_code == 200, resp.text
        # Parent, then parent again: the child was never invoked.
        assert len(app.spy.prompts) == 2
        assert "[policy denied] missing scopes for task: delegate" in _text(app.spy.prompts[1])


async def test_a_delegate_that_resolves_nowhere_fails_the_compile(boot: Any) -> None:
    async with boot(manifests={"e2e-lead": _lead()}) as app:
        with pytest.raises(LookupError, match="e2e-researcher"):
            await _chat(app)
        assert app.spy.prompts == []


async def test_delegation_beside_sub_agents_is_refused(boot: Any) -> None:
    """`sub_agents` binds no tools, so the `task` tool would compile and never be offered."""
    lead = _lead(pattern="router", sub_agents=["e2e-researcher"])
    async with boot(manifests={"e2e-lead": lead, "e2e-researcher": RESEARCHER}) as app:
        with pytest.raises(ValueError, match=r"spec\.delegation needs an agent with tools"):
            await _chat(app)


async def test_a_pinned_thread_refuses_a_turn_after_its_delegate_was_edited(boot: Any) -> None:
    from felix.manifests.store import activate_version, put_version

    lead = _lead(governance={"pin_compile": True})
    script = [ScriptedTurn(content="one"), ScriptedTurn(content="two")]
    async with boot(script, manifests={"e2e-lead": lead, "e2e-researcher": RESEARCHER}) as app:
        assert (await _chat(app, text="first")).status_code == 200
        await put_version(
            app.settings,
            "default",
            "e2e-researcher",
            _agent("e2e-researcher", system_prompt={"inline": "v2"}),
        )
        await activate_version(app.settings, "default", "e2e-researcher", version=2)
        resp = await _chat(app, text="second")
        assert resp.status_code == 409, resp.text


def test_delegate_names_must_be_unique() -> None:
    """Two refs for one name would leave the model's `agent` enum ambiguous about which
    description applies."""
    from felix.manifests.loader import ManifestParseError

    with pytest.raises(ManifestParseError, match="names must be unique"):
        _agent(
            "e2e-lead",
            delegation={"agents": [{"name": "e2e-researcher"}, {"name": "e2e-researcher"}]},
        )
