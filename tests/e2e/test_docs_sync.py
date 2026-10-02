"""`felix_client.docs_sync` against the real `/documents` routes.

`FelixClient` opens its own `httpx.AsyncClient` per call, so the booted app's ASGI transport is
bound in underneath it: every ingest, listing and delete goes through the middleware, the
scope check and the store, as `felix ingest-docs` does against a deployment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from felix_client import FelixClient, docs_sync
from felix_client.docs_sync import read_pages, sync_pages

_SITE = "https://docs.example"


@pytest.fixture
def site(tmp_path: Path) -> Path:
    (tmp_path / "guide").mkdir()
    (tmp_path / "guide" / "durable.mdx").write_text(
        "---\ntitle: Durable runs\n---\nA durable run returns a resume_token and survives a disconnect."
    )
    (tmp_path / "guide" / "auth.mdx").write_text(
        "---\ntitle: Auth\n---\nAPI keys live in FELIX_AUTH_API_KEYS."
    )
    return tmp_path


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> Any:
    def bind(app: Any) -> FelixClient:
        transport = app.client._transport
        real = httpx.AsyncClient

        class _Bound(real):  # type: ignore[misc,valid-type]
            def __init__(self, *a: Any, **k: Any) -> None:
                k["transport"] = transport
                super().__init__(*a, **k)

        monkeypatch.setattr(httpx, "AsyncClient", _Bound)
        return FelixClient(base_url="http://felix.test")

    return bind


async def _sources(client: FelixClient) -> list[str]:
    return sorted(i["source"] for i in (await client.list_documents(limit=500))["items"])


async def test_pages_land_at_their_urls_and_are_searchable(boot: Any, bound: Any, site: Path) -> None:
    pages, _ = read_pages(site, site_url=_SITE)
    async with boot([]) as app:
        client = bound(app)
        result = await sync_pages(client, pages, site_url=_SITE)
        hits = await app.client.get("/documents/search", params={"q": "resume_token", "limit": 1})
        listed = await _sources(client)
    assert sorted(s for s, _ in result.ingested) == [f"{_SITE}/guide/auth/", f"{_SITE}/guide/durable/"]
    assert all(chunks >= 1 for _, chunks in result.ingested)
    assert listed == [f"{_SITE}/guide/auth/", f"{_SITE}/guide/durable/"]
    assert hits.json()["items"][0]["source"] == f"{_SITE}/guide/durable/"


async def test_a_second_sync_replaces_rather_than_duplicates(boot: Any, bound: Any, site: Path) -> None:
    pages, _ = read_pages(site, site_url=_SITE)
    async with boot([]) as app:
        client = bound(app)
        await sync_pages(client, pages, site_url=_SITE)
        await sync_pages(client, pages, site_url=_SITE)
        assert len(await _sources(client)) == 2


async def test_prune_removes_what_the_site_dropped_and_nothing_outside_it(
    boot: Any, bound: Any, site: Path
) -> None:
    async with boot([]) as app:
        client = bound(app)
        await client.ingest_document("Old", "Removed from the site.", source=f"{_SITE}/guide/old/")
        await client.ingest_document("Runbook", "Someone else's.", source="https://wiki.example/runbook")
        # A page retitled: same URL, new title, so the old (source, title) is stale too.
        await client.ingest_document("Durable runs (old title)", "x", source=f"{_SITE}/guide/durable/")
        pages, _ = read_pages(site, site_url=_SITE)
        dry = await sync_pages(client, pages, site_url=_SITE, prune=True, dry_run=True)
        assert len(await _sources(client)) == 3  # a dry run changes nothing
        result = await sync_pages(client, pages, site_url=_SITE, prune=True)
        remaining = (await client.list_documents(limit=500))["items"]
    assert sorted(dry.pruned) == sorted(result.pruned) == [f"{_SITE}/guide/durable/", f"{_SITE}/guide/old/"]
    assert sorted((i["source"], i["title"]) for i in remaining) == [
        (f"{_SITE}/guide/auth/", "Auth"),
        (f"{_SITE}/guide/durable/", "Durable runs"),
        ("https://wiki.example/runbook", "Runbook"),
    ]


async def test_a_failed_page_is_reported_and_blocks_prune(boot: Any, bound: Any, site: Path) -> None:
    """Pruning after a partial sync could delete a document whose replacement was the failure."""
    (site / "guide" / "huge.mdx").write_text("---\ntitle: Huge\n---\n" + "x" * 800_000)
    pages, _ = read_pages(site, site_url=_SITE)
    async with boot([]) as app:
        client = bound(app)
        await client.ingest_document("Old", "Removed from the site.", source=f"{_SITE}/guide/old/")
        result = await sync_pages(client, pages, site_url=_SITE, prune=True)
        remaining = await _sources(client)
    assert [s for s, _ in result.failed] == [f"{_SITE}/guide/huge/"]
    assert "HTTP 4" in result.failed[0][1]
    assert len(result.ingested) == 2
    assert result.pruned == []
    assert f"{_SITE}/guide/old/" in remaining


async def test_a_full_listing_refuses_to_prune(
    boot: Any, bound: Any, site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docs_sync, "LIST_CEILING", 2)
    pages, _ = read_pages(site, site_url=_SITE)
    async with boot([]) as app:
        client = bound(app)
        await client.ingest_document("Old", "Removed.", source=f"{_SITE}/guide/old/")
        result = await sync_pages(client, pages, site_url=_SITE, prune=True)
        remaining = await _sources(client)
    assert result.pruned == []
    assert "partial view" in result.failed[0][1]
    assert f"{_SITE}/guide/old/" in remaining


async def test_more_stale_documents_than_the_limit_deletes_none(boot: Any, bound: Any, site: Path) -> None:
    """A --site-url wider than the pages synced looks like the site dropping many pages at once."""
    pages, _ = read_pages(site, site_url=_SITE)
    async with boot([]) as app:
        client = bound(app)
        for n in range(3):
            await client.ingest_document(f"Blog {n}", "A post.", source=f"{_SITE}/blog/{n}/")
        refused = await sync_pages(client, pages, site_url=_SITE, prune=True, max_prune=2)
        allowed = await sync_pages(client, pages, site_url=_SITE, prune=True, max_prune=3)
        remaining = await _sources(client)
    assert refused.pruned == [] and "3 documents would be pruned" in refused.failed[0][1]
    assert sorted(allowed.pruned) == [f"{_SITE}/blog/{n}/" for n in range(3)]
    assert remaining == [f"{_SITE}/guide/auth/", f"{_SITE}/guide/durable/"]


async def test_a_listing_error_is_reported_not_raised(
    boot: Any, bound: Any, site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages, _ = read_pages(site, site_url=_SITE)
    async with boot([]) as app:
        client = bound(app)

        async def forbidden(*, limit: int = 100) -> Any:
            request = httpx.Request("GET", "http://felix.test/documents")
            raise httpx.HTTPStatusError("403", request=request, response=httpx.Response(403, request=request))

        monkeypatch.setattr(client, "list_documents", forbidden)
        result = await sync_pages(client, pages, site_url=_SITE, prune=True)
    assert len(result.ingested) == 2
    assert "listing the corpus to prune: HTTP 403" in result.failed[0][1]
