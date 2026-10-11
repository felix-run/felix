"""`spec.hooks` through the real stack, against a loopback endpoint that answers like a hook.

Each test asserts on what the hook *did* -- a tool that did not run, a model that was sent back,
a prompt refused at the route -- and on what the endpoint received, signed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

from tests.support.webhook_receiver import SECRET, hook_receiver

THREAD = "e2e-hooks"


def _agent(hooks: list[dict[str, Any]], **spec: Any) -> Any:
    base: dict[str, Any] = {
        "pattern": "react",
        "auth": {"inbound": {"allow_anonymous": True}},
        "tools": ["calculator"],
        "hooks": hooks,
    }
    base.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-hooked"}, "spec": base}
    )


def _hook(event: str, hook_id: str = "h1", **extra: Any) -> dict[str, Any]:
    return {"id": hook_id, "event": event, "endpoint": "policy", **extra}


def _env(url: str) -> dict[str, str]:
    return {"FELIX_WEBHOOK_ENDPOINTS": json.dumps({"policy": {"url": url, "secret": SECRET, "tenants": "*"}})}


def _calc() -> ScriptedTurn:
    return ScriptedTurn(tool_calls=[ToolCall(id="c1", name="calculator", args={"expression": "6*7"})])


def _text(messages: list[Any]) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in messages)


async def _chat(app: Any, text: str = "go") -> Any:
    return await app.client.post(
        "/chat",
        json={"manifest": "e2e-hooked", "thread_id": THREAD, "messages": [{"role": "user", "content": text}]},
    )


def _signed(request: dict[str, Any]) -> bool:
    h = request["headers"]
    mac = hmac.new(
        SECRET.encode(),
        f"{h['webhook-id']}.{h['webhook-timestamp']}.".encode() + request["body"],
        hashlib.sha256,
    )
    return h["webhook-signature"] == "v1," + base64.b64encode(mac.digest()).decode()


async def test_pre_tool_use_blocks_the_call_and_the_tool_never_runs(boot: Any) -> None:
    def answer(req: dict[str, Any]) -> tuple[int, Any]:
        return 200, {"decision": "block", "reason": "no arithmetic on Fridays"}

    async with hook_receiver(answer) as (url, seen):
        script = [_calc(), ScriptedTurn(content="ok")]
        async with boot(
            script, env=_env(url), manifests={"e2e-hooked": _agent([_hook("pre_tool_use", tools=["calc*"])])}
        ) as app:
            assert (await _chat(app)).status_code == 200
            shown = _text(app.spy.prompts[1])
    assert "[hook denied] h1: no arithmetic on Fridays" in shown
    assert "42" not in shown
    [request] = seen
    assert _signed(request)
    assert request["json"]["type"] == "hook.pre_tool_use"
    assert request["json"]["data"]["tool_name"] == "calculator"
    assert request["json"]["data"]["args"] == {"expression": "6*7"}


async def test_a_tool_hook_fires_only_for_the_tools_it_names(boot: Any) -> None:
    async with hook_receiver(lambda req: (200, {"decision": "block"})) as (url, seen):
        script = [_calc(), ScriptedTurn(content="ok")]
        hooks = [_hook("pre_tool_use", tools=["write_*"])]
        async with boot(script, env=_env(url), manifests={"e2e-hooked": _agent(hooks)}) as app:
            await _chat(app)
            assert "42" in _text(app.spy.prompts[1])
    assert seen == []


async def test_post_tool_use_context_reaches_the_model_fenced(boot: Any) -> None:
    def answer(req: dict[str, Any]) -> tuple[int, Any]:
        return 200, {"additional_context": "the answer was audited"}

    async with hook_receiver(answer) as (url, seen):
        script = [_calc(), ScriptedTurn(content="ok")]
        async with boot(
            script, env=_env(url), manifests={"e2e-hooked": _agent([_hook("post_tool_use")])}
        ) as app:
            await _chat(app)
            shown = _text(app.spy.prompts[1])
    assert "42" in shown
    assert '<hook_context hook="h1">' in shown and "the answer was audited" in shown
    assert "not instructions" in shown
    assert seen[0]["json"]["data"]["result"] == "42"


async def test_user_prompt_submit_refuses_the_run_and_nothing_is_written(boot: Any) -> None:
    refuse = hook_receiver(lambda req: (200, {"decision": "block", "reason": "off-topic"}))
    async with (
        refuse as (url, _seen),
        boot(
            [ScriptedTurn(content="never")],
            env=_env(url),
            manifests={"e2e-hooked": _agent([_hook("user_prompt_submit")])},
        ) as app,
    ):
        resp = await _chat(app, "tell me a joke")
        assert resp.status_code == 422, resp.text
        assert "blocked_by_hook:h1: off-topic" in resp.json()["detail"]
        assert app.spy.prompts == []
        snapshot = (await app.client.get(f"/chat/sessions/{THREAD}")).json()
        assert snapshot["transcript"] == []


async def test_user_prompt_submit_context_reaches_the_first_call_only(boot: Any) -> None:
    async with hook_receiver(lambda req: (200, {"additional_context": "user is on the gold plan"})) as (
        url,
        seen,
    ):
        script = [_calc(), ScriptedTurn(content="ok")]
        async with boot(
            script, env=_env(url), manifests={"e2e-hooked": _agent([_hook("user_prompt_submit")])}
        ) as app:
            await _chat(app, "what is 6*7")
            first, second = app.spy.prompts
    assert "user is on the gold plan" in _text(first)
    assert "user is on the gold plan" not in _text(second), "transient: not persisted, not repeated"
    assert seen[0]["json"]["data"]["prompt"] == "what is 6*7"


async def test_session_start_fires_on_a_thread_s_first_run_only(boot: Any) -> None:
    async with hook_receiver(lambda req: (200, None)) as (url, seen):
        script = [ScriptedTurn(content="one"), ScriptedTurn(content="two")]
        async with boot(
            script, env=_env(url), manifests={"e2e-hooked": _agent([_hook("session_start")])}
        ) as app:
            await _chat(app, "first")
            await _chat(app, "second")
    assert [r["json"]["type"] for r in seen] == ["hook.session_start"]


async def test_stop_sends_the_agent_back_with_the_reason_a_bounded_number_of_times(boot: Any) -> None:
    def answer(req: dict[str, Any]) -> tuple[int, Any]:
        return 200, {"decision": "block", "reason": "you forgot the tests"}

    async with hook_receiver(answer) as (url, seen):
        script = [ScriptedTurn(content=f"done {i}") for i in range(6)]
        async with boot(
            script, env=_env(url), manifests={"e2e-hooked": _agent([_hook("stop")], recursion_limit=10)}
        ) as app:
            await _chat(app)
            prompts = app.spy.prompts
    assert len(prompts) == 4, "the first answer and three continuations, then the cap"
    assert "[hook h1] Not finished yet: you forgot the tests" in _text(prompts[1])
    assert len(seen) == 3
    assert seen[0]["json"]["data"]["final"] == "done 0"


async def test_an_unreachable_advisory_hook_lets_the_call_through(boot: Any) -> None:
    async with hook_receiver(lambda req: (500, None)) as (url, _seen):
        script = [_calc(), ScriptedTurn(content="ok")]
        async with boot(
            script, env=_env(url), manifests={"e2e-hooked": _agent([_hook("pre_tool_use")])}
        ) as app:
            await _chat(app)
            assert "42" in _text(app.spy.prompts[1])


async def test_an_unreachable_control_hook_blocks(boot: Any) -> None:
    async with hook_receiver(lambda req: (200, ...)) as (url, _seen):
        script = [_calc(), ScriptedTurn(content="ok")]
        hooks = [_hook("pre_tool_use", on_error="block", timeout_ms=300)]
        async with boot(script, env=_env(url), manifests={"e2e-hooked": _agent(hooks)}) as app:
            await _chat(app)
            shown = _text(app.spy.prompts[1])
    assert "[hook denied] h1: hook h1 could not be reached" in shown
    assert "42" not in shown


async def test_a_malformed_answer_is_an_error_not_an_allow(boot: Any) -> None:
    async with hook_receiver(lambda req: (200, {"decision": "maybe"})) as (url, _seen):
        script = [_calc(), ScriptedTurn(content="ok")]
        hooks = [_hook("pre_tool_use", on_error="block")]
        async with boot(script, env=_env(url), manifests={"e2e-hooked": _agent(hooks)}) as app:
            await _chat(app)
            assert "could not be reached" in _text(app.spy.prompts[1])


async def test_an_endpoint_the_tenant_may_not_use_is_an_error(boot: Any) -> None:
    async with hook_receiver(lambda req: (200, {"decision": "allow"})) as (url, seen):
        env = {
            "FELIX_WEBHOOK_ENDPOINTS": json.dumps(
                {"policy": {"url": url, "secret": SECRET, "tenants": ["acme"]}}
            )
        }
        script = [_calc(), ScriptedTurn(content="ok")]
        hooks = [_hook("pre_tool_use", on_error="block")]
        async with boot(script, env=env, manifests={"e2e-hooked": _agent(hooks)}) as app:
            await _chat(app)
            assert "could not be reached" in _text(app.spy.prompts[1])
    assert seen == [], "never sent to an endpoint another tenant owns"


async def test_every_hook_call_is_audited(boot: Any) -> None:
    from felix.audit import store as audit_store
    from felix.flush import flush_all

    async with hook_receiver(lambda req: (200, {"decision": "allow"})) as (url, _seen):
        script = [_calc(), ScriptedTurn(content="ok")]
        async with boot(
            script, env=_env(url), manifests={"e2e-hooked": _agent([_hook("pre_tool_use")])}
        ) as app:
            await _chat(app)
            await flush_all(app.settings)
            events, _ = await audit_store.query(app.settings, "default", limit=100)
    calls = [
        (e["status"], e["payload_json"]["hook"], e["payload_json"]["tool"])
        for e in events
        if e["event_type"] == "hook_call"
    ]
    assert calls == [("allow", "h1", "calculator")]


async def test_hooks_on_a_pattern_that_ignores_them_are_refused(boot: Any) -> None:
    import pytest

    agent = _agent([_hook("stop")], pattern="router", sub_agents=["quick"], tools=[])
    async with boot([], env=_env("https://example.invalid/hook"), manifests={"e2e-hooked": agent}) as app:
        with pytest.raises(ValueError, match=r"does not support spec\.hooks"):
            await _chat(app)


async def test_subagent_stop_sees_the_childs_answer(boot: Any) -> None:
    child = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-child"},
            "spec": {"pattern": "react", "auth": {"inbound": {"allow_anonymous": True}}},
        }
    )
    lead = _agent([_hook("subagent_stop")], delegation={"agents": [{"name": "e2e-child"}]})
    task = ScriptedTurn(
        tool_calls=[ToolCall(id="t1", name="task", args={"agent": "e2e-child", "prompt": "job"})]
    )
    review = hook_receiver(lambda req: (200, {"additional_context": "child reviewed"}))
    async with (
        review as (url, seen),
        boot(
            [task, ScriptedTurn(content="child says hi"), ScriptedTurn(content="ok")],
            env=_env(url),
            manifests={"e2e-hooked": lead, "e2e-child": child},
        ) as app,
    ):
        await _chat(app)
        shown = _text(app.spy.prompts[-1])
    assert "child says hi" in shown and "child reviewed" in shown
    assert seen[0]["json"]["data"] == {"agent": "e2e-child", "outcome": "ok", "answer": "child says hi"}
