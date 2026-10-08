"""One durable run per thread (felix-run/felix#529).

A send to a thread whose durable run was still going started a second run beside it. The two
appended to one log, and neither saw the other's work until a whole tool batch landed, so each
re-did it: on a production `cowork` thread the same files were written twice, user turns landed
between another run's tool calls, and three write approvals were pending at once. Nothing could
have refused it -- the fiber did not record its thread -- and a reloaded client could not even
find the run, because the snapshot said `idle` and only the response that started a run carried
its token.

These drive the real routes over the in-memory fiber store, with no worker: an enqueued run
stays `pending`, which is exactly "in flight".
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.config import Settings
from felix.durability import fibers
from felix.durability.fibers import RunInProgress, create_fiber, run_in_flight
from felix.manifests.loader import load_bundled
from httpx import ASGITransport, AsyncClient

THREAD = "th-529"
KEYS = json.dumps({"sk-acme": {"tenant_id": "acme", "sub": "alice", "scopes": ["*"]}})


def _settings() -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="api_key",
        auth_api_keys=KEYS,
        host="127.0.0.1",
        rate_limit=100_000,
        environment="development",
        object_store="memory",
        database_url="memory://one-run-per-thread",
        redis_url="",
        anthropic_api_key="",
        openai_api_key="",
    )


def _client(settings: Settings) -> AsyncClient:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=30.0,
        headers={"authorization": "Bearer sk-acme"},
    )


def _turn(text: str = "hi", thread: str = THREAD) -> dict[str, Any]:
    return {"manifest": "cowork", "thread_id": thread, "messages": [{"role": "user", "content": text}]}


def _finish_every_run(status: str = "completed") -> None:
    for row in fibers._memory_fibers.values():
        row["status"] = status


@pytest.fixture(autouse=True)
def _cowork_is_durable() -> None:
    assert load_bundled("cowork").spec.execution.mode == "durable", "these need a durable manifest"


# --- what counts as in flight ----------------------------------------------------------


def _row(status: str, *, expires_at: int, lease_until: int | None = None) -> dict[str, Any]:
    return {"status": status, "state_json": {"expires_at": expires_at}, "lease_until": lease_until}


def test_a_run_holds_its_thread_until_it_ends() -> None:
    assert run_in_flight(_row("pending", expires_at=2_000), now=1_000)
    assert run_in_flight(_row("running", expires_at=2_000), now=1_000)
    for status in ("completed", "failed", "expired", "dead"):
        assert not run_in_flight(_row(status, expires_at=2_000), now=1_000), status


def test_an_expired_run_nobody_holds_does_not_lock_the_thread_for_good() -> None:
    """With no worker it would never be claimed and marked `expired` -- so the thread would be
    refused forever on the strength of a run that cannot do anything."""
    assert not run_in_flight(_row("pending", expires_at=1_000), now=2_000)
    assert not run_in_flight(_row("running", expires_at=1_000, lease_until=1_500), now=2_000)


def test_an_expired_run_a_worker_still_holds_is_still_running() -> None:
    """Expiry is checked between steps, and a durable chat is one step: past `expires_at` a
    claimed run is still in its invoke, still writing to the thread."""
    assert run_in_flight(_row("running", expires_at=1_000, lease_until=3_000), now=2_000)


# --- the store ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_enqueue_refuses_a_second_run_on_the_thread() -> None:
    settings = _settings()
    state = {"expires_at": fibers.now_ms() + 60_000}
    first = await create_fiber(settings, "t", state=state, thread_id=THREAD, exclusive_on_thread=True)
    with pytest.raises(RunInProgress) as refused:
        await create_fiber(settings, "t", state=state, thread_id=THREAD, exclusive_on_thread=True)
    assert refused.value.resume_token == first["id"]
    # Another thread, and another tenant's thread of the same name, are their own.
    await create_fiber(settings, "t", state=state, thread_id="other", exclusive_on_thread=True)
    await create_fiber(settings, "u", state=state, thread_id=THREAD, exclusive_on_thread=True)


# --- the routes ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_send_is_refused_with_the_run_to_watch() -> None:
    async with _client(_settings()) as client:
        first = await client.post("/chat", json=_turn())
        assert first.status_code == 202, first.text
        token = first.json()["resume_token"]

        again = await client.post("/chat", json=_turn("proceed"))
        assert again.status_code == 409, again.text
        assert again.json()["detail"] == f"run_in_progress:{token}"

        streamed = await client.post("/chat/stream", json=_turn("proceed"))
        assert streamed.status_code == 409, streamed.text
        assert streamed.json()["detail"] == f"run_in_progress:{token}"

    assert sum(1 for r in fibers._memory_fibers.values() if r.get("thread_id")) == 1


@pytest.mark.asyncio
async def test_a_transient_send_is_refused_too() -> None:
    """It appends to the same log the run is writing, which is the whole problem."""
    async with _client(_settings()) as client:
        first = await client.post("/chat", json=_turn())
        token = first.json()["resume_token"]
        quick = await client.post("/chat/stream", json={**_turn(), "manifest": "quick"})
    assert quick.status_code == 409, quick.text
    assert quick.json()["detail"] == f"run_in_progress:{token}"


@pytest.mark.asyncio
async def test_the_thread_is_free_once_the_run_ends() -> None:
    async with _client(_settings()) as client:
        assert (await client.post("/chat", json=_turn())).status_code == 202
        _finish_every_run()
        again = await client.post("/chat", json=_turn("next"))
    assert again.status_code == 202, again.text


@pytest.mark.asyncio
async def test_other_threads_are_not_held() -> None:
    async with _client(_settings()) as client:
        assert (await client.post("/chat", json=_turn())).status_code == 202
        elsewhere = await client.post("/chat", json=_turn(thread="th-elsewhere"))
    assert elsewhere.status_code == 202, elsewhere.text


@pytest.mark.asyncio
async def test_a_refused_keyed_send_frees_its_key() -> None:
    """The refusal is not the key's answer. Held, the same message resent once the run has
    finished would be `idempotency_in_progress` -- or replayed as a refusal -- forever."""
    async with _client(_settings()) as client:
        first = await client.post("/chat", json=_turn())
        token = first.json()["resume_token"]
        headers = {"idempotency-key": "resend-me-0001"}
        for _ in range(2):
            refused = await client.post("/chat/stream", json=_turn("proceed"), headers=headers)
            assert refused.status_code == 409, refused.text
            assert refused.json()["detail"] == f"run_in_progress:{token}"
        _finish_every_run()
        accepted = await client.post("/chat", json=_turn("proceed"), headers=headers)
    assert accepted.status_code == 202, accepted.text
    assert accepted.headers.get("idempotent-replayed") is None


@pytest.mark.asyncio
async def test_a_resend_of_the_starting_message_is_answered_from_its_key_not_refused() -> None:
    """The run in flight is this message's own, so its resend replays the 202 it got."""
    async with _client(_settings()) as client:
        headers = {"idempotency-key": "first-key-0001"}
        first = await client.post("/chat", json=_turn(), headers=headers)
        again = await client.post("/chat", json=_turn(), headers=headers)
    assert again.status_code == 202, again.text
    assert again.headers.get("idempotent-replayed") == "true"
    assert again.json()["resume_token"] == first.json()["resume_token"]


@pytest.mark.asyncio
async def test_the_snapshot_names_the_run_in_flight() -> None:
    """The handle a reloaded client needs: `phase` reads `idle` throughout a durable run."""
    async with _client(_settings()) as client:
        first = await client.post("/chat", json=_turn())
        token = first.json()["resume_token"]
        during = (await client.get(f"/chat/sessions/{THREAD}")).json()
        _finish_every_run()
        after = (await client.get(f"/chat/sessions/{THREAD}")).json()

    assert during["activeRun"]["resumeToken"] == token
    assert during["activeRun"]["status"] == "pending"
    assert isinstance(during["activeRun"]["expiresAt"], int)
    assert after["activeRun"] is None
