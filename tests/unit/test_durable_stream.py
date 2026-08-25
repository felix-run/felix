"""`POST /chat/stream` honours `spec.execution.mode: durable`.

`POST /chat` enqueues a fiber and returns 202 with a `resume_token`. This endpoint did
not mention the field at all, so a manifest asking for durable execution got it on one
route and was silently ignored on the other — the run went inline, and a client
disconnect tore it down.

Streaming the run keeps the SSE contract a caller of this endpoint already has, and it
delivers what durable is actually for: a disconnect tears down the *poll*, not the run.
That is the opposite of the transient path, where a hung-up client deliberately kills
the run so it stops burning tokens.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.config import Settings
from httpx import ASGITransport, AsyncClient


def _settings() -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        object_store="memory",
        database_url="memory://durable",
        redis_url="",
        stream_resume_poll_seconds=0.1,
        stream_resume_poll_max_seconds=0.1,
    )


def _client(settings: Settings) -> AsyncClient:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0)


def _frames(body: str) -> list[dict[str, Any]]:
    out = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                out.append(json.loads(line[6:]))
    return out


@pytest.fixture
def durable(monkeypatch: pytest.MonkeyPatch):
    """A manifest in durable mode, with the fiber machinery stubbed.

    The fiber store and the worker are not what is under test here — whether this route
    routes to them is.
    """
    runs: dict[str, dict[str, Any]] = {}
    states: list[str] = []

    async def _start(*_a: Any, **kw: Any) -> dict[str, Any]:
        # `setdefault`, so a test can stage the run's outcome before the request.
        runs.setdefault("token-1", {"status": "pending", "final": {}, "error": ""})
        return {
            "status": "accepted",
            "resume_token": "token-1",
            "fiber_id": "token-1",
            "expires_at": 1 << 62,
            "thread_id": kw.get("thread_id"),
        }

    async def _get(_settings: Any, _tenant: str, token: str) -> dict[str, Any] | None:
        row = runs.get(token)
        if row is None:
            return None
        if states:
            row["status"] = states.pop(0)
        return dict(row)

    import felix.durability.runs as runs_mod

    monkeypatch.setattr(runs_mod, "start_durable_chat", _start)
    monkeypatch.setattr(runs_mod, "get_durable_run", _get)

    return {"runs": runs, "states": states}


async def _post_stream(client: AsyncClient, manifest: str = "quick") -> str:
    body = ""
    async with client.stream(
        "POST", "/chat/stream", json={"manifest": manifest, "messages": [{"role": "user", "content": "hi"}]}
    ) as resp:
        assert resp.status_code == 200, (resp.status_code, await resp.aread())
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for chunk in resp.aiter_text():
            body += chunk
    return body


@pytest.mark.asyncio
async def test_a_durable_manifest_streams_the_run_instead_of_running_inline(
    durable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _force_durable(monkeypatch)
    durable["states"].extend(["running", "completed"])
    durable["runs"]["token-1"] = {
        "status": "pending",
        "final": {"role": "assistant", "content": "done"},
        "error": "",
    }

    async with _client(settings) as client:
        body = await _post_stream(client)

    events = [f.get("event") for f in _frames(body)]
    assert "run_accepted" in events, f"no acceptance frame: {events}"
    assert "final" in events, f"the run never reported a result: {events}"
    assert body.rstrip().endswith("[DONE]")


@pytest.mark.asyncio
async def test_the_first_frame_carries_the_resume_token(
    durable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that drops before the run finishes must be able to come back to
    `GET /chat/runs/{token}` rather than starting over."""
    settings = _settings()
    _force_durable(monkeypatch)
    durable["states"].append("completed")

    async with _client(settings) as client:
        body = await _post_stream(client)

    first = _frames(body)[0]
    assert first["event"] == "run_accepted"
    assert first["data"]["resume_token"] == "token-1"


@pytest.mark.asyncio
async def test_a_failed_run_reports_the_failure_rather_than_closing_quietly(
    durable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _force_durable(monkeypatch)
    durable["states"].append("failed")
    durable["runs"]["token-1"] = {"status": "pending", "final": {}, "error": "model_unavailable"}

    async with _client(settings) as client:
        body = await _post_stream(client)

    assert "event: error" in body, "a failed run closed the stream with no error frame"
    assert "model_unavailable" in body


@pytest.mark.asyncio
async def test_a_transient_manifest_is_untouched() -> None:
    """The default path must not have moved: `quick` is transient, and it should still
    stream the agent rather than a run."""
    settings = _settings()
    async with _client(settings) as client:
        body = await _post_stream(client)
    assert "run_accepted" not in body, "a transient manifest was enqueued as durable"


def _force_durable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every resolved manifest durable, without editing a bundled file."""
    from felix_api.routes import chat as chat_mod

    real = chat_mod.resolve_tenant_manifest

    async def _resolve(*a: Any, **k: Any) -> Any:
        resolved = await real(*a, **k)
        # A copy. `resolve_tenant_manifest` returns the object held in the resolver's
        # cache, so mutating it here left `quick` durable for every later test in the
        # process -- which is how this suite first hung. Worth knowing that the cache
        # hands out a mutable shared manifest; nothing in the product mutates one, but
        # nothing stops it either.
        resolved.manifest = resolved.manifest.model_copy(deep=True)
        resolved.manifest.spec.execution.mode = "durable"
        return resolved

    monkeypatch.setattr(chat_mod, "resolve_tenant_manifest", _resolve)
