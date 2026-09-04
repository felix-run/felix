"""A durable chat run by Temporal must land in Postgres, not only in workflow history.

Found by running it: the workflow completed, `tctl workflow show` reported
`"status":"completed"`, and `GET /chat/runs/{resume_token}` stayed `pending` forever. The
worker had logged `fiber version conflict id=... expected=0; discarding stale write` once
per activity, which is the whole story:

  * `create_fiber` returned a row dict with no `version` key at all, so `_save_fiber`'s
    compare-and-set read `int(row.get("version") or 0)` == 0 for a row the database had
    already stored.
  * `start_durable_chat` then started the workflow *before* the save that stamps
    `backend: temporal`, and that save bumped the stored version behind the workflow's
    back — so the snapshot Temporal was carrying could never match again.

The Postgres sweeper never hit either one, because it re-reads every row it claims. Only
the Temporal path uses the returned dict directly, which is why `FELIX_DURABILITY=temporal`
appeared to work and silently persisted nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.durability.fibers import create_fiber, save_fiber

SETTINGS = Settings(database_url="memory://durable-temporal", object_store="memory")


@pytest.fixture(autouse=True)
def _clean() -> Any:
    from felix.durability.fibers import _memory_fibers

    _memory_fibers.clear()
    yield
    _memory_fibers.clear()


@pytest.mark.asyncio
async def test_create_fiber_returns_the_version_it_stored() -> None:
    """Without this the first compare-and-set is against a version that was never written."""
    fiber = await create_fiber(SETTINGS, "default", kind="durable_chat", status="pending", state={})
    assert "version" in fiber, "the returned row omits `version`, so any writer using it is stale"
    assert fiber["version"] == 0


@pytest.mark.asyncio
async def test_a_write_using_the_returned_row_is_not_discarded() -> None:
    """The exact sequence the Temporal activity performs: take the row, advance it, save."""
    from felix.durability.fibers import _memory_fibers

    fiber = await create_fiber(SETTINGS, "default", kind="durable_chat", status="pending", state={})
    fiber["status"] = "completed"
    await save_fiber(SETTINGS, fiber)

    stored = _memory_fibers[("default", fiber["id"])]
    assert stored["status"] == "completed", (
        "the write was discarded as a version conflict; a durable chat would complete in "
        "Temporal and stay `pending` to every reader of the fiber row"
    )


@pytest.mark.asyncio
async def test_the_workflow_is_started_only_after_the_row_is_final() -> None:
    """Starting first and saving second hands Temporal a snapshot that is immediately stale.

    Asserted by ordering, because the failure is invisible in any single-call test: both
    the start and the save succeed, and only their sequence decides whether every
    subsequent write from the activity is dropped.
    """
    import felix.durability.runs as runs

    calls: list[str] = []
    real_save = runs.save_fiber

    async def _save(settings: Any, row: dict[str, Any]) -> None:
        calls.append(f"save:{(row.get('state_json') or {}).get('backend')}")
        await real_save(settings, row)

    async def _start(settings: Any, row: dict[str, Any]) -> str:
        calls.append("start")
        # What the real client does: serialise the row as it stands now.
        assert (row.get("state_json") or {}).get("backend") == "temporal", (
            "the workflow was handed a row that does not yet record its own backend"
        )
        assert "version" in row, "the workflow was handed a row with no version"
        return "wf-1"

    import felix.durability.temporal as temporal_mod

    monkey_save, monkey_start = runs.save_fiber, temporal_mod.start_fiber_workflow
    runs.save_fiber = _save  # type: ignore[assignment]
    temporal_mod.start_fiber_workflow = _start  # type: ignore[assignment]
    try:
        settings = Settings(
            database_url="memory://durable-temporal",
            object_store="memory",
            durability="temporal",
        )
        from felix.manifests.schema import ExecutionSpec

        await runs.start_durable_chat(
            settings,
            tenant_id="default",
            manifest_id="quick",
            messages=[{"role": "user", "content": "hi"}],
            thread_id=None,
            model_id=None,
            execution=ExecutionSpec(mode="durable"),
        )
    finally:
        runs.save_fiber = monkey_save  # type: ignore[assignment]
        temporal_mod.start_fiber_workflow = monkey_start  # type: ignore[assignment]

    assert calls, "start_durable_chat took neither path"
    assert calls.index("save:temporal") < calls.index("start"), (
        f"the workflow was started before the row was persisted (order: {calls}); every "
        "write the activity makes will then be discarded as a version conflict"
    )
