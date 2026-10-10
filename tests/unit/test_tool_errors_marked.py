"""A built-in tool that fails says so with a marked error, not a plain `error: ...` string.

Found in a live run: the calculator answered a bad expression with `error: invalid syntax`, so
the runner handed `after_tool` `is_error=False`, wrote a `tool_call` row with `status: ok`, and
eval's `trajectory_of` scored the call a success — none of them can read a failure into free
text. The plan tools did the same nine times. `tool_error_output` is the marker all three read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.hooks import get_agent_hooks
from felix.patterns import _plan_tools
from felix.patterns import tool_runner as runner_mod
from felix.patterns.tool_runner import ToolRunner
from felix.patterns.types import ToolCall
from felix.tools.builtins import register_builtin_tools
from felix.tools.errors import ToolErrorCode, read_tool_error_code
from felix.tools.provider import InMemoryToolProvider
from felix.tools.types import is_failure_content, tool_output_content


def _calculator():
    provider = InMemoryToolProvider()
    register_builtin_tools(provider)
    return provider.get("calculator")


@pytest.mark.parametrize("expression", ["import os", "1/0", "2 ** 10 ** 10"])
async def test_a_bad_expression_is_a_marked_invalid_arguments_error(expression: str) -> None:
    out = await _calculator().executor.execute({"expression": expression})

    assert read_tool_error_code(out) is ToolErrorCode.INVALID_ARGUMENTS, out
    assert is_failure_content(tool_output_content(out)), out


async def test_a_good_expression_is_still_plain_text() -> None:
    assert await _calculator().executor.execute({"expression": "12 * 7"}) == "84"


async def test_the_runner_reports_a_failed_calculation_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The consequences that reach a hook, the audit log and the model."""
    rows: list[tuple[str, str]] = []
    monkeypatch.setattr(runner_mod, "emit_agent_audit", lambda kind, **kw: rows.append((kind, kw["status"])))
    errors: list[bool] = []
    get_agent_hooks().register_after_tool(lambda call, result, is_error, ctx: errors.append(is_error))

    runner = ToolRunner(tool_map={"calculator": _calculator()}, manifest_id="m")
    messages, _, _, _ = await runner.run_batch(
        [ToolCall(id="1", name="calculator", args={"expression": "import os"})], thread_id="th", tenant_id="t"
    )

    assert errors == [True]
    assert rows == [("tool_call", "error")]
    assert messages[0].content.startswith("[tool error/invalid_arguments] ")


def _ctx(tenant: str, thread: str | None) -> RequestContext:
    settings = Settings(database_url="memory://tool-errors", allow_insecure=True, environment="development")
    return RequestContext(settings=settings, auth=AuthContext(tenant_id=tenant), thread_id=thread)


async def _plan(name: str, args: dict[str, Any]) -> Any:
    return await {t.name: t for t in _plan_tools()}[name].executor.execute(args)


@pytest.mark.parametrize(
    ("tool", "args", "code"),
    [
        ("plan_get", {"plan_id": "missing"}, ToolErrorCode.INVALID_ARGUMENTS),
        ("plan_get", {}, ToolErrorCode.INVALID_ARGUMENTS),
        ("plan_update_step", {"plan_id": "p"}, ToolErrorCode.INVALID_ARGUMENTS),
        ("plan_update_step", {"plan_id": "missing", "step_id": "s"}, ToolErrorCode.INVALID_ARGUMENTS),
    ],
    ids=["get-missing", "get-none-on-thread", "update-no-step-id", "update-missing"],
)
async def test_plan_tool_failures_are_marked(tool: str, args: dict[str, Any], code: ToolErrorCode) -> None:
    async with async_run_with_context(_ctx("tool-errors", "tool-errors:th")):
        out = await _plan(tool, args)

    assert read_tool_error_code(out) is code, out


@pytest.mark.parametrize("tool", ["plan_create", "plan_update_step", "plan_get"])
async def test_a_plan_tool_outside_a_request_is_a_marked_internal_error(tool: str) -> None:
    out = await _plan(tool, {"plan_id": "p", "step_id": "s"})

    assert read_tool_error_code(out) is ToolErrorCode.INTERNAL, out


def _plain_error_returns(source: str) -> list[int]:
    """Lines of `return` statements whose value, or either branch of a conditional, is a string
    starting `error: `: an AST walk, so `ruff format` wrapping a long one in parentheses or a
    ternary choosing between two cannot hide it the way a line regex let them."""
    import ast

    def plain_error(node: ast.expr | None) -> bool:
        if isinstance(node, ast.IfExp):
            return plain_error(node.body) or plain_error(node.orelse)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.lower().startswith("error: ")
        if isinstance(node, ast.JoinedStr) and node.values:
            return plain_error(node.values[0])
        return False

    return [
        n.lineno for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Return) and plain_error(n.value)
    ]


@pytest.mark.parametrize(
    "source",
    [
        'def f():\n    return "error: x"',
        'def f(a):\n    return (\n        f"error: a long message about {a} that ruff wrapped"\n    )',
        'def f(ok):\n    return "fine" if ok else "error: no"',
        "def f():\n    return 'Error: shouted'",
    ],
    ids=["plain", "parenthesized-f-string", "ternary", "capitalised"],
)
def test_the_scan_finds_every_shape_of_a_plain_error_return(source: str) -> None:
    assert _plain_error_returns(source) == [2]


def test_no_tool_in_core_answers_a_failure_as_plain_error_text() -> None:
    """The shape this file fixes, held for every module, so a new tool cannot reintroduce it."""
    root = Path(__file__).resolve().parents[2] / "packages/harness/src/felix"
    offenders = [
        f"{path.relative_to(root)}:{line}"
        for path in sorted(root.rglob("*.py"))
        for line in _plain_error_returns(path.read_text())
    ]
    assert offenders == [], f"return tool_error_output(...) instead: {offenders}"


async def test_arguments_the_schema_refuses_are_spelled_as_a_failure() -> None:
    """`define_tool`'s own refusal starts `[invalid args for ...]`, which no failure prefix named."""
    out = await _calculator().executor.execute({"expression": ""})

    assert read_tool_error_code(out) is ToolErrorCode.INVALID_ARGUMENTS, out
    assert is_failure_content(tool_output_content(out)), tool_output_content(out)


async def test_an_oversized_expression_is_refused_as_invalid_arguments() -> None:
    """Thousands of nested unary operators overflowed the parser with an uncaught MemoryError."""
    out = await _calculator().executor.execute({"expression": "-" * 6000 + "1"})

    assert read_tool_error_code(out) is ToolErrorCode.INVALID_ARGUMENTS, out


async def test_mcp_reports_a_failed_calculation_as_an_error() -> None:
    """`isError` was set only for a governance deny, so an MCP client read a tool's own failure
    as a success: the same misreading as the runner's, on the other surface."""
    from felix.mcp.server import handle_rpc
    from felix_api.composition import compose

    settings = Settings(
        auth_mode="none", allow_insecure=True, object_store="memory", database_url="memory://t"
    )
    called = await handle_rpc(
        settings=settings,
        tools=compose(settings),
        method="tools/call",
        params={"name": "calculator", "arguments": {"expression": "import os"}},
        rpc_id=1,
    )

    assert called["result"]["isError"] is True, called
    assert called["result"]["content"][0]["text"].startswith("[tool error/invalid_arguments] ")
