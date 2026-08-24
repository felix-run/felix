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
