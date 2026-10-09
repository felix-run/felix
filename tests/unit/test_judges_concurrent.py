"""Tool-output judges: free ones first, model ones together, denials in the manifest's order."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.manifests import builder
from felix.manifests.schema import Guardrails, JudgeRule
from felix.tools.types import Tool, ToolInvocationCtx, tool_output_content


class _Echo:
    transport = "local"

    async def execute(self, args: Any, ctx: ToolInvocationCtx | None = None) -> str:
        return "tool output"


def _judged(*judges: JudgeRule) -> Tool:
    tool = Tool(name="lookup", description="d", args_schema=None, executor=_Echo())
    return builder.apply_judges([tool], Guardrails(judges=list(judges)), "m")[0]


def _model(name: str) -> JudgeRule:
    return JudgeRule(name=name, criteria="is fine", model="judge-model")


def _heuristic(name: str) -> JudgeRule:
    return JudgeRule(name=name, criteria="is fine")


async def _run(tool: Tool) -> str:
    return str(tool_output_content(await tool.executor.execute({}, ToolInvocationCtx(manifest_id="m"))))


async def test_model_judges_run_together(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each waits until both are in flight; judged one after another, this deadlocks."""
    both = asyncio.Barrier(2)

    async def score(content: str, judge: JudgeRule, **kw: Any) -> float:
        await both.wait()
        return 1.0

    monkeypatch.setattr(builder, "judge_score", score)
    out = await asyncio.wait_for(_run(_judged(_model("a"), _model("b"))), timeout=2)
    assert out == "tool output"


async def test_a_free_denial_spares_the_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    async def score(content: str, judge: JudgeRule, **kw: Any) -> float:
        asked.append(judge.name)
        return 0.0 if judge.name == "cheap" else 1.0

    monkeypatch.setattr(builder, "judge_score", score)
    # Declared after the model judge, still asked first.
    out = await _run(_judged(_model("pricey"), _heuristic("cheap")))
    assert "[judge denied] cheap" in out
    assert asked == ["cheap"]


async def test_the_denial_named_is_the_first_in_manifest_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent, but the second judge answering first does not change which denial is named."""

    async def score(content: str, judge: JudgeRule, **kw: Any) -> float:
        if judge.name == "first":
            await asyncio.sleep(0.05)
        return 0.0

    monkeypatch.setattr(builder, "judge_score", score)
    out = await _run(_judged(_model("first"), _model("second")))
    assert "[judge denied] first" in out


async def test_a_decider_judge_runs_with_the_model_judges(monkeypatch: pytest.MonkeyPatch) -> None:
    """A decider judge is a paid call, so it belongs in the concurrent lane, not the free one."""
    from types import SimpleNamespace

    both = asyncio.Barrier(2)

    async def score(content: str, judge: JudgeRule, **kw: Any) -> float:
        await both.wait()
        return 1.0

    monkeypatch.setattr(builder, "judge_score", score)
    tool = Tool(name="lookup", description="d", args_schema=None, executor=_Echo())
    judges = [_model("m"), JudgeRule(name="d", criteria="is fine", decider=True)]
    decider: Any = SimpleNamespace(model_id="jev")
    wrapped = builder.apply_judges([tool], Guardrails(judges=judges), "m", decider=decider)[0]
    assert await asyncio.wait_for(_run(wrapped), timeout=2) == "tool output"
