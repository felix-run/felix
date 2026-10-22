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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from felix.context import AuthContext, RequestContext


class ToolRefused(Exception):
    """A call the tool will not make. ``detail`` is written for the model."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail

    def result(self) -> str:
        return json.dumps({"error": self.code, "detail": self.detail})


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


__all__ = ["ToolRefused", "require_person"]
