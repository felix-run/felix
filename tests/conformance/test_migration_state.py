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
