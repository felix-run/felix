"""What each step kind does in a run: start it, look at it, stop it.

One executor per runnable kind, in `EXECUTORS`; `RUNNABLE_KINDS` is its keys, so a kind is
runnable exactly when it has an executor and `engine.run_problems` refuses any other at start.
Internal to the engine for now: a public registration seam needs a reader outside it first.
See `engine.py` for when each method is called and what idempotency each must keep.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Protocol

from felix.config import Settings
from felix.logging_setup import loggable
from felix.workflows.kinds import AgentTurnConfig, ApprovalConfig, BranchConfig, ToolCallConfig, render_prompt
from felix.workflows.runs import StepState, fences_prompts
from felix.workflows.schema import Step, WorkflowSpec

logger = logging.getLogger("felix.workflows.executors")

# An approval step that sets no `ttl_seconds` waits this long: the most an approval rule may.
APPROVAL_DEFAULT_TTL_SECONDS = 3600

_CHILD_NAMESPACE = uuid.UUID("6c0f6a43-55b8-4c1e-9d0e-6f9f2f6b1d27")


class RunContext(Protocol):
    """What an executor reads of the run it is acting in (`engine._Advance`)."""

    settings: Settings
    run: dict[str, Any]
    tenant_id: str
    spec: WorkflowSpec


# --- executors --------------------------------------------------------------------------------


class Executor(Protocol):
    """What one step kind does in a run. Each method edits `state` in place."""

    async def start(self, run: RunContext, step: Step, inputs: dict[str, Any], state: StepState) -> None: ...

    async def poll(self, run: RunContext, step: Step, state: StepState) -> None: ...

    async def stop(self, run: RunContext, step: Step, state: StepState) -> None:
        """Make sure the step does nothing further, whether or not it started."""
        ...


def _now() -> int:
    from felix.durability.fibers import now_ms

    return now_ms()


def _fail(state: StepState, error: str) -> None:
    state.update(status="failed", error=error, finished_at=_now())


class _Branch:
    async def start(self, run: RunContext, step: Step, inputs: dict[str, Any], state: StepState) -> None:
        config = BranchConfig.model_validate(step.config)
        value = inputs.get("value")
        if not config.cases:
            if not isinstance(value, bool):
                _fail(state, f"expected a boolean, got {_kind_of(value)}")
                return
            port = "true" if value else "false"
        else:
            port = value if isinstance(value, str) and value in config.cases else "default"
        state.update(status="completed", fired=[port], outputs={}, finished_at=_now())

    async def poll(self, run: RunContext, step: Step, state: StepState) -> None:
        return

    async def stop(self, run: RunContext, step: Step, state: StepState) -> None:
        return


class _Approval:
    @staticmethod
    def _signature(run: RunContext, step: Step) -> tuple[str, str]:
        return f"workflow:{run.run['workflow_id']}.{step.id}", f"{run.run['id']}:{step.id}"

    async def start(self, run: RunContext, step: Step, inputs: dict[str, Any], state: StepState) -> None:
        from felix.approvals import store as approvals
        from felix.thread_ids import workflow_run_key

        tool_name, signature = self._signature(run, step)
        # A replayed start finds the row the first attempt opened, even one decided since.
        row = await approvals.find_by_signature(
            run.settings, run.tenant_id, tool_name=tool_name, call_signature=signature
        )
        if row is None:
            config = ApprovalConfig.model_validate(step.config)
            row = await approvals.create_pending(
                run.settings,
                run.tenant_id,
                tool_name=tool_name,
                call_signature=signature,
                args={"payload": inputs.get("payload")},
                principal_subj=run.run["started_by"],
                ttl_seconds=config.ttl_seconds or APPROVAL_DEFAULT_TTL_SECONDS,
                reason=config.prompt or f"{run.spec.name}: {step.name or step.id}",
                thread_id=workflow_run_key(run.tenant_id, run.run["id"]) or "",
                tool_call_id=step.id,
            )
        state.update(status="waiting", approval_id=row["id"])

    async def poll(self, run: RunContext, step: Step, state: StepState) -> None:
        from felix.approvals import store as approvals

        row = await approvals.get_approval(run.settings, run.tenant_id, state.get("approval_id", ""))
        if row is None:
            _fail(state, "the approval request is gone")
            return
        timed_out = row["status"] == "denied" and row.get("decided_by") == approvals.TIMEOUT_DECIDER
        lapsed = (
            row["status"] == "pending" and row.get("expires_at") is not None and row["expires_at"] <= _now()
        )
        if timed_out or lapsed:
            # Nobody decided. That is not a denial a person made, so it routes nowhere: it fails.
            _fail(state, "no decision before the approval expired")
            return
        if row["status"] not in {"approved", "denied"}:
            return
        approved = row["status"] == "approved"
        state.update(
            status="completed",
            fired=["approved" if approved else "denied"],
            outputs={"decision": {"approved": approved, "reason": row.get("decision_note") or ""}},
            decided_by=row.get("decided_by") or "",
            finished_at=_now(),
        )

    async def stop(self, run: RunContext, step: Step, state: StepState) -> None:
        from felix.approvals import store as approvals
        from felix.thread_ids import workflow_run_key

        key = workflow_run_key(run.tenant_id, run.run["id"])
        if key:
            # By the run's key and the step, so a row a lost attempt opened is closed too.
            await approvals.close_interrupted(run.settings, run.tenant_id, key, [step.id])


def child_fiber_id(run: RunContext, step: Step) -> str:
    """The fiber id of a step's child run, derived so a replayed or overlapping start finds it."""
    return uuid.uuid5(_CHILD_NAMESPACE, f"{run.tenant_id}:{run.run['id']}:{step.id}").hex


async def _stop_child(run: RunContext, step: Step, state: StepState) -> None:
    """Stop a step's child run, started or not: end its fiber, abort its turn, answer its gates.

    A tool call already executing has no loop to read the abort: it runs to its end, and its
    worker's save then loses to the stop (or its next lease renewal does, within
    `FIBER_LEASE_RENEW_MS`). Its result is discarded either way.
    """
    from felix.approvals import store as approvals
    from felix.approvals.interrupt import signal_decision
    from felix.durability.fibers import stop_fiber
    from felix.steer import request_abort
    from felix.thread_ids import workflow_thread_id

    # By the derived id, so a child a lost attempt started — or one not yet claimed — stops.
    stopped = await stop_fiber(
        run.settings, run.tenant_id, child_fiber_id(run, step), "stopped by its workflow"
    )
    thread = state.get("thread_id") or workflow_thread_id(run.tenant_id, run.run["id"], step.id)
    if not thread or not (stopped or state.get("status") == "running"):
        return
    # A turn under way is told to abort, and a gate it is waiting on is answered `denied` now:
    # approving it later would run the tool with the starter's scopes after the run ended
    # (felix-run/felix#531's shape).
    await request_abort(run.tenant_id, thread)
    for approval_id in await approvals.close_pending_on_thread(run.settings, run.tenant_id, thread):
        await signal_decision(approval_id, "denied", note=approvals.INTERRUPTED_NOTE)


class _AgentTurn:
    async def start(self, run: RunContext, step: Step, inputs: dict[str, Any], state: StepState) -> None:
        from felix.durability.fibers import RunInProgress, get_fiber
        from felix.thread_ids import workflow_thread_id

        config = AgentTurnConfig.model_validate(step.config)
        thread = workflow_thread_id(run.tenant_id, run.run["id"], step.id)
        if thread is None:
            _fail(state, "no thread id for this step")
            return
        child_id = child_fiber_id(run, step)
        state.update(thread_id=thread, fiber_id=child_id)
        if await get_fiber(run.settings, run.tenant_id, child_id) is not None:
            state["status"] = "running"  # a replay: an earlier attempt started it
            return
        try:
            await self._enqueue(run, config, inputs, thread, child_id)
        except RunInProgress as busy:
            if busy.resume_token == child_id:
                state["status"] = "running"  # another claim of this run started it a moment ago
                return
            _fail(state, "the step's thread already has a run")
            return
        state["status"] = "running"

    async def _enqueue(
        self, run: RunContext, config: AgentTurnConfig, inputs: dict[str, Any], thread: str, child_id: str
    ) -> None:
        from felix.context import RequestContext, async_run_with_context
        from felix.durability.fibers import auth_from_state
        from felix.durability.runs import RUN_NOT_AFTER_EXTRA, start_durable_chat
        from felix.manifests.schema import ExecutionSpec
        from felix.patterns.types import ChatMessage

        pinned = run.run["pins"].get(config.manifest) or {}
        remaining_s = max(1, (int(run.run["expires_at"]) - _now()) // 1000)
        start = start_durable_chat(
            run.settings,
            run.tenant_id,
            manifest_id=config.manifest,
            messages=[
                ChatMessage(
                    role="user",
                    content=render_prompt(config, inputs, fenced=fences_prompts(run.run)),
                )
            ],
            thread_id=thread,
            model_id=None,
            # The run's remaining life, and never more than the manifest grants a durable run.
            execution=ExecutionSpec(
                mode="durable",
                resume_token_ttl_seconds=max(1, min(remaining_s, int(pinned.get("ttl_s") or 86400), 86400)),
            ),
            pin=pinned.get("pin"),
            fiber_id=child_id,
            output_schema=config.output_schema,
        )
        if not run.run["auth"]:
            # No recorded starter: no context either, so the child records no caller and
            # resumes with no scopes — the fail-closed path every durable run already has.
            await start
            return
        # As the starter, so `record_caller` records them on the child, clamped to the run's
        # deadline through `RUN_NOT_AFTER_EXTRA`.
        context = RequestContext(
            settings=run.settings,
            auth=auth_from_state(run.tenant_id, run.run["auth"]),
            manifest_id=config.manifest,
            thread_id=thread,
        )
        context.extras[RUN_NOT_AFTER_EXTRA] = int(run.run["expires_at"])
        async with async_run_with_context(context):
            await start

    async def poll(self, run: RunContext, step: Step, state: StepState) -> None:
        from felix.durability.fibers import FIBER_TERMINAL_STATUSES, get_fiber
        from felix.durability.runs import run_view

        child = await get_fiber(run.settings, run.tenant_id, state.get("fiber_id", ""))
        if child is None:
            _fail(state, "the step's agent run is gone")
            return
        if child["status"] not in FIBER_TERMINAL_STATUSES:
            return
        view = run_view(child)
        if child["status"] != "completed" or view["error"]:
            # The child's own error is an exception's text: logged, never returned.
            logger.warning(
                "workflow run %s step %s: agent run %s ended %s: %s",
                run.run["id"],
                step.id,
                child["id"],
                child["status"],
                loggable(view["error"], limit=500),
            )
            _fail(state, _child_failure("agent run", child["status"]))
            return
        answer = str((view["final"] or {}).get("content") or "")
        config = AgentTurnConfig.model_validate(step.config)
        if config.output_schema is None:
            state.update(status="completed", outputs={"text": answer}, finished_at=_now())
            return
        parsed = _structured(answer, config.output_schema)
        if parsed is None:
            _fail(state, "the answer was not a JSON object with the fields its output_schema requires")
            return
        fields = {k: parsed[k] for k in config.output_schema["properties"] if k in parsed}
        state.update(status="completed", outputs={"output": parsed, **fields}, finished_at=_now())

    async def stop(self, run: RunContext, step: Step, state: StepState) -> None:
        await _stop_child(run, step, state)


class _ToolCall:
    async def start(self, run: RunContext, step: Step, inputs: dict[str, Any], state: StepState) -> None:
        from felix.durability.fibers import RunInProgress, create_fiber, get_fiber
        from felix.thread_ids import workflow_thread_id
        from felix.workflows.tool_step import TOOL_CHILD_KIND

        config = ToolCallConfig.model_validate(step.config)
        thread = workflow_thread_id(run.tenant_id, run.run["id"], step.id)
        if thread is None:
            _fail(state, "no thread id for this step")
            return
        child_id = child_fiber_id(run, step)
        state.update(thread_id=thread, fiber_id=child_id)
        if await get_fiber(run.settings, run.tenant_id, child_id) is not None:
            state["status"] = "running"  # a replay: an earlier attempt started it
            return
        pinned = run.run["pins"].get(config.manifest) or {}
        ttl_ms = int(pinned.get("ttl_s") or 86400) * 1000
        child_state: dict[str, Any] = {
            "steps": [
                {
                    "op": "tool",
                    "manifest_id": config.manifest,
                    "tool": config.tool,
                    # Fixed arguments, then what the edges carried in; an unwired optional input
                    # is simply not sent.
                    "args": {**config.args, **inputs},
                    "input_schema": config.input_schema,
                    "thread_id": thread,
                    "step_id": step.id,
                }
            ],
            "cursor": 0,
            "stash": {},
            "expires_at": min(int(run.run["expires_at"]), _now() + ttl_ms),
        }
        if run.run["auth"]:
            child_state["auth"] = run.run["auth"]  # the starter, as recorded when the run began
        if pinned.get("pin"):
            child_state["pin"] = pinned["pin"]
        try:
            await create_fiber(
                run.settings,
                run.tenant_id,
                kind=TOOL_CHILD_KIND,
                state=child_state,
                thread_id=thread,
                exclusive_on_thread=True,
                fiber_id=child_id,
            )
        except RunInProgress as busy:
            if busy.resume_token == child_id:
                state["status"] = "running"
                return
            _fail(state, "the step's thread already has a run")
            return
        state["status"] = "running"

    async def poll(self, run: RunContext, step: Step, state: StepState) -> None:
        from felix.durability.fibers import FIBER_TERMINAL_STATUSES, get_fiber

        child = await get_fiber(run.settings, run.tenant_id, state.get("fiber_id", ""))
        if child is None:
            _fail(state, "the step's tool call is gone")
            return
        if child["status"] not in FIBER_TERMINAL_STATUSES:
            return
        last = dict(((child.get("state_json") or {}).get("stash") or {}).get("last") or {})
        if child["status"] != "completed" or last.get("error"):
            # `tool_step` writes these for a client; a status with no error of its own is mapped.
            _fail(state, str(last.get("error") or _child_failure("tool call", child["status"])))
            return
        state.update(status="completed", outputs={"result": last.get("output")}, finished_at=_now())

    async def stop(self, run: RunContext, step: Step, state: StepState) -> None:
        await _stop_child(run, step, state)


def _child_failure(what: str, status: str) -> str:
    """What a client reads when a step's child run ended without a usable result."""
    return {"expired": f"the {what} expired", "dead": f"the {what} could not be completed"}.get(
        status, f"the {what} failed"
    )


EXECUTORS: dict[str, Executor] = {
    "agent_turn": _AgentTurn(),
    "approval": _Approval(),
    "branch": _Branch(),
    "tool_call": _ToolCall(),
}
# The kinds this engine can run. A registered kind with no executor (a plugin's) is refused at
# start rather than stalling the run on it.
RUNNABLE_KINDS = frozenset(EXECUTORS)


def _structured(answer: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    """The answer as the JSON object `schema` asks for, or None. Tolerates a fenced code block."""
    text = answer.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    required = schema.get("required")
    if isinstance(required, list) and any(isinstance(k, str) and k not in parsed for k in required):
        return None
    return parsed


def _kind_of(value: Any) -> str:
    if value is None:
        return "nothing"
    return type(value).__name__


__all__ = [
    "APPROVAL_DEFAULT_TTL_SECONDS",
    "EXECUTORS",
    "RUNNABLE_KINDS",
    "Executor",
    "RunContext",
    "child_fiber_id",
]
