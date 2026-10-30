"""A call a tool will not make, answered as a result the model reads, and the caller check the
person-addressed tools share.

`schedule_task` and its siblings (`jobs/agent_tools.py`) and `notify` (`push/agent_tool.py`) each
act for one person -- a task is owned by its writer, a notice goes to its reader -- so each refuses
a run that names no one, or names a credential every holder shares. One check, so the two cannot
drift on what "a person" means; the wording stays each tool's own, since the model reads it.

A refusal is `{"error": <code>, "detail": <message>}`, written by the tool and never an
exception's text. Each tool's wrapper catches `ToolRefused` once and returns `result()`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from felix.context import AuthContext, RequestContext
    from felix.tools.types import ToolInvocationCtx, ToolOutput


class ToolRefused(Exception):
    """A call the tool will not make. ``detail`` is written for the model."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail

    def result(self) -> str:
        return json.dumps({"error": self.code, "detail": self.detail})


# What each kind of gate could not get in an unattended run (`RequestContext.unattended`).
_UNATTENDED_WANTED = {
    "approval": "no one can approve it",
    "command approval": "no one can approve it",
    "client tool": "no client is attached to run it",
}


def unattended_refusal(gate: str, tool_name: str, rule_id: str | None = None) -> str:
    """The tool result of a call an unattended run could not have made: `gate` is the kind of wait
    it would have needed (`approval`, `command approval`, `client tool`). Written for the model --
    why, and that nothing happened -- so the run can carry on and report it. The one wording."""
    rule = f" rule={rule_id}" if rule_id else ""
    return (
        f"[{gate} unavailable] tool={tool_name}{rule}: this run is unattended (no person started it), "
        f"so {_UNATTENDED_WANTED[gate]}; the call was not made."
    )


def require_person(*, no_caller: str, shared_subject: str) -> tuple[RequestContext, AuthContext]:
    """The request context and caller of a call made for a named person, or a refusal.

    `no_caller` when there is no context, or the caller is anonymous or names no one;
    `shared_subject` when who the run acts for is a subject that names no person
    (`auth/recorded.py:is_shared_subject`). The two messages are the tool's own.
    """
    from felix.auth.recorded import is_shared_subject
    from felix.context import try_get_context

    ctx = try_get_context()
    auth = ctx.auth if ctx is not None else None
    if ctx is None or auth is None or auth.anonymous or not auth.acting_for:
        raise ToolRefused("no_caller", no_caller)
    if is_shared_subject(auth.acting_for):
        raise ToolRefused("shared_subject", shared_subject)
    return ctx, auth


def answered[A](
    handler: Callable[[A, ToolInvocationCtx | None], Awaitable[ToolOutput]],
) -> Callable[[A, ToolInvocationCtx | None], Awaitable[ToolOutput]]:
    """`handler` with a `ToolRefused` it raises returned as the result the model reads -- the one
    place a tool catches its refusals, so none can escape as an error the loop reports instead."""

    async def run(args: A, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        try:
            return await handler(args, ctx)
        except ToolRefused as refused:
            return refused.result()

    return run


__all__ = ["ToolRefused", "answered", "require_person"]
