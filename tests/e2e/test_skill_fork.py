"""A skill whose frontmatter says `context: fork` runs in a subagent, and only its answer returns.

Through the real app: the lead's model (or a person's `/name`) activates the skill, the harness runs
one of the lead's delegation agents with the skill's instructions as its job, and `activate_skill`
answers with that agent's reply. The instructions never enter the lead's conversation, and the
skill is not an activation of it -- nothing to retain, nothing listed as active.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

BODY = "Look the answer up and reply with one line. Subject: $ARGUMENTS."


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "SKILL.md").write_text(
        f"---\nname: research\ndescription: Research a question.\ncontext: fork\nagent: e2e-researcher\n---\n{BODY}\n"
    )
    return tmp_path


def _agent(name: str, **spec: Any) -> Any:
    base: dict[str, Any] = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": True}}}
    base.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": base}
    )


def _manifests(**lead: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "skills": [{"name": "research"}],
        "delegation": {"agents": [{"name": "e2e-researcher", "description": "Looks things up."}]},
        **lead,
    }
    return {
        "e2e-lead": _agent("e2e-lead", system_prompt={"inline": "You lead."}, **spec),
        "e2e-researcher": _agent("e2e-researcher", system_prompt={"inline": "You research."}),
    }


async def _chat(app: Any, text: str, thread: str = "e2e-fork-1") -> None:
    response = await app.client.post(
        "/chat",
        json={"manifest": "e2e-lead", "thread_id": thread, "messages": [{"role": "user", "content": text}]},
    )
    assert response.status_code == 200, response.text


def _result(prompt: list[Any], tool: str = "activate_skill") -> Any:
    [message] = [m for m in prompt if getattr(m, "role", "") == "tool" and getattr(m, "name", "") == tool][
        -1:
    ]
    return json.loads(str(message.content))


def _text(prompt: list[Any]) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in prompt)


async def test_the_model_runs_a_forked_skill_and_reads_only_the_answer(boot: Any, skills_dir: Path) -> None:
    run = ToolCall(
        id="f1", name="activate_skill", args={"name": "research", "task": "How tall is the tower?"}
    )
    listing = ToolCall(id="l1", name="list_skills", args={})
    script = [
        ScriptedTurn(tool_calls=[run]),
        ScriptedTurn(content="330 metres."),  # the researcher
        ScriptedTurn(tool_calls=[listing]),
        ScriptedTurn(content="It is 330 m."),
    ]
    async with boot(script, env={"FELIX_SKILLS_DIR": str(skills_dir)}, manifests=_manifests()) as app:
        await _chat(app, "how tall is the tower?")
        _lead_first, child, after_fork, after_list = app.spy.prompts

    assert "You research." in _text(child) and "Look the answer up" in _text(child)
    assert "How tall is the tower?" in _text(child)
    assert _result(after_fork) == {"ran": "research", "agent": "e2e-researcher", "result": "330 metres."}
    assert "Look the answer up" not in _text(after_list), (
        "the instructions never enter the lead's conversation"
    )
    research = next(s for s in _result(after_list, "list_skills") if s["name"] == "research")
    assert research["active"] is False, "a fork is not an activation of this conversation"


async def test_a_person_runs_a_forked_skill_with_arguments(boot: Any, skills_dir: Path) -> None:
    script = [ScriptedTurn(content="It opened in 1889."), ScriptedTurn(content="1889.")]
    async with boot(script, env={"FELIX_SKILLS_DIR": str(skills_dir)}, manifests=_manifests()) as app:
        await _chat(app, "/research the Eiffel Tower")
        child, lead = app.spy.prompts

    assert "Subject: the Eiffel Tower." in _text(child)
    assert _result(lead)["result"] == "It opened in 1889."


async def test_a_fork_counts_as_a_peer_hop(boot: Any, skills_dir: Path) -> None:
    """Delegation's run-time bound holds for forks: past `max_peer_hops`, the agent is not called."""
    first = ToolCall(id="f1", name="activate_skill", args={"name": "research", "task": "one"})
    second = ToolCall(id="f2", name="activate_skill", args={"name": "research", "task": "two"})
    script = [
        ScriptedTurn(tool_calls=[first]),
        ScriptedTurn(content="first answer"),  # the researcher, once
        ScriptedTurn(tool_calls=[second]),
        ScriptedTurn(content="done"),
    ]
    async with boot(
        script, env={"FELIX_SKILLS_DIR": str(skills_dir)}, manifests=_manifests(limits={"max_peer_hops": 1})
    ) as app:
        await _chat(app, "go")
        _lead, _child, _after_first, after_second = app.spy.prompts

    [refused] = [m for m in after_second if getattr(m, "role", "") == "tool"][-1:]
    assert "max_peer_hops (1) exceeded" in str(refused.content), "refused by the limits on `task`"


async def test_a_fork_with_nowhere_to_run_says_so(boot: Any, skills_dir: Path) -> None:
    manifests = _manifests()
    manifests["e2e-lead"] = _agent("e2e-lead", skills=[{"name": "research"}])
    run = ToolCall(id="f1", name="activate_skill", args={"name": "research", "task": "x"})
    async with boot(
        [ScriptedTurn(tool_calls=[run]), ScriptedTurn(content="ok")],
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests=manifests,
    ) as app:
        await _chat(app, "go")
        assert _result(app.spy.prompts[1])["error"] == "fork_unavailable"


async def test_an_approval_on_task_holds_a_fork_too(boot: Any, skills_dir: Path) -> None:
    """A fork is a call of the governed `task` tool: a gate the author put on delegation holds for it.
    Left to expire, the agent never runs."""
    gated = _manifests(
        approvals=[{"id": "delegate", "tools": ["task"], "ttl_seconds": 1, "allow_unattended": True}]
    )
    run = ToolCall(id="f1", name="activate_skill", args={"name": "research", "task": "x"})
    async with boot(
        [ScriptedTurn(tool_calls=[run]), ScriptedTurn(content="no answer")],
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests=gated,
    ) as app:
        await _chat(app, "go")
        _lead_first, lead_second = app.spy.prompts  # the researcher never ran
    [held] = [m for m in lead_second if getattr(m, "role", "") == "tool"][-1:]
    assert "approval" in str(held.content).lower()


async def test_a_forked_skill_is_not_preloaded(boot: Any, skills_dir: Path) -> None:
    """A fork is never an activation, so "once per conversation" could not hold: every turn would run
    the subagent again."""
    manifests = _manifests(skills=[{"name": "research", "preload": True}])
    async with boot(
        [ScriptedTurn(content="one"), ScriptedTurn(content="two")],
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests=manifests,
    ) as app:
        await _chat(app, "hello")
        await _chat(app, "again")
        first, second = app.spy.prompts  # two lead calls, and no researcher
    assert "You research." not in _text(first) + _text(second)
