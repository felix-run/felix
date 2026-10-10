"""`todo_write`: the agent's working checklist for the current task, visible to the person watching.

Any pattern that binds tools can carry it (`spec.tools: [todo_write]`); `deep`'s plan tools are the
heavier sibling, a persisted, operator-editable plan. This one is the run's own scratch list:
each call replaces the whole list, the way the model thinks about it, so there is no step id to
get wrong and no partial update to reconcile.

Nothing stores the list beside the transcript: it *is* the transcript's last successful
`todo_write` call on the current branch (`todos_on_branch`), which is how the snapshot shows it
after a reload. Kept anywhere else, it would not follow the log -- a rewind would leave the
abandoned branch's list on screen and a fork would start with none. Each write is also announced on
the stream as `todo_updated`, so a client can redraw it live.

`completed`, not the `done` that `deep`'s plan steps default to: these are the three states a
checklist UI draws, closed to the model, where a plan step's status is an open string an operator
may also write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from felix.context import try_get_context
from felix.side_events import emit as emit_side_event
from felix.tools.errors import ToolErrorCode, tool_error_output
from felix.tools.types import Tool, ToolOutput, define_tool

if TYPE_CHECKING:
    from felix.session.types import SessionEvent

TODO_TOOL_NAME = "todo_write"
# Every successful result starts with one of these; a refusal (invalid args, a policy, limits)
# starts with "[". `todos_on_branch` tells the two apart by it.
_SUCCESS_PREFIXES = ("Todo list updated:", "Todo list cleared.")
MAX_TODOS = 100

TodoStatus = Literal["pending", "in_progress", "completed"]


class TodoItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=500, description="What to do, in the imperative.")
    status: TodoStatus = Field(default="pending")
    active_form: str = Field(
        default="",
        max_length=500,
        description="The same item as it reads while under way, e.g. 'Running tests'.",
    )


class TodoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    todos: list[TodoItem] = Field(
        max_length=MAX_TODOS, description="The whole list, in order. It replaces the previous one."
    )

    @model_validator(mode="after")
    def _one_in_progress(self) -> TodoArgs:
        # One thing at a time is what makes the list a progress report rather than a backlog.
        if sum(1 for t in self.todos if t.status == "in_progress") > 1:
            raise ValueError("at most one item may be in_progress")
        return self


def _summary(todos: list[dict[str, str]]) -> str:
    if not todos:
        return "Todo list cleared."
    done = sum(1 for t in todos if t["status"] == "completed")
    current = next((t for t in todos if t["status"] == "in_progress"), None)
    lines = [f"Todo list updated: {done}/{len(todos)} completed."]
    if current is not None:
        lines.append(f"In progress: {current['content']}")
    mark = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}
    lines.extend(f"{mark[t['status']]} {t['content']}" for t in todos)
    return "\n".join(lines)


def _normalise(args: TodoArgs) -> list[dict[str, str]]:
    return [
        {"id": str(i), "content": t.content, "status": t.status, "active_form": t.active_form}
        for i, t in enumerate(args.todos, start=1)
    ]


async def _todo_write(args: TodoArgs) -> ToolOutput:
    req = try_get_context()
    if req is None:
        return tool_error_output(ToolErrorCode.INTERNAL, "[todo_write] no request context")
    todos = _normalise(args)
    await emit_side_event(req.thread_id, "todo_updated", {"todos": todos})
    return _summary(todos)


def todos_on_branch(branch: list[SessionEvent]) -> list[dict[str, str]]:
    """The checklist as the newest successful `todo_write` on `branch` left it, or `[]`.

    `branch` is the active path (`session.tree.active_branch_events`), so a rewind or a fork
    shows the list that branch had. A call whose result was a refusal -- invalid arguments, a
    policy, a limit -- changed nothing and is passed over.
    """
    succeeded: set[str] = set()
    for ev in reversed(branch):
        if ev.role == "tool" and ev.name == TODO_TOOL_NAME and ev.tool_call_id:
            if str(ev.content or "").startswith(_SUCCESS_PREFIXES):
                succeeded.add(ev.tool_call_id)
            continue
        for call in reversed(ev.tool_calls or []):
            if call.get("name") != TODO_TOOL_NAME or call.get("id") not in succeeded:
                continue
            args: Any = call.get("args")
            try:
                return _normalise(TodoArgs.model_validate(args))
            except ValueError:
                continue
    return []


def make_todo_tool() -> Tool:
    return define_tool(
        name=TODO_TOOL_NAME,
        description=(
            "Keep a checklist for a task with several steps, so you and the person watching can see "
            "where it stands. Send the whole list each time; it replaces the last one. Mark an item "
            "in_progress before you start it -- only one at a time -- and completed as soon as it "
            "is done. Skip it for a single, simple step."
        ),
        args=TodoArgs,
        handler=_todo_write,
        # The run's own checklist: it changes nothing outside the conversation, and planning is
        # exactly when it is wanted.
        read_only=True,
    )


__all__ = ["MAX_TODOS", "TODO_TOOL_NAME", "TodoArgs", "TodoItem", "make_todo_tool", "todos_on_branch"]
