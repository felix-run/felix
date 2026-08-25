"""`FelixClient.prompt` waits for a durable run instead of returning its receipt.

A manifest with `spec.execution.mode: durable` answers 202 with a `resume_token`,
because the run is handed to a worker. `prompt` returned that envelope as though it
were the answer — so a caller switching a manifest to durable got
`{"status": "accepted", …}` where the content used to be. No error, just the wrong
shape, which is the kind of thing that reaches production because nothing raises.

`wait_s` is the escape hatch: `0` returns the receipt immediately for a caller that
wants to hold the token, and a number bounds the wait without turning "still running"
into a failure.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from felix.sdk import FelixClient


def _bind(transport: httpx.MockTransport) -> type[httpx.AsyncClient]:
    real = httpx.AsyncClient

    class _Bound(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            k["transport"] = transport
            super().__init__(*a, **k)

    return _Bound


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    """A scripted Felix, and the request paths the client actually asked for."""
    calls: list[str] = []

    def script(
        statuses: list[str],
        *,
        expires_at: int | None = None,
        durable: bool = True,
        run_status_code: int = 200,
    ) -> None:
        seq = list(statuses)

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("/chat"):
                if not durable:
                    return httpx.Response(200, json={"final": {"content": "inline"}})
                return httpx.Response(
                    202,
                    json={
                        "status": "accepted",
                        "resume_token": "tok",
                        "expires_at": expires_at if expires_at is not None else (1 << 62),
                    },
                )
            if run_status_code != 200:
                return httpx.Response(run_status_code, json={"detail": "run_not_found"})
            status = seq.pop(0) if seq else "running"
            body: dict[str, Any] = {"status": status, "resume_token": "tok"}
            if status == "completed":
                body["final"] = {"role": "assistant", "content": "the answer"}
            if status == "failed":
                body["error"] = "model_unavailable"
            return httpx.Response(200, json=body)

        monkeypatch.setattr(httpx, "AsyncClient", _bind(httpx.MockTransport(handler)))

    return {"script": script, "calls": calls}


def _client() -> FelixClient:
    return FelixClient(base_url="http://felix.test")


@pytest.mark.asyncio
async def test_a_durable_prompt_returns_the_answer_not_the_receipt(server: dict[str, Any]) -> None:
    server["script"](["running", "completed"])

    result = await _client().prompt("hi")

    assert result["status"] == "completed", result
    assert result["final"]["content"] == "the answer"


@pytest.mark.asyncio
async def test_progress_reaches_subscribers(server: dict[str, Any]) -> None:
    """A caller waiting minutes for a run should not wait blind."""
    server["script"](["pending", "running", "completed"])
    client = _client()
    seen: list[str] = []
    client.subscribe(lambda ev: seen.append(str(ev.get("event"))))

    await client.prompt("hi")

    assert "run_accepted" in seen, seen
    assert "run_status" in seen, seen
    assert seen[-1] == "prompt_result", seen


@pytest.mark.asyncio
async def test_wait_s_zero_returns_the_receipt_without_polling(server: dict[str, Any]) -> None:
    server["script"](["completed"])

    result = await _client().prompt("hi", wait_s=0)

    assert result["status"] == "accepted"
    assert result["resume_token"] == "tok"
    assert not any("/chat/runs/" in c for c in server["calls"]), server["calls"]


@pytest.mark.asyncio
async def test_running_out_of_patience_is_not_a_failure(server: dict[str, Any]) -> None:
    """`wait_s` elapsing means the caller stopped waiting, not that the run broke.

    The token still resolves, so reporting `failed` would be a lie the caller acts on —
    it would retry a run that is still going.
    """
    server["script"](["running"] * 20)

    started = time.monotonic()
    result = await _client().prompt("hi", wait_s=1.0)
    elapsed = time.monotonic() - started

    assert result["status"] == "waiting", result
    assert result["resume_token"] == "tok"
    assert elapsed < 5.0, f"waited {elapsed:.1f}s against a 1s budget"


@pytest.mark.asyncio
async def test_a_failed_run_surfaces_its_error(server: dict[str, Any]) -> None:
    server["script"](["failed"])

    result = await _client().prompt("hi")

    assert result["status"] == "failed"
    assert result["error"] == "model_unavailable"


@pytest.mark.asyncio
async def test_an_expired_run_stops_rather_than_polling_forever(server: dict[str, Any]) -> None:
    """The run's own TTL bounds the wait. Past it the result cannot arrive, so polling
    on would hold the caller for nothing."""
    server["script"](["running"] * 30, expires_at=int(time.time() * 1000) + 300)

    result = await _client().prompt("hi")

    assert result["status"] == "expired", result


@pytest.mark.asyncio
async def test_a_missing_run_is_reported_not_retried(server: dict[str, Any]) -> None:
    server["script"]([], run_status_code=404)

    result = await _client().prompt("hi")

    assert result["status"] == "expired"
    assert "run_not_found" in result["error"]


@pytest.mark.asyncio
async def test_a_transient_prompt_is_untouched(server: dict[str, Any]) -> None:
    """The default path must not have moved: a 200 is the answer, with no polling."""
    server["script"]([], durable=False)

    result = await _client().prompt("hi")

    assert result["final"]["content"] == "inline"
    assert not any("/chat/runs/" in c for c in server["calls"]), server["calls"]


def test_the_poll_pacing_is_bounded() -> None:
    from felix.sdk import RUN_POLL_CEILING_SECONDS, RUN_POLL_FACTOR, RUN_POLL_FLOOR_SECONDS

    assert 0 < RUN_POLL_FLOOR_SECONDS <= RUN_POLL_CEILING_SECONDS
    assert 1.0 < RUN_POLL_FACTOR <= 2.0
