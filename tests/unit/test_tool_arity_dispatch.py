"""One model tool call runs a tool once.

Both dispatch sites decided how to call a function by *calling it* with the wider signature
and catching `TypeError`. That cannot distinguish "wrong arity" from "a `TypeError` raised
inside a body that already ran", so any tool whose body raises `TypeError` on attacker-shaped
input — an MCP server returning a list where a dict was expected, a JSON field that is null —
executed twice for one model tool call. Past every governance wrapper, with no interrupted-call
marker, and the model sees one call.

`wrap_executor` is the worse of the two: it is benign for the eight wrappers in
`manifests/builder.py`, whose `execute` takes two parameters so the three-argument call always
raises before the body runs, and *not* benign for `apply_artifact_spill`, whose `execute` is
`(args, ctx=None, _inner=inner)` and dispatches on the first call.

The rule is that arity is decided by introspection, once, before anything runs.
"""

from __future__ import annotations

import pytest
from felix.tools.executor import wrap_executor
from felix.tools.types import ToolInput, ToolInvocationCtx, ToolOutput, accepts_positional, define_tool


class _Inner:
    transport = "builtin"

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        return "inner"


@pytest.mark.asyncio
async def test_a_three_parameter_execute_runs_once_when_its_body_raises_type_error() -> None:
    """The `apply_artifact_spill` shape: three parameters, so the first call dispatches."""
    calls: list[int] = []

    async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None, _inner=None) -> ToolOutput:
        calls.append(1)
        raise TypeError("raised inside the body, e.g. 'NoneType' object is not subscriptable")

    executor = wrap_executor(_Inner(), execute)

    with pytest.raises(TypeError):
        await executor.execute({})

    assert len(calls) == 1, f"one model tool call executed the tool {len(calls)} times"


@pytest.mark.asyncio
async def test_a_handler_runs_once_when_its_body_raises_type_error() -> None:
    """The `define_tool` shape: `handler(parsed, ctx)` fell back to `handler(parsed)`."""
    calls: list[int] = []

    async def handler(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        calls.append(1)
        raise TypeError("attacker-shaped input: expected dict, got list")

    tool = define_tool(name="t", description="d", handler=handler)

    with pytest.raises(TypeError):
        await tool.executor.execute({})

    assert len(calls) == 1, f"one model tool call executed the handler {len(calls)} times"


@pytest.mark.asyncio
async def test_both_signatures_still_dispatch_correctly() -> None:
    """A guard that broke dispatch would be worse than the bug it fixed."""

    async def three(args: ToolInput, ctx: ToolInvocationCtx | None = None, inner=None) -> ToolOutput:
        return f"got inner: {inner is not None}"

    async def two(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        return "two"

    assert await wrap_executor(_Inner(), three).execute({}) == "got inner: True"
    assert await wrap_executor(_Inner(), two).execute({}) == "two"

    async def one_arg(args: ToolInput) -> ToolOutput:
        return "one"

    async def two_arg(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        return "two"

    assert await define_tool(name="a", description="d", handler=one_arg).executor.execute({}) == "one"
    assert await define_tool(name="b", description="d", handler=two_arg).executor.execute({}) == "two"


# Checked against `signature().bind`, not against hand-written expectations. A table of
# expectations is written by the same person who wrote the helper and shares its blind spots:
# the first version of this file asserted `(lambda *a: None, 5, True)` for the varargs branch,
# which is the one varargs shape that cannot reveal the bug that branch had — it answered True
# for `f(a, b, c, *rest)` at count=2, which a real call rejects.
CALLABLES = [
    lambda a: None,
    lambda a, b: None,
    lambda a, b=None: None,
    lambda a, b=None, c=None: None,
    lambda *a: None,
    lambda a, *rest: None,
    lambda a, b, c, *rest: None,
    lambda a, *, kw=None: None,
    lambda **kw: None,
]


@pytest.mark.parametrize("fn", CALLABLES, ids=range(len(CALLABLES)))
@pytest.mark.parametrize("count", [1, 2, 3])
def test_the_arity_check_agrees_with_what_a_real_call_would_do(fn, count: int) -> None:
    import inspect

    try:
        inspect.signature(fn).bind(*range(count))
        callable_with_count = True
    except TypeError:
        callable_with_count = False

    assert accepts_positional(fn, count) is callable_with_count, (
        f"accepts_positional disagrees with a real {count}-argument call"
    )


def test_an_unintrospectable_callable_selects_the_narrow_call() -> None:
    """Guessing narrow is the safe guess: a genuine mismatch raises once and loudly, rather
    than running a side effect a second time."""
    assert accepts_positional(print, 3) in (True, False)  # must not raise
    assert accepts_positional(object(), 2) is False  # not callable at all


@pytest.mark.asyncio
async def test_the_real_artifact_spill_wrapper_runs_its_inner_chain_once() -> None:
    """The coupling, not a hand-written mimic of it.

    The test above builds a three-parameter `execute` that *looks* like
    `apply_artifact_spill`. If that function is rewritten — as it now has been, to close over
    `inner` — the mimic stays green while saying it covers the real thing. This drives the
    real wrapper, which is the one whose `execute` dispatched on the probe's first call and
    whose inner is the entire governance chain below it.
    """
    from felix.artifacts import apply_artifact_spill
    from felix.manifests.schema import ArtifactsSpec

    calls: list[int] = []

    class _Raises:
        transport = "builtin"

        async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            calls.append(1)
            raise TypeError("raised inside the governance chain below the spill wrapper")

    from felix.tools.types import Tool

    tool = Tool(name="t", description="d", args_schema=None, executor=_Raises())
    wrapped = apply_artifact_spill(
        [tool],
        ArtifactsSpec(),
        object_store=None,
        tenant_id="t",
        manifest_id="m",
    )[0]

    with pytest.raises(TypeError):
        await wrapped.executor.execute({})

    assert len(calls) == 1, f"one model tool call ran the governance chain {len(calls)} times"
