"""`create_fiber` hands back the row it stored, `version` included, so a write through it lands.

Found by running the Temporal backend (since removed): its activity wrote through the dict
`create_fiber` returned, which had no `version` key, so `_save_fiber`'s compare-and-set read 0
against a stored row and discarded every write — the run completed and its row stayed
`pending`. The Postgres sweeper re-reads every row it claims and never saw it. The property
outlives the backend: any caller holding the returned row must be able to save through it.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.durability.fibers import create_fiber, save_fiber

SETTINGS = Settings(database_url="memory://fiber-returned-row", object_store="memory")


@pytest.fixture(autouse=True)
def _clean() -> Any:
    from felix.durability.fibers import _memory_fibers

    _memory_fibers.clear()
    yield
    _memory_fibers.clear()


async def test_create_fiber_returns_the_version_it_stored() -> None:
    """Without this the first compare-and-set is against a version that was never written."""
    fiber = await create_fiber(SETTINGS, "default", kind="durable_chat", status="pending", state={})
    assert "version" in fiber, "the returned row omits `version`, so any writer using it is stale"
    assert fiber["version"] == 0


async def test_a_write_using_the_returned_row_is_not_discarded() -> None:
    """Take the returned row, advance it, save — what any writer holding it does."""
    from felix.durability.fibers import _memory_fibers

    fiber = await create_fiber(SETTINGS, "default", kind="durable_chat", status="pending", state={})
    fiber["status"] = "completed"
    await save_fiber(SETTINGS, fiber)

    stored = _memory_fibers[("default", fiber["id"])]
    assert stored["status"] == "completed", (
        "the write was discarded as a version conflict; the run would finish and stay "
        "`pending` to every reader of the fiber row"
    )
