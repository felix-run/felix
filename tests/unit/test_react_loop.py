"""ReAct loop smoke — calculator tool via compose()."""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.tools.types import ToolInvocationCtx
from felix_api.composition import compose


@pytest.mark.asyncio
async def test_calculator_tool_via_compose() -> None:
    settings = Settings(allow_insecure=True, auth_mode="none", environment="development")
    provider = compose(settings)
    assert provider.has("calculator")
    tool = provider.get("calculator")
    out = await tool.executor.execute(
        {"expression": "7 * 6"},
        ToolInvocationCtx(manifest_id="quick"),
    )
    assert str(out) == "42"


@pytest.mark.asyncio
async def test_calculator_rejects_unsafe_expr() -> None:
    settings = Settings(allow_insecure=True, auth_mode="none", environment="development")
    tool = compose(settings).get("calculator")
    out = await tool.executor.execute(
        {"expression": "__import__('os').system('id')"},
        ToolInvocationCtx(),
    )
    assert "error" in str(out).lower() or "unsupported" in str(out).lower()


@pytest.mark.asyncio
async def test_react_pattern_registered() -> None:
    # `react` is the default pattern and imports nothing optional, so an ImportError
    # here means the registry is broken -- which is the thing this test exists to
    # notice, not to skip.
    import felix.patterns.react  # noqa: F401
    from felix.patterns.registry import get_pattern

    pattern = get_pattern("react")
    assert pattern is not None


@pytest.mark.asyncio
async def test_approval_timeout_writes_final_response_error() -> None:
    """A run ending on approval timeout records final_response with status=error."""
    from felix.audit import store as audit_store
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.manifests.builder import apply_policies
    from felix.manifests.schema import ModelSpec, Policy
    from felix.patterns.react import _ReactAgent
    from felix.patterns.types import ChatMessage, InvokeInput, ToolCall
    from felix.tools.types import Tool, ToolInput, ToolOutput
    from felix_ai.types import ModelChatResult, TokenUsage

    # A simple echo tool
    class _Echo:
        transport = "local"

        async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            return "ran"

    # Wrap the tool with a policy that will deny it
    tool = Tool(name="gated_tool", description="d", args_schema=None, executor=_Echo())
    policy = Policy(id="needs-scope", tools=["gated_tool"], required_scopes=["tools:gated"])
    gated_tool = apply_policies([tool], [policy], manifest_id="test-manifest")[0]

    # A model that calls the tool once, then returns a final answer
    class _CallsOnceThenStops:
        model_id = "test-model"

        def __init__(self) -> None:
            self.turns = 0

        async def chat(self, messages: list[ChatMessage], tools: list, opts=None) -> ModelChatResult:
            self.turns += 1
            if self.turns == 1:
                # First turn: request the tool
                return ModelChatResult(
                    message=ChatMessage(
                        role="assistant",
                        content="",
                        tool_calls=[ToolCall(id="call_1", name="gated_tool", args={})],
                    ),
                    stop_reason="tool_use",
                    usage=TokenUsage(),
                )
            else:
                # After tool denial: return final answer
                return ModelChatResult(
                    message=ChatMessage(
                        role="assistant",
                        content="I cannot proceed without that scope.",
                    ),
                    stop_reason="end_turn",
                    usage=TokenUsage(),
                )

    model = _CallsOnceThenStops()
    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        database_url="memory://test-approval-timeout",
        object_store="memory",
        host="127.0.0.1",
    )

    agent = _ReactAgent(
        tools=[gated_tool],
        pattern="react",
        manifest_id="test-manifest",
        manifest_version="1.0.0",
        system_prompt="Test",
        model_spec=ModelSpec(id="test-model"),
        settings=settings,
        recursion_limit=10,
    )
    agent._resolve_model = lambda _input: model  # type: ignore[method-assign]

    # Clear audit buffer before test
    audit_store._pending.reset_for_tests()

    # Run with context that lacks the required scope, so the tool is denied
    ctx = RequestContext(
        settings=settings,
        auth=AuthContext(tenant_id="default", scopes=frozenset()),
        manifest_id="test-manifest",
    )
    async with async_run_with_context(ctx):
        await agent.invoke(
            InvokeInput(
                messages=[ChatMessage(role="user", content="test")],
                tenant_id="default",
            )
        )

    # Check that final_response was written with status=error
    rows = list(audit_store._pending)
    final_responses = [r for r in rows if r.get("event_type") == "final_response"]
    assert len(final_responses) == 1, f"Expected 1 final_response, got {len(final_responses)}"
    assert final_responses[0]["status"] == "error", (
        f"Expected final_response status=error for denied tool, got status={final_responses[0]['status']}"
    )


@pytest.mark.asyncio
async def test_denial_followed_by_permitted_tool_records_ok() -> None:
    """A denial followed by more tool work records final_response with status=ok."""
    from felix.audit import store as audit_store
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.manifests.builder import apply_policies
    from felix.manifests.schema import ModelSpec, Policy
    from felix.patterns.react import _ReactAgent
    from felix.patterns.types import ChatMessage, InvokeInput, ToolCall
    from felix.tools.types import Tool, ToolInput, ToolOutput
    from felix_ai.types import ModelChatResult, TokenUsage

    # Two tools: one gated, one not
    class _Echo:
        transport = "local"

        def __init__(self, name: str) -> None:
            self.name = name

        async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            return f"{self.name} ran"

    gated = Tool(name="gated", description="d", args_schema=None, executor=_Echo("gated"))
    ungated = Tool(name="ungated", description="d", args_schema=None, executor=_Echo("ungated"))

    # Apply policy only to the gated tool
    policy = Policy(id="needs-scope", tools=["gated"], required_scopes=["tools:gated"])
    tools = apply_policies([gated, ungated], [policy], manifest_id="test-manifest")

    # Model: call gated (denied), then ungated (succeeds), then finish
    class _CallsBothThenStops:
        model_id = "test-model"

        def __init__(self) -> None:
            self.turns = 0

        async def chat(self, messages: list[ChatMessage], tools: list, opts=None) -> ModelChatResult:
            self.turns += 1
            if self.turns == 1:
                # First turn: request gated tool (will be denied)
                return ModelChatResult(
                    message=ChatMessage(
                        role="assistant",
                        content="",
                        tool_calls=[ToolCall(id="call_1", name="gated", args={})],
                    ),
                    stop_reason="tool_use",
                    usage=TokenUsage(),
                )
            elif self.turns == 2:
                # Second turn: request ungated tool (will succeed)
                return ModelChatResult(
                    message=ChatMessage(
                        role="assistant",
                        content="",
                        tool_calls=[ToolCall(id="call_2", name="ungated", args={})],
                    ),
                    stop_reason="tool_use",
                    usage=TokenUsage(),
                )
            else:
                # Third turn: final answer
                return ModelChatResult(
                    message=ChatMessage(
                        role="assistant",
                        content="Done with both tools.",
                    ),
                    stop_reason="end_turn",
                    usage=TokenUsage(),
                )

    model = _CallsBothThenStops()
    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        database_url="memory://test-denial-then-ok",
        object_store="memory",
        host="127.0.0.1",
    )

    agent = _ReactAgent(
        tools=tools,
        pattern="react",
        manifest_id="test-manifest",
        manifest_version="1.0.0",
        system_prompt="Test",
        model_spec=ModelSpec(id="test-model"),
        settings=settings,
        recursion_limit=10,
    )
    agent._resolve_model = lambda _input: model  # type: ignore[method-assign]

    # Clear audit buffer before test
    audit_store._pending.reset_for_tests()

    # Run with context that lacks the scope for gated but can run ungated
    ctx = RequestContext(
        settings=settings,
        auth=AuthContext(tenant_id="default", scopes=frozenset()),
        manifest_id="test-manifest",
    )
    async with async_run_with_context(ctx):
        await agent.invoke(
            InvokeInput(
                messages=[ChatMessage(role="user", content="test")],
                tenant_id="default",
            )
        )

    # Check that final_response was written with status=ok
    # because the last batch (ungated) succeeded
    rows = list(audit_store._pending)
    final_responses = [r for r in rows if r.get("event_type") == "final_response"]
    assert len(final_responses) == 1, f"Expected 1 final_response, got {len(final_responses)}"
    assert final_responses[0]["status"] == "ok", (
        f"Expected final_response status=ok when last batch succeeded, got status={final_responses[0]['status']}"
    )
