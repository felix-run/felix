"""A scheduled job's own state: a few short text values its runs leave the next one.

What a scheduled agent keeps between firings -- a bookmark per source ("read up to item 4182"), a
ledger of what it has already done -- read back by key, whole. Per-person memory was the nearest
thing before, and a firing recalls it by rank, so a bookmark could be missed; the job thread's
workspace held the rest by convention.

One row per tenant, job and key (`job_state`, migration `0053`). The state belongs to the **job**,
not to the manifest it runs: two jobs on one manifest keep their own. Only the job's runs write it
(`state_set`, `felix.jobs.state_tools`); a person reads and clears it over `/jobs/{name}/state`.
Deleting the job deletes it, and so does a re-save by another writer (`jobs/store.py:put_job`),
since what the old writer's runs stored could otherwise carry text into the new writer's runs.

Every write a run makes is checked, in one step, against the job as it is now: the job must still
exist and still be held as the firing read it (`RunHolder`: the same writer, and no clear of its state
since -- `state_cleared_at`, which a re-save by another writer moves even when the two share one
credential's subject), so a firing still going on when the job was deleted or taken over cannot leave
rows behind for whoever holds the name next. A value never reaches a log line: the tools log an
error's class only, since a driver error renders its statement's parameters. On Postgres
that step is a lock on the job's row (`FOR NO KEY UPDATE`), which also serialises every write to one
job's state -- so `max_keys` is exact and a compare-and-set (`expected`) cannot lose an update to an
overlapping firing. `put_job` and `delete_job` take the same row lock before they clear the state,
so a write lands either before the clear (and is cleared) or after it (and is refused). On the
memory arm there is no await between the check and the write.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from felix.config import Settings
from felix.db.models import Job, JobStateRow
from felix.db.session import _use_memory, tenant_session

now_ms = lambda: int(time.time() * 1000)

#: A key: short, lowercase, and safe to name in a prompt, a log line or a URL path.
KEY_PATTERN = r"^[a-z0-9][a-z0-9._:-]{0,63}$"
# `\Z`, not `$`, which also matches before a trailing newline.
_KEY_RE = re.compile(KEY_PATTERN.removesuffix("$") + r"\Z")
MAX_KEY_CHARS = 64

_memory_state: dict[tuple[str, str, str], dict[str, Any]] = {}


class _Unset:
    """No `expected` was given: the write is unconditional. A sentinel, since None is a value
    `expected` takes ("must not exist")."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()

SetStatus = Literal["saved", "job_changed", "too_many_keys", "conflict"]


@dataclass(frozen=True, slots=True)
class RunHolder:
    """Who held the job when the firing read it: its writer and its state's last clear. A run's write
    lands only while the job is still held exactly so (`holds`)."""

    writer: str | None
    state_cleared_at: int | None = None

    def holds(self, current: tuple[str | None, int | None] | None) -> bool:
        """Whether `current` -- the job's `(created_by, state_cleared_at)` now, None when it is gone --
        is still this holder."""
        return current is not None and current == (self.writer, self.state_cleared_at)


@dataclass(frozen=True, slots=True)
class SetResult:
    """What a write did. `saved` with `created` when the key is new. `job_changed`: the job is gone,
    or is now someone else's. `too_many_keys`: a new key past the cap. `conflict`: `expected` did not
    match, and `current_exists` says whether the key held a value."""

    status: SetStatus
    created: bool = False
    current_exists: bool = False


def reset_job_state_for_tests() -> None:
    _memory_state.clear()


def valid_key(key: str) -> bool:
    return isinstance(key, str) and bool(_KEY_RE.match(key))


def value_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def _row_dict(row: JobStateRow | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        out = dict(row)
    else:
        out = {c.key: getattr(row, c.key) for c in JobStateRow.__table__.columns}
    out["bytes"] = value_bytes(out["value"])
    return out


def _memory_holds(tenant_id: str, job_name: str, run: RunHolder) -> bool:
    from felix.jobs.store import memory_job_holder

    return run.holds(memory_job_holder(tenant_id, job_name))


def _memory_keys(tenant_id: str, job_name: str) -> list[tuple[str, str, str]]:
    """The memory arm's keys of one job's rows."""
    return [k for k in _memory_state if k[0] == tenant_id and k[1] == job_name]


def forget_memory_job(tenant_id: str, job_name: str) -> int:
    """Drop a job's state on the memory arm, and say how many keys it held: `jobs/store.py` calls it
    where the Postgres arm deletes the rows in the job write's own transaction."""
    doomed = _memory_keys(tenant_id, job_name)
    for k in doomed:
        del _memory_state[k]
    return len(doomed)


def delete_for_job(tenant_id: str, job_name: str) -> Any:
    """The statement that deletes a job's state, for `jobs/store.py` to run in its own transaction
    -- after its write to the job's row, whose lock is what keeps a run's write from slipping past."""
    from sqlalchemy import delete

    return delete(JobStateRow).where(JobStateRow.tenant_id == tenant_id, JobStateRow.job_name == job_name)


async def get(settings: Settings, tenant_id: str, job_name: str, key: str) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_state.get((tenant_id, job_name, key))
        return _row_dict(row) if row is not None else None
    async with tenant_session(settings, tenant_id) as db:
        row = await db.get(JobStateRow, (tenant_id, job_name, key))
        return _row_dict(row) if row is not None else None


async def list_state(settings: Settings, tenant_id: str, job_name: str) -> list[dict[str, Any]]:
    """Every key the job holds, values included, ordered by key (the primary key's last part)."""
    if _use_memory(settings):
        return sorted(
            (_row_dict(_memory_state[k]) for k in _memory_keys(tenant_id, job_name)), key=lambda r: r["key"]
        )
    from sqlalchemy import collate, select

    async with tenant_session(settings, tenant_id) as db:
        result = await db.scalars(
            select(JobStateRow)
            .where(JobStateRow.tenant_id == tenant_id, JobStateRow.job_name == job_name)
            # "C", so the order is the memory arm's code-point order under any database collation.
            .order_by(collate(JobStateRow.key, "C"))
        )
        return [_row_dict(r) for r in result]


def _matches(expected: Any, current: str | None) -> bool:
    """`expected` against the key's current value: "" or None means the key must not exist."""
    if expected is None or expected == "":
        return current is None
    return current == expected


@dataclass(frozen=True, slots=True)
class StateWrite:
    """One run's write: `key` to `value`, as a firing held by `run` (the job as the firing read it),
    under the manifest's `max_keys`. With `expected` (anything but `UNSET`) only when the key's
    current value is `expected` -- "" or None meaning it must not exist. `thread_id` is the firing's,
    recorded beside the value."""

    key: str
    value: str
    run: RunHolder
    max_keys: int
    expected: Any = UNSET
    thread_id: str = ""

    def refusal(self, current: str | None, held: int) -> SetResult | None:
        """The refusal for this write against the key's current value and the keys the job holds,
        or None: the one rule both arms apply."""
        if self.expected is not UNSET and not _matches(self.expected, current):
            return SetResult("conflict", current_exists=current is not None)
        if current is None and held >= self.max_keys:
            return SetResult("too_many_keys")
        return None


async def set_value(settings: Settings, tenant_id: str, job_name: str, write: StateWrite) -> SetResult:
    """Write one key for the job, as a run of it. A new key is refused once the job holds
    `max_keys`; overwriting one never is. The caller checks the key's grammar and the value's size
    (`state_tools`); this checks everything that needs the job."""
    if _use_memory(settings):
        return _set_memory(tenant_id, job_name, write)
    return await _set_postgres(settings, tenant_id, job_name, write)


def _set_memory(tenant_id: str, job_name: str, write: StateWrite) -> SetResult:
    # Synchronous from the check to the write: the event loop makes this the atomic step.
    if not _memory_holds(tenant_id, job_name, write.run):
        return SetResult("job_changed")
    row = _memory_state.get((tenant_id, job_name, write.key))
    held = len(_memory_keys(tenant_id, job_name))
    refused = write.refusal(row["value"] if row is not None else None, held)
    if refused is not None:
        return refused
    _memory_state[(tenant_id, job_name, write.key)] = {
        "tenant_id": tenant_id,
        "job_name": job_name,
        "key": write.key,
        "value": write.value,
        "updated_at": now_ms(),
        "updated_by_thread": write.thread_id,
    }
    return SetResult("saved", created=row is None)


async def _set_postgres(settings: Settings, tenant_id: str, job_name: str, write: StateWrite) -> SetResult:
    from sqlalchemy import func, select

    async with tenant_session(settings, tenant_id) as db:
        # The job's row lock first: every write to this job's state waits here for the one before.
        if not await _lock_job(db, tenant_id, job_name, write.run):
            await db.rollback()
            return SetResult("job_changed")
        row = await db.get(JobStateRow, (tenant_id, job_name, write.key), populate_existing=True)
        held = 0
        if row is None:
            held = int(
                await db.scalar(
                    select(func.count())
                    .select_from(JobStateRow)
                    .where(JobStateRow.tenant_id == tenant_id, JobStateRow.job_name == job_name)
                )
                or 0
            )
        refused = write.refusal(row.value if row is not None else None, held)
        if refused is not None:
            await db.rollback()
            return refused
        ts = now_ms()
        if row is None:
            db.add(
                JobStateRow(
                    tenant_id=tenant_id,
                    job_name=job_name,
                    key=write.key,
                    value=write.value,
                    updated_at=ts,
                    updated_by_thread=write.thread_id,
                )
            )
        else:
            row.value, row.updated_at, row.updated_by_thread = write.value, ts, write.thread_id
        await db.commit()
        return SetResult("saved", created=row is None)


async def _lock_job(db: Any, tenant_id: str, job_name: str, run: RunHolder) -> bool:
    """Lock the job's row for this transaction and say whether `run` still holds it. Every write to
    one job's state takes this lock first, so they run one at a time; `put_job` and `delete_job`
    conflict with it too (`FOR NO KEY UPDATE` against their `UPDATE` / `DELETE`)."""
    from sqlalchemy import select

    found = (
        await db.execute(
            select(Job.created_by, Job.state_cleared_at)
            .where(Job.tenant_id == tenant_id, Job.name == job_name)
            .with_for_update(key_share=True)
        )
    ).first()
    return run.holds(None if found is None else (found[0], found[1]))


def _delete_one(tenant_id: str, job_name: str, key: str) -> Any:
    from sqlalchemy import delete

    return delete(JobStateRow).where(
        JobStateRow.tenant_id == tenant_id, JobStateRow.job_name == job_name, JobStateRow.key == key
    )


async def delete_key(settings: Settings, tenant_id: str, job_name: str, key: str) -> bool:
    """A person's delete of one key (the route checked who may): whether there was one."""
    if _use_memory(settings):
        return _memory_state.pop((tenant_id, job_name, key), None) is not None
    async with tenant_session(settings, tenant_id) as db:
        result = await db.execute(_delete_one(tenant_id, job_name, key))
        await db.commit()
        return bool(getattr(result, "rowcount", 0))


async def delete_key_as_run(
    settings: Settings, tenant_id: str, job_name: str, key: str, run: RunHolder
) -> Literal["deleted", "missing", "job_changed"]:
    """A run's delete of one key: only while `run` still holds the job, as for `set_value`."""
    if _use_memory(settings):
        if not _memory_holds(tenant_id, job_name, run):
            return "job_changed"
        return "deleted" if _memory_state.pop((tenant_id, job_name, key), None) is not None else "missing"
    async with tenant_session(settings, tenant_id) as db:
        if not await _lock_job(db, tenant_id, job_name, run):
            await db.rollback()
            return "job_changed"
        result = await db.execute(_delete_one(tenant_id, job_name, key))
        await db.commit()
        return "deleted" if getattr(result, "rowcount", 0) else "missing"


async def clear(settings: Settings, tenant_id: str, job_name: str) -> int:
    """Delete every key the job holds; how many there were."""
    if _use_memory(settings):
        return forget_memory_job(tenant_id, job_name)
    async with tenant_session(settings, tenant_id) as db:
        result = await db.execute(delete_for_job(tenant_id, job_name))
        await db.commit()
        return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "KEY_PATTERN",
    "MAX_KEY_CHARS",
    "UNSET",
    "RunHolder",
    "SetResult",
    "StateWrite",
    "clear",
    "delete_for_job",
    "delete_key",
    "delete_key_as_run",
    "forget_memory_job",
    "get",
    "list_state",
    "reset_job_state_for_tests",
    "set_value",
    "valid_key",
    "value_bytes",
]
