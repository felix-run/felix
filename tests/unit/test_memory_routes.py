"""The memory management API: scopes, tenant isolation, and the read/write split.

An agent that remembers across sessions builds a store nobody can see. These routes
are how an operator finds a stale or poisoned fact and removes it — which makes them
both a read surface over model-written text and a write surface that feeds the model,
so the scope split and the tenant scoping are the parts worth asserting.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.memory import store as memory_store
from httpx import ASGITransport, AsyncClient

KEYS = (
    '{"sk-read":{"tenant_id":"acme","sub":"ops","scopes":["memory:read"]},'
    '"sk-write":{"tenant_id":"acme","sub":"ops","scopes":["memory:write"]},'
    '"sk-none":{"tenant_id":"acme","sub":"ops","scopes":["chat:write"]},'
    '"sk-other":{"tenant_id":"other","sub":"ops","scopes":["memory:write"]}}'
)


@pytest.fixture(autouse=True)
def _clean() -> None:
    memory_store._memory_rows.clear()


def _settings() -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="api_key",
        auth_api_keys=KEYS,
        environment="development",
        object_store="memory",
        database_url="memory://mem-routes",
    )


def _client() -> AsyncClient:
    from felix_api.app import create_app

    app = create_app(settings=_settings(), plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.asyncio
async def test_scopes_gate_reads_and_writes() -> None:
    async with _client() as client:
        denied = await client.get("/memory", headers=_auth("sk-none"))
        assert denied.status_code == 403
        assert "memory:read" in denied.json()["detail"]

        assert (await client.get("/memory", headers=_auth("sk-read"))).status_code == 200

        # A reader may not write.
        wrote = await client.post("/memory", json={"content": "A fact."}, headers=_auth("sk-read"))
        assert wrote.status_code == 403
        assert "memory:write" in wrote.json()["detail"]


@pytest.mark.asyncio
async def test_write_scope_implies_read() -> None:
    """`x:write` satisfies `x:read` — the existing rule, applied to a new pair."""
    async with _client() as client:
        assert (await client.get("/memory", headers=_auth("sk-write"))).status_code == 200


@pytest.mark.asyncio
async def test_round_trip_write_list_search_forget() -> None:
    async with _client() as client:
        created = await client.post(
            "/memory",
            json={"content": "The deploy runbook lives in the ops repo.", "manifest_id": "m"},
            headers=_auth("sk-write"),
        )
        assert created.status_code == 200
        mem_id = created.json()["id"]

        listed = await client.get("/memory?manifest_id=m", headers=_auth("sk-read"))
        assert [i["id"] for i in listed.json()["items"]] == [mem_id]

        found = await client.get(
            "/memory/search?q=where is the runbook&manifest_id=m", headers=_auth("sk-read")
        )
        assert found.status_code == 200
        hit = found.json()["items"][0]
        assert hit["id"] == mem_id
        # The channels are what make a surprising result explainable.
        assert "fts" in hit["channels"]

        gone = await client.delete(f"/memory/{mem_id}", headers=_auth("sk-write"))
        assert gone.status_code == 200
        assert gone.json()["status"] == "forgotten"

        after = await client.get("/memory?manifest_id=m", headers=_auth("sk-read"))
        assert after.json()["items"] == []


@pytest.mark.asyncio
async def test_forgetting_something_that_is_not_there_is_a_404() -> None:
    async with _client() as client:
        resp = await client.delete("/memory/nope", headers=_auth("sk-write"))
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_one_tenant_cannot_see_anothers_memory() -> None:
    """The tenant comes from the principal, never from the request."""
    async with _client() as client:
        await client.post("/memory", json={"content": "Acme's private fact."}, headers=_auth("sk-write"))
        seen = await client.get("/memory", headers=_auth("sk-other"))
        assert seen.status_code == 200
        assert seen.json()["items"] == []

        searched = await client.get("/memory/search?q=private fact", headers=_auth("sk-other"))
        assert searched.json()["items"] == []


@pytest.mark.asyncio
async def test_as_of_is_read_only_and_shows_the_earlier_belief() -> None:
    settings = _settings()
    for content, seq in (("Timezone is UTC.", 4), ("Timezone is CET.", 7)):
        await memory_store.put_memory(
            settings,
            "acme",
            content=content,
            manifest_id="m",
            topic_key="user.timezone",
            origin_seq=seq,
        )

    async with _client() as client:
        at5 = await client.get("/memory/as-of/5?manifest_id=m", headers=_auth("sk-read"))
        assert [i["content"] for i in at5.json()["items"]] == ["Timezone is UTC."]

        at9 = await client.get("/memory/as-of/9?manifest_id=m", headers=_auth("sk-read"))
        assert [i["content"] for i in at9.json()["items"]] == ["Timezone is CET."]

        # No write verb on the time-travel surface: rewinding memory is a data-loss
        # primitive, and session rewind is deliberately non-destructive.
        assert (await client.post("/memory/as-of/5", headers=_auth("sk-write"))).status_code == 405


@pytest.mark.asyncio
async def test_oversized_content_is_rejected() -> None:
    """Written content is text the model will later read, so it is bounded."""
    async with _client() as client:
        resp = await client.post("/memory", json={"content": "x" * 5000}, headers=_auth("sk-write"))
        assert resp.status_code == 422
