"""Bookkeeping over a tool that ran cannot rewrite whether it ran.

`ToolRunner.dispatch` executed the tool and then did its metering, audit and after-tool hook
inside the *same* `try`. So a failure in any of that fell into the handler written for "the
tool call failed", which does two things it must not: it invokes `run_after_tool` a second
time — now with `result=None, is_error=True` — and it returns `[error/...]` to the model for a
call that succeeded. A model told a side-effecting tool failed may run it again.

Reachable, though not by the route the finding first described. `run_after_tool` isolates each
hook (`hooks.py`) and `emit_agent_audit` swallows its own failures (`audit/emit.py`), so
neither can raise. What can is the handling of a hook's *return*: an after-tool hook replacing
`content` with an object whose `__str__` raises produced two hook invocations, `[False, True]`,
and an `[error/internal]` message for a tool that had succeeded.

Verified against the pre-fix code, which is why this file exists rather than a note.
"""

from __future__ import annotations

import pytest
from felix.hooks import get_agent_hooks, reset_agent_hooks
from felix.patterns.tool_runner import ToolRunner
from felix.patterns.types import ToolCall
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput


@pytest.fixture(autouse=True)
def _clean_hooks():
    reset_agent_hooks()
    yield
    reset_agent_hooks()


class _Succeeds:
    transport = "builtin"

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        self.calls += 1
        return "the tool succeeded"


class _Unprintable:
    """A hook-supplied replacement content whose `str()` raises."""

    def __str__(self) -> str:
        raise RuntimeError("boom in str()")


def _runner(executor) -> ToolRunner:
    tool = Tool(name="t", description="d", args_schema=None, executor=executor)
    return ToolRunner(tool_map={"t": tool}, manifest_id="m")


@pytest.mark.asyncio
async def test_the_after_tool_hook_runs_once_when_post_call_handling_fails() -> None:
    seen: list[bool] = []

    def hook(tool_call, result, is_error, ctx):
        seen.append(is_error)
        return {"content": _Unprintable()}

    get_agent_hooks().register_after_tool(hook)

    await _runner(_Succeeds()).run_batch([ToolCall(id="1", name="t", args={})], thread_id="th", tenant_id="t")

    assert seen == [False], (
        f"one tool call invoked the after-tool hook {len(seen)} times with {seen}; the second "
        "invocation reports is_error=True for a call that succeeded"
    )


@pytest.mark.asyncio
async def test_a_successful_call_is_not_reported_to_the_model_as_an_error() -> None:
    """The consequence that reaches the model, and the reason this is not cosmetic."""

    def hook(tool_call, result, is_error, ctx):
        return {"content": _Unprintable()}

    get_agent_hooks().register_after_tool(hook)
    executor = _Succeeds()

    messages, had_fatal, _ = await _runner(executor).run_batch(
        [ToolCall(id="1", name="t", args={})], thread_id="th", tenant_id="t"
    )

    assert executor.calls == 1
    assert not had_fatal
    assert "[error/" not in messages[0].content, (
        f"a successful tool call was reported as an error: {messages[0].content!r}"
    )
    assert messages[0].content == "the tool succeeded"


@pytest.mark.asyncio
async def test_a_genuinely_failing_tool_still_reports_an_error_and_hooks_once() -> None:
    """The narrowed `try` must still catch the thing it was written for."""
    seen: list[bool] = []

    def hook(tool_call, result, is_error, ctx):
        seen.append(is_error)
        return None

    get_agent_hooks().register_after_tool(hook)

    class _Fails:
        transport = "builtin"

        async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            raise RuntimeError("the tool itself failed")

    messages, _, _ = await _runner(_Fails()).run_batch(
        [ToolCall(id="1", name="t", args={})], thread_id="th", tenant_id="t"
    )

    assert seen == [True], f"the error path invoked the hook {len(seen)} times: {seen}"
    assert "[error/" in messages[0].content


@pytest.mark.asyncio
async def test_a_hook_can_still_replace_content_and_terminate() -> None:
    """A guard that broke the hook's contract would be worse than the bug it fixed."""

    def hook(tool_call, result, is_error, ctx):
        return {"content": "replaced", "terminate": True}

    get_agent_hooks().register_after_tool(hook)

    messages, _, all_terminate = await _runner(_Succeeds()).run_batch(
        [ToolCall(id="1", name="t", args={})], thread_id="th", tenant_id="t"
    )

    assert messages[0].content == "replaced"
    assert all_terminate is True
