"""The `task` tool: hand a self-contained job to a child agent and get its answer back.

A child is a compiled `Agent` (`spec.delegation.agents`, compiled by the builder the way a
sub-agent is), so it runs its own manifest's governance stack on its own tools. It starts from
an empty transcript -- only `prompt` -- which is the point: a long search or a noisy tool loop
spends the child's context, and the parent keeps one tool result.

The child runs inside the parent's request context, so it spends the parent's run budgets
(`limits` state is per request), and the tool is a peer for `limits.max_peer_hops`, which bounds
delegation chains at run time the way `MAX_SUB_AGENT_DEPTH` bounds them at compile time.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from felix.patterns.types import Agent, ChatMessage, InvokeInput
from felix.tools.errors import tool_error_output
from felix.tools.types import Tool, ToolOutput, define_tool

logger = logging.getLogger("felix.tools.delegation")

TASK_TOOL_NAME = "task"

# Not `local`: a child's answer can quote whatever its own tools read, so content screening
# treats it the way it treats a peer's reply. `_TRUSTED_TRANSPORTS` is an allowlist, so an
# unlisted label is untrusted without anything else to keep in step.
TASK_TRANSPORT = "agent"


def _description(agents: Mapping[str, str]) -> str:
    lines = [
        "Hand a self-contained job to a specialist agent and get its final answer back.",
        "The agent starts with no memory of this conversation: put everything it needs in "
        "`prompt`. Call it several times in one turn to run independent jobs side by side.",
        "",
        "Agents:",
    ]
    lines.extend(f"- {name}: {desc}" if desc else f"- {name}" for name, desc in agents.items())
    return "\n".join(lines)


def make_task_tool(children: Mapping[str, Agent], descriptions: Mapping[str, str]) -> Tool:
    """`task(agent, prompt)` over `children`, keyed by manifest name, in declaration order."""
    names = list(children)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "enum": names, "description": "Which agent to hand the job to."},
            "prompt": {
                "type": "string",
                "minLength": 1,
                "description": "The complete job, with every fact the agent needs.",
            },
        },
        "required": ["agent", "prompt"],
        "additionalProperties": False,
    }

    def _validate(args: Mapping[str, Any]) -> Mapping[str, Any]:
        agent = args.get("agent")
        prompt = args.get("prompt")
        if agent not in children:
            raise ValueError(f"agent must be one of: {', '.join(names)}")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        return {"agent": agent, "prompt": prompt}

    async def handler(args: Mapping[str, Any]) -> ToolOutput:
        name = str(args["agent"])
        try:
            result = await children[name].invoke(
                InvokeInput(messages=[ChatMessage(role="user", content=str(args["prompt"]))])
            )
        except Exception:
            # The child's own failure is the operator's to read; the model gets a fact it can
            # act on (retry, or do the job itself) rather than another agent's traceback.
            logger.warning("task_child_failed agent=%s", name, exc_info=True)
            return tool_error_output("provider_error", f"[task] agent '{name}' failed before answering")
        answer = result.final.content if result.final else ""
        return str(answer) if answer else f"[task] agent '{name}' returned no answer"

    return define_tool(
        name=TASK_TOOL_NAME,
        description=_description({n: descriptions.get(n, "") for n in names}),
        args_schema=schema,
        handler=handler,
        validate=_validate,
        is_peer=True,
        source="agent:task",
        transport=TASK_TRANSPORT,
    )


__all__ = ["TASK_TOOL_NAME", "TASK_TRANSPORT", "make_task_tool"]
