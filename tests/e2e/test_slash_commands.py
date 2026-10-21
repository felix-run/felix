"""Slash commands through the stack: the model sees the expansion, not the command."""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

from tests.support.screening import EMAIL

CHAT_ROUTES = ("/chat", "/chat/stream")


def _agent(**spec: Any) -> Any:
    base = {
        "pattern": "react",
        "auth": {"inbound": {"allow_anonymous": True}},
        "prompts": [
            {
                "name": "review",
                "body": "Review $1 for ${2:-correctness}.",
                "description": "Review a file",
                "argument_hint": "<file> [focus]",
            },
            {"name": "blank", "body": ""},
            {"name": "contact", "body": f"Write to {EMAIL} about $1."},
        ],
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-cmd"}, "spec": base | spec}
    )


def _user_turns(prompt: list[Any]) -> list[str]:
    return [m.content for m in prompt if m.role == "user" and not m.transient]


async def _chat(app: Any, route: str, text: str | None, **extra: Any) -> Any:
    messages = [{"role": "user", "content": text}] if text is not None else []
    return await app.client.post(route, json={"manifest": "e2e-cmd", "messages": messages, **extra})


async def _v1(app: Any, messages: list[dict[str, str]]) -> Any:
    return await app.client.post("/v1/chat/completions", json={"model": "e2e-cmd", "messages": messages})


@pytest.mark.parametrize("route", CHAT_ROUTES)
async def test_chat_expands_a_command_before_the_model_sees_it(boot: Any, route: str) -> None:
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-cmd": _agent()}) as app:
        resp = await _chat(app, route, "/review a.py perf")
        assert resp.status_code == 200, resp.text
        assert _user_turns(app.spy.prompts[0]) == ["Review a.py for perf."]


@pytest.mark.parametrize("route", CHAT_ROUTES)
async def test_chat_refuses_a_command_whose_prompt_is_empty(boot: Any, route: str) -> None:
    """A 404 before any stream opens, not an error frame inside one."""
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-cmd": _agent()}) as app:
        resp = await _chat(app, route, "/blank")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "empty_prompt:blank"
        assert app.spy.prompts == []


async def test_chat_sends_an_undeclared_slash_as_written(boot: Any) -> None:
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-cmd": _agent()}) as app:
        resp = await _chat(app, "/chat", "/etc/hosts?")
        assert resp.status_code == 200, resp.text
        assert _user_turns(app.spy.prompts[0]) == ["/etc/hosts?"]


async def test_the_template_field_appends_its_expansion(boot: Any) -> None:
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-cmd": _agent()}) as app:
        resp = await _chat(app, "/chat", None, template="review", template_args=["a.py"])
        assert resp.status_code == 200, resp.text
        assert _user_turns(app.spy.prompts[0]) == ["Review a.py for correctness."]


async def test_a_command_and_a_template_both_expand_in_order(boot: Any) -> None:
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-cmd": _agent()}) as app:
        resp = await _chat(app, "/chat", "/review a.py", template="review", template_args=["b.py"])
        assert resp.status_code == 200, resp.text
        assert _user_turns(app.spy.prompts[0]) == [
            "Review a.py for correctness.",
            "Review b.py for correctness.",
        ]


async def test_an_unknown_template_is_refused_by_name(boot: Any) -> None:
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-cmd": _agent()}) as app:
        resp = await _chat(app, "/chat", None, template="nope")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "unknown_prompt:nope"
        assert app.spy.prompts == []


async def test_the_expansion_is_screened_and_stored_as_the_users_turn(boot: Any) -> None:
    """Expanded before inbound screening: the body's address is redacted like a typed one, and
    the thread keeps the screened expansion, not the command."""
    screened = _agent(guardrails={"providers": ["pii"], "targets": ["input"]})
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-cmd": screened}) as app:
        resp = await _chat(app, "/chat", "/contact billing", thread_id="cmd-log")
        assert resp.status_code == 200, resp.text
        [shown] = _user_turns(app.spy.prompts[0])
        history = (await app.client.get("/chat/history/cmd-log")).text
    assert shown.startswith("Write to ") and shown.endswith(" about billing.")
    assert EMAIL not in shown
    assert "/contact" not in history and "about billing." in history and EMAIL not in history


async def test_openai_compat_expands_every_command_in_the_history(boot: Any) -> None:
    history = [
        {"role": "user", "content": "/review a.py"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "/review b.py perf"},
        {"role": "user", "content": "/shrug"},
    ]
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-cmd": _agent()}) as app:
        resp = await _v1(app, history)
        assert resp.status_code == 200, resp.text
        assert _user_turns(app.spy.prompts[0]) == [
            "Review a.py for correctness.",
            "Review b.py for perf.",
            "/shrug",
        ]


async def test_openai_compat_refuses_a_command_whose_prompt_is_empty(boot: Any) -> None:
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-cmd": _agent()}) as app:
        resp = await _v1(app, [{"role": "user", "content": "/blank"}])
        assert resp.status_code == 404
        assert resp.json()["error"]["message"] == "empty_prompt:blank"
        assert app.spy.prompts == []


async def test_the_model_listing_carries_the_commands(boot: Any) -> None:
    async with boot(manifests={"e2e-cmd": _agent()}) as app:
        listing = (await app.client.get("/v1/models")).json()
    [entry] = [m for m in listing["data"] if m["id"] == "e2e-cmd"]
    assert entry["felix"]["commands"] == [
        {"name": "review", "description": "Review a file", "argumentHint": "<file> [focus]"},
        {"name": "blank", "description": None, "argumentHint": None},
        {"name": "contact", "description": None, "argumentHint": None},
    ]
