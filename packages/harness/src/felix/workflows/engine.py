"""Running a workflow: start a run, and advance it one look at a time.

A run is a row (`runs.py`) and an orchestrator fiber whose one op, `workflow`, calls `advance`.
Each call reads the run, polls the steps in flight, starts every step that has become ready, and
saves the run under its compare-and-set; then the fiber parks for `POLL_MS` and looks again. The
orchestrator never blocks on a step. What a step *does* is its kind's executor (`executors.py`):

- `agent_turn` starts a **child durable run** on its own thread (`thread_ids.workflow_thread_id`)
  as the person who started the workflow, against the manifest **pinned when the run started**,
  bounded by the run's deadline and by that manifest's own execution TTL;
- `approval` opens a pending approval row and completes when someone decides it;
- `branch` completes at once, firing the control port its input selects.

**When a step runs.** Once every step it reads from — through data edges or `when` — is finished.
It is *skipped* instead when a data input's source was skipped or did not produce that port, or
when it has `when` edges and none of their control ports fired. A failed step fails the run:
whatever is in flight is stopped and nothing else starts. At most `MAX_CHILD_STEPS_IN_FLIGHT`
steps of one run that start a child run (agent and tool steps) are in flight at once; the rest
wait their turn.

**Running twice.** A claim can replay `advance` after its save was lost, and two claims can
overlap, so every start is idempotent: a child's fiber id is derived from the run and step and
found before it is created again (and a `RunInProgress` naming it is adopted), and an approval row
is found by its call signature in any status before another is opened.

**Stopping.** Cancel, expiry and failure stop every step not yet finished, started or not: a
child fiber is ended by id (`fibers.stop_fiber`, so one not yet claimed never runs) and its turn
told to abort, and an approval row is closed so its card stops asking. An orchestrator fiber that
is buried fails its run (`orchestrator_ended`) rather than leaving it running.

**Authority.** The starter is recorded as `start_durable_chat` records a caller (`record_caller`),
and may only start a run of manifests whose inbound rules admit them. Each such manifest is pinned
then (`pin_fields_for`); a child run carrying the starter's scopes refuses to run a version
published since (`pin_compile` is forced on resume). The run's deadline — `RUN_TTL_SECONDS`,
clamped to the starter's token — bounds every child and approval.

Step errors a client reads are written here, never an exception's text; the detail is logged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from felix.config import Settings
from felix.logging_setup import loggable
from felix.workflows import runs as run_store
from felix.workflows.check import Problem, check
from felix.workflows.executors import EXECUTORS, RUNNABLE_KINDS
from felix.workflows.ports import WHEN
from felix.workflows.runs import STEP_IN_FLIGHT, STEP_TERMINAL, StepState, StepStatus
from felix.workflows.schema import TRIGGER, Step, WorkflowSpec

logger = logging.getLogger("felix.workflows.engine")

# How long the orchestrator sleeps between looks while anything is in flight.
POLL_MS = 2000
# A run's longest life, before the starter's own token clamps it.
RUN_TTL_SECONDS = 24 * 3600
# How many of one run's child-run steps (agent and tool) may be in flight at once, and how many
# runs a tenant may have going. One request otherwise fans out to as many fibers as the definition
# has steps (200), each holding a worker slot — a tool waiting on an approval holds it for the ttl.
MAX_CHILD_STEPS_IN_FLIGHT = 8
MAX_ACTIVE_RUNS_PER_TENANT = 50
ORCHESTRATOR_KIND = "workflow_run"
# The kinds whose config names a manifest the step runs as the starter, each in a child run.
_MANIFEST_KINDS = frozenset({"agent_turn", "tool_call"})
_CHILD_RUN_KINDS = _MANIFEST_KINDS


class NotRunnable(Exception):
    """The definition has problems, or names something this run cannot use; nothing was started."""

    def __init__(self, problems: list[Problem]) -> None:
        super().__init__("workflow is not runnable")
        self.problems = problems


class BadInputs(Exception):
    """The inputs do not match the workflow's: `missing` declared ones absent, `unknown` extra."""

    def __init__(self, missing: list[str], unknown: list[str]) -> None:
        super().__init__("inputs do not match the workflow")
        self.missing = missing
        self.unknown = unknown


class NotPermitted(Exception):
    """The starter may not invoke a manifest a step names (its inbound rules refuse them)."""

    def __init__(self, manifest: str, status_code: int) -> None:
        super().__init__(f"may not run manifest {manifest!r}")
        self.manifest = manifest
        self.status_code = status_code


class TooManyRuns(Exception):
    """The tenant already has `MAX_ACTIVE_RUNS_PER_TENANT` runs going."""


def _now() -> int:
    from felix.durability.fibers import now_ms

    return now_ms()


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the orchestrator fiber does next: finish (with `error` when the run did not complete)
    or park for `delay_ms`."""

    finished: bool
    error: str = ""
    delay_ms: int = POLL_MS


# --- starting and stopping runs ---------------------------------------------------------------


def run_problems(spec: WorkflowSpec) -> list[Problem]:
    """Everything that stops `spec` running here: its graph problems, then kinds with no executor."""
    _, found = check(spec)
    for step in spec.steps:
        if step.kind not in RUNNABLE_KINDS:
            found.append(Problem("not_runnable", f"steps of kind {step.kind!r} cannot run yet", step=step.id))
    return found


def check_inputs(spec: WorkflowSpec, inputs: dict[str, Any]) -> None:
    """Every declared input present and nothing else; raises `BadInputs`. Types are the steps'."""
    missing = sorted(set(spec.inputs) - set(inputs))
    unknown = sorted(set(inputs) - set(spec.inputs))
    if missing or unknown:
        raise BadInputs(missing, unknown)


async def _pin_manifests(settings: Settings, tenant_id: str, spec: WorkflowSpec) -> dict[str, Any]:
    """Each manifest an agent or tool step names, resolved, checked against the starter, and pinned.

    Raises `NotRunnable` for one that does not resolve and `NotPermitted` for one whose inbound
    rules refuse the caller in context. With no caller in context (no request), nothing is
    refused here; the child enforces the manifest's rules when it runs.
    """
    from felix.context import try_get_context
    from felix.durability.runs import ttl_seconds
    from felix.manifests.inbound_auth import InboundAuthError, enforce_inbound_auth
    from felix.manifests.pin import pin_fields_for
    from felix.runtime import resolve_tenant_manifest

    caller = try_get_context()
    pins: dict[str, Any] = {}
    unresolved: list[Problem] = []
    for step in spec.steps:
        if step.kind not in _MANIFEST_KINDS:
            continue
        name = str(step.config["manifest"])  # validated by `run_problems` through the kind's config
        if name in pins:
            continue
        try:
            resolved = await resolve_tenant_manifest(settings, tenant_id, name)
        except Exception:
            logger.warning(
                "workflow start: manifest %s did not resolve", loggable(name, limit=200), exc_info=True
            )
            unresolved.append(Problem("not_runnable", f"no manifest named {name!r}", step=step.id))
            continue
        if caller is not None:
            try:
                enforce_inbound_auth(resolved.manifest, caller.auth)
            except InboundAuthError as refused:
                raise NotPermitted(name, int(getattr(refused, "status_code", 403) or 403)) from None
        pins[name] = {
            "pin": await pin_fields_for(settings, tenant_id, resolved.manifest, version=resolved.version),
            "ttl_s": ttl_seconds(settings, resolved.manifest.spec.execution),
        }
    if unresolved:
        raise NotRunnable(unresolved)
    return pins


async def start_run(
    settings: Settings,
    tenant_id: str,
    workflow: dict[str, Any],
    inputs: dict[str, Any],
    *,
    trigger: str,
    started_by: str,
) -> dict[str, Any]:
    """Start a run of a stored workflow (a `store.get_workflow` row) and its orchestrator fiber.

    The caller in context is recorded as the run's starter, and `started_by` defaults to them.
    Raises `NotRunnable`, `BadInputs`, `NotPermitted` or `TooManyRuns` before anything is written.
    """
    from felix.durability.runs import record_caller

    spec = WorkflowSpec.model_validate(workflow["definition"])
    problems = run_problems(spec)
    if problems:
        raise NotRunnable(problems)
    check_inputs(spec, inputs)
    pins = await _pin_manifests(settings, tenant_id, spec)
    # A soft cap: counted, not locked, so starts racing at the limit can pass it by a few.
    if await run_store.count_active(settings, tenant_id) >= MAX_ACTIVE_RUNS_PER_TENANT:
        raise TooManyRuns()
    auth, expires_at = record_caller(tenant_id, _now() + RUN_TTL_SECONDS * 1000)
    run = await run_store.create_run(
        settings,
        tenant_id,
        run_store.NewRun(
            workflow_id=workflow["id"],
            revision=workflow["revision"],
            definition=workflow["definition"],
            inputs=inputs,
            steps={step.id: {"status": "pending"} for step in spec.steps},
            auth=auth,
            pins=pins,
            expires_at=expires_at,
            trigger=trigger,
            started_by=str(auth.get("principal_sub") or started_by),
        ),
    )
    try:
        fiber = await _create_orchestrator(settings, tenant_id, run["id"], expires_at)
    except Exception:
        logger.exception("workflow run %s: the orchestrator fiber was not created", run["id"])
        return await _update(
            settings, tenant_id, run["id"], status="failed", error="the run could not be scheduled"
        )
    return await _update(settings, tenant_id, run["id"], fiber_id=fiber["id"])


async def _create_orchestrator(
    settings: Settings, tenant_id: str, run_id: str, expires_at: int
) -> dict[str, Any]:
    from felix.durability.fibers import create_fiber

    return await create_fiber(
        settings,
        tenant_id,
        kind=ORCHESTRATOR_KIND,
        state={
            "steps": [{"op": "workflow", "run_id": run_id}],
            "cursor": 0,
            "stash": {},
            # Past the run's own deadline, which `advance` enforces itself so it can stop what
            # is in flight; the fiber's would end it without telling the run.
            "expires_at": expires_at + 10 * 60 * 1000,
        },
    )


async def _update(settings: Settings, tenant_id: str, run_id: str, **fields: Any) -> dict[str, Any]:
    """Set `fields` on a run, re-reading after a lost save (the orchestrator may have moved it)."""
    for _ in range(5):
        run = await run_store.get_run(settings, tenant_id, run_id)
        if run is None:
            break
        if "status" in fields and run["status"] in run_store.RUN_TERMINAL:
            return run
        run.update(fields)
        if await run_store.save_run(settings, run):
            return run
    current = await run_store.get_run(settings, tenant_id, run_id)
    if current is None:
        raise LookupError(run_id)
    return current


async def request_cancel(settings: Settings, tenant_id: str, run_id: str) -> dict[str, Any] | None:
    """Ask a run to stop; the orchestrator stops what is in flight on its next look.

    Returns the run as saved, or as it stands when it was already over; `None` when there is none.
    """
    for _ in range(5):
        run = await run_store.get_run(settings, tenant_id, run_id)
        if run is None or run["status"] in run_store.RUN_TERMINAL or run["status"] == "cancelling":
            return run
        run["status"] = "cancelling"
        if await run_store.save_run(settings, run):
            return run
    return await run_store.get_run(settings, tenant_id, run_id)


async def orchestrator_ended(settings: Settings, fiber: dict[str, Any]) -> None:
    """The orchestrator fiber reached a terminal status: a run it left unfinished fails. Never raises."""
    try:
        run_id = str(((fiber.get("state_json") or {}).get("steps") or [{}])[0].get("run_id") or "")
        for _ in range(5):
            run = await run_store.get_run(settings, fiber["tenant_id"], run_id)
            if run is None or run["status"] in run_store.RUN_TERMINAL:
                return
            await _Advance(settings, run).stop_everything()
            run["status"], run["error"] = "failed", "the run stopped: its orchestrator failed"
            if await run_store.save_run(settings, run):
                return
    except Exception:
        logger.exception("workflow orchestrator %s ended; its run could not be closed", fiber.get("id"))


async def advance(settings: Settings, tenant_id: str, run_id: str) -> Outcome:
    """One look at a run: poll, start what is ready, save. Called by the orchestrator fiber."""
    run = await run_store.get_run(settings, tenant_id, run_id)
    if run is None:
        return Outcome(finished=True, error="workflow run not found")
    if run["status"] in run_store.RUN_TERMINAL:
        return _finished(run)
    before = json.dumps([run["status"], run["error"], run["steps"]], sort_keys=True, default=str)
    await _Advance(settings, run).go()
    after = json.dumps([run["status"], run["error"], run["steps"]], sort_keys=True, default=str)
    # A look that changed nothing writes nothing: most looks are a step still in flight.
    if after != before and not await run_store.save_run(settings, run):
        # Another look moved it first. If that ended the run, anything this look started is
        # stopped now: the run that won will never look again.
        current = await run_store.get_run(settings, tenant_id, run_id)
        if current is not None and current["status"] in run_store.RUN_TERMINAL:
            await _Advance(settings, current).stop_everything()
            return _finished(current)
        return Outcome(finished=False, delay_ms=0)
    if run["status"] in run_store.RUN_TERMINAL:
        return _finished(run)
    return Outcome(finished=False)


def _finished(run: dict[str, Any]) -> Outcome:
    failed = run["status"] in {"failed", "expired"}
    return Outcome(finished=True, error=(run["error"] or run["status"]) if failed else "")


class _Advance:
    """One `advance` over one run row, edited in place and saved by the caller."""

    def __init__(self, settings: Settings, run: dict[str, Any]) -> None:
        self.settings = settings
        self.run = run
        self.tenant_id: str = run["tenant_id"]
        self.spec = WorkflowSpec.model_validate(run["definition"])
        self.steps: dict[str, StepState] = run["steps"]
        self.by_id = {step.id: step for step in self.spec.steps}

    async def go(self) -> None:
        if self.run["status"] == "cancelling":
            await self.stop_everything()
            self.run["status"] = "cancelled"
            return
        if _now() > int(self.run["expires_at"]):
            await self.stop_everything()
            self.run["status"], self.run["error"] = "expired", "the run passed its deadline"
            return
        for step_id, state in self.steps.items():
            if state["status"] in STEP_IN_FLIGHT:
                await EXECUTORS[self.by_id[step_id].kind].poll(self, self.by_id[step_id], state)
        await self._start_ready()
        if self._failed() is not None:
            # Fail fast: whatever is in flight is stopped and nothing else starts.
            await self.stop_everything(not_started="skipped")
        self._settle()

    async def _start_ready(self) -> None:
        changed = True
        while changed and self._failed() is None:
            changed = False
            for step in self._ordered():
                state = self.steps[step.id]
                if state["status"] != "pending":
                    continue
                readiness = self._readiness(step)
                if readiness == "skip":
                    state.update(status="skipped", finished_at=_now())
                    changed = True
                elif readiness == "ready" and not self._at_child_limit(step):
                    await self._start_step(step, state)
                    changed = changed or state["status"] in STEP_TERMINAL

    async def _start_step(self, step: Step, state: StepState) -> None:
        inputs = self._inputs(step)
        state.update(inputs=inputs, started_at=_now())
        try:
            await EXECUTORS[step.kind].start(self, step, inputs, state)
        except Exception:
            logger.exception("workflow run %s: step %s did not start", self.run["id"], step.id)
            state.update(status="failed", error="the step could not be started", finished_at=_now())

    def _at_child_limit(self, step: Step) -> bool:
        if step.kind not in _CHILD_RUN_KINDS:
            return False
        running = sum(
            1
            for sid, s in self.steps.items()
            if s["status"] == "running" and self.by_id[sid].kind in _CHILD_RUN_KINDS
        )
        return running >= MAX_CHILD_STEPS_IN_FLIGHT

    async def stop_everything(self, *, not_started: StepStatus = "cancelled") -> None:
        """Stop every step not yet finished; for cancel, expiry, failure and a buried orchestrator."""
        for step_id, state in self.steps.items():
            if state["status"] in STEP_TERMINAL:
                continue
            step = self.by_id[step_id]
            try:
                await EXECUTORS[step.kind].stop(self, step, state)
            except Exception:
                logger.exception("workflow run %s: step %s did not stop cleanly", self.run["id"], step_id)
            in_flight = state["status"] in STEP_IN_FLIGHT
            state.update(status="cancelled" if in_flight else not_started, finished_at=_now())

    def _settle(self) -> None:
        failed = self._failed()
        if failed is not None:
            self.run["status"] = "failed"
            self.run["error"] = (
                f"step {failed} failed: {self.steps[failed].get('error') or 'no reason given'}"
            )
            return
        statuses = {s["status"] for s in self.steps.values()}
        if statuses <= STEP_TERMINAL:
            self.run["status"] = "completed"
        elif "running" not in statuses and "waiting" in statuses:
            self.run["status"] = "waiting"
        else:
            self.run["status"] = "running"

    def _failed(self) -> str | None:
        return next((sid for sid, s in self.steps.items() if s["status"] == "failed"), None)

    def _ordered(self) -> list[Step]:
        """Steps in an order where each comes after everything it reads from."""
        upstream: dict[str, set[str]] = {step.id: set() for step in self.spec.steps}
        for edge in self.spec.edges:
            if edge.source.step != TRIGGER:
                upstream[edge.target.step].add(edge.source.step)
        ordered: list[Step] = []
        done: set[str] = set()
        while len(ordered) < len(self.spec.steps):
            ready = [s for s in self.spec.steps if s.id not in done and upstream[s.id] <= done]
            if not ready:  # cannot happen: a run starts only on a checked, acyclic definition
                break
            ordered.extend(ready)
            done.update(s.id for s in ready)
        return ordered

    def _readiness(self, step: Step) -> str:
        inbound = [e for e in self.spec.edges if e.target.step == step.id]
        for edge in inbound:
            if edge.source.step != TRIGGER and self.steps[edge.source.step]["status"] not in STEP_TERMINAL:
                return "wait"
        control = [e for e in inbound if e.target.port == WHEN]
        for edge in inbound:
            if edge in control or edge.source.step == TRIGGER:
                continue
            source = self.steps[edge.source.step]
            if source["status"] != "completed" or edge.source.port not in source.get("outputs", {}):
                return "skip"
        if control and not any(
            self.steps[e.source.step]["status"] == "completed"
            and e.source.port in self.steps[e.source.step].get("fired", [])
            for e in control
        ):
            return "skip"
        return "ready"

    def _inputs(self, step: Step) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for edge in self.spec.edges:
            if edge.target.step != step.id or edge.target.port == WHEN:
                continue
            if edge.source.step == TRIGGER:
                values[edge.target.port] = self.run["inputs"].get(edge.source.port)
            else:
                values[edge.target.port] = self.steps[edge.source.step]["outputs"][edge.source.port]
        return values


__all__ = [
    "MAX_ACTIVE_RUNS_PER_TENANT",
    "MAX_CHILD_STEPS_IN_FLIGHT",
    "ORCHESTRATOR_KIND",
    "POLL_MS",
    "RUNNABLE_KINDS",
    "RUN_TTL_SECONDS",
    "BadInputs",
    "NotPermitted",
    "NotRunnable",
    "Outcome",
    "TooManyRuns",
    "advance",
    "check_inputs",
    "orchestrator_ended",
    "request_cancel",
    "run_problems",
    "start_run",
]
