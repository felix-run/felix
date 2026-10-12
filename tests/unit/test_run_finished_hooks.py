"""`PluginRegistry.register_run_finished`: the worker tells a plugin when one of its runs ends.

Pinned against the real step loop (`resume_due_fibers` over the in-memory fiber store) as well as
the dispatch helper: a hook sees each terminal durable run its plugin tagged, once, with the
run's answer and `origin`; never another plugin's run or an untagged one; one hook that raises or
hangs does not stop the next; and a run that dies after its retries is announced like any other.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.plugins import RunFinished, get_registry

from tests.support.factories import make_settings

TERMINAL = ("completed", "failed", "expired", "dead")


def _row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "run-1",
        "tenant_id": "acme",
        "thread_id": "acme:t1",
        "kind": "durable_chat",
        "status": "completed",
        "state_json": {
            "origin": {"plugin": "p", "conversation": "C1"},
            "stash": {"last": {"final": {"role": "assistant", "content": "Done."}}},
        },
    }
    row.update(over)
    return row


def _recorder(seen: list[RunFinished]) -> Any:
    async def hook(settings: Any, finished: RunFinished) -> None:
        seen.append(finished)

    return hook


@pytest.mark.parametrize("status", TERMINAL)
async def test_a_terminal_run_reaches_its_plugins_hooks_with_its_answer_and_origin(status: str) -> None:
    from felix.durability.fibers import _after_terminal

    seen: list[RunFinished] = []

    async def broken(settings: Any, finished: RunFinished) -> None:
        raise RuntimeError("a broken plugin")

    get_registry().register_run_finished(broken, origin="p")
    get_registry().register_run_finished(_recorder(seen), origin="p")
    await _after_terminal(make_settings(), _row(status=status))
    assert seen == [
        RunFinished(
            tenant_id="acme",
            thread_id="acme:t1",
            run_id="run-1",
            status=status,  # type: ignore[arg-type]
            answer="Done.",
            # `run_view`'s own account of a buried run, which recorded none.
            error="step failed 0 times" if status == "dead" else "",
            origin={"plugin": "p", "conversation": "C1"},
        )
    ]


@pytest.mark.parametrize(
    "state", [{"origin": {"plugin": "someone-else"}}, {"origin": {}}, {}], ids=["other", "empty", "untagged"]
)
async def test_a_hook_never_sees_another_plugins_run_or_an_untagged_one(state: dict[str, Any]) -> None:
    from felix.durability.fibers import _after_terminal

    seen: list[RunFinished] = []
    get_registry().register_run_finished(_recorder(seen), origin="p")
    await _after_terminal(make_settings(), _row(state_json=state))
    assert seen == []


def test_a_hook_must_say_whose_runs_it_takes() -> None:
    with pytest.raises(ValueError, match="origin"):
        get_registry().register_run_finished(_recorder([]), origin="")


async def test_a_hook_that_hangs_is_cut_off_and_the_next_still_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.durability import fibers

    monkeypatch.setattr(fibers, "RUN_FINISHED_HOOK_TIMEOUT_S", 0.05)
    seen: list[RunFinished] = []

    async def hangs(settings: Any, finished: RunFinished) -> None:
        await asyncio.Event().wait()

    get_registry().register_run_finished(hangs, origin="p")
    get_registry().register_run_finished(_recorder(seen), origin="p")
    await asyncio.wait_for(fibers._after_terminal(make_settings(), _row()), 2)
    assert [f.run_id for f in seen] == ["run-1"]


@pytest.mark.parametrize("row", [_row(status="running"), _row(kind="job")], ids=["in-flight", "not-a-chat"])
async def test_a_run_in_flight_or_another_kind_of_fiber_reaches_no_hook(row: dict[str, Any]) -> None:
    from felix.durability.fibers import _after_terminal

    seen: list[RunFinished] = []
    get_registry().register_run_finished(_recorder(seen), origin="p")
    await _after_terminal(make_settings(), row)
    assert seen == []


async def _start(settings: Any) -> str:
    from felix.durability import runs
    from felix.manifests.schema import ExecutionSpec
    from felix_ai.types import ChatMessage

    started = await runs.start_durable_chat(
        settings,
        "acme",
        manifest_id="m",
        messages=[ChatMessage(role="user", content="hi")],
        thread_id="acme:t9",
        model_id=None,
        execution=ExecutionSpec(),
        origin={"plugin": "p", "conversation": "C9"},
    )
    return str(started["resume_token"])


async def test_a_run_that_finishes_in_the_worker_reaches_the_hook_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through `start_durable_chat` and the step loop: `origin` round-trips through the run's state."""
    from felix.durability import fibers as F

    seen: list[RunFinished] = []
    get_registry().register_run_finished(_recorder(seen), origin="p")

    async def step(settings: Any, row: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        row["status"] = "completed"
        row["state_json"]["stash"] = {"last": {"final": {"role": "assistant", "content": "ok"}}}
        await F._save_fiber(settings, row)
        return row

    monkeypatch.setattr(F, "_run_fiber_step", step)
    settings = make_settings()
    run_id = await _start(settings)
    for _ in range(3):
        await F.resume_due_fibers(settings)
    assert [(f.run_id, f.status, f.answer, dict(f.origin)) for f in seen] == [
        (run_id, "completed", "ok", {"plugin": "p", "conversation": "C9"})
    ]


async def test_a_run_that_dies_after_its_retries_is_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other way a run ends: its step keeps raising until the retry budget buries it."""
    from felix.durability import fibers as F

    seen: list[RunFinished] = []
    get_registry().register_run_finished(_recorder(seen), origin="p")

    async def step(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the store blinked")

    monkeypatch.setattr(F, "_run_fiber_step", step)
    settings = make_settings(fiber_max_attempts=1)
    run_id = await _start(settings)
    await F.resume_due_fibers(settings)
    [finished] = seen
    assert (finished.run_id, finished.status) == (run_id, "dead")
    assert finished.error.startswith("step failed 1 times")
