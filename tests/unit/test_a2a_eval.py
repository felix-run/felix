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
