"""A plan names the conversation it was written in (`0025_plan_thread_id`).

A plan was keyed on (tenant, id) alone, so the only answer to "this thread's plan" was the
tenant's newest one: a client drew another conversation's plan beside this one, and the
agent's own bare `plan_get` handed one thread the plan another was following.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.patterns import _plan_tools
from felix.plans import store as plans_store

from tests.support.factories import make_settings


@pytest.fixture
def settings() -> Settings:
    # Its own tenant per test: the memory store is a module-level dict shared by every
    # `memory://` URL, whatever its name, and these tests list by tenant.
    return make_settings()


def _tenant() -> str:
    return f"t{uuid.uuid4().hex[:8]}"


def _req(settings: Settings, tenant: str, thread: str | None) -> RequestContext:
    auth = AuthContext(tenant_id=tenant, principal_sub="tester", anonymous=False)
    return RequestContext(settings=settings, auth=auth, manifest_id="deep", thread_id=thread)


async def _call(name: str, args: dict[str, Any]) -> str:
    tools = {t.name: t for t in _plan_tools()}
    out = await tools[name].executor.execute(args)
    return out if isinstance(out, str) else out.content


async def test_plan_create_records_the_thread_it_ran_in(settings: Settings) -> None:
    tenant = _tenant()
    async with async_run_with_context(_req(settings, tenant, f"{tenant}:a")):
        await _call("plan_create", {"plan_id": "p1", "title": "Ship"})
    row = await plans_store.get_plan(settings, tenant, "p1")
    assert row is not None and row["thread_id"] == f"{tenant}:a"


async def test_a_thread_filter_is_applied_before_the_limit(settings: Settings) -> None:
    # Filtering a returned page would let newer plans from other threads push this
    # thread's off it — which is the whole failure the column exists to fix.
    tenant = _tenant()
    await plans_store.put_plan(settings, tenant, "mine", plan={}, thread_id=f"{tenant}:a")
    for i in range(5):
        await plans_store.put_plan(settings, tenant, f"other{i}", plan={}, thread_id=f"{tenant}:b")
    mine = await plans_store.list_plans(settings, tenant, limit=1, thread_id=f"{tenant}:a")
    assert [p["id"] for p in mine] == ["mine"]
    everything = await plans_store.list_plans(settings, tenant, limit=10)
    assert len(everything) == 6


async def test_an_empty_thread_filter_means_plans_written_outside_a_chat(settings: Settings) -> None:
    tenant = _tenant()
    await plans_store.put_plan(settings, tenant, "loose", plan={})
    await plans_store.put_plan(settings, tenant, "threaded", plan={}, thread_id=f"{tenant}:a")
    loose = await plans_store.list_plans(settings, tenant, thread_id="")
    assert [p["id"] for p in loose] == ["loose"]


async def test_a_write_that_does_not_name_the_thread_keeps_it(settings: Settings) -> None:
    tenant = _tenant()
    await plans_store.put_plan(settings, tenant, "p", plan={"v": 1}, thread_id=f"{tenant}:a")
    row = await plans_store.put_plan(settings, tenant, "p", plan={"v": 2})
    assert row["thread_id"] == f"{tenant}:a"


async def test_a_step_update_backfills_a_legacy_plan_and_never_moves_a_threaded_one(
    settings: Settings,
) -> None:
    tenant = _tenant()
    # Written before plans named their thread.
    await plans_store.put_plan(settings, tenant, "old", plan={"steps": [{"id": "1", "status": "pending"}]})
    # Written in thread a.
    async with async_run_with_context(_req(settings, tenant, f"{tenant}:a")):
        await _call("plan_create", {"plan_id": "mine", "steps": ["one"]})

    # Thread b updates both by id. The legacy plan is attributed to b; a's stays a's —
    # attribution is where a plan was written, not whoever touched it last.
    async with async_run_with_context(_req(settings, tenant, f"{tenant}:b")):
        await _call("plan_update_step", {"plan_id": "old", "step_id": "1"})
        await _call("plan_update_step", {"plan_id": "mine", "step_id": "1"})
    assert (await plans_store.get_plan(settings, tenant, "old"))["thread_id"] == f"{tenant}:b"
    assert (await plans_store.get_plan(settings, tenant, "mine"))["thread_id"] == f"{tenant}:a"


async def test_a_bare_plan_get_answers_for_this_thread_only(settings: Settings) -> None:
    tenant = _tenant()
    async with async_run_with_context(_req(settings, tenant, f"{tenant}:a")):
        await _call("plan_create", {"plan_id": "a-plan", "title": "A's"})
    # Thread b's plan is newer, which is exactly what used to win.
    async with async_run_with_context(_req(settings, tenant, f"{tenant}:b")):
        await _call("plan_create", {"plan_id": "b-plan", "title": "B's"})

    async with async_run_with_context(_req(settings, tenant, f"{tenant}:a")):
        got = json.loads(await _call("plan_get", {}))
    assert got["id"] == "a-plan"

    async with async_run_with_context(_req(settings, tenant, f"{tenant}:c")):
        empty = await _call("plan_get", {})
    assert empty == "[tool error/invalid_arguments] no plans on this thread"


async def test_outside_a_chat_a_bare_plan_get_stays_tenant_wide(settings: Settings) -> None:
    tenant = _tenant()
    await plans_store.put_plan(settings, tenant, "any", plan={}, thread_id=f"{tenant}:a")
    async with async_run_with_context(_req(settings, tenant, None)):
        got = json.loads(await _call("plan_get", {}))
    assert got["id"] == "any"
