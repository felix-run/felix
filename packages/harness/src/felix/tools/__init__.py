"""Felix tools package."""

from __future__ import annotations

from felix.tools.errors import (
    ToolError,
    ToolErrorCode,
    code_for_status,
    infer_error_code,
    read_tool_error_code,
    tool_error_output,
)
from felix.tools.executor import ToolExecutor, local_executor, wrap_executor, wrap_tool
from felix.tools.provider import InMemoryToolProvider, ToolProvider
from felix.tools.types import (
    Tool,
    ToolInput,
    ToolInvocationCtx,
    ToolOutput,
    ToolOutputDict,
    define_tool,
    define_tool_with_executor,
    deny_output,
    is_wrapper_deny,
    tool_output_content,
)

__all__ = [
    "InMemoryToolProvider",
    "Tool",
    "ToolError",
    "ToolErrorCode",
    "ToolExecutor",
    "ToolInput",
    "ToolInvocationCtx",
    "ToolOutput",
    "ToolOutputDict",
    "ToolProvider",
    "code_for_status",
    "define_tool",
    "define_tool_with_executor",
    "deny_output",
    "infer_error_code",
    "is_wrapper_deny",
    "local_executor",
    "read_tool_error_code",
    "tool_error_output",
    "tool_output_content",
    "wrap_executor",
    "wrap_tool",
]
