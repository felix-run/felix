"""A broken agent-loop hook is said out loud, once, and the hook types say how a hook is called.

Hooks fail open, so a broken one is skipped on every call and the run carries on. While that
was logged at debug, a broken hook could not be told from a working one: the reference plugin's
`before_tool` took `**kwargs`, raised `TypeError` on every tool call, and blocked nothing for as
long as it existed. These pin the warning, its once-per-hook ceiling, the counter, and the
spelled-out signatures that let a type checker catch the `**kwargs` hook before it ships.
"""

from __future__ import annotations

import logging
import typing
from typing import Any

import pytest
from felix import hooks as hooks_mod
from felix.hooks import get_agent_hooks, run_after_model, run_before_tool
from felix.patterns.types import ChatMessage
from prometheus_client import REGISTRY

KINDS = (
    "before_turn",
    "filter_history",
    "before_compact",
    "before_tool",
    "after_tool",
    "compact_failed",
    "before_model",
    "after_model",
)


def _failures(kind: str) -> float:
    return REGISTRY.get_sample_value("felix_hook_failures_total", {"hook": kind}) or 0.0


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "felix.hooks" and r.levelno == logging.WARNING]


async def kwargs_only_hook(**kwargs: Any) -> dict[str, Any] | None:
    """The reference plugin's old shape: hooks are called positionally, so this always raises."""
    return {"block": True}


@pytest.mark.asyncio
async def test_a_hook_with_the_wrong_signature_warns_once_and_is_counted_every_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    get_agent_hooks().register_before_tool(kwargs_only_hook)
    before = _failures("before_tool")
    caplog.set_level(logging.DEBUG, logger="felix.hooks")

    for _ in range(3):
        assert await run_before_tool({"id": "1", "name": "t", "args": {}}) is None

    warned = _warnings(caplog)
    assert len(warned) == 1, f"expected one warning for three failures, got {len(warned)}"
    message = warned[0].getMessage()
    assert "before_tool" in message and "kwargs_only_hook" in message and "raised" in message
    assert warned[0].exc_info and warned[0].exc_info[0] is TypeError, "the warning should carry the traceback"
    assert _failures("before_tool") == before + 3


@pytest.mark.asyncio
async def test_each_broken_hook_gets_its_own_warning(caplog: pytest.LogCaptureFixture) -> None:
    def first(tool_call, ctx):
        raise RuntimeError("first")

    def second(tool_call, ctx):
        raise RuntimeError("second")

    hooks = get_agent_hooks()
    hooks.register_before_tool(first)
    hooks.register_before_tool(second)
    await run_before_tool({"id": "1", "name": "t", "args": {}})
    await run_before_tool({"id": "2", "name": "t", "args": {}})

    named = sorted("first" in r.getMessage() for r in _warnings(caplog))
    assert named == [False, True], "one warning per hook, not one for the whole kind"


@pytest.mark.asyncio
async def test_a_wrong_shaped_answer_warns_once_too(caplog: pytest.LogCaptureFixture) -> None:
    def impersonates_the_user(response, ctx):
        return {"message": ChatMessage(role="user", content="not an assistant")}

    get_agent_hooks().register_after_model(impersonates_the_user)
    before = _failures("after_model")
    reply = ChatMessage(role="assistant", content="ok")

    for _ in range(2):
        assert await run_after_model(reply, stop_reason="end_turn") is reply

    assert len(_warnings(caplog)) == 1
    assert _failures("after_model") == before + 2


@pytest.mark.asyncio
async def test_a_working_hook_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    get_agent_hooks().register_before_tool(lambda tool_call, ctx: None)
    before = _failures("before_tool")
    caplog.set_level(logging.DEBUG, logger="felix.hooks")

    await run_before_tool({"id": "1", "name": "t", "args": {}})

    assert [r for r in caplog.records if r.name == "felix.hooks"] == []
    assert _failures("before_tool") == before


@pytest.mark.parametrize("kind", KINDS)
def test_every_hook_type_spells_out_its_parameters(kind: str) -> None:
    """`Callable[..., X]` accepts a `**kwargs` hook; a parameter list makes a checker refuse it."""
    alias = getattr(hooks_mod, "".join(part.title() for part in kind.split("_")) + "Hook")
    params = typing.get_args(alias)[0]
    assert params is not Ellipsis and isinstance(params, list), f"{kind}'s hook type is Callable[..., ...]"
    assert len(params) == (4 if kind == "after_tool" else 2)


@pytest.mark.parametrize("kind", KINDS)
def test_every_registrar_takes_its_hook_type(kind: str) -> None:
    from felix.plugins import PluginRegistry

    alias = "".join(part.title() for part in kind.split("_")) + "Hook"
    for owner in (PluginRegistry, hooks_mod.AgentHookRegistry):
        annotation = getattr(owner, f"register_{kind}").__annotations__["hook"]
        assert annotation == alias, f"{owner.__name__}.register_{kind} takes {annotation}, not {alias}"
