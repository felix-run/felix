"""One contract, run against every eval-store backend.

Written after the two copies were found to disagree about the single thing the store is
asked to do most often. `put_dataset` is the *only* way an eval item is written, and every
caller repeats item ids by design — `felix eval --fixture` on each run, the continuous-eval
job on each tick, an edited dataset re-`PUT` to the management route. The in-memory twin
overwrote; Postgres raised `UniqueViolation` on `(tenant_id, dataset_name, item_id)`. Every
test in the repo runs the twin, so the suite was green and only a deployment failed.

`tests/unit/test_invariants.py` asserts every Postgres-touching module *has* a twin. This is
where the two are held to the same answers.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.eval import store as eval_store

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)

TENANT = "conformance"


def _item(item_id: str, text: str, rubric: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"item_id": item_id, "user_input": text, "rubric": rubric or {"contains": "x"}}


def _by_id(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {i["item_id"]: i for i in dataset["items"]}


# --- datasets -------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_a_dataset_round_trips_with_its_items(store_settings: Any) -> None:
    await eval_store.put_dataset(
        store_settings, TENANT, "d", description="questions", items=[_item("a", "one"), _item("b", "two")]
    )

    fetched = await eval_store.get_dataset(store_settings, TENANT, "d")

    assert fetched is not None
    assert fetched["name"] == "d"
    assert fetched["description"] == "questions"
    assert {i["item_id"]: i["user_input"] for i in fetched["items"]} == {"a": "one", "b": "two"}


@parametrized
@pytest.mark.asyncio
async def test_writing_the_same_item_id_again_updates_it_in_place(store_settings: Any) -> None:
    """The divergence this file was written for.

    Not an edge case: it is what happens the second time anyone runs the same eval.
    """
    await eval_store.put_dataset(store_settings, TENANT, "d", items=[_item("a", "first", {"contains": "1"})])

    await eval_store.put_dataset(store_settings, TENANT, "d", items=[_item("a", "second", {"contains": "2"})])

    fetched = await eval_store.get_dataset(store_settings, TENANT, "d")
    assert fetched is not None
    assert len(fetched["items"]) == 1, "the item was duplicated rather than updated"
    assert _by_id(fetched)["a"]["user_input"] == "second"
    assert _by_id(fetched)["a"]["rubric"] == {"contains": "2"}


@parametrized
@pytest.mark.asyncio
async def test_rewriting_an_item_does_not_reset_when_it_first_appeared(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`created_at` is the item's age, not the age of its last edit.

    The clock is stubbed because both writes otherwise land in the same millisecond, which
    would make this assertion pass whatever the store did.
    """
    clock = iter([1_000, 2_000, 3_000, 4_000])
    monkeypatch.setattr(eval_store, "now_ms", lambda: next(clock))

    await eval_store.put_dataset(store_settings, TENANT, "d", items=[_item("a", "first")])
    await eval_store.put_dataset(store_settings, TENANT, "d", items=[_item("a", "second")])

    fetched = await eval_store.get_dataset(store_settings, TENANT, "d")
    assert fetched is not None
    assert _by_id(fetched)["a"]["created_at"] == 1_000


@parametrized
@pytest.mark.asyncio
async def test_an_item_the_second_write_omits_survives_it(store_settings: Any) -> None:
    """`put_dataset` adds and updates; it never removes.

    Callers depend on knowing this — an import tool has to tell its user which items it is
    leaving behind — so it is pinned rather than left as an implementation detail.
    """
    await eval_store.put_dataset(store_settings, TENANT, "d", items=[_item("a", "one"), _item("b", "two")])

    await eval_store.put_dataset(store_settings, TENANT, "d", items=[_item("a", "one again")])

    fetched = await eval_store.get_dataset(store_settings, TENANT, "d")
    assert fetched is not None
    assert sorted(_by_id(fetched)) == ["a", "b"]


@parametrized
@pytest.mark.asyncio
async def test_an_item_with_no_id_is_given_one(store_settings: Any) -> None:
    await eval_store.put_dataset(store_settings, TENANT, "d", items=[{"user_input": "anonymous"}])

    fetched = await eval_store.get_dataset(store_settings, TENANT, "d")
    assert fetched is not None
    assert len(fetched["items"]) == 1
    assert fetched["items"][0]["item_id"]


@parametrized
@pytest.mark.asyncio
async def test_re_putting_a_dataset_updates_its_description(store_settings: Any) -> None:
    await eval_store.put_dataset(store_settings, TENANT, "d", description="before", items=[_item("a", "x")])

    await eval_store.put_dataset(store_settings, TENANT, "d", description="after", items=[])

    fetched = await eval_store.get_dataset(store_settings, TENANT, "d")
    assert fetched is not None
    assert fetched["description"] == "after"


@parametrized
@pytest.mark.asyncio
async def test_an_unknown_dataset_is_none_not_empty(store_settings: Any) -> None:
    assert await eval_store.get_dataset(store_settings, TENANT, "never-written") is None


@parametrized
@pytest.mark.asyncio
async def test_datasets_do_not_leak_between_tenants(store_settings: Any) -> None:
    await eval_store.put_dataset(store_settings, TENANT, "shared-name", items=[_item("a", "mine")])
    await eval_store.put_dataset(store_settings, "other", "shared-name", items=[_item("a", "theirs")])

    mine = await eval_store.get_dataset(store_settings, TENANT, "shared-name")
    listed = await eval_store.list_datasets(store_settings, TENANT)

    assert mine is not None
    assert _by_id(mine)["a"]["user_input"] == "mine"
    assert [d["name"] for d in listed] == ["shared-name"]


# --- runs -----------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_a_run_completes_and_keeps_its_scores(store_settings: Any) -> None:
    run = await eval_store.create_run(
        store_settings, tenant_id=TENANT, dataset_name="d", candidate_manifest="quick", manifest_version=3
    )
    assert run["status"] == "in_progress"
    assert run["finished_at"] is None

    scores = [{"item_id": "a", "pass": True, "score": 1.0, "rule": "contains"}]
    completed = await eval_store.complete_run(
        store_settings, TENANT, run["id"], pass_count=1, fail_count=0, scores=scores
    )

    assert completed is not None
    fetched = await eval_store.get_run(store_settings, TENANT, run["id"])
    assert fetched is not None
    assert fetched["status"] == "completed"
    assert fetched["finished_at"] is not None
    assert fetched["pass_count"] == 1
    assert fetched["manifest_version"] == 3
    assert fetched["scores"] == scores


@parametrized
@pytest.mark.asyncio
async def test_completing_an_unknown_run_is_none_not_a_new_row(store_settings: Any) -> None:
    assert await eval_store.complete_run(store_settings, TENANT, "no-such-run") is None
    assert await eval_store.list_runs(store_settings, TENANT) == []


@parametrized
@pytest.mark.asyncio
async def test_runs_list_newest_first(store_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([1_000, 2_000, 3_000])
    monkeypatch.setattr(eval_store, "now_ms", lambda: next(clock))

    for name in ("first", "second", "third"):
        await eval_store.create_run(
            store_settings, tenant_id=TENANT, dataset_name=name, candidate_manifest="quick"
        )

    listed = await eval_store.list_runs(store_settings, TENANT)

    assert [r["dataset_name"] for r in listed] == ["third", "second", "first"]


@parametrized
@pytest.mark.asyncio
async def test_runs_do_not_leak_between_tenants(store_settings: Any) -> None:
    mine = await eval_store.create_run(
        store_settings, tenant_id=TENANT, dataset_name="d", candidate_manifest="quick"
    )
    await eval_store.create_run(
        store_settings, tenant_id="other", dataset_name="d", candidate_manifest="quick"
    )

    assert [r["id"] for r in await eval_store.list_runs(store_settings, TENANT)] == [mine["id"]]
    assert await eval_store.get_run(store_settings, "other", mine["id"]) is None
