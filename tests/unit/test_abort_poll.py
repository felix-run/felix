"""A streaming turn checks for Stop once per delta without a Redis round trip per token."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from felix import steer


@pytest.fixture
def remote_checks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stands in for the shared-store read, recording each one and answering "not aborted"."""
    seen: list[str] = []

    async def fake_is_aborted(tenant_id: str, thread_id: str) -> bool:
        seen.append(thread_id)
        return False

    monkeypatch.setattr(steer, "is_aborted", fake_is_aborted)
    # The react loop imports the name; a regression back to it must be counted too.
    from felix.patterns import react

    monkeypatch.setattr(react, "is_aborted", fake_is_aborted)
    return seen


async def test_the_shared_store_is_asked_at_most_once_per_interval(
    monkeypatch: pytest.MonkeyPatch, remote_checks: list[str]
) -> None:
    now = [100.0]
    monkeypatch.setattr(steer, "time", SimpleNamespace(monotonic=lambda: now[0]))
    poll = steer.AbortPoll("acme", "t-poll", interval_s=0.25)

    for _ in range(50):  # fifty deltas inside one interval
        assert await poll.aborted() is False
    assert len(remote_checks) == 1

    now[0] += 0.25
    assert await poll.aborted() is False
    assert len(remote_checks) == 2


async def test_a_local_abort_is_seen_on_the_next_delta(
    monkeypatch: pytest.MonkeyPatch, remote_checks: list[str]
) -> None:
    monkeypatch.setattr(steer, "time", SimpleNamespace(monotonic=lambda: 100.0))
    poll = steer.AbortPoll("acme", "t-local", interval_s=0.25)
    assert await poll.aborted() is False
    await steer.request_abort("acme", "t-local")
    assert await poll.aborted() is True
    # Seen from the in-process flag, inside the interval, without asking the store again.
    assert len(remote_checks) == 1
    await steer.release_run_queue("acme", "t-local")


async def test_a_remote_abort_is_seen_once_the_interval_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(steer, "time", SimpleNamespace(monotonic=lambda: now[0]))
    remote = {"aborted": False}

    async def fake_is_aborted(tenant_id: str, thread_id: str) -> bool:
        return remote["aborted"]

    monkeypatch.setattr(steer, "is_aborted", fake_is_aborted)
    poll = steer.AbortPoll("acme", "t-remote", interval_s=0.25)
    assert await poll.aborted() is False
    remote["aborted"] = True  # another replica set the Redis flag
    assert await poll.aborted() is False
    now[0] += 0.25
    assert await poll.aborted() is True
    await steer.release_run_queue("acme", "t-remote")


async def test_a_streamed_turn_does_not_ask_the_store_per_delta(
    monkeypatch: pytest.MonkeyPatch, remote_checks: list[str]
) -> None:
    """The react loop's streaming path goes through `AbortPoll`, not `is_aborted` per token."""
    from felix.manifests.schema import ModelSpec
    from felix.patterns.react import _ReactAgent
    from felix_ai.types import ChatMessage, ModelChatResult, StreamDelta, TokenUsage

    monkeypatch.setattr(steer, "time", SimpleNamespace(monotonic=lambda: 100.0))

    class _ManyDeltas:
        model_id = "m"

        async def stream_turn(self, messages, tools, opts=None):
            for _ in range(200):
                yield StreamDelta(kind="text", text="x")
            yield ModelChatResult(
                message=ChatMessage(role="assistant", content="x" * 200),
                stop_reason="end_turn",
                usage=TokenUsage(),
            )

    agent = _ReactAgent(
        tools=[],
        pattern="react",
        manifest_id="m",
        manifest_version="1",
        system_prompt="s",
        model_spec=ModelSpec(id="m"),
        settings=None,
        recursion_limit=1,
    )
    deltas = [
        item
        async for item in agent._stream_one_turn(_ManyDeltas(), [], [], "t-stream", "acme")
        if getattr(item, "event", None) == "text_delta"
    ]
    assert len(deltas) == 200
    assert len(remote_checks) == 1
    await steer.release_run_queue("acme", "t-stream")


def _agent() -> object:
    from felix.manifests.schema import ModelSpec
    from felix.patterns.react import _ReactAgent

    return _ReactAgent(
        tools=[],
        pattern="react",
        manifest_id="m",
        manifest_version="1",
        system_prompt="s",
        model_spec=ModelSpec(id="m"),
        settings=None,
        recursion_limit=1,
    )


async def test_a_stream_only_client_does_not_ask_the_store_per_delta(
    monkeypatch: pytest.MonkeyPatch, remote_checks: list[str]
) -> None:
    """The `model.stream` fallback -- plugin clients without `stream_turn` -- is throttled too."""
    monkeypatch.setattr(steer, "time", SimpleNamespace(monotonic=lambda: 100.0))

    class _StreamOnly:
        model_id = "m"

        async def stream(self, messages, tools, opts=None):
            for _ in range(200):
                yield "x"

    deltas = [
        item
        async for item in _agent()._stream_one_turn(_StreamOnly(), [], [], "t-stream-only", "acme")
        if getattr(item, "event", None) == "text_delta"
    ]
    assert len(deltas) == 200
    assert len(remote_checks) == 1
    await steer.release_run_queue("acme", "t-stream-only")


async def test_a_stop_raised_mid_stream_ends_it_at_the_next_delta(
    monkeypatch: pytest.MonkeyPatch, remote_checks: list[str]
) -> None:
    """Throttled, not ignored: a Stop on this process ends the turn on the very next delta."""
    from felix_ai.types import StreamDelta

    monkeypatch.setattr(steer, "time", SimpleNamespace(monotonic=lambda: 100.0))

    class _StopsAfterFive:
        model_id = "m"

        async def stream_turn(self, messages, tools, opts=None):
            for i in range(200):
                if i == 5:
                    await steer.request_abort("acme", "t-stop")
                yield StreamDelta(kind="text", text="x")

    try:
        deltas = [
            item
            async for item in _agent()._stream_one_turn(_StopsAfterFive(), [], [], "t-stop", "acme")
            if getattr(item, "event", None) == "text_delta"
        ]
    finally:
        await steer.release_run_queue("acme", "t-stop")
    assert len(deltas) == 5
