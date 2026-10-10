"""`todo_write`: the agent's working checklist for the current task, visible to the person watching.

Any pattern that binds tools can carry it (`spec.tools: [todo_write]`); `deep`'s plan tools are the
heavier sibling, a persisted, operator-editable plan. This one is the run's own scratch list:
each call replaces the whole list, the way the model thinks about it, so there is no step id to
get wrong and no partial update to reconcile.

The list lives on the thread (`thread_state` meta, key `todos`), so a reload's snapshot shows it,
and each write is announced on the stream as `todo_updated` so a client can redraw it live.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from felix.context import try_get_context
from felix.side_events import emit as emit_side_event
from felix.tools.errors import tool_error_output
from felix.tools.types import Tool, ToolOutput, define_tool

TODO_TOOL_NAME = "todo_write"
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


async def _todo_write(args: TodoArgs) -> ToolOutput:
    from felix.session.thread_state import update_thread_meta

    req = try_get_context()
    if req is None:
        return tool_error_output("permission_denied", "[todo_write] no request context")
    todos = [
        {"id": str(i), "content": t.content, "status": t.status, "active_form": t.active_form}
        for i, t in enumerate(args.todos, start=1)
    ]
    if req.thread_id:
        await update_thread_meta(
            settings=req.settings, tenant_id=req.auth.tenant_id, thread_id=req.thread_id, todos=todos
        )
    await emit_side_event(req.thread_id, "todo_updated", {"todos": todos})
    return _summary(todos)


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
    )


__all__ = ["MAX_TODOS", "TODO_TOOL_NAME", "TodoArgs", "TodoItem", "make_todo_tool"]
