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
