"""`payload.fresh_thread` gives each firing of a scheduled job a thread of its own.

The default — one thread per job name — is right for a digest that should remember what it
said last week and wrong for a job that works a different ticket every run, where ticket N's
transcript would sit in ticket N+1's context. Driven through the real `run_due_jobs` with only
the manifest resolution and agent build replaced, so the thread the agent is invoked on is
the one the scheduler actually chose.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.jobs import scheduler
from felix.jobs import store as jobs_store
from felix.patterns.types import ChatMessage, InvokeInput, InvokeOutput


def _settings() -> Settings:
    return Settings(
        database_url="memory://jobs", object_store="memory", auth_mode="none", allow_insecure=True
    )


class _Recorder:
    def __init__(self) -> None:
        self.threads: list[str | None] = []

    async def invoke(self, inp: InvokeInput) -> InvokeOutput:
        self.threads.append(inp.thread_id)
        return InvokeOutput(messages=[], final=ChatMessage(role="assistant", content="ok"))


async def _fire_twice(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> list[str | None]:
    import felix.runtime as runtime

    jobs_store._memory_jobs.clear()
    jobs_store._memory_runs.clear()
    settings = _settings()
    rec = _Recorder()

    async def _resolve(_s: Any, _t: Any, name: str, **k: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(manifest=None, sub_agents={})

    async def _build(settings: Settings, **k: Any) -> Any:
        return rec

    monkeypatch.setattr(runtime, "resolve_tenant_manifest", _resolve)
    monkeypatch.setattr(runtime, "build_tenant_agent", _build)
    await jobs_store.put_job(
        settings, "acme", "triage", schedule="60", manifest_id="triage", payload=payload, enabled=True
    )
    assert await scheduler.run_due_jobs(settings, tenant_id="acme") == 1
    # Make it due again: the store advanced next_run_at on the first claim.
    await jobs_store.touch_run(settings, "acme", "triage", last_run_at=0, next_run_at=0, last_status="ok")
    assert await scheduler.run_due_jobs(settings, tenant_id="acme") == 1
    return rec.threads


@pytest.mark.asyncio
async def test_a_job_shares_one_thread_across_firings_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = await _fire_twice(monkeypatch, {"prompt": "digest"})
    assert threads == ["acme:job:triage", "acme:job:triage"]


@pytest.mark.asyncio
async def test_fresh_thread_gives_each_firing_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = await _fire_twice(monkeypatch, {"prompt": "take the next ticket", "fresh_thread": True})
    assert len(threads) == 2 and threads[0] != threads[1], "two firings in one millisecond must still differ"
    assert all(t is not None and t.startswith("acme:job:triage:") for t in threads), threads
