"""A2A JSON-RPC smoke tests."""

from __future__ import annotations

import pytest
from felix.a2a.server import handle_rpc
from felix.config import Settings
from felix.tools.provider import InMemoryToolProvider


@pytest.fixture
def settings() -> Settings:
    return Settings(
        auth_mode="none",
        allow_insecure=True,
        object_store="memory",
        database_url="memory://a2a",
        default_manifest="quick",
    )


@pytest.mark.asyncio
async def test_a2a_agent_card(settings: Settings) -> None:
    tools = InMemoryToolProvider()
    resp = await handle_rpc(
        settings=settings,
        tools=tools,
        tenant_id="default",
        method="agent/authenticatedExtendedCard",
        params={"manifest": "quick"},
        rpc_id=1,
    )
    assert resp["result"]["name"] == "quick"
    assert resp["result"]["capabilities"]["streaming"] is True


@pytest.mark.asyncio
async def test_a2a_message_send_requires_text(settings: Settings) -> None:
    tools = InMemoryToolProvider()
    resp = await handle_rpc(
        settings=settings,
        tools=tools,
        tenant_id="default",
        method="message/send",
        params={"manifest": "quick", "message": {"parts": []}},
        rpc_id=2,
    )
    assert resp["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_eval_empty_dataset_completes(settings: Settings) -> None:
    from felix.eval import store as eval_store
    from felix.eval.runner import start_run

    await eval_store.put_dataset(settings, "default", "empty", description="no items")
    run = await start_run(
        settings,
        tenant_id="default",
        dataset_name="empty",
        candidate_manifest="quick",
    )
    assert run["status"] in {"completed", "in_progress", "complete"} or run.get("pass_count") == 0
    assert run.get("fail_count", 0) == 0


@pytest.mark.asyncio
async def test_eval_scores_the_version_it_reports(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canary run must benchmark the canary, not the active manifest.

    `run_continuous_eval` reads `canary_version` from each active canary and hands it to
    `start_run`, which writes it onto the run row — but resolution never used it, so the
    score belonged to whatever version was live while the label said "canary". A rollout
    then looks benchmarked when nothing benchmarked it, which is worse than not measuring:
    the number exists and is wrong.
    """
    from felix import runtime as runtime_mod
    from felix.eval import store as eval_store
    from felix.eval.runner import start_run

    seen: list[int | None] = []
    real = runtime_mod.resolve_tenant_manifest

    async def _spy(settings_, tenant_id, name, **kwargs):
        seen.append(kwargs.get("pin_version"))
        return await real(settings_, tenant_id, name, **kwargs)

    monkeypatch.setattr("felix.eval.runner.resolve_tenant_manifest", _spy)
    await eval_store.put_dataset(
        settings,
        "default",
        "pinned",
        description="one item",
        items=[{"item_id": "a", "user_input": "hi", "rubric": {"min_chars": 1}}],
    )
    await start_run(
        settings,
        tenant_id="default",
        dataset_name="pinned",
        candidate_manifest="quick",
        manifest_version=7,
    )
    assert seen == [7], f"the recorded version never reached resolution: {seen}"


@pytest.mark.asyncio
async def test_eval_fails_loudly_when_the_pinned_version_is_gone(settings: Settings) -> None:
    """A canary that cannot be resolved must fail its run, not fall back to active.

    Falling back is how the original bug read from the outside: a green run against a
    version that was never loaded. Failing is the honest outcome — the eval has nothing to
    say about that manifest.
    """
    from felix.eval import store as eval_store
    from felix.eval.runner import start_run

    await eval_store.put_dataset(
        settings,
        "default",
        "missing-version",
        description="one item",
        items=[{"item_id": "a", "user_input": "hi", "rubric": {"min_chars": 1}}],
    )
    run = await start_run(
        settings,
        tenant_id="default",
        dataset_name="missing-version",
        candidate_manifest="quick",
        manifest_version=9999,
    )
    assert run.get("pass_count") == 0
    assert run.get("fail_count") == 1
    scores = run.get("scores") or []
    assert scores and "error" in scores[0], f"the failure was not recorded: {scores}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "task_id"),
    [
        ("`#` mints a thread `/internal` refuses and no operator can address", "task#1"),
        ("an oversized id fails the `a2a_tasks` btree primary key on Postgres", "x" * 600),
    ],
)
async def test_a2a_refuses_a_task_id_it_cannot_make_a_thread_of(
    settings: Settings, label: str, task_id: str
) -> None:
    """`taskId` is caller-chosen and was interpolated straight into a thread id.

    The refusal has to land *before* `put_task`, so the second assertion is the load-bearing
    one: `task_id` is half of the `a2a_tasks` primary key, and an id that cannot become a
    thread must not leave a row behind either.
    """
    from felix.a2a import tasks as task_store

    task_store.clear_tasks()
    resp = await handle_rpc(
        settings=settings,
        tools=InMemoryToolProvider(),
        tenant_id="default",
        method="message/send",
        params={"manifest": "quick", "taskId": task_id, "message": {"parts": [{"text": "hi"}]}},
        rpc_id=3,
    )
    assert resp.get("error", {}).get("code") == -32602, f"{label}: {resp}"
    assert await task_store.get_task(settings, "default", task_id) is None, "a row was written anyway"


@pytest.mark.asyncio
async def test_eval_fails_one_item_rather_than_the_run_on_an_unusable_item_id(
    settings: Settings,
) -> None:
    """Dataset items come off `PUT /eval/datasets/{name}`, so `item_id` is caller-supplied.

    It reached `{tenant}:eval:{run}:{item_id}` by f-string, which is the same defect as the
    A2A one on a different surface. The run must still score every other item — that is the
    choice the rubric check beside it already makes, and a whole run lost to one bad id is
    the failure that change was written to prevent.
    """
    from felix.eval import store as eval_store
    from felix.eval.runner import start_run

    await eval_store.put_dataset(
        settings,
        "default",
        "bad-item-id",
        description="one unusable id, one good",
        items=[
            {"item_id": "x" * 600, "user_input": "hi", "rubric": {"min_chars": 1}},
            {"item_id": "fine", "user_input": "hi", "rubric": {"min_chars": 1}},
        ],
    )
    run = await start_run(
        settings,
        tenant_id="default",
        dataset_name="bad-item-id",
        candidate_manifest="quick",
        mock=True,
    )
    scores = {str(s.get("item_id")): s for s in run.get("scores") or []}
    assert len(scores) == 2, f"an item went missing: {run.get('scores')}"
    assert "error" in scores["x" * 600], "the unusable id was not reported as that item's error"
    assert "error" not in scores["fine"], f"the good item lost its score too: {scores['fine']}"


@pytest.mark.asyncio
async def test_an_unlabelled_item_still_reaches_the_runner_with_an_id(settings: Settings) -> None:
    """Why `eval_thread_id` needs no stand-in for a missing `item_id`.

    `start_run` always reads its items back from `get_dataset`, and `put_dataset` mints a
    `uuid4().hex` for an item that carries none — so an empty id never reaches the composer
    and a fallback there would be a branch nothing takes. This pins the assumption rather
    than leaving it as a comment, because it is the whole reason that branch is absent.
    """
    from felix.eval import store as eval_store

    await eval_store.put_dataset(
        settings,
        "default",
        "unlabelled",
        description="two items, no ids",
        items=[
            {"user_input": "hi", "rubric": {"min_chars": 1}},
            {"user_input": "hi", "rubric": {"min_chars": 1}},
        ],
    )
    stored = await eval_store.get_dataset(settings, "default", "unlabelled")
    ids = [str((i or {}).get("item_id") or "") for i in (stored or {}).get("items") or []]
    assert len(ids) == 2, f"the dataset did not round-trip: {stored}"
    assert all(ids), f"an item came back with no id: {ids}"
    assert len(set(ids)) == 2, f"two unlabelled items share an id: {ids}"
