"""A durable chat run over HTTP, with the fiber store and the worker scripted."""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from httpx import AsyncClient

from tests.support.factories import make_settings


def durable_settings() -> Settings:
    """`make_settings` with the stream polls fast enough for a test to watch a run end."""
    return make_settings(
        redis_url="",
        stream_resume_poll_seconds=0.1,
        stream_resume_poll_max_seconds=0.1,
        # Only the reattach stream reads this, and it is what lets that stream *end*: the
        # durable loop is bounded by the run's terminal status instead. Left at its 300s
        # default the resume test holds the connection for five minutes.
        stream_resume_idle_seconds=0.2,
    )


def force_durable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every resolved manifest durable, without editing a bundled file."""
    from felix_api.routes import chat as chat_mod

    real = chat_mod.resolve_tenant_manifest

    async def _resolve(*a: Any, **k: Any) -> Any:
        resolved = await real(*a, **k)
        # A copy: the resolver hands out the object held in its cache, and mutating that
        # one leaves `quick` durable for every later test in the process.
        resolved.manifest = resolved.manifest.model_copy(deep=True)
        resolved.manifest.spec.execution.mode = "durable"
        return resolved

    monkeypatch.setattr(chat_mod, "resolve_tenant_manifest", _resolve)


_UNSET = object()


def stub_fiber(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_start: Any = None,
    on_poll: list[Any] | None = None,
    statuses: list[str],
    expires_at: int = 1 << 62,
    error: str = "",
) -> dict[str, Any]:
    """Stand in for the fiber store and the worker.

    `on_start` runs when the run is enqueued and each `on_poll` entry runs before the
    matching status read, which is how a test says "the worker appended this much by
    now" without a worker.

    The *session store* is real (`memory://`) — only the fiber row and the worker are
    scripted, because a test needs to say exactly when the worker appended relative to
    when the stream polled, and a real worker cannot be asked that. The keys returned here
    are pinned against the real `start_durable_chat` by
    `test_the_accepted_shape_matches_what_the_real_start_returns`, so this fixture cannot
    drift into describing a run shape the harness does not produce.
    """
    # A sentinel, not None: `assert seen["thread_id"] is None` is the assertion the
    # anonymous-run test makes, and initialising to None would let it pass vacuously if
    # `_start` were never called at all.
    seen: dict[str, Any] = {"thread_id": _UNSET}
    pending = list(statuses)
    steps = list(on_poll or [])

    async def _start(*_a: Any, **kw: Any) -> dict[str, Any]:
        seen["thread_id"] = kw.get("thread_id")
        if on_start is not None:
            await on_start()
        return {
            "status": "accepted",
            "resume_token": "fiber-1",
            "fiber_id": "fiber-1",
            "expires_at": expires_at,
            "thread_id": kw.get("thread_id"),
        }

    async def _get(_settings: Any, _tenant: str, token: str) -> dict[str, Any] | None:
        if token != "fiber-1":
            return None
        if steps:
            step = steps.pop(0)
            if step is not None:
                await step()
        status = pending.pop(0) if pending else "completed"
        return {
            "status": status,
            "fiber_id": token,
            "resume_token": token,
            "expires_at": expires_at,
            "final": {"role": "assistant", "content": "42 it is"},
            "error": error,
            "manifest_id": "quick",
        }

    import felix.durability.runs as runs_mod

    monkeypatch.setattr(runs_mod, "start_durable_chat", _start)
    monkeypatch.setattr(runs_mod, "get_durable_run", _get)
    return seen


async def post_stream(client: AsyncClient, thread: str | None) -> str:
    payload: dict[str, Any] = {"manifest": "quick", "messages": [{"role": "user", "content": "hi"}]}
    if thread is not None:
        payload["thread_id"] = thread
    body = ""
    async with client.stream("POST", "/chat/stream", json=payload) as resp:
        assert resp.status_code == 200, (resp.status_code, await resp.aread())
        async for chunk in resp.aiter_text():
            body += chunk
    return body


async def pending_gate(settings: Settings, thread: str, **kw: Any) -> dict[str, Any]:
    """A pending approval on `thread`, written the way the governance wrapper writes one."""
    from felix.approvals.store import create_pending

    return await create_pending(
        settings,
        "default",
        tool_name=kw.pop("tool_name", "write_file"),
        call_signature=kw.pop("call_signature", "sig-1"),
        manifest_id="quick",
        args=kw.pop("args", {"path": "notes.txt"}),
        thread_id=thread,
        **kw,
    )
