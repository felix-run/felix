"""`before_tool`: a block ends the chain, anything else does not, and a block leaves a record.

Found in a live run. `run_before_tool` returned at the first hook answering any non-empty dict,
so a hook that answered `{"deny": True}` — the reference plugin's old shape — blocked nothing
*and* switched off every hook registered after it, a blocking one included. And a block wrote no
audit row and no metric, so a call an operator's plugin refused was invisible in the ledger,
while the same refusal from a governance wrapper was a `policy_deny` row.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from felix.hooks import get_agent_hooks
from felix.patterns import tool_runner as runner_mod
from felix.patterns.tool_runner import ToolRunner
from felix.patterns.types import ToolCall
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput


class _Counts:
    transport = "probe-transport"

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        self.calls += 1
        return "ran"


async def _run(executor: _Counts):
    tool = Tool(name="t", description="d", args_schema=None, executor=executor)
    runner = ToolRunner(tool_map={"t": tool}, manifest_id="m")
    return await runner.run_batch([ToolCall(id="1", name="t", args={})], thread_id="th", tenant_id="t")


def answers_without_block(tool_call, ctx):
    return {"deny": True}


def blocks(tool_call, ctx):
    # A hook cannot name itself in the audit row: the runner's name for it wins.
    return {"block": True, "reason": "no", "hook": "spoofed"}


@pytest.mark.asyncio
async def test_a_non_block_answer_does_not_switch_off_a_later_blocking_hook() -> None:
    hooks = get_agent_hooks()
    hooks.register_before_tool(answers_without_block)
    hooks.register_before_tool(blocks)
    executor = _Counts()

    messages, _, _, _ = await _run(executor)

    assert executor.calls == 0, "the blocking hook never ran: the earlier answer ended the chain"
    assert messages[0].content == "[error/blocked] no"


@pytest.mark.asyncio
async def test_the_first_block_wins_and_ends_the_chain() -> None:
    later: list[str] = []
    hooks = get_agent_hooks()
    hooks.register_before_tool(blocks)
    hooks.register_before_tool(lambda tool_call, ctx: later.append(tool_call["name"]))
    executor = _Counts()

    await _run(executor)

    assert executor.calls == 0
    assert later == []


@pytest.mark.asyncio
async def test_an_answer_without_a_block_key_is_warned_about_and_an_explicit_allow_is_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def allows(tool_call, ctx):
        return {"block": False}

    hooks = get_agent_hooks()
    hooks.register_before_tool(answers_without_block)
    hooks.register_before_tool(allows)
    executor = _Counts()

    await _run(executor)

    warned = [
        r.getMessage() for r in caplog.records if r.name == "felix.hooks" and r.levelno == logging.WARNING
    ]
    assert len(warned) == 1 and "answers_without_block" in warned[0], warned
    assert "without a `block` key" in warned[0], warned
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_a_block_is_audited_counted_and_marks_the_batch_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[tuple[str, dict[str, Any]]] = []
    counts: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(runner_mod, "emit_agent_audit", lambda kind, **kw: rows.append((kind, kw)))
    monkeypatch.setattr(
        runner_mod, "record_counter", lambda name, labels, *a, **k: counts.append((name, labels))
    )
    # A non-blocking hook first, so the row must name the hook that blocked, not the first one.
    get_agent_hooks().register_before_tool(answers_without_block)
    get_agent_hooks().register_before_tool(blocks)

    _, _, _, had_denied = await _run(_Counts())

    assert rows == [
        (
            "policy_deny",
            {
                "status": "denied",
                "manifest_id": "m",
                "payload": {
                    "tool": "t",
                    "tool_call_id": "1",
                    "thread_id": "th",
                    "control": "hook",
                    "hook": f"{__name__}.blocks",
                },
            },
        )
    ]
    assert counts == [
        ("felix_tool_calls", {"transport": "probe-transport", "status": "denied", "manifest_id": "m"})
    ]
    assert had_denied == 1, "a hook block must mark the run's final audit row as an error, as a deny does"


@pytest.mark.parametrize("answer", [True, "block"], ids=["true", "string"])
async def test_an_answer_that_is_not_a_dict_is_warned_about_and_blocks_nothing(
    answer: object, caplog: pytest.LogCaptureFixture
) -> None:
    def not_a_dict(tool_call, ctx):
        return answer

    get_agent_hooks().register_before_tool(not_a_dict)
    executor = _Counts()

    await _run(executor)

    warned = [
        r.getMessage() for r in caplog.records if r.name == "felix.hooks" and r.levelno == logging.WARNING
    ]
    assert len(warned) == 1 and "not_a_dict" in warned[0] and "other than a dict" in warned[0], warned
    assert executor.calls == 1


@pytest.mark.parametrize("mode", ["sequential", "parallel"])
async def test_a_batch_counts_each_refused_call(mode: str) -> None:
    """`run_batch` returns how many calls were refused, which `final_response` sums per run."""
    get_agent_hooks().register_before_tool(blocks)
    tool = Tool(name="t", description="d", args_schema=None, executor=_Counts())
    runner = ToolRunner(tool_map={"t": tool}, manifest_id="m", tool_execution=mode)

    _, _, _, denied_calls = await runner.run_batch(
        [ToolCall(id="1", name="t", args={}), ToolCall(id="2", name="t", args={})],
        thread_id=None,
        tenant_id="t",
    )

    assert denied_calls == 2
