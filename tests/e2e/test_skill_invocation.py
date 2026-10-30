"""A person runs a skill as `/name args`; a manifest preloads one. Through the real app.

The harness activates the skill before the model's first step -- through the tool runner, logged as
an activation -- so the model's first call already holds the instructions, with the arguments put
where the body asks. The person's turn is sent as written. A declared prompt of the same name wins,
a skill a person may not run is not intercepted, and a skill hidden from the model is still theirs
to run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

SKILLS = {
    "triage": "description: Triage an invoice.\nargument-hint: <vendor> <amount>\n",
    "secret-ops": "description: Operator runbook.\ndisable-model-invocation: true\n",
    "model-only": "description: Only the model picks this.\nuser-invocable: false\n",
}
BODIES = {
    "triage": "Triage the invoice from $0 for $1 ($ARGUMENTS).",
    "secret-ops": "Run the operator runbook.",
    "model-only": "Model-only steps.",
}


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    for name, front in SKILLS.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(f"---\nname: {name}\n{front}---\n{BODIES[name]}\n")
    return tmp_path


def _manifest(**spec: Any) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-invoke"},
            "spec": {
                "pattern": "react",
                "skills": [{"name": "triage"}],
                "auth": {"inbound": {"allow_anonymous": True}},
                **spec,
            },
        }
    )


async def _chat(app: Any, text: str, thread: str = "e2e-invoke-1") -> None:
    response = await app.client.post(
        "/chat",
        json={"manifest": "e2e-invoke", "thread_id": thread, "messages": [{"role": "user", "content": text}]},
    )
    assert response.status_code == 200, response.text


def _activations(prompt: list[Any]) -> list[dict[str, Any]]:
    out = []
    for m in prompt:
        if getattr(m, "role", "") == "tool" and getattr(m, "name", "") == "activate_skill":
            try:
                out.append(json.loads(str(m.content)))
            except ValueError:
                out.append({"text": str(m.content)})  # a refusal or an expired gate
    return out


async def test_a_person_runs_a_skill_with_arguments(boot: Any, skills_dir: Path) -> None:
    async with boot(
        [ScriptedTurn(content="triaged")],
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-invoke": _manifest()},
    ) as app:
        await _chat(app, "/triage ACME 900")
        first = app.spy.prompts[0]

    [activated] = _activations(first)
    assert activated["activated"] == "triage" and activated["arguments"] == "ACME 900"
    assert activated["instructions"] == "Triage the invoice from ACME for 900 (ACME 900)."
    turns = [str(m.content) for m in first if getattr(m, "role", "") == "user"]
    assert "/triage ACME 900" in turns, "the person's turn is sent as written"


async def test_a_skill_hidden_from_the_model_is_still_the_persons_to_run(boot: Any, skills_dir: Path) -> None:
    guess = ToolCall(id="m1", name="activate_skill", args={"name": "secret-ops"})
    script = [
        ScriptedTurn(content="ran it"),
        ScriptedTurn(tool_calls=[guess]),
        ScriptedTurn(content="tried"),
    ]
    # Declared: a hidden skill is a person's to run only where the manifest names it.
    declared = _manifest(skills=[{"name": "triage"}, {"name": "secret-ops"}])
    async with boot(
        script, env={"FELIX_SKILLS_DIR": str(skills_dir)}, manifests={"e2e-invoke": declared}
    ) as app:
        await _chat(app, "/secret-ops")
        assert _activations(app.spy.prompts[0])[0]["activated"] == "secret-ops"
        # Another conversation: the model naming it gets nothing.
        await _chat(app, "do the operator thing", thread="e2e-invoke-2")
        assert _activations(app.spy.prompts[2]) == [{"error": "unknown_skill", "name": "secret-ops"}]


async def test_a_skill_a_person_may_not_run_is_sent_as_written(boot: Any, skills_dir: Path) -> None:
    async with boot(
        [ScriptedTurn(content="ok")],
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-invoke": _manifest()},
    ) as app:
        await _chat(app, "/model-only now")
        assert _activations(app.spy.prompts[0]) == []
        assert "/model-only now" in [
            str(m.content) for m in app.spy.prompts[0] if getattr(m, "role", "") == "user"
        ]


async def test_a_declared_prompt_of_the_same_name_wins(boot: Any, skills_dir: Path) -> None:
    manifest = _manifest(prompts=[{"name": "triage", "body": "Summarise the invoice $1."}])
    async with boot(
        [ScriptedTurn(content="ok")],
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-invoke": manifest},
    ) as app:
        await _chat(app, "/triage ACME")
        first = app.spy.prompts[0]
    assert _activations(first) == []
    assert any(
        "Summarise the invoice ACME." in str(m.content) for m in first if getattr(m, "role", "") == "user"
    )


async def test_a_preloaded_skill_is_activated_once_per_conversation(boot: Any, skills_dir: Path) -> None:
    manifest = _manifest(skills=[{"name": "triage", "preload": True}])
    script = [ScriptedTurn(content="one"), ScriptedTurn(content="two"), ScriptedTurn(content="three")]
    async with boot(
        script, env={"FELIX_SKILLS_DIR": str(skills_dir)}, manifests={"e2e-invoke": manifest}
    ) as app:
        await _chat(app, "hello")
        await _chat(app, "and again")
        await _chat(app, "a new conversation", thread="e2e-invoke-other")
        first, second, other = app.spy.prompts

    assert [a["activated"] for a in _activations(first)] == ["triage"]
    assert _activations(second) == _activations(first), "the first turn's activation, not a second one"
    assert [a["activated"] for a in _activations(other)] == ["triage"]


async def test_the_model_cannot_reach_a_hidden_skill_in_the_run_a_person_started(
    boot: Any, skills_dir: Path
) -> None:
    """The person's round is over before the model's first step: in the same run, the model naming
    the hidden skill is told it does not exist."""
    guess = ToolCall(id="m1", name="activate_skill", args={"name": "secret-ops"})
    script = [ScriptedTurn(tool_calls=[guess]), ScriptedTurn(content="done")]
    async with boot(
        script, env={"FELIX_SKILLS_DIR": str(skills_dir)}, manifests={"e2e-invoke": _manifest()}
    ) as app:
        await _chat(app, "/triage ACME 900")
        assert _activations(app.spy.prompts[1])[-1] == {"error": "unknown_skill", "name": "secret-ops"}


async def test_running_a_skill_again_with_other_arguments_sends_them(boot: Any, skills_dir: Path) -> None:
    script = [ScriptedTurn(content="a"), ScriptedTurn(content="b"), ScriptedTurn(content="c")]
    async with boot(
        script, env={"FELIX_SKILLS_DIR": str(skills_dir)}, manifests={"e2e-invoke": _manifest()}
    ) as app:
        await _chat(app, "/triage A 1")
        await _chat(app, "/triage B 2")
        await _chat(app, "/triage B 2")
        _, second, third = app.spy.prompts
    assert _activations(second)[-1]["instructions"] == "Triage the invoice from B for 2 (B 2)."
    assert _activations(third)[-1]["already_active"] == "triage"


async def test_the_stream_shows_the_person_s_activation_as_a_tool_round(boot: Any, skills_dir: Path) -> None:
    from tests.support.sse import sse_payloads

    async with boot(
        [ScriptedTurn(content="triaged")],
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-invoke": _manifest()},
    ) as app:
        resp = await app.client.post(
            "/chat/stream",
            json={
                "manifest": "e2e-invoke",
                "thread_id": "e2e-stream",
                "messages": [{"role": "user", "content": "/triage A 1"}],
            },
        )
    names = [f.get("event") for f in sse_payloads(resp.text)]
    for frame in ("tool_start", "tool_execution_update", "tool_end"):
        assert frame in names
    assert names.index("tool_end") < names.index("text_delta"), "activated before the model speaks"


async def test_an_approval_gate_on_activation_is_asked_for_on_the_stream(boot: Any, skills_dir: Path) -> None:
    """Gated like any tool: the stream says so while the round waits, and a gate left to expire
    leaves the skill unactivated -- the model then answers without it."""
    from tests.support.sse import sse_payloads

    gated = _manifest(
        approvals=[
            {"id": "skill-gate", "tools": ["activate_skill"], "ttl_seconds": 1, "allow_unattended": True}
        ]
    )
    async with boot(
        [ScriptedTurn(content="without it")],
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-invoke": gated},
    ) as app:
        resp = await app.client.post(
            "/chat/stream",
            json={
                "manifest": "e2e-invoke",
                "thread_id": "e2e-gated",
                "messages": [{"role": "user", "content": "/triage A 1"}],
            },
        )
        first = app.spy.prompts[0]
    names = [f.get("event") for f in sse_payloads(resp.text)]
    assert "approval_required" in names and names.index("approval_required") < names.index("tool_end")
    assert all("activated" not in a for a in _activations(first))


async def test_a_pattern_that_cannot_preload_says_so(
    boot: Any, skills_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A router runs no tools of its own; its `preload` would be a field that does nothing."""
    import logging

    router = _manifest(pattern="router", sub_agents=["quick"], skills=[{"name": "triage", "preload": True}])
    with caplog.at_level(logging.WARNING, logger="felix.manifests.builder"):
        async with boot(
            [ScriptedTurn(content="quick"), ScriptedTurn(content="ok")],
            env={"FELIX_SKILLS_DIR": str(skills_dir)},
            manifests={"e2e-invoke": router},
        ) as app:
            await _chat(app, "hello")
    assert any(
        "preloads skills, but pattern 'router' does not run them" in r.getMessage() for r in caplog.records
    )


async def test_the_skills_route_reports_who_may_run_each(boot: Any, skills_dir: Path) -> None:
    async with boot(
        [], env={"FELIX_SKILLS_DIR": str(skills_dir)}, manifests={"e2e-invoke": _manifest()}
    ) as app:
        listing = (await app.client.get("/skills/e2e-invoke")).json()
    by = {i["name"]: i for i in listing["items"]}
    assert (by["secret-ops"]["model_invocable"], by["secret-ops"]["user_invocable"]) == (False, True)
    assert (by["model-only"]["model_invocable"], by["model-only"]["user_invocable"]) == (True, False)
    assert by["triage"]["argument_hint"] == "<vendor> <amount>"


async def test_a_model_cannot_run_a_hidden_skill_by_delegating_its_name(boot: Any, skills_dir: Path) -> None:
    """A `task` prompt is the parent model's text, not a person's turn: written `/secret-ops`, it
    must not run the skill hidden from every model in the child."""
    lead = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-lead"},
            "spec": {
                "pattern": "react",
                "delegation": {"agents": [{"name": "e2e-invoke", "description": "Does ops."}]},
                "auth": {"inbound": {"allow_anonymous": True}},
            },
        }
    )
    task = ToolCall(id="t1", name="task", args={"agent": "e2e-invoke", "prompt": "/secret-ops now"})
    script = [
        ScriptedTurn(tool_calls=[task]),
        ScriptedTurn(content="child done"),
        ScriptedTurn(content="lead done"),
    ]
    async with boot(
        script,
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-lead": lead, "e2e-invoke": _manifest()},
    ) as app:
        response = await app.client.post(
            "/chat",
            json={
                "manifest": "e2e-lead",
                "thread_id": "e2e-lead-1",
                "messages": [{"role": "user", "content": "go"}],
            },
        )
        assert response.status_code == 200, response.text
        child = app.spy.prompts[1]
    assert "/secret-ops now" in [str(m.content) for m in child if getattr(m, "role", "") == "user"]
    assert _activations(child) == [], "no activation ran for the model's text"


async def test_an_undeclared_hidden_skill_is_no_one_s_to_run(boot: Any, skills_dir: Path) -> None:
    async with boot(
        [ScriptedTurn(content="ok")],
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-invoke": _manifest()},
    ) as app:
        await _chat(app, "/secret-ops")
        assert _activations(app.spy.prompts[0]) == []


async def test_a_plan_execute_agent_runs_the_person_s_skill_in_every_step(
    boot: Any, skills_dir: Path
) -> None:
    """Each subtask is a fresh conversation the harness wrote; the person's `/name` is run in each."""
    script = [
        ScriptedTurn(content="1. look up the vendor\n2. file it"),
        ScriptedTurn(content="looked up"),
        ScriptedTurn(content="filed"),
        ScriptedTurn(content="done"),
    ]
    async with boot(
        script,
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-invoke": _manifest(pattern="plan_execute")},
    ) as app:
        await _chat(app, "/triage ACME 900")
        steps = app.spy.prompts[1:3]
    assert len(steps) == 2
    for i, step in enumerate(steps, start=1):
        assert any(f"Subtask {i}/2" in str(m.content) for m in step), "the prompt is the step"
        [activated] = _activations(step)
        assert activated["activated"] == "triage" and activated["arguments"] == "ACME 900"
        assert activated["instructions"] == "Triage the invoice from ACME for 900 (ACME 900)."


async def test_a_model_s_task_to_a_plan_execute_agent_runs_no_skill(boot: Any, skills_dir: Path) -> None:
    """A `task` prompt is the parent model's text: a `plan_execute` child must not carry its
    `/secret-ops` into the steps as if a person had typed it."""
    lead = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-lead"},
            "spec": {
                "pattern": "react",
                "delegation": {"agents": [{"name": "e2e-invoke", "description": "Does ops."}]},
                "auth": {"inbound": {"allow_anonymous": True}},
            },
        }
    )
    task = ToolCall(id="t1", name="task", args={"agent": "e2e-invoke", "prompt": "/secret-ops now"})
    script = [
        ScriptedTurn(tool_calls=[task]),
        ScriptedTurn(content="1. do it"),
        ScriptedTurn(content="step done"),
        ScriptedTurn(content="child done"),
        ScriptedTurn(content="lead done"),
    ]
    declared = _manifest(pattern="plan_execute", skills=[{"name": "triage"}, {"name": "secret-ops"}])
    async with boot(
        script,
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-lead": lead, "e2e-invoke": declared},
    ) as app:
        response = await app.client.post(
            "/chat",
            json={
                "manifest": "e2e-lead",
                "thread_id": "e2e-lead-1",
                "messages": [{"role": "user", "content": "go"}],
            },
        )
        assert response.status_code == 200, response.text
        step = app.spy.prompts[2]
    assert any("Subtask 1/1" in str(m.content) for m in step), "the prompt is the child's step"
    assert _activations(step) == []


def _specialist(name: str, skills: list[str]) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": name},
            "spec": {
                "pattern": "react",
                "skills": [{"name": s} for s in skills],
                "auth": {"inbound": {"allow_anonymous": True}},
            },
        }
    )


async def test_a_parallel_agent_runs_the_person_s_skill_in_each_specialist(
    boot: Any, skills_dir: Path
) -> None:
    """A specialist is another manifest, handed the person's messages as a step: the `/name` rides
    with them and runs in every specialist, not only the first, under each one's own catalog."""
    lead = _manifest(pattern="parallel", sub_agents=["e2e-spec-a", "e2e-spec-b"])
    async with boot(
        [ScriptedTurn(content="a says"), ScriptedTurn(content="b says"), ScriptedTurn(content="combined")],
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={
            "e2e-invoke": lead,
            "e2e-spec-a": _specialist("e2e-spec-a", ["triage"]),
            "e2e-spec-b": _specialist("e2e-spec-b", ["triage"]),
        },
    ) as app:
        await _chat(app, "/triage ACME 900")
        # The specialists run concurrently, so in no fixed order: count, not index.
        activations = [a for p in app.spy.prompts for a in _activations(p)]
    assert [(a["activated"], a["arguments"]) for a in activations] == [("triage", "ACME 900")] * 2


async def test_a_skill_the_parent_declares_is_not_the_child_s_to_run(boot: Any, skills_dir: Path) -> None:
    """A hidden skill is a person's only where the manifest running it declares it: the lead's
    `spec.skills` does not reach a specialist that names only `triage`."""
    lead = _manifest(
        pattern="parallel",
        sub_agents=["e2e-spec-a"],
        skills=[{"name": "triage"}, {"name": "secret-ops"}],
    )
    async with boot(
        [ScriptedTurn(content="a says"), ScriptedTurn(content="combined")],
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-invoke": lead, "e2e-spec-a": _specialist("e2e-spec-a", ["triage"])},
    ) as app:
        await _chat(app, "/secret-ops")
        specialist = app.spy.prompts[0]
    assert any(str(m.content) == "/secret-ops" for m in specialist), "the prompt is the specialist's"
    assert _activations(specialist) == []


async def test_a_reflect_agent_s_second_draft_keeps_the_person_s_skill(boot: Any, skills_dir: Path) -> None:
    """Reflect re-runs the same thread, so a later draft reads the first draft's activation back from
    the log: the instructions are there once, with no second `already_active` round."""
    script = [
        ScriptedTurn(content="draft one"),
        ScriptedTurn(content="0.1"),
        ScriptedTurn(content="draft two"),
    ]
    async with boot(
        script,
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-invoke": _manifest(pattern="reflect", reflect={"max_iterations": 2})},
    ) as app:
        await _chat(app, "/triage ACME 900")
        second = app.spy.prompts[2]
    assert any("Previous answer scored" in str(m.content) for m in second), "the prompt is the second draft"
    [activated] = _activations(second)
    assert activated["activated"] == "triage" and activated["arguments"] == "ACME 900"


async def test_a_threadless_reflect_specialist_keeps_the_person_s_skill_in_every_draft(
    boot: Any, skills_dir: Path
) -> None:
    """A reflect agent run as a `parallel` specialist has no thread to read its first activation
    back from: its later draft runs the person's carried `/name` again."""
    child = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-reflector"},
            "spec": {
                "pattern": "reflect",
                "reflect": {"max_iterations": 2},
                "skills": [{"name": "triage"}],
                "auth": {"inbound": {"allow_anonymous": True}},
            },
        }
    )
    lead = _manifest(pattern="parallel", sub_agents=["e2e-reflector"])
    script = [
        ScriptedTurn(content="draft one"),
        ScriptedTurn(content="0.1"),
        ScriptedTurn(content="draft two"),
        ScriptedTurn(content="combined"),
    ]
    async with boot(
        script,
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-invoke": lead, "e2e-reflector": child},
    ) as app:
        await _chat(app, "/triage ACME 900")
        second = app.spy.prompts[2]
    assert any("Previous answer scored" in str(m.content) for m in second), "the prompt is the second draft"
    [activated] = _activations(second)
    assert activated["activated"] == "triage" and activated["arguments"] == "ACME 900"


async def test_a_threadless_reflect_on_v1_keeps_the_person_s_skill_in_every_draft(
    boot: Any, skills_dir: Path
) -> None:
    """`/v1` with no `user` runs with no thread: a later draft has no log to read the activation
    back from, so the person's `/name` is run again with it."""
    script = [
        ScriptedTurn(content="draft one"),
        ScriptedTurn(content="0.1"),
        ScriptedTurn(content="draft two"),
    ]
    async with boot(
        script,
        env={"FELIX_SKILLS_DIR": str(skills_dir)},
        manifests={"e2e-invoke": _manifest(pattern="reflect", reflect={"max_iterations": 2})},
    ) as app:
        response = await app.client.post(
            "/v1/chat/completions",
            json={"model": "e2e-invoke", "messages": [{"role": "user", "content": "/triage ACME 900"}]},
        )
        assert response.status_code == 200, response.text
        second = app.spy.prompts[2]
    assert any("Previous answer scored" in str(m.content) for m in second), "the prompt is the second draft"
    [activated] = _activations(second)
    assert activated["activated"] == "triage" and activated["arguments"] == "ACME 900"


async def test_a_manual_only_skill_on_v1_needs_skills_invoke(boot: Any, skills_dir: Path) -> None:
    """`/v1`'s caller is as often an agent: a skill kept from the model runs there only for a caller
    holding `skills:invoke`. A skill the model may use still runs for anyone, and `/chat` is
    unchanged -- each half is what tells the gate from an outage of `/name` on `/v1`."""
    from tests.support.mgmt_keys import READER, WRITER, bearer, scoped_keys

    declared = _manifest(skills=[{"name": "triage"}, {"name": "secret-ops"}])

    async def v1(app: Any, token: str, text: str, *, stream: bool = False) -> None:
        response = await app.client.post(
            "/v1/chat/completions",
            json={"model": "e2e-invoke", "stream": stream, "messages": [{"role": "user", "content": text}]},
            headers=bearer(token),
        )
        assert response.status_code == 200, response.text

    async with boot(
        [ScriptedTurn(content=str(n)) for n in range(5)],
        env={"FELIX_SKILLS_DIR": str(skills_dir), **scoped_keys(reader=[], writer=["skills:invoke"])},
        manifests={"e2e-invoke": declared},
    ) as app:
        await v1(app, READER, "/secret-ops now")
        await v1(app, WRITER, "/secret-ops now")
        await v1(app, READER, "/triage ACME 900")
        await v1(app, READER, "/secret-ops now", stream=True)
        response = await app.client.post(
            "/chat",
            json={"manifest": "e2e-invoke", "messages": [{"role": "user", "content": "/secret-ops now"}]},
            headers=bearer(READER),
        )
        assert response.status_code == 200, response.text
        prompts = app.spy.prompts

    assert len(prompts) == 5
    assert _activations(prompts[0]) == [], "a caller without skills:invoke runs no manual-only skill on /v1"
    turns = [str(m.content) for m in prompts[0] if getattr(m, "role", "") == "user"]
    assert "/secret-ops now" in turns, "the refused turn is sent as written"
    assert [a.get("activated") for a in _activations(prompts[1])] == ["secret-ops"]
    assert [a.get("activated") for a in _activations(prompts[2])] == ["triage"]
    assert _activations(prompts[3]) == [], "the streamed /v1 path is held to it too"
    assert [a.get("activated") for a in _activations(prompts[4])] == ["secret-ops"], "/chat is a person's"
