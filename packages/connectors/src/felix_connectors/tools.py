"""How a connector becomes a governed tool: validated arguments, one transport, written errors.

Each tool is an `http`-like call to a fixed host, so it is bound with its own transport --
`connectors`, which `_TRUSTED_TRANSPORTS` does not name -- so a manifest that enables content
screening has what it returns screened without listing it. It declares the hosts it reaches
(`Tool.egress_hosts`), which is how an agent's `network.allow_hosts` holds it: the compile refuses
the tool under a bound that does not allow them, and `http.get_json` refuses any other host. What
reaches the model on a failure is written here, never an exception's text: an upstream's error
body, a resolver failure and the operator's endpoint are all deployment detail.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import date
from typing import Any

from felix.tools.errors import ToolErrorCode, code_for_status, tool_error_output
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, define_tool_with_executor
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from felix_connectors.http import Busy, KeyRefused, QuotaSpent, UpstreamError

logger = logging.getLogger("felix_connectors.tools")

TRANSPORT = "connectors"

Run = Callable[[Any], Awaitable[str]]


def _invalid(exc: ValidationError) -> ToolOutput:
    # Field names and pydantic's own message for each, which describe the model's input back to
    # it; never the input value, which can be long.
    fields = "; ".join(
        f"{one_line('.'.join(str(p) for p in err['loc']), 64) or 'arguments'}: {one_line(err['msg'], 120)}"
        for err in exc.errors()[:5]
    )
    return tool_error_output(ToolErrorCode.INVALID_ARGUMENTS, f"invalid arguments: {fields}")


def _failed(service: str, exc: Exception) -> ToolOutput:
    from felix.security.ssrf import EgressBlocked

    logger.warning("connector_failed service=%s error=%s", service, type(exc).__name__, exc_info=exc)
    if isinstance(exc, UpstreamError):
        code = code_for_status(exc.status)
        return tool_error_output(code, f"{service} answered {exc.status}")
    if isinstance(exc, KeyRefused):
        return tool_error_output(ToolErrorCode.PERMISSION_DENIED, f"{service} refused this deployment's key")
    if isinstance(exc, QuotaSpent):
        return tool_error_output(ToolErrorCode.RATE_LIMITED, f"{service}'s request quota is spent for now")
    if isinstance(exc, Busy):
        return tool_error_output(ToolErrorCode.RATE_LIMITED, f"{service} is busy; try again shortly")
    if isinstance(exc, TimeoutError):
        return tool_error_output(ToolErrorCode.TIMEOUT, f"{service} did not answer in time")
    if isinstance(exc, EgressBlocked):
        return tool_error_output(ToolErrorCode.PERMISSION_DENIED, f"{service} is not reachable from here")
    return tool_error_output(ToolErrorCode.PROVIDER_ERROR, f"{service} gave an unusable answer")


class _ConnectorExecutor:
    transport = TRANSPORT

    def __init__(self, *, service: str, args: type[BaseModel], run: Run) -> None:
        self._service = service
        self._args = args
        self._run = run

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        _ = ctx
        try:
            parsed = self._args.model_validate(dict(args or {}))
        except ValidationError as exc:
            return _invalid(exc)
        try:
            return await self._run(parsed)
        except Exception as exc:
            return _failed(self._service, exc)


def connector_tool(
    *,
    name: str,
    family: str,
    service: str,
    description: str,
    args: type[BaseModel],
    run: Run,
    hosts: Iterable[str],
    prompt_guidance: str = "",
) -> Tool:
    """A read-only connector tool: it changes nothing anywhere, so it is replay-safe and plan
    mode lets it run. `service` names the upstream in what a failure tells the model -- one family
    can have several (weather's forecast and alerts), and "it failed" should say which. `hosts`
    may not be empty: a tool that declares nothing is one `network.allow_hosts` cannot hold."""
    hosts = frozenset(hosts)
    if not hosts:
        raise ValueError(f"connector tool {name!r} declares no hosts")
    return define_tool_with_executor(
        name=name,
        description=description,
        executor=_ConnectorExecutor(service=service, args=args, run=run),
        args=args,
        source=f"connectors:{family}",
        replay_safe=True,
        read_only=True,
        prompt_guidance=prompt_guidance,
        egress_hosts=hosts,
    )


class Point(BaseModel):
    """A latitude/longitude argument, bounded, so a coordinate formatted into a URL is digits."""

    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90, description="Latitude in decimal degrees.")
    longitude: float = Field(ge=-180, le=180, description="Longitude in decimal degrees.")


def number(value: object) -> float | None:
    """A JSON number, or a numeric string (Nominatim sends coordinates as strings); None for
    anything else, a bool included."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def iso_day(value: object) -> date | None:
    """A `YYYY-MM-DD` the upstream sent, or None for anything else."""
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def one_line(value: object, cap: int) -> str:
    """`value` as one line of at most `cap` characters.

    The rendered answer is lines of `label: value`, and the upstream's strings are not ours: an
    alert headline with a newline in it could write a line of its own. Control characters go and
    whitespace collapses, so every value stays inside the line it was given.
    """
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    text = " ".join(text.split())
    return text[:cap] + "…" if len(text) > cap else text


__all__ = ["TRANSPORT", "Point", "connector_tool", "iso_day", "number", "one_line"]
