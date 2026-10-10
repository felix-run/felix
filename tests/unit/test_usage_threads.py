"""Usage rows carry the thread they were spent on, and `GET /usage/threads` groups by it.

`usage_events` had a tenant, a manifest and a model and no thread, so "what did this
conversation cost" could not be asked of the harness at all. The row now carries the run's
`ctx.thread_id` — the `{tenant}:{suffix}` the audit payload's `thread_id` carries, so the two
join — and `''` for a call outside any thread. The store's grouping is under conformance on
both arms (`tests/conformance/test_usage_store.py`); this file covers the write path that
fills the column, the plugin sink, and the routes' scoping.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.patterns.model import ModelChatResult, TokenUsage, record_usage
from felix.patterns.types import ChatMessage
from felix.usage import store as usage_store


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "memory://usage-threads",
        "object_store": "memory",
        "redis_url": "",
        "allow_insecure": True,
        "auth_mode": "none",
        "host": "127.0.0.1",
        "environment": "development",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _result() -> ModelChatResult:
    return ModelChatResult(
        message=ChatMessage(role="assistant", content="x"),
        stop_reason="end_turn",
        usage=TokenUsage(input=100, output=10),
    )


@pytest.fixture(autouse=True)
def _clean() -> Any:
    usage_store.clear_memory()
    yield
    usage_store.clear_memory()


def _rows() -> list[dict[str, Any]]:
    return usage_store.pending_buffer().snapshot()


# --- the write path ----------------------------------------------------------------------


async def test_record_usage_records_the_runs_thread() -> None:
    ctx = RequestContext(
        settings=_settings(),
        auth=AuthContext(principal_sub="alice", tenant_id="acme", anonymous=False),
        manifest_id="support",
        thread_id="acme:t-1",
    )
    async with async_run_with_context(ctx):
        record_usage(_result(), manifest_id="support", model_id="fast")
    (row,) = _rows()
    assert row["thread_id"] == "acme:t-1", "the stored form, the one the audit payload carries"


async def test_a_call_with_no_thread_records_empty() -> None:
    ctx = RequestContext(
        settings=_settings(),
        auth=AuthContext(principal_sub="alice", tenant_id="acme", anonymous=False),
        manifest_id="support",
    )
    async with async_run_with_context(ctx):
        record_usage(_result(), manifest_id="support")
    record_usage(_result(), manifest_id="maintenance")  # no context at all
    assert [r["thread_id"] for r in _rows()] == ["", ""]


def test_record_tokens_defaults_the_thread_to_empty() -> None:
    usage_store.record_tokens(_settings(), tenant_id="acme", manifest_id="m")
    (row,) = _rows()
    assert row["thread_id"] == ""


# --- the plugin sink ---------------------------------------------------------------------


class _OldSink:
    """A sink written against the four keywords the contract had before the column."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, *, tenant_id: str, manifest_id: str, model_id: str, usage: Any) -> None:
        self.calls.append({"tenant_id": tenant_id, "manifest_id": manifest_id})


class _ThreadSink:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.mark.parametrize("sink_cls", [_OldSink, _ThreadSink])
async def test_the_plugin_sink_gets_the_thread_only_if_it_takes_one(
    sink_cls: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.plugins import get_registry

    sink = sink_cls()
    monkeypatch.setattr(get_registry(), "_usage_sink_factory", lambda _settings: sink)
    ctx = RequestContext(
        settings=_settings(),
        auth=AuthContext(principal_sub="alice", tenant_id="acme", anonymous=False),
        manifest_id="support",
        thread_id="acme:t-1",
    )
    async with async_run_with_context(ctx):
        record_usage(_result(), manifest_id="support", model_id="fast")
    (call,) = sink.calls
    assert call["tenant_id"] == "acme", "the old sink is still called, not dropped by a TypeError"
    if sink_cls is _ThreadSink:
        assert call["thread_id"] == "acme:t-1"


# --- the routes --------------------------------------------------------------------------

_KEYS = (
    '{"sk-acme": {"tenant_id": "acme", "sub": "a", "scopes": ["usage:read"]},'
    ' "sk-other": {"tenant_id": "other", "sub": "o", "scopes": ["usage:read"]},'
    ' "sk-none": {"tenant_id": "acme", "sub": "n", "scopes": []}}'
)


async def _seed(settings: Settings) -> None:
    for tenant, thread, tokens in (
        ("acme", "acme:t-1", 100),
        ("acme", "acme:t-1", 200),
        ("acme", "acme:t-2", 300),
        ("acme", "", 400),
        ("other", "other:t-1", 9_000),
    ):
        usage_store.record_tokens(
            settings,
            tenant_id=tenant,
            manifest_id="m",
            model_id="fast",
            tokens_input=tokens,
            thread_id=thread,
        )
    await usage_store.flush_pending(settings)


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_the_listing_filters_by_the_threads_suffix_within_the_tenant() -> None:
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    settings = _settings(auth_mode="api_key", auth_api_keys=_KEYS)
    await _seed(settings)
    app = create_app(settings=settings, plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        every = (await client.get("/usage", headers=_auth("sk-acme"))).json()["items"]
        assert {r["thread_id"] for r in every} == {"acme:t-1", "acme:t-2", ""}

        one = (await client.get("/usage?thread_id=t-1", headers=_auth("sk-acme"))).json()["items"]
        assert sorted(r["tokens_input"] for r in one) == [100, 200]
        assert {r["thread_id"] for r in one} == {"acme:t-1"}, "rows carry the stored form"

        none = (await client.get("/usage?thread_id=", headers=_auth("sk-acme"))).json()["items"]
        assert [r["tokens_input"] for r in none] == [400], "empty is the calls outside a thread"

        # The suffix is composed under the caller's tenant: the same suffix from `other` sees its own.
        theirs = (await client.get("/usage?thread_id=t-1", headers=_auth("sk-other"))).json()["items"]
        assert [r["tokens_input"] for r in theirs] == [9_000]

        # A full id is not a suffix, and naming another tenant's thread is refused, not filtered.
        bad = await client.get("/usage?thread_id=other:t-1", headers=_auth("sk-acme"))
        assert bad.status_code == 400
        assert bad.json()["detail"] == "invalid_thread_id"


async def test_the_threads_route_groups_the_callers_tenant_under_usage_read() -> None:
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    settings = _settings(auth_mode="api_key", auth_api_keys=_KEYS)
    await _seed(settings)
    app = create_app(settings=settings, plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        acme = (await client.get("/usage/threads", headers=_auth("sk-acme"))).json()
        assert set(acme) == {"since_ms", "until_ms", "items", "totals", "truncated"}
        assert {i["thread_id"]: i["calls"] for i in acme["items"]} == {"acme:t-1": 2, "acme:t-2": 1, "": 1}
        assert set(acme["items"][0]) == {
            "thread_id",
            "calls",
            "tokens_input",
            "tokens_output",
            "cache_creation",
            "cache_read",
            "cost_usd",
            "first_ts",
            "last_ts",
        }
        assert acme["totals"]["tokens_input"] == 1_000, "not the other tenant's"
        assert acme["truncated"] is False

        page = (await client.get("/usage/threads?limit=1", headers=_auth("sk-acme"))).json()
        assert len(page["items"]) == 1 and page["truncated"] is True
        assert page["totals"]["calls"] == 4, "the totals cover every thread, not the page"

        other = (await client.get("/usage/threads", headers=_auth("sk-other"))).json()
        assert [i["thread_id"] for i in other["items"]] == ["other:t-1"]

        assert (await client.get("/usage/threads", headers=_auth("sk-none"))).status_code == 403
        assert (await client.get("/usage/threads")).status_code == 401
        assert (
            await client.get("/usage/threads?since_ms=5&until_ms=5", headers=_auth("sk-acme"))
        ).status_code == 422
        assert (await client.get("/usage/threads?limit=0", headers=_auth("sk-acme"))).status_code == 422
        assert (await client.get("/usage/threads?limit=201", headers=_auth("sk-acme"))).status_code == 422
