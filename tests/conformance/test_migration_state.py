"""The migration-state probe against a real schema: at head after upgrade, behind after
a downgrade, unmigrated on an empty database."""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.db import migrations
from felix.db.session import dispose_engine

from tests.conformance.conftest import (
    downgrade_to_base,
    drop_everything,
    migrate_to_head,
    postgres_url_or_skip,
)


@pytest.mark.asyncio
@pytest.mark.usefixtures("empty_database")
async def test_state_tracks_the_schema() -> None:
    url = postgres_url_or_skip("the migration-state contract")
    settings = Settings(database_url=url)
    try:
        await migrate_to_head(url)
        state = await migrations.migration_state(settings)
        assert state.at_head and state.current == migrations.script_head()

        await downgrade_to_base(url)
        await dispose_engine()
        state = await migrations.migration_state(settings)
        assert not state.at_head and state.current is None, state
    finally:
        await dispose_engine()
        await drop_everything(url)


@pytest.mark.asyncio
@pytest.mark.usefixtures("empty_database")
async def test_a_revision_the_database_has_passed_is_named() -> None:
    """`felix migrate <older>` asks this before upgrading, because `command.upgrade` to a
    revision already passed changes nothing and says so to nobody."""
    url = postgres_url_or_skip("the passed-revision contract")
    settings = Settings(database_url=url)
    head = migrations.script_head()
    try:
        await migrate_to_head(url)
        assert await migrations.passed_revision(settings, "0021_push_subscriptions") == head
        # Not passed: the revision the database sits at, the moving targets, and an unknown id.
        assert await migrations.passed_revision(settings, head or "") is None
        for target in ("head", "heads", "base", "-1", "no-such-revision"):
            assert await migrations.passed_revision(settings, target) is None, target

        await downgrade_to_base(url)
        await dispose_engine()
        # An unmigrated database has passed nothing.
        assert await migrations.passed_revision(settings, "0021_push_subscriptions") is None
    finally:
        await dispose_engine()
        await drop_everything(url)
