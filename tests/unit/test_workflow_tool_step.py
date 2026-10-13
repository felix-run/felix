"""A `tool_call` step's call: schema drift, the replay guard, and how outcomes are reported.

`_call` is driven with a fake governed tool set (`_governed_tools` patched), so each rule is
checked against the tool it is about rather than whatever the bundled manifests bind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from felix.tools.types import ToolInvocationCtx
from felix.workflows import tool_step
from felix.workflows.tool_step import TOOL_BEGAN, schema_drift

from tests.support.factories import make_settings

LIVE = {
    "type": "object",
    "properties": {"body": {"type": "string"}, "issue": {"type": "integer"}, "label": {"type": "string"}},
    "required": ["body", "issue"],
}


@pytest.mark.parametrize(
    ("stored", "sent", "reason"),
    [
        (LIVE, {"body", "issue"}, None),
        (LIVE, {"body", "issue", "label"}, None),
        (LIVE, {"body"}, "the tool now requires issue"),
        (
            {**LIVE, "properties": {**LIVE["properties"], "title": {}}},
            {"body", "issue", "title"},
            "no longer takes title",
        ),
        (
            {**LIVE, "properties": {**LIVE["properties"], "issue": {"type": "string"}}},
            {"body", "issue"},
            "argument issue changed type",
        ),
    ],
)
def test_schema_drift(stored: dict[str, Any], sent: set[str], reason: str | None) -> None:
    found = schema_drift(stored, LIVE, sent)
    if reason is None:
        assert found is None
    else:
        assert found is not None and reason in found


def test_a_new_optional_argument_is_not_drift() -> None:
    older = {"type": "object", "properties": {"body": {}, "issue": {}}, "required": ["body", "issue"]}
    assert schema_drift(older, LIVE, {"body", "issue"}) is None


@dataclass
class _Executor:
    output: Any = "done"
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def transport(self) -> str:
        return "builtin"

    async def execute(self, args: dict[str, Any], ctx: ToolInvocationCtx | None = None) -> Any:
        self.calls.append(args)
        return self.output


@dataclass
class _Tool:
    name: str
    executor: _Executor
    replay_safe: bool = False
    raw_input_schema: dict[str, Any] | None = None
    fatal: bool = False


def _row(**state: Any) -> dict[str, Any]:
    return {"tenant_id": "default", "id": "f1", "state_json": {"steps": [], **state}}


def _step(**overrides: Any) -> dict[str, Any]:
    step = {
        "op": "tool",
        "manifest_id": "quick",
        "tool": "post",
        "args": {"body": "hi", "issue": 7},
        "input_schema": LIVE,
        "thread_id": "default:workflow:r:post",
        "step_id": "post",
    }
    return {**step, **overrides}


class _Manifest:
    """Just what a call reads of a compiled manifest: its hooks and its name."""

    class spec:
        hooks: list[Any] = []

    class metadata:
        name = "quick"


async def _call(
    monkeypatch: pytest.MonkeyPatch, tool: _Tool, row: dict[str, Any], **step: Any
) -> dict[str, Any]:
    async def governed(*_a: Any, **_k: Any) -> tuple[Any, list[Any]]:
        return _Manifest(), [tool]

    async def unscreened(_manifest: Any, args: dict[str, Any], _settings: Any) -> dict[str, Any]:
        return args

    async def owned() -> bool:
        return True

    monkeypatch.setattr(tool_step, "_governed_tools", governed)
    monkeypatch.setattr("felix.governance.tool_screening.screen_tool_arguments", unscreened)
    return await tool_step._call(make_settings(), row, _step(**step), object(), owned)


async def test_a_call_returns_its_output_parsed_when_it_is_json(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _Tool("post", _Executor('{"id": 12}'), raw_input_schema=LIVE)
    assert await _call(monkeypatch, tool, _row()) == {"output": {"id": 12}}
    assert tool.executor.calls == [{"body": "hi", "issue": 7}]


async def test_a_call_that_may_have_run_is_not_repeated_unless_the_tool_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe = _Tool("post", _Executor(), raw_input_schema=LIVE)
    replayed = await _call(monkeypatch, unsafe, _row(**{TOOL_BEGAN: True}))
    assert replayed == {"error": "the call may have run before its worker stopped; it is not repeated"}
    assert unsafe.executor.calls == []

    safe = _Tool("post", _Executor(), replay_safe=True, raw_input_schema=LIVE)
    assert await _call(monkeypatch, safe, _row(**{TOOL_BEGAN: True})) == {"output": "done"}


async def test_the_marker_is_written_before_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row()
    seen: list[bool] = []

    class Recording(_Executor):
        async def execute(self, args: dict[str, Any], ctx: ToolInvocationCtx | None = None) -> Any:
            seen.append(bool(row["state_json"].get(TOOL_BEGAN)))
            return "done"

    await _call(monkeypatch, _Tool("post", Recording(), raw_input_schema=LIVE), row)
    assert seen == [True]


async def test_a_lost_claim_calls_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _Tool("post", _Executor(), raw_input_schema=LIVE)

    async def governed(*_a: Any, **_k: Any) -> tuple[Any, list[Any]]:
        return _Manifest(), [tool]

    async def unscreened(_m: Any, args: dict[str, Any], _s: Any) -> dict[str, Any]:
        return args

    async def lost() -> bool:
        return False

    monkeypatch.setattr(tool_step, "_governed_tools", governed)
    monkeypatch.setattr("felix.governance.tool_screening.screen_tool_arguments", unscreened)
    assert await tool_step._call(make_settings(), _row(), _step(), object(), lost) == {"lost": True}
    assert tool.executor.calls == []


async def test_a_governance_deny_and_a_tool_error_fail_the_step(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.tools.errors import tool_error_output
    from felix.tools.types import deny_output

    denied = deny_output("denied by policy", "policy")
    assert await _call(monkeypatch, _Tool("post", _Executor(denied), raw_input_schema=LIVE), _row()) == {
        "error": "governance or a hook refused the call"
    }
    errored = tool_error_output("rate_limited", "slow down")
    assert await _call(monkeypatch, _Tool("post", _Executor(errored), raw_input_schema=LIVE), _row()) == {
        "error": "the tool reported rate_limited"
    }


async def test_a_tool_the_manifest_does_not_bind_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    other = _Tool("comment", _Executor(), raw_input_schema=LIVE)
    assert await _call(monkeypatch, other, _row()) == {"error": "manifest quick does not bind tool post"}


async def test_a_plugin_before_tool_hook_can_refuse_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.hooks import get_agent_hooks

    seen: list[dict[str, Any]] = []

    async def refuse(call: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
        seen.append(call)
        return {"block": True, "reason": "not on a Friday"}

    get_agent_hooks().register_before_tool(refuse)
    tool = _Tool("post", _Executor(), raw_input_schema=LIVE)
    assert await _call(monkeypatch, tool, _row()) == {"error": "governance or a hook refused the call"}
    assert tool.executor.calls == [], "the hook refused before the tool ran"
    assert seen and seen[0]["args"] == {"body": "hi", "issue": 7}


async def test_a_call_writes_the_tool_call_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.audit import store as audit_store
    from felix.context import AuthContext, RequestContext, async_run_with_context

    settings = make_settings()
    tool = _Tool("post", _Executor("done"), raw_input_schema=LIVE)
    async with async_run_with_context(RequestContext(settings=settings, auth=AuthContext())):
        await _call(monkeypatch, tool, _row())
    await audit_store.flush_pending(settings)
    rows, _cursor = await audit_store.list_events(settings, "default", event_type="tool_call")
    assert [(r["status"], r["payload_json"].get("tool")) for r in rows] == [("ok", "post")]


async def test_a_raised_exception_is_named_by_its_code_not_its_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class Raising(_Executor):
        async def execute(self, args: dict[str, Any], ctx: ToolInvocationCtx | None = None) -> Any:
            raise TimeoutError("upstream proxy.internal:3128 did not answer")

    result = await _call(monkeypatch, _Tool("post", Raising(), raw_input_schema=LIVE), _row())
    from felix.tools.errors import infer_error_code

    code = infer_error_code(TimeoutError()).value
    assert result == {"error": f"the tool call failed ({code})"}
    assert "proxy.internal" not in str(result), "the exception's text never reaches the run view"
