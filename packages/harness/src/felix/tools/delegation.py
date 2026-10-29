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
`subagent_end`, so a client can tell a child's work from the parent's -- and, between them, a
`subagent_tool` frame at the start and end of each call a foreground child makes
(`tools/subagent_trace.py`), since `invoke()` streams nothing of its own.

In a thread with a repository checkout, a background child works in a git worktree of it on a
branch of its own (`repos/worktrees.py`), and so does a foreground child under
`spec.delegation.worktree` -- bound to its own thread's workspace for the call, while it keeps the
parent's request. What it committed is in the thread's repository; the answer says on which branch.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from felix.context import RequestContext, try_get_context
from felix.governance.inbound import INBOUND_SCREENED_EXTRA
from felix.logging_setup import loggable
from felix.manifests.inbound_auth import InboundAuthError, enforce_inbound_auth
from felix.patterns.types import ChatMessage, InvokeInput, InvokeOutput
from felix.security.host_allowlist import EgressBound
from felix.side_events import emit as emit_side_event
from felix.tools import task_workspace
from felix.tools.errors import tool_error_output
from felix.tools.subagent_trace import subagent_running
from felix.tools.types import Tool, ToolInvocationCtx, ToolOutput, define_tool
from felix.tools.workspace_scope import bound_workspace_thread

if TYPE_CHECKING:
    from felix.limits import EffectiveLimits
    from felix.manifest_hooks import ManifestHooks
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


class ChildAgent(Protocol):
    """What `task` runs: a compiled `Agent`, or the builder's `_SelfFork` -- the manifest itself, as a
    forked skill's subagent, compiled on first use. A child is only ever invoked."""

    async def invoke(self, input: InvokeInput) -> InvokeOutput: ...


def make_task_tool(
    children: Mapping[str, tuple[ChildAgent, str, Manifest]],
    *,
    ceiling: EffectiveLimits,
    background: bool = False,
    max_background: int = 3,
    manifest_hooks: ManifestHooks | None = None,
    egress: EgressBound = EgressBound(),
    worktree: bool = False,
    model_overrides: dict[str, str] | None = None,
) -> Tool:
    """`task(agent, prompt)` over `children`: manifest name -> (compiled agent, description, the
    manifest it compiled from), in declaration order. `ceiling` is the delegating agent's own
    effective limits. `background` adds the `background` argument (`spec.delegation.background`).
    `egress` is the delegating agent's `allow_hosts` bound, which a background child carries into
    the worker; a foreground child was compiled under it already. `worktree` gives a foreground
    child a git worktree of the thread's repository (`spec.delegation.worktree`)."""
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

    async def handler(args: Mapping[str, Any], ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        name = str(args["agent"])
        child, _description_text, manifest = children[name]
        # A forked skill's vetted `model:` (`ForkRunner`), keyed by the call id it minted: never a
        # field of the model-facing schema, so the model cannot pick its children's models.
        call_id = getattr(ctx, "tool_call_id", None)
        model_id = model_overrides.pop(call_id, None) if model_overrides is not None and call_id else None
        req, refused = _admit(name, manifest, tag="task")
        if req is None:
            return refused
        if args["background"]:
            return await _start_in_background(
                req, name, manifest, str(args["prompt"]), ceiling, max_background, egress
            )
        return await _run_foreground(
            req,
            name,
            child,
            str(args["prompt"]),
            ceiling=ceiling,
            worktree=worktree,
            manifest_hooks=manifest_hooks,
            model_id=model_id,
        )

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
        # A foreground child shares this run, and with it the permission mode: in plan mode the
        # child is held to read-only tools as well. A background start is refused in plan mode
        # (`_start_in_background`), since a worker run would not inherit it.
        read_only=True,
    )


def _admit(name: str, manifest: Manifest, *, tag: str) -> tuple[RequestContext | None, ToolOutput]:
    """Admit a run of the child ``name``: `(request, "")`, or `(None, refusal)`. The one door both
    `task` and a forked skill go through; a rule added here holds for each."""
    req = try_get_context()
    if req is None:
        # Fail closed, like `limits`: without a context there is no caller to admit.
        return None, tool_error_output(
            "permission_denied", f"[{tag}] no request context to run agent '{name}'"
        )
    try:
        # The model chose this child, so it must not be a way past the child's own door: a caller
        # who could not call it by name cannot reach it through `task` or a skill either.
        enforce_inbound_auth(manifest, req.auth)
    except InboundAuthError as exc:
        logger.info("%s_child_refused agent=%s reason=%s", tag, name, loggable(str(exc), limit=200))
        return None, tool_error_output("permission_denied", f"[{tag}] the caller may not run agent '{name}'")
    # The route marks a turn it screened, and the first agent to screen consumes the mark. A parent
    # with screening off never consumes it, so it would reach the child -- and this prompt was
    # written by the model (or a skill), often from tool output, not by the caller the route
    # screened. The child screens what it is handed.
    req.extras.pop(INBOUND_SCREENED_EXTRA, None)
    return req, ""


async def _run_foreground(
    req: RequestContext,
    name: str,
    child: ChildAgent,
    prompt: str,
    *,
    ceiling: EffectiveLimits,
    worktree: bool,
    manifest_hooks: ManifestHooks | None,
    model_id: str | None = None,
) -> ToolOutput:
    """Run ``child`` on ``prompt`` in this run and answer what it said: the `task` tool's foreground
    path, and a forked skill's (`ForkRunner`). The caller has already admitted the call."""
    child_thread = _child_thread_id(req.thread_id) if req.thread_id else ""
    workspace = await task_workspace.prepare(req, child_thread, wanted=worktree)
    if not isinstance(workspace, task_workspace.ChildWorkspace):
        return workspace
    await emit_side_event(req.thread_id, "subagent_start", {"agent": name, "background": False})
    result = None
    req.limit_state.ceilings.append(ceiling)
    try:
        with bound_workspace_thread(workspace.thread), subagent_running(name):
            result = await child.invoke(
                InvokeInput(messages=[ChatMessage(role="user", content=prompt)], model_id=model_id)
            )
    except Exception:
        # The child's own failure is the operator's to read; the model gets a fact it can
        # act on (retry, or do the job itself) rather than another agent's traceback.
        logger.warning("task_child_failed agent=%s", name, exc_info=True)
    finally:
        req.limit_state.ceilings.remove(ceiling)
        # Every way out, cancellation included: where its work is, and a worktree it left
        # nothing in removed. Shielded, so a cancellation that keeps arriving cannot cut it short.
        where = await asyncio.shield(task_workspace.finish_foreground(req, workspace))
        outcome = "ok" if result is not None else "error"
        await emit_side_event(req.thread_id, "subagent_end", {"agent": name, "outcome": outcome})
    if result is None:
        failed = f"[task] agent '{name}' failed before answering"
        return tool_error_output("provider_error", f"{failed}\n\n{where}" if where else failed)
    answer = str(result.final.content or "") if result.final else ""
    reply = answer or f"[task] agent '{name}' returned no answer"
    if where:
        reply = f"{reply}\n\n{where}"
    if manifest_hooks is not None and manifest_hooks.has("subagent_stop"):
        observed = await manifest_hooks.fire(
            "subagent_stop", {"agent": name, "outcome": outcome, "answer": answer}
        )
        if observed.contexts:
            reply = f"{reply}\n\n{observed.context_block()}"
    return reply


@dataclass(slots=True)
class ForkRunner:
    """How a skill whose frontmatter says `context: fork` runs (`skills/tools.py`): as a call of this
    manifest's *governed* `task` tool, bound once the governance stack has wrapped it (`bind`). So
    every control that names `task` -- an approval, a policy, the limits' peer hops, the permission
    mode -- holds for a fork as for the model's own `task` call, and the child runs on `task`'s path,
    `subagent_stop` included. A `model:` the skill asks for reaches the `task` handler by the call
    id this mints (`model_overrides`), and only when the deployment defines the route and
    request-model governance would let a caller ask for it on this manifest and its children."""

    children: Mapping[str, tuple[ChildAgent, str, Manifest]]
    lead: Manifest
    model_overrides: dict[str, str]
    _task: Tool | None = None

    @property
    def agents(self) -> tuple[str, ...]:
        return tuple(self.children)

    def bind(self, governed_task: Tool | None) -> None:
        self._task = governed_task

    async def __call__(
        self, agent: str, prompt: str, model_id: str | None, *, outer_call_id: str | None = None
    ) -> ToolOutput:
        if self._task is None:
            return tool_error_output("permission_denied", "[skill] this agent's task tool is not available")
        req = try_get_context()
        # Derived from the `activate_skill` call the model made (`fork_call_id`), so whatever the inner
        # call leaves behind -- an approval row, a client's tool card -- is found by the id in the
        # transcript: a run interrupted mid-fork closes the gate it was waiting on (felix#531).
        call_id = fork_call_id(outer_call_id) if outer_call_id else f"fork_{uuid.uuid4().hex[:16]}"
        model = await _vetted_model(self.lead, model_id, req) if req is not None else None
        if model:
            self.model_overrides[call_id] = model
        try:
            return await self._task.executor.execute(
                {"agent": agent, "prompt": prompt},
                ToolInvocationCtx(thread_id=getattr(req, "thread_id", None), tool_call_id=call_id),
            )
        finally:
            self.model_overrides.pop(call_id, None)


def fork_call_id(outer_call_id: str) -> str:
    """The id of the `task` call a forked skill makes inside the `activate_skill` call ``outer_call_id``."""
    return f"{outer_call_id}:fork"


async def _vetted_model(manifest: Manifest, model_id: str | None, req: RequestContext) -> str | None:
    """``model_id`` when the deployment defines that route and governance would let a caller ask
    for it on ``manifest`` and every agent below it -- the whole tree, resolved as the chat route
    resolves it, since a delegating child hands the model on to its own sub-agents; otherwise None,
    and the agent's own model runs."""
    if not model_id:
        return None
    from felix.manifests.governance import request_model_tree_refusal
    from felix.patterns.model import parse_model_routes

    # A typo, or a route this install never had, is dropped rather than handed to a child to fail on.
    refusal = (
        await request_model_tree_refusal(
            manifest,
            model_id,
            req.settings,
            tenant_id=req.auth.tenant_id,
        )
        if model_id in parse_model_routes(req.settings)
        else "unknown_route"
    )
    if refusal is None:
        return model_id
    logger.warning(
        "forked skill asked for model %s (%s); the agent's own is used", loggable(model_id, limit=80), refusal
    )
    return None


async def _start_in_background(
    req: RequestContext,
    name: str,
    manifest: Manifest,
    prompt: str,
    ceiling: EffectiveLimits,
    max_background: int,
    egress: EgressBound,
) -> ToolOutput:
    """Enqueue the child as a durable run on a thread of its own, and say how to read it."""
    from felix.durability.fibers import TooManyChildren
    from felix.durability.runs import BACKGROUND_CHILD_EXTRA, start_durable_chat
    from felix.limits import residual
    from felix.manifests.pin import pin_fields_for
    from felix.session.thread_state import claim_thread

    settings, tenant_id, parent = req.settings, req.auth.tenant_id, req.thread_id
    from felix.governance.permission_mode import in_plan_mode

    if await in_plan_mode(req):
        return tool_error_output(
            "permission_denied",
            "[task] plan mode: a background task would run outside it; delegate in the foreground",
        )
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
    child_thread = _child_thread_id(parent)
    # In the thread's repository, if it has one: on a branch of its own, or not at all.
    workspace = await task_workspace.prepare(req, child_thread, wanted=True)
    if not isinstance(workspace, task_workspace.ChildWorkspace):
        return workspace
    try:
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
            max_children=max_background,
            egress_ceilings=egress.as_state(),
        )
    except TooManyChildren as full:
        await task_workspace.discard(req, workspace)
        return tool_error_output(
            "rate_limited",
            f"[task] {full.limit} background tasks are already running here; a slot frees when one "
            f"finishes (wait on it with {TASK_RESULT_TOOL_NAME}), or do this one in the foreground",
        )
    except Exception:
        logger.warning("task_background_start_failed agent=%s", name, exc_info=True)
        await task_workspace.discard(req, workspace)
        return tool_error_output("provider_error", f"[task] could not start agent '{name}' in the background")
    except BaseException:
        # Cancelled mid-start: no run, so no worktree either.
        await task_workspace.discard(req, workspace)
        raise
    task_id = str(run["resume_token"])
    try:
        # Linked once the run exists, so a refused start leaves no orphan thread behind. A failed
        # link does not un-start the run: the model still gets its id, or it would hold a slot
        # nothing could read.
        await claim_thread(
            settings=settings, tenant_id=tenant_id, thread_id=child_thread, parent_session_id=parent
        )
    except Exception:
        logger.warning("task_background_link_failed agent=%s", name, exc_info=True)
    await emit_side_event(
        parent,
        "subagent_start",
        {"agent": name, "background": True, "task_id": task_id, "thread_id": child_thread},
    )
    where = task_workspace.background_note(workspace)
    return (
        f"[task] agent '{name}' is working in the background as task {task_id}. "
        f"Call {TASK_RESULT_TOOL_NAME} with this id to read its answer.{where}"
    )


def _child_thread_id(parent: str) -> str:
    return f"{parent}:task:{uuid.uuid4().hex[:16]}"


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
        read_only=True,
    )


__all__ = [
    "MAX_RESULT_WAIT_SECONDS",
    "TASK_RESULT_TOOL_NAME",
    "TASK_TOOL_NAME",
    "TASK_TRANSPORT",
    "make_task_result_tool",
    "make_task_tool",
]
