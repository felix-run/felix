"""Plan store contract: listing order, on both backends.

`list_plans` ordered on `updated_at` alone and then cut to `limit`, so plans written in the same
millisecond paged differently on Postgres and on the memory twin. It ends on the id now, in byte
order on both — the ids below differ in case because Postgres's default collation orders
`Bravo` and `charlie` differently from code point.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.plans import store as plans

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)
TENANT = "conformance"


@parametrized
@pytest.mark.asyncio
async def test_plans_updated_in_one_millisecond_page_the_same_on_both_arms(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plans, "now_ms", lambda: 1_800_000_000_000)
    ids = ["alpha", "Bravo", "charlie", "Delta"]
    for plan_id in ids:
        await plans.put_plan(store_settings, TENANT, plan_id, plan={"steps": []})

    full = [p["id"] for p in await plans.list_plans(store_settings, TENANT, limit=100)]
    assert full == sorted(ids, reverse=True), "newest first, then by id in byte order"
    assert [p["id"] for p in await plans.list_plans(store_settings, TENANT, limit=2)] == full[:2]
