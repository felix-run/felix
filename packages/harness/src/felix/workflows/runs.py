"""Workflow run rows: the definition a run started on, its inputs, and each step's state.

The engine (`engine.py`) is the only writer after creation, and writes through `save_run`, a
compare-and-set on `version`: a lost save means another advance already moved the run, and the
caller re-reads rather than overwriting it. Cancelling is the one write from outside the engine,
and it goes through the same compare-and-set.

`auth` is the caller who started the run, recorded the way `start_durable_chat` records one, so
each agent step's child run starts as that caller; `pins` is each manifest the steps name, pinned
when the run started. Neither is ever part of `workflow_run_view`.

The statuses and the step-state shape are defined here, once: the engine writes them and the
routes declare them from these types.
"""

from __future__ import annotations

import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, Required, TypedDict

from sqlalchemy import collate, func, select, update

from felix.config import Settings
from felix.db.models import WorkflowRun
from felix.db.session import _use_memory, tenant_session

now_ms = lambda: int(time.time() * 1000)

MAX_RUN_LIST = 200

RunStatus = Literal[
    "pending", "running", "waiting", "cancelling", "completed", "failed", "cancelled", "expired"
]
StepStatus = Literal["pending", "running", "waiting", "completed", "failed", "skipped", "cancelled"]
# A run's own terminal statuses: never advanced again.
RUN_TERMINAL: frozenset[RunStatus] = frozenset({"completed", "failed", "cancelled", "expired"})
STEP_IN_FLIGHT: frozenset[StepStatus] = frozenset({"running", "waiting"})
STEP_TERMINAL: frozenset[StepStatus] = frozenset({"completed", "failed", "skipped", "cancelled"})


class StepState(TypedDict, total=False):
    """One step's state in a run. Which keys are set depends on the kind and how far it got."""

    status: Required[StepStatus]
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    # The control ports it fired: a branch's case, an approval's `approved` or `denied`.
    fired: list[str]
    error: str
    # An agent step's child run and the thread its transcript is on.
    thread_id: str
    fiber_id: str
    # An approval step's request, and who decided it.
    approval_id: str
    decided_by: str
    started_at: int
    finished_at: int


_memory_runs: dict[tuple[str, str], dict[str, Any]] = {}

_COLUMNS = (
    "tenant_id",
    "id",
    "workflow_id",
    "revision",
    "status",
    "trigger",
    "started_by",
    "fiber_id",
    "error",
    "expires_at",
    "version",
    "created_at",
    "updated_at",
    "finished_at",
)
_JSON = {
    "definition": "definition_json",
    "inputs": "inputs_json",
    "steps": "steps_json",
    "auth": "auth_json",
    "pins": "pins_json",
    "untrusted_inputs": "untrusted_json",
}
# What `save_run` may change; everything else is fixed when the run is created.
_MUTABLE = ("status", "steps", "fiber_id", "error", "finished_at", "version", "updated_at")
# What a save that ends the run also clears.
_ENDED = ("auth", "pins")


def reset_workflow_runs_for_tests() -> None:
    """Clear the in-memory runs."""
    _memory_runs.clear()


def _run_dict(row: WorkflowRun | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        # A copy all the way down: the engine edits the steps it read before saving them.
        return deepcopy(row)
    out = {c: getattr(row, c) for c in _COLUMNS}
    out.update({key: getattr(row, column) for key, column in _JSON.items()})
    return out


@dataclass(frozen=True, slots=True)
class NewRun:
    """What a run starts from; the engine builds one (`engine.start_run`)."""

    workflow_id: str
    revision: str
    definition: dict[str, Any]
    inputs: dict[str, Any]
    steps: dict[str, StepState]
    auth: dict[str, Any]
    pins: dict[str, Any]
    expires_at: int
    trigger: str
    started_by: str
    # The inputs a third party supplied: an agent step reads every value it is given as data.
    untrusted_inputs: tuple[str, ...] = ()
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)


async def create_run(settings: Settings, tenant_id: str, new: NewRun) -> dict[str, Any]:
    ts = now_ms()
    row: dict[str, Any] = {
        "tenant_id": tenant_id,
        "id": new.run_id,
        "workflow_id": new.workflow_id,
        "revision": new.revision,
        "status": "pending",
        "trigger": new.trigger,
        "started_by": new.started_by,
        "fiber_id": "",
        "error": "",
        "expires_at": new.expires_at,
        "version": 1,
        "created_at": ts,
        "updated_at": ts,
        "finished_at": None,
        "definition": new.definition,
        "inputs": new.inputs,
        "steps": new.steps,
        "auth": new.auth,
        "pins": new.pins,
        "untrusted_inputs": list(new.untrusted_inputs),
    }
    if _use_memory(settings):
        _memory_runs[(tenant_id, row["id"])] = deepcopy(row)
        return _run_dict(row)

    async with tenant_session(settings, tenant_id) as db:
        db.add(
            WorkflowRun(
                **{c: row[c] for c in _COLUMNS},
                **{column: row[key] for key, column in _JSON.items()},
            )
        )
        await db.commit()
    return _run_dict(row)


async def get_run(settings: Settings, tenant_id: str, run_id: str) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_runs.get((tenant_id, run_id))
        return _run_dict(row) if row else None

    async with tenant_session(settings, tenant_id) as db:
        found = await db.get(WorkflowRun, (tenant_id, run_id))
        return _run_dict(found) if found else None


async def list_runs(
    settings: Settings, tenant_id: str, workflow_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    """One workflow's runs, newest first, ending on the id in byte order."""
    limit = max(1, min(limit, MAX_RUN_LIST))
    if _use_memory(settings):
        items = [
            _run_dict(r)
            for (t, _), r in _memory_runs.items()
            if t == tenant_id and r["workflow_id"] == workflow_id
        ]
        items.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return items[:limit]

    async with tenant_session(settings, tenant_id) as db:
        stmt = (
            select(WorkflowRun)
            .where(WorkflowRun.tenant_id == tenant_id, WorkflowRun.workflow_id == workflow_id)
            .order_by(WorkflowRun.created_at.desc(), collate(WorkflowRun.id, "C").desc())
            .limit(limit)
        )
        return [_run_dict(r) for r in (await db.scalars(stmt)).all()]


async def save_run(settings: Settings, row: dict[str, Any]) -> bool:
    """Write `row` over the version it was read at; False, writing nothing, if that moved.

    On success `row["version"]` and `row["updated_at"]` are advanced in place, so a caller that
    keeps the dict can save it again.
    """
    tenant_id, run_id, read_at = row["tenant_id"], row["id"], int(row["version"])
    ts = max(now_ms(), int(row["updated_at"]) + 1)
    ended = row["status"] in RUN_TERMINAL
    if ended and row.get("finished_at") is None:
        row["finished_at"] = ts
    if ended:
        # The starter's authority and the pins it was checked against start no more steps once
        # the run is over; they are not kept for as long as the run's record is.
        row["auth"], row["pins"] = {}, {}

    if _use_memory(settings):
        current = _memory_runs.get((tenant_id, run_id))
        if current is None or current["version"] != read_at:
            return False
        row["version"], row["updated_at"] = read_at + 1, ts
        # Only what Postgres writes: the twin must not persist an edit the real store drops.
        current.update(deepcopy({k: row.get(k) for k in (*_MUTABLE, *(_ENDED if ended else ()))}))
        await _announce(tenant_id, run_id)
        return True

    async with tenant_session(settings, tenant_id) as db:
        result = await db.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.tenant_id == tenant_id,
                WorkflowRun.id == run_id,
                WorkflowRun.version == read_at,
            )
            .values(
                status=row["status"],
                steps_json=row["steps"],
                fiber_id=row["fiber_id"],
                error=row["error"],
                finished_at=row.get("finished_at"),
                version=read_at + 1,
                updated_at=ts,
                **({"auth_json": {}, "pins_json": {}} if ended else {}),
            )
        )
        await db.commit()
        if not result.rowcount:
            return False
    row["version"], row["updated_at"] = read_at + 1, ts
    await _announce(tenant_id, run_id)
    return True


async def _announce(tenant_id: str, run_id: str) -> None:
    """Wake whoever watches this run (`GET /workflows/{id}/runs/{run}/stream`).

    On the run's key (`thread_ids.workflow_run_key`), the channel its approval rows already wake,
    so a gate opening or a step moving reaches a watcher on another replica as it lands. Best
    effort, as every notification is: a watcher polls behind it.
    """
    from felix.session.notify import notify_appended
    from felix.thread_ids import workflow_run_key

    key = workflow_run_key(tenant_id, run_id)
    if key:
        await notify_appended(tenant_id, key)


async def count_active(settings: Settings, tenant_id: str) -> int:
    """How many of a tenant's runs are not yet over."""
    if _use_memory(settings):
        return sum(
            1 for (t, _), r in _memory_runs.items() if t == tenant_id and r["status"] not in RUN_TERMINAL
        )
    async with tenant_session(settings, tenant_id) as db:
        stmt = (
            select(func.count())
            .select_from(WorkflowRun)
            .where(WorkflowRun.tenant_id == tenant_id, WorkflowRun.status.not_in(sorted(RUN_TERMINAL)))
        )
        return int(await db.scalar(stmt) or 0)


# A run's `trigger` when a signed trigger started it: `trigger:<id>`.
TRIGGER_RUN_PREFIX = "trigger:"


def fences_prompts(run: dict[str, Any]) -> bool:
    """Whether the run's agent steps read every value they are given as fenced data: a third party
    supplied some of its inputs, or started it at all -- a run whose mapped inputs were all
    numbers can still fetch the sender's text in a tool step and hand it to an agent."""
    return bool(run.get("untrusted_inputs")) or str(run.get("trigger") or "").startswith(TRIGGER_RUN_PREFIX)


def workflow_run_view(row: dict[str, Any]) -> dict[str, Any]:
    """A run as a client sees it: everything but the recorded caller, the pins and the definition."""
    return {k: row[k] for k in (*_COLUMNS, "inputs", "steps", "untrusted_inputs") if k != "tenant_id"}


__all__ = [
    "MAX_RUN_LIST",
    "RUN_TERMINAL",
    "STEP_IN_FLIGHT",
    "STEP_TERMINAL",
    "TRIGGER_RUN_PREFIX",
    "NewRun",
    "RunStatus",
    "StepState",
    "StepStatus",
    "count_active",
    "create_run",
    "fences_prompts",
    "get_run",
    "list_runs",
    "reset_workflow_runs_for_tests",
    "save_run",
    "workflow_run_view",
]
