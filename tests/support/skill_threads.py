"""Call a skill tool inside a thread, logging the call and its result as a turn would.

Which skills are active is read from the thread's log (`felix.skills.activation`), which the
pattern writes after each tool batch -- the tools themselves write nothing. A test calling a tool
directly logs the same two events a react step appends: the assistant's call, then the result.
"""

from __future__ import annotations

from typing import Any

from felix.session.tree import annotate_and_append
from felix.session.types import chat_message_to_event
from felix.tools.types import ToolInvocationCtx, tool_output_content
from felix_ai.types import ChatMessage, ToolCall


async def call_in_thread(
    tool: Any, args: dict[str, Any], *, session_store: Any, thread_id: str, call_id: str = "c1"
) -> str:
    """``tool``'s result text for ``args`` in ``thread_id``, with the call and result logged."""
    out = await tool.executor.execute(args, ToolInvocationCtx(thread_id=thread_id, tool_call_id=call_id))
    text = tool_output_content(out)
    # As a react step appends: with the tree ids a rewind or a fork moves along.
    await annotate_and_append(
        session_store.open(thread_id),
        [
            chat_message_to_event(
                ChatMessage(
                    role="assistant", content="", tool_calls=[ToolCall(id=call_id, name=tool.name, args=args)]
                )
            ),
            chat_message_to_event(
                ChatMessage(role="tool", name=tool.name, tool_call_id=call_id, content=text)
            ),
        ],
    )
    return text
