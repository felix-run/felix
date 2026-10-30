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


async def test_a_fork_with_nowhere_to_run_says_so(
    boot: Any, skills_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Its `agent:` names one the lead does not delegate to, and is not the lead itself."""
    import logging

    manifests = _manifests()
    manifests["e2e-lead"] = _agent("e2e-lead", skills=[{"name": "research"}])
    run = ToolCall(id="f1", name="activate_skill", args={"name": "research", "task": "x"})
    with caplog.at_level(logging.WARNING, logger="felix.manifests.builder"):
        async with boot(
            [ScriptedTurn(tool_calls=[run]), ScriptedTurn(content="ok")],
            env={"FELIX_SKILLS_DIR": str(skills_dir)},
            manifests=manifests,
        ) as app:
            await _chat(app, "go")
            assert _result(app.spy.prompts[1])["error"] == "fork_unavailable"
    said = [r for r in caplog.records if "nowhere to run them" in r.getMessage()]
    assert len(said) == 1, "said at the compile, once"


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


@pytest.fixture
def general_skills_dir(tmp_path: Path) -> Path:
    """A forked skill that names no agent: Claude Code's general-purpose subagent."""
    (tmp_path / "digest").mkdir()
    (tmp_path / "digest" / "SKILL.md").write_text(
        "---\nname: digest\ndescription: Digest a document.\ncontext: fork\n---\n"
        "Read it closely and reply with a summary. Subject: $ARGUMENTS.\n"
    )
    return tmp_path


def _solo(**spec: Any) -> dict[str, Any]:
    """A lead that delegates to nothing."""
    return {
        "e2e-lead": _agent(
            "e2e-lead", system_prompt={"inline": "You lead."}, skills=[{"name": "digest"}], **spec
        )
    }


async def test_a_fork_with_no_delegation_runs_in_the_manifest_itself(
    boot: Any, general_skills_dir: Path
) -> None:
    run = ToolCall(id="f1", name="activate_skill", args={"name": "digest", "task": "Summarise the memo."})
    script = [
        ScriptedTurn(tool_calls=[run]),
        ScriptedTurn(content="A short memo."),  # the lead, again, as the fork's subagent
        ScriptedTurn(content="It is short."),
    ]
    async with boot(script, env={"FELIX_SKILLS_DIR": str(general_skills_dir)}, manifests=_solo()) as app:
        await _chat(app, "summarise the memo")
        lead_first, child, after_fork = app.spy.prompts
        offered = app.spy.tools[0]

    assert "You lead." in _text(child) and "Read it closely" in _text(child)
    assert "Summarise the memo." in _text(child)
    assert "Read it closely" not in _text(lead_first), "the instructions stay in the subagent"
    assert _result(after_fork) == {"ran": "digest", "agent": "e2e-lead", "result": "A short memo."}
    assert "task" not in offered, "the model was given no delegation to call"


async def test_the_manifest_s_own_subagent_does_not_fork_again(boot: Any, general_skills_dir: Path) -> None:
    """One level: the subagent is the manifest itself, and its own forks run nowhere."""
    outer = ToolCall(id="f1", name="activate_skill", args={"name": "digest", "task": "go"})
    inner = ToolCall(id="f2", name="activate_skill", args={"name": "digest", "task": "deeper"})
    script = [
        ScriptedTurn(tool_calls=[outer]),
        ScriptedTurn(tool_calls=[inner]),  # the subagent tries to fork
        ScriptedTurn(content="stopped"),
        ScriptedTurn(content="done"),
    ]
    async with boot(script, env={"FELIX_SKILLS_DIR": str(general_skills_dir)}, manifests=_solo()) as app:
        await _chat(app, "go")
        _lead, _child, child_after, _lead_after = app.spy.prompts
    assert _result(child_after)["error"] == "fork_unavailable"


async def test_an_approval_on_task_holds_a_fork_into_the_manifest_itself(
    boot: Any, general_skills_dir: Path
) -> None:
    """The hidden `task` is governed as delegation's is: a gate an author put on `task` holds."""
    gated = _solo(
        approvals=[{"id": "delegate", "tools": ["task"], "ttl_seconds": 1, "allow_unattended": True}]
    )
    run = ToolCall(id="f1", name="activate_skill", args={"name": "digest", "task": "x"})
    async with boot(
        [ScriptedTurn(tool_calls=[run]), ScriptedTurn(content="no answer")],
        env={"FELIX_SKILLS_DIR": str(general_skills_dir)},
        manifests=gated,
    ) as app:
        await _chat(app, "go")
        _lead_first, lead_second = app.spy.prompts  # the subagent never ran
    [held] = [m for m in lead_second if getattr(m, "role", "") == "tool"][-1:]
    assert "approval" in str(held.content).lower()


async def test_a_person_runs_a_general_forked_skill_with_arguments(
    boot: Any, general_skills_dir: Path
) -> None:
    script = [ScriptedTurn(content="Summary of Q3."), ScriptedTurn(content="Q3, summarised.")]
    async with boot(script, env={"FELIX_SKILLS_DIR": str(general_skills_dir)}, manifests=_solo()) as app:
        await _chat(app, "/digest the Q3 report")
        child, lead = app.spy.prompts
    assert "Subject: the Q3 report." in _text(child)
    assert _result(lead)["result"] == "Summary of Q3."


async def test_two_forks_compile_the_manifest_s_subagent_once_and_quietly(
    boot: Any, general_skills_dir: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compiled on the first fork and reused; and the compile does not report the manifest's rules on
    `task` -- what gates its forks -- as gating nothing, since the subagent binds no `task`."""
    import logging

    import felix.manifests.builder as builder

    subagent_compiles: list[str] = []
    real = builder.build_agent

    async def counted(manifest: Any, tools: Any = None, deps: Any = None, **kw: Any) -> Any:
        if deps is not None and deps.self_fork_of:
            subagent_compiles.append(deps.self_fork_of)
        return await real(manifest, tools, deps, **kw)

    # A pass-through count: model clients are built per run, not per compile, so the spy cannot say.
    monkeypatch.setattr(builder, "build_agent", counted)

    first = ToolCall(id="f1", name="activate_skill", args={"name": "digest", "task": "one"})
    second = ToolCall(id="f2", name="activate_skill", args={"name": "digest", "task": "two"})
    script = [
        ScriptedTurn(tool_calls=[first]),
        ScriptedTurn(content="first answer"),
        ScriptedTurn(tool_calls=[second]),
        ScriptedTurn(content="second answer"),
        ScriptedTurn(content="done"),
    ]
    guided = _solo(tool_guidance={"task": "Hand over the whole job."})
    with caplog.at_level(logging.WARNING, logger="felix.manifests.builder"):
        async with boot(script, env={"FELIX_SKILLS_DIR": str(general_skills_dir)}, manifests=guided) as app:
            await _chat(app, "go")
            assert len(app.spy.prompts) == 5, "the lead, a fork, the lead, a fork, the lead"
            assert "first answer" not in _text(app.spy.prompts[3]), "each fork starts a fresh conversation"
    assert subagent_compiles == ["e2e-lead"], "compiled on the first fork, reused by the second"
    assert not [r for r in caplog.records if "gates nothing" in r.getMessage()]
    assert not [r for r in caplog.records if "nowhere to run them" in r.getMessage()]


async def test_a_skill_that_names_its_own_manifest_forks_into_it(boot: Any, tmp_path: Path) -> None:
    (tmp_path / "digest").mkdir()
    (tmp_path / "digest" / "SKILL.md").write_text(
        "---\nname: digest\ndescription: Digest.\ncontext: fork\nagent: e2e-lead\n---\nSummarise it.\n"
    )
    run = ToolCall(id="f1", name="activate_skill", args={"name": "digest", "task": "the memo"})
    script = [
        ScriptedTurn(tool_calls=[run]),
        ScriptedTurn(content="summary"),
        ScriptedTurn(content="done"),
    ]
    async with boot(script, env={"FELIX_SKILLS_DIR": str(tmp_path)}, manifests=_solo()) as app:
        await _chat(app, "go")
        assert len(app.spy.prompts) == 3, "the lead, its subagent, the lead"
        after_fork = app.spy.prompts[2]
    assert _result(after_fork) == {"ran": "digest", "agent": "e2e-lead", "result": "summary"}


async def test_a_fork_is_one_call_against_max_tool_calls(boot: Any, skills_dir: Path) -> None:
    """The `task` call a fork makes is the `activate_skill` call's own work: with a cap of two, the
    fork and one more call both run. Counted as a second call, a fork at the last allowed call was
    refused by the cap it had just used, and the call after it one early."""
    run = ToolCall(id="f1", name="activate_skill", args={"name": "research", "task": "How tall?"})
    listing = ToolCall(id="l1", name="list_skills", args={})
    script = [
        ScriptedTurn(tool_calls=[run]),
        ScriptedTurn(content="330 metres."),  # the researcher
        ScriptedTurn(tool_calls=[listing]),
        ScriptedTurn(content="ok"),
    ]
    async with boot(
        script, env={"FELIX_SKILLS_DIR": str(skills_dir)}, manifests=_manifests(limits={"max_tool_calls": 2})
    ) as app:
        await _chat(app, "go")
        _lead, _child, after_fork, after_list = app.spy.prompts
    assert _result(after_fork)["result"] == "330 metres."
    [listed] = [m for m in after_list if getattr(m, "role", "") == "tool"][-1:]
    assert "max_tool_calls" not in str(listed.content), "the fork was counted once"


async def test_a_fork_s_task_call_is_audited_as_a_call(boot: Any, skills_dir: Path) -> None:
    """The fork's `task` runs through a tool runner: the ledger has a `tool_call` row for it, by the
    id derived from the model's `activate_skill` call."""
    from felix.audit import store as audit_store
    from felix.flush import flush_all

    run = ToolCall(id="f1", name="activate_skill", args={"name": "research", "task": "x"})
    script = [ScriptedTurn(tool_calls=[run]), ScriptedTurn(content="answer"), ScriptedTurn(content="ok")]
    async with boot(script, env={"FELIX_SKILLS_DIR": str(skills_dir)}, manifests=_manifests()) as app:
        await _chat(app, "go")
        await flush_all(app.settings)
        rows, _ = await audit_store.query(app.settings, "default", limit=200)
    calls = {
        (r["payload_json"] or {}).get("tool_call_id"): (r["payload_json"] or {}).get("tool")
        for r in rows
        if r["event_type"] == "tool_call" and r.get("status") == "ok"
    }
    assert {k: calls.get(k) for k in ("f1", "f1:fork")} == {"f1": "activate_skill", "f1:fork": "task"}


async def test_a_pre_tool_use_hook_on_task_holds_a_fork_too(boot: Any, skills_dir: Path) -> None:
    """A hook the author put on `task` is asked about a fork's `task`, and its block stops the agent."""
    from tests.support.webhook_receiver import SECRET, hook_receiver

    def answer(req: dict[str, Any]) -> tuple[int, Any]:
        return 200, {"decision": "block", "reason": "no research today"}

    run = ToolCall(id="f1", name="activate_skill", args={"name": "research", "task": "x"})
    hooks = [{"id": "h1", "event": "pre_tool_use", "endpoint": "policy", "tools": ["task"]}]
    async with hook_receiver(answer) as (url, seen):
        env = {
            "FELIX_SKILLS_DIR": str(skills_dir),
            "FELIX_WEBHOOK_ENDPOINTS": json.dumps(
                {"policy": {"url": url, "secret": SECRET, "tenants": "*", "hooks": True}}
            ),
        }
        script = [ScriptedTurn(tool_calls=[run]), ScriptedTurn(content="no answer")]
        async with boot(script, env=env, manifests=_manifests(hooks=hooks)) as app:
            await _chat(app, "go")
            _lead_first, lead_second = app.spy.prompts  # the researcher never ran
            from felix.audit import store as audit_store
            from felix.flush import flush_all

            await flush_all(app.settings)
            rows, _ = await audit_store.query(app.settings, "default", limit=200)
    denies = {
        (r["payload_json"] or {}).get("tool_call_id"): (r["payload_json"] or {}).get("control")
        for r in rows
        if r["event_type"] == "policy_deny"
    }
    assert denies == {"f1:fork": "manifest_hook", "f1": "manifest_hook"}, (
        "both calls, by the hook that refused"
    )
    [held] = [m for m in lead_second if getattr(m, "role", "") == "tool"][-1:]
    # Passed on as the deny it is, not wrapped into a `fork_failed` answer that quotes it.
    assert str(held.content).startswith("[hook denied] h1\n") and "no research today" in str(held.content)
    [request] = seen
    assert request["json"]["data"]["tool_name"] == "task"
    assert request["json"]["data"]["args"]["prompt"].startswith("x\n\n<skill_instructions")


async def test_a_post_tool_use_hook_on_task_adds_to_a_fork_s_answer(boot: Any, skills_dir: Path) -> None:
    """What a `post_tool_use` hook on `task` says joins the fork's answer, fenced, as for the model's
    own `task` call."""
    from tests.support.webhook_receiver import SECRET, hook_receiver

    def answer(req: dict[str, Any]) -> tuple[int, Any]:
        return 200, {"additional_context": "checked by policy"}

    run = ToolCall(id="f1", name="activate_skill", args={"name": "research", "task": "x"})
    hooks = [{"id": "h2", "event": "post_tool_use", "endpoint": "policy", "tools": ["task"]}]
    async with hook_receiver(answer) as (url, seen):
        env = {
            "FELIX_SKILLS_DIR": str(skills_dir),
            "FELIX_WEBHOOK_ENDPOINTS": json.dumps(
                {"policy": {"url": url, "secret": SECRET, "tenants": "*", "hooks": True}}
            ),
        }
        script = [
            ScriptedTurn(tool_calls=[run]),
            ScriptedTurn(content="330 metres."),
            ScriptedTurn(content="ok"),
        ]
        async with boot(script, env=env, manifests=_manifests(hooks=hooks)) as app:
            await _chat(app, "go")
            _lead, _child, after = app.spy.prompts
    result = _result(after)["result"]
    assert result.startswith("330 metres.") and '<hook_context hook="h2">' in result
    assert "checked by policy" in result
    [request] = seen
    assert request["json"]["data"]["tool_name"] == "task"
