"""The `task` tool: hand a self-contained job to a child agent and get its answer back.

A child is a compiled `Agent` (`spec.delegation.agents`, compiled by the builder the way a
sub-agent is), so it runs its own manifest's governance stack on its own tools. It starts from
an empty transcript -- only `prompt` -- which is the point: a long search or a noisy tool loop
spends the child's context, and the parent keeps one tool result.

The child runs inside the parent's request context, so it spends the parent's run budgets
(`limits` state is per request) and is held to the parent's caps as well as its own: the parent's
limits ride `LimitState.ceilings` while the child runs. The tool is a peer for
`limits.max_peer_hops`, which bounds delegation chains at run time the way `MAX_SUB_AGENT_DEPTH`
bounds them at compile time.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from felix.context import try_get_context
from felix.governance.inbound import INBOUND_SCREENED_EXTRA
from felix.logging_setup import loggable
from felix.manifests.inbound_auth import InboundAuthError, enforce_inbound_auth
from felix.patterns.types import Agent, ChatMessage, InvokeInput
from felix.tools.errors import tool_error_output
from felix.tools.types import Tool, ToolOutput, define_tool

if TYPE_CHECKING:
    from felix.limits import EffectiveLimits
    from felix.manifests.schema import Manifest

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


def make_task_tool(children: Mapping[str, tuple[Agent, str, Manifest]], *, ceiling: EffectiveLimits) -> Tool:
    """`task(agent, prompt)` over `children`: manifest name -> (compiled agent, description, the
    manifest it compiled from), in declaration order. `ceiling` is the delegating agent's own
    effective limits."""
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
            raise ValueError(f"unknown agent {agent!r} (known: {', '.join(names)})")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        return {"agent": agent, "prompt": prompt}

    async def handler(args: Mapping[str, Any]) -> ToolOutput:
        name = str(args["agent"])
        child, _description_text, manifest = children[name]
        req = try_get_context()
        if req is None:
            # Fail closed, like `limits`: without a context there is no caller to admit.
            return tool_error_output("permission_denied", f"[task] no request context to run agent '{name}'")
        try:
            # The model chose this child, so it must not be a way past the child's own door:
            # a caller who could not call it by name cannot reach it through `task` either.
            enforce_inbound_auth(manifest, req.auth)
        except InboundAuthError as exc:
            logger.info("task_child_refused agent=%s reason=%s", name, loggable(str(exc), limit=200))
            return tool_error_output("permission_denied", f"[task] the caller may not run agent '{name}'")
        # The route marks a turn it screened, and the first agent to screen consumes the mark.
        # A parent with screening off never consumes it, so it would reach the child -- and
        # this prompt was written by the model, often from tool output, not by the caller the
        # route screened. The child screens what it is handed.
        req.extras.pop(INBOUND_SCREENED_EXTRA, None)
        req.limit_state.ceilings.append(ceiling)
        try:
            result = await child.invoke(
                InvokeInput(messages=[ChatMessage(role="user", content=str(args["prompt"]))])
            )
        except Exception:
            # The child's own failure is the operator's to read; the model gets a fact it can
            # act on (retry, or do the job itself) rather than another agent's traceback.
            logger.warning("task_child_failed agent=%s", name, exc_info=True)
            return tool_error_output("provider_error", f"[task] agent '{name}' failed before answering")
        finally:
            req.limit_state.ceilings.remove(ceiling)
        answer = result.final.content if result.final else ""
        return str(answer) if answer else f"[task] agent '{name}' returned no answer"

    return define_tool(
        name=TASK_TOOL_NAME,
        description=_description({n: desc for n, (_agent, desc, _manifest) in children.items()}),
        args_schema=schema,
        handler=handler,
        validate=_validate,
        is_peer=True,
        source="agent:task",
        transport=TASK_TRANSPORT,
    )


__all__ = ["TASK_TOOL_NAME", "TASK_TRANSPORT", "make_task_tool"]
