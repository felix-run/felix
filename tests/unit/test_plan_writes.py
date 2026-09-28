"""Conditional plan writes, and what a write leaves alone (felix-run/felix#320).

An operator's `PUT /plans/{id}` replaces the whole plan while the agent's
`plan_update_step` reads, modifies and writes it back. Unconditional, whichever
landed second erased the other with nothing reporting it — and every write also
reset `manifest_id` and `expires_at` to whatever the caller did not send.
"""

from __future__ import annotations

import json
import uuid

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.patterns import _plan_tools
from felix.plans import store as plans_store


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="memory://plan-writes")


def _id() -> str:
    # The memory store is a module-level dict shared by the whole run.
    return f"p-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_a_write_that_names_neither_field_keeps_both(settings: Settings) -> None:
    pid = _id()
    await plans_store.put_plan(settings, "t1", pid, plan={"v": 1}, manifest_id="deep", expires_at=123)
    row = await plans_store.put_plan(settings, "t1", pid, plan={"v": 2})
    assert (row["manifest_id"], row["expires_at"], row["plan"]) == ("deep", 123, {"v": 2})


@pytest.mark.asyncio
async def test_a_write_that_names_them_still_sets_them(settings: Settings) -> None:
    pid = _id()
    await plans_store.put_plan(settings, "t1", pid, plan={}, manifest_id="deep", expires_at=123)
    row = await plans_store.put_plan(settings, "t1", pid, plan={}, manifest_id="", expires_at=None)
    assert (row["manifest_id"], row["expires_at"]) == ("", None)


@pytest.mark.asyncio
async def test_a_new_plan_gets_the_column_defaults(settings: Settings) -> None:
    row = await plans_store.put_plan(settings, "t1", _id(), plan={})
    assert (row["manifest_id"], row["expires_at"]) == ("", None)


@pytest.mark.asyncio
async def test_a_stale_write_is_refused_and_told_what_is_there(settings: Settings) -> None:
    pid = _id()
    first = await plans_store.put_plan(settings, "t1", pid, plan={"v": 1})
    second = await plans_store.put_plan(settings, "t1", pid, plan={"v": 2})
    with pytest.raises(plans_store.PlanConflict) as caught:
        await plans_store.put_plan(
            settings, "t1", pid, plan={"v": "stale"}, expected_updated_at=first["updated_at"]
        )
    assert caught.value.current == second
    assert (await plans_store.get_plan(settings, "t1", pid))["plan"] == {"v": 2}


@pytest.mark.asyncio
async def test_a_current_write_goes_through(settings: Settings) -> None:
    pid = _id()
    first = await plans_store.put_plan(settings, "t1", pid, plan={"v": 1})
    row = await plans_store.put_plan(
        settings, "t1", pid, plan={"v": 2}, expected_updated_at=first["updated_at"]
    )
    assert row["plan"] == {"v": 2}


@pytest.mark.asyncio
async def test_a_conditional_write_to_a_missing_plan_conflicts(settings: Settings) -> None:
    with pytest.raises(plans_store.PlanConflict) as caught:
        await plans_store.put_plan(settings, "t1", _id(), plan={}, expected_updated_at=1)
    assert caught.value.current is None


@pytest.mark.asyncio
async def test_every_write_moves_updated_at_even_within_a_millisecond(settings: Settings) -> None:
    # Otherwise two writes in one tick share a version and a stale edit passes.
    pid = _id()
    seen = [(await plans_store.put_plan(settings, "t1", pid, plan={"v": i}))["updated_at"] for i in range(5)]
    assert seen == sorted(set(seen))


@pytest.mark.asyncio
async def test_the_agent_step_update_keeps_an_operator_edit_made_under_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _id()
    tools = {t.name: t for t in _plan_tools()}
    auth = AuthContext(tenant_id="t1", principal_sub="tester", anonymous=False)
    req = RequestContext(settings=settings, auth=auth, manifest_id="deep", thread_id="th1")
    async with async_run_with_context(req):
        await tools["plan_create"].executor.execute(
            {"plan_id": pid, "title": "Ship", "steps": ["build", "test"]}
        )
        stale = await plans_store.get_plan(settings, "t1", pid)
        assert stale is not None

        # The operator retitles the plan after the agent has read it...
        edited = {**stale["plan"], "title": "Ship carefully"}
        await plans_store.put_plan(settings, "t1", pid, plan=edited)

        # ...so the agent's read returns the version from before the edit.
        real_get = plans_store.get_plan
        reads = iter([stale])

        async def get_plan_once_stale(*args: object, **kwargs: object) -> object:
            return next(reads, None) or await real_get(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(plans_store, "get_plan", get_plan_once_stale)
        out = await tools["plan_update_step"].executor.execute({"plan_id": pid, "step_id": "1"})

    body = json.loads(out if isinstance(out, str) else out.content)
    assert body["plan"]["title"] == "Ship carefully"
    assert body["plan"]["steps"][0]["status"] == "done"
