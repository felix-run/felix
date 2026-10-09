"""`after_tool` can rewrite what the model is told about a tool that raised.

The error path used to read only `terminate` from the hook and hand it `result=None`, so a
redacting hook saw nothing to redact and its `content` was dropped: the exception's own text —
where a secret or an internal path most often surfaces — reached the model untouched, while the
same hook worked on every successful call.
"""

from __future__ import annotations

import pytest
from felix.hooks import get_agent_hooks, reset_agent_hooks
from felix.patterns.tool_runner import ToolRunner
from felix.patterns.types import ToolCall
from felix.tools.errors import ToolErrorCode, tool_error_output
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, is_failure_content

SECRET = "hunter2"


@pytest.fixture(autouse=True)
def _clean_hooks():
    reset_agent_hooks()
    yield
    reset_agent_hooks()


class _Leaks:
    transport = "builtin"

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        raise RuntimeError(f"connect failed: password={SECRET}")


def redact(tool_call, result, is_error, ctx):
    return {"content": str(result).replace(SECRET, "***")}


async def _run(*, fatal: bool = False):
    tool = Tool(name="t", description="d", args_schema=None, executor=_Leaks(), fatal=fatal)
    runner = ToolRunner(tool_map={"t": tool}, manifest_id="m")
    return await runner.run_batch([ToolCall(id="1", name="t", args={})], thread_id="th", tenant_id="t")


@pytest.mark.parametrize(("fatal", "prefix"), [(False, "[error/"), (True, "[fatal/")])
@pytest.mark.asyncio
async def test_a_redacting_hook_redacts_a_failed_calls_message(fatal: bool, prefix: str) -> None:
    seen: list[tuple[object, bool]] = []
    get_agent_hooks().register_after_tool(lambda call, result, is_error, ctx: seen.append((result, is_error)))
    get_agent_hooks().register_after_tool(redact)

    messages, had_fatal, _, _ = await _run(fatal=fatal)

    assert seen == [(f"{prefix}internal] connect failed: password={SECRET}", True)]
    # Exact, so an already-spelled failure is not prefixed a second time.
    assert messages[0].content == f"{prefix}internal] connect failed: password=***"
    assert had_fatal is fatal


@pytest.mark.parametrize(
    ("fatal", "expected"), [(False, "[error/internal] unavailable"), (True, "[fatal/internal] unavailable")]
)
@pytest.mark.asyncio
async def test_a_rewrite_without_the_failure_prefix_gets_the_original_one_back(
    fatal: bool, expected: str
) -> None:
    """Eval counts failures by spelling; a hook's plain text must not turn one into a success,
    and a fatal call must not come back spelled as an ordinary error."""
    get_agent_hooks().register_after_tool(lambda call, result, is_error, ctx: {"content": "unavailable"})

    messages, _, _, _ = await _run(fatal=fatal)

    assert messages[0].content == expected
    assert is_failure_content(messages[0].content)


@pytest.mark.asyncio
async def test_an_unprintable_rewrite_keeps_the_error() -> None:
    class _Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("boom in str()")

    get_agent_hooks().register_after_tool(lambda call, result, is_error, ctx: {"content": _Unprintable()})

    messages, _, _, _ = await _run()

    assert messages[0].content.startswith("[error/") and SECRET in messages[0].content


class _ReturnsError:
    """Fails the other way: returns an error output instead of raising."""

    transport = "builtin"

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        return tool_error_output(ToolErrorCode.PROVIDER_ERROR, f"connect failed: password={SECRET}")


async def _run_returned_error():
    tool = Tool(name="t", description="d", args_schema=None, executor=_ReturnsError())
    runner = ToolRunner(tool_map={"t": tool}, manifest_id="m")
    return await runner.run_batch([ToolCall(id="1", name="t", args={})], thread_id="th", tenant_id="t")


@pytest.mark.asyncio
async def test_a_returned_error_is_handed_to_the_hook_as_text_and_redacted() -> None:
    seen: list[tuple[object, bool]] = []
    get_agent_hooks().register_after_tool(lambda call, result, is_error, ctx: seen.append((result, is_error)))
    get_agent_hooks().register_after_tool(redact)

    messages, _, _, _ = await _run_returned_error()

    assert seen == [(f"[tool error/provider_error] connect failed: password={SECRET}", True)]
    assert messages[0].content == "[tool error/provider_error] connect failed: password=***"


@pytest.mark.asyncio
async def test_a_returned_errors_plain_rewrite_keeps_its_failure_spelling() -> None:
    get_agent_hooks().register_after_tool(lambda call, result, is_error, ctx: {"content": "unavailable"})

    messages, _, _, _ = await _run_returned_error()

    assert messages[0].content == "[tool error/provider_error] unavailable"
