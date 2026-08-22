"""Tool error taxonomy — soft outputs + hard ToolError."""

from __future__ import annotations

from enum import StrEnum

from felix.tools.types import ToolOutput, ToolOutputDict, tool_output_content

_TOOL_ERROR_MARKER: object = object()


class ToolErrorCode(StrEnum):
    INVALID_ARGUMENTS = "invalid_arguments"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"
    USER_ABORTED = "user_aborted"
    RATE_LIMITED = "rate_limited"
    PERMISSION_DENIED = "permission_denied"
    INTERNAL = "internal"


class ToolError(Exception):
    def __init__(self, code: ToolErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.name = "ToolError"


def tool_error_output(code: ToolErrorCode | str, content: str) -> ToolOutputDict:
    code_str = code.value if isinstance(code, ToolErrorCode) else str(code)
    # Prefer caller content; prefix lightly when bare.
    text = content if content.startswith("[") else f"[tool error/{code_str}] {content}"
    return ToolOutputDict(
        content=text,
        metadata={_TOOL_ERROR_MARKER: True, "error_code": code_str},
    )


def read_tool_error_code(output: ToolOutput) -> ToolErrorCode | None:
    if isinstance(output, str):
        return None
    if isinstance(output, ToolOutputDict):
        md = output.metadata
    elif isinstance(output, dict):
        md = output.get("metadata")
        if not isinstance(md, dict):
            return None
    else:
        return None
    if md.get(_TOOL_ERROR_MARKER) is not True and "error_code" not in md:
        # Accept legacy unbranded error_code for parallel-agent compatibility.
        if "error_code" not in md:
            return None
    code = md.get("error_code")
    if isinstance(code, str):
        try:
            return ToolErrorCode(code)
        except ValueError:
            return ToolErrorCode.INTERNAL
    return None


def infer_error_code(err: object) -> ToolErrorCode:
    if isinstance(err, ToolError):
        return err.code
    name = getattr(err, "name", None) or type(err).__name__
    if name in {"AbortError", "TimeoutError", "CancelledError"}:
        return ToolErrorCode.USER_ABORTED
    msg = str(err).lower()
    if "timeout" in name.lower() or "timeout" in msg:
        return ToolErrorCode.TIMEOUT
    code = getattr(err, "code", None)
    if isinstance(code, str):
        if code in {"ETIMEDOUT", "ETIME"}:
            return ToolErrorCode.TIMEOUT
        if code in {"ECONNREFUSED", "ENOTFOUND", "ECONNRESET", "EHOSTUNREACH"}:
            return ToolErrorCode.TRANSPORT_UNAVAILABLE
    status = getattr(err, "status", None) or getattr(err, "status_code", None)
    if isinstance(status, int):
        return code_for_status(status)
    if "permission" in msg or "forbidden" in msg:
        return ToolErrorCode.PERMISSION_DENIED
    return ToolErrorCode.INTERNAL


def code_for_status(status: int) -> ToolErrorCode:
    if status == 429:
        return ToolErrorCode.RATE_LIMITED
    if status in {401, 403}:
        return ToolErrorCode.PERMISSION_DENIED
    if status >= 500:
        return ToolErrorCode.PROVIDER_ERROR
    if status >= 400:
        return ToolErrorCode.INVALID_ARGUMENTS
    return ToolErrorCode.TRANSPORT_UNAVAILABLE


__all__ = [
    "ToolError",
    "ToolErrorCode",
    "code_for_status",
    "infer_error_code",
    "read_tool_error_code",
    "tool_error_output",
    "tool_output_content",
]
