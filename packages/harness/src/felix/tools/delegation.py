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

With `spec.delegation.background`, `task(..., background=true)` starts the child as a durable run
instead and returns its id at once; `task_result` reads it. A background child runs in the
worker on a thread of its own, linked to the parent's (`parent_session_id`), as the caller who
started it, pinned to the manifest the parent compiled, no longer than the run or token that
started it, and on what is *left* of every budget above it (`limits.residual`) -- the parent's
run may end first, so the child cannot share live counters, and handing it whole caps let each
child spend the budget again. A background child cannot start background children of its own.

Either way the parent's stream carries `subagent_start` and, for a foreground child,
`subagent_end`, so a client can tell a child's work from the parent's.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from felix.context import RequestContext, try_get_context
from felix.governance.inbound import INBOUND_SCREENED_EXTRA
from felix.logging_setup import loggable
from felix.manifests.inbound_auth import InboundAuthError, enforce_inbound_auth
from felix.patterns.types import Agent, ChatMessage, InvokeInput
from felix.side_events import emit as emit_side_event
from felix.tools.errors import tool_error_output
from felix.tools.types import Tool, ToolOutput, define_tool

if TYPE_CHECKING:
    from felix.limits import EffectiveLimits
    from felix.manifests.schema import Manifest

logger = logging.getLogger("felix.tools.delegation")

TASK_TOOL_NAME = "task"
TASK_RESULT_TOOL_NAME = "task_result"

# How long `task_result` may hold a turn waiting for a background child, and how often it looks.
MAX_RESULT_WAIT_SECONDS = 60
_RESULT_POLL_SECONDS = 1.0

# Not `local`: a child's answer can quote whatever its own tools read, so content screening
# treats it the way it treats a peer's reply. `_TRUSTED_TRANSPORTS` is an allowlist, so an
# unlisted label is untrusted without anything else to keep in step.
TASK_TRANSPORT = "agent"


def _description(agents: Mapping[str, str], *, background: bool) -> str:
    lines = [
        "Hand a self-contained job to a specialist agent and get its final answer back.",
        "The agent starts with no memory of this conversation: put everything it needs in "
        "`prompt`. Call it several times in one turn to run independent jobs side by side.",
    ]
    if background:
        lines.append(
            "Set `background` for a long job: it returns a task id at once and the agent keeps "
            f"working; read its answer later with `{TASK_RESULT_TOOL_NAME}`."
        )
    lines += ["", "Agents:"]
    lines.extend(f"- {name}: {desc}" if desc else f"- {name}" for name, desc in agents.items())
    return "\n".join(lines)


def make_task_tool(
    children: Mapping[str, tuple[Agent, str, Manifest]],
    *,
    ceiling: EffectiveLimits,
    background: bool = False,
) -> Tool:
    """`task(agent, prompt)` over `children`: manifest name -> (compiled agent, description, the
    manifest it compiled from), in declaration order. `ceiling` is the delegating agent's own
    effective limits. `background` adds the `background` argument (`spec.delegation.background`)."""
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
    if background:
        schema["properties"]["background"] = {
            "type": "boolean",
            "description": "Start the job and return its task id at once instead of waiting.",
        }

    def _validate(args: Mapping[str, Any]) -> Mapping[str, Any]:
        agent = args.get("agent")
        prompt = args.get("prompt")
        if agent not in children:
            raise ValueError(f"unknown agent {agent!r} (known: {', '.join(names)})")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        in_background = args.get("background", False)
        if not isinstance(in_background, bool) or (in_background and not background):
            raise ValueError("background is not enabled for this agent's delegation")
        return {"agent": agent, "prompt": prompt, "background": in_background}

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
        if args["background"]:
            return await _start_in_background(req, name, manifest, str(args["prompt"]), ceiling)
        await emit_side_event(req.thread_id, "subagent_start", {"agent": name, "background": False})
        outcome = "error"
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
        else:
            outcome = "ok"
        finally:
            req.limit_state.ceilings.remove(ceiling)
            await emit_side_event(req.thread_id, "subagent_end", {"agent": name, "outcome": outcome})
        answer = result.final.content if result.final else ""
        return str(answer) if answer else f"[task] agent '{name}' returned no answer"

    return define_tool(
        name=TASK_TOOL_NAME,
        description=_description(
            {n: desc for n, (_agent, desc, _manifest) in children.items()}, background=background
        ),
        args_schema=schema,
        handler=handler,
        validate=_validate,
        is_peer=True,
        source="agent:task",
        transport=TASK_TRANSPORT,
    )


async def _start_in_background(
    req: RequestContext, name: str, manifest: Manifest, prompt: str, ceiling: EffectiveLimits
) -> ToolOutput:
    """Enqueue the child as a durable run on a thread of its own, and say how to read it."""
    from felix.durability.runs import BACKGROUND_CHILD_EXTRA, start_durable_chat
    from felix.limits import residual
    from felix.manifests.pin import pin_fields_for
    from felix.session.thread_state import claim_thread

    settings, tenant_id, parent = req.settings, req.auth.tenant_id, req.thread_id
    if req.extras.get(BACKGROUND_CHILD_EXTRA):
        # Each background run starts on fresh counters, so one that could start more would let
        # a single instruction fan out level by level. A foreground child shares live ones.
        return tool_error_output(
            "permission_denied", "[task] a background task cannot start background tasks of its own"
        )
    if not parent:
        # `task_result` reads a run only from the thread that started it; with no thread there
        # is nothing to tell this caller's runs from another threadless caller's.
        return tool_error_output("invalid_arguments", "[task] a background task needs a conversation thread")
    child_thread = f"{parent}:task:{uuid.uuid4().hex[:16]}"
    try:
        await claim_thread(
            settings=settings, tenant_id=tenant_id, thread_id=child_thread, parent_session_id=parent
        )
        run = await start_durable_chat(
            settings,
            tenant_id,
            manifest_id=name,
            messages=[ChatMessage(role="user", content=prompt)],
            thread_id=child_thread,
            model_id=None,
            execution=manifest.spec.execution,
            # The manifest the parent compiled, not whatever is active when the worker gets to
            # it: the run carries the caller's scopes, so the code they run under is pinned too.
            pin=await pin_fields_for(settings, tenant_id, manifest),
            parent_thread_id=parent,
            ceilings=[residual(c, req.limit_state) for c in (*req.limit_state.ceilings, ceiling)],
        )
    except Exception:
        logger.warning("task_background_start_failed agent=%s", name, exc_info=True)
        return tool_error_output("provider_error", f"[task] could not start agent '{name}' in the background")
    task_id = str(run["resume_token"])
    await emit_side_event(
        parent,
        "subagent_start",
        {"agent": name, "background": True, "task_id": task_id, "thread_id": child_thread},
    )
    return (
        f"[task] agent '{name}' is working in the background as task {task_id}. "
        f"Call {TASK_RESULT_TOOL_NAME} with this id to read its answer."
    )


def make_task_result_tool() -> Tool:
    """`task_result(task_id, wait_seconds)`: a background child's status, and its answer once done.

    Reads only runs this thread started: the run records its parent thread, and an id from
    anywhere else -- another thread's, a guessed one -- reads as unknown.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1, "description": "The id `task` returned."},
            "wait_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_RESULT_WAIT_SECONDS,
                "description": "Wait up to this long for the task to finish. 0 checks once.",
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    }

    def _validate(args: Mapping[str, Any]) -> Mapping[str, Any]:
        task_id = args.get("task_id")
        wait = args.get("wait_seconds", 0)
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(wait, int) or isinstance(wait, bool) or not 0 <= wait <= MAX_RESULT_WAIT_SECONDS:
            raise ValueError(f"wait_seconds must be an integer from 0 to {MAX_RESULT_WAIT_SECONDS}")
        return {"task_id": task_id.strip(), "wait_seconds": wait}

    async def handler(args: Mapping[str, Any]) -> ToolOutput:
        from felix.durability.fibers import FIBER_TERMINAL_STATUSES
        from felix.durability.runs import get_child_run

        req = try_get_context()
        if req is None:
            return tool_error_output("permission_denied", "[task_result] no request context")
        task_id = str(args["task_id"])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + int(args["wait_seconds"])
        while True:
            view = await get_child_run(req.settings, req.auth.tenant_id, task_id, req.thread_id or "")
            if view is None:
                return tool_error_output(
                    "invalid_arguments", f"[task_result] no background task {task_id!r} here"
                )
            if view["status"] in FIBER_TERMINAL_STATUSES or loop.time() >= deadline:
                break
            await asyncio.sleep(_RESULT_POLL_SECONDS)
        status = str(view["status"])
        if status == "completed":
            answer = str((view.get("final") or {}).get("content") or "")
            return answer or f"[task_result] task {task_id} finished with no answer"
        if status in FIBER_TERMINAL_STATUSES:
            # `run_view`'s error is the one `GET /chat/runs` shows a caller: written for them.
            reason = str(view.get("error") or "").strip()
            return tool_error_output(
                "provider_error",
                f"[task_result] task {task_id} ended: {status}" + (f" ({reason})" if reason else ""),
            )
        return f"[task_result] task {task_id} is still {status}"

    return define_tool(
        name=TASK_RESULT_TOOL_NAME,
        description="Read a background task started with `task`: its status, and its answer once it is done.",
        args_schema=schema,
        handler=handler,
        validate=_validate,
        # The answer is a child's, as untrusted as `task`'s own.
        source="agent:task_result",
        transport=TASK_TRANSPORT,
    )


__all__ = [
    "MAX_RESULT_WAIT_SECONDS",
    "TASK_RESULT_TOOL_NAME",
    "TASK_TOOL_NAME",
    "TASK_TRANSPORT",
    "make_task_result_tool",
    "make_task_tool",
]
