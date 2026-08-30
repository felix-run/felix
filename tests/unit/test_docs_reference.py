"""The `/docs` reference UI.

`/docs` used to be Swagger UI, which FastAPI mounts by default. These pin the
replacement: Scalar, served from the same `/openapi.json`, still public and still
unauthenticated in every auth mode.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi import FastAPI
from felix.config import Settings
from felix_api.app import create_app
from felix_api.docs import SCALAR_JS_SRI, SCALAR_JS_URL, register_docs, scalar_html
from httpx import ASGITransport, AsyncClient


def _settings(name: str, **over) -> Settings:
    """Explicit settings, so a repo `.env` cannot reach into the test.

    `redis_url=""` matters: with the ambient value these tests open a real TCP
    connection to localhost:6379 on every request, fail, and degrade through
    ResilientRateLimiter.
    """
    return Settings(
        database_url=f"memory://{name}",
        object_store="memory",
        allow_insecure=True,
        host="127.0.0.1",
        environment="development",
        redis_url="",
        **{"auth_mode": "none", **over},
    )


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_docs_serves_scalar_not_swagger_ui() -> None:
    app = create_app(settings=_settings("docs-scalar"), plugins=[])
    async with _client(app) as client:
        res = await client.get("/docs")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    body = res.text
    assert SCALAR_JS_URL in body
    assert "swagger-ui" not in body.lower()
    assert res.headers["cache-control"] == "private, max-age=300"
    # The runtime `servers` entry is what makes a curl snippet copy-pasteable; the
    # spec has no `servers` block to fall back on. Unprefixed, it is the bare origin.
    assert _server_expr(body) == ""
    # The two config values that are decisions rather than Scalar's own defaults.
    config = _config(body)
    assert config["layout"] == "modern"
    assert config["defaultHttpClient"] == {"targetKey": "shell", "clientKey": "curl"}


@pytest.mark.asyncio
async def test_openapi_and_redoc_are_untouched() -> None:
    """Only Swagger UI gave up its path. `docs_url` and `redoc_url` sit adjacent."""
    app = create_app(settings=_settings("docs-siblings"), plugins=[])
    async with _client(app) as client:
        spec = await client.get("/openapi.json")
        redoc = await client.get("/redoc")

    assert spec.status_code == 200
    assert spec.json()["info"]["title"] == "Felix"
    assert redoc.status_code == 200
    # The reference documents the API, it is not part of it.
    assert "/docs" not in spec.json()["paths"]


def _config(page: str) -> dict:
    """Scalar's config object, parsed out of the inline script."""
    match = re.search(r"^\s*\.\.\.(\{.+\}),$", page, re.MULTILINE)
    assert match, page
    return json.loads(match.group(1))


def _spec_url(page: str) -> str:
    """The `url` Scalar is told to render, read out of the page's config."""
    matches = re.findall(r'"url":\s*"([^"]+)"', page)
    assert len(matches) == 1, f"expected one spec url, got {matches}"
    return matches[0]


def _server_expr(page: str) -> str:
    """The prefix the page appends to the origin for its `servers` entry."""
    match = re.search(r'window\.location\.origin\s*\+\s*"([^"]*)"', page)
    assert match, page
    return match.group(1)


@pytest.mark.asyncio
async def test_page_follows_a_relocated_spec_path() -> None:
    """Two sources for the spec path is how /docs ends up rendering a 404."""
    app = FastAPI(title="Felix", openapi_url="/v1/openapi.json", docs_url=None)
    register_docs(app)
    async with _client(app) as client:
        page = await client.get("/docs")
        spec = await client.get(_spec_url(page.text))

    assert _spec_url(page.text) == "/v1/openapi.json"
    # The point is reachability, not spelling: the page must name a URL that answers.
    assert spec.status_code == 200


@pytest.mark.asyncio
async def test_page_follows_the_request_s_root_path() -> None:
    """Behind a proxy prefix, a precomputed spec path is a 404.

    FastAPI resolves it per request for `/redoc` and did for Swagger UI, so a static
    page here would leave `/redoc` working and `/docs` blank — and every curl snippet
    missing the prefix.
    """
    app = FastAPI(title="Felix", docs_url=None, root_path="/felix")
    register_docs(app)
    transport = ASGITransport(app=app, root_path="/felix")
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        page = await client.get("/felix/docs")
        spec = await client.get(_spec_url(page.text))

    assert _spec_url(page.text) == "/felix/openapi.json"
    assert spec.status_code == 200
    assert _server_expr(page.text) == "/felix"


@pytest.mark.asyncio
async def test_docs_is_public_under_api_key_auth() -> None:
    """A reference UI behind a 401 is useless — but the exemption must be narrow."""
    app = create_app(
        settings=_settings("docs-auth", auth_mode="api_key", api_keys='{"k":{"scopes":["*"]}}'),
        plugins=[],
    )
    async with _client(app) as client:
        docs = await client.get("/docs")
        # Control: without it a 200 from an app whose auth middleware stopped running
        # looks exactly like a working exemption.
        guarded = await client.post("/chat", json={"input": "hi"})

    assert docs.status_code == 200
    assert SCALAR_JS_URL in docs.text
    assert guarded.status_code == 401


def test_bundle_is_pinned_and_carries_integrity_attributes() -> None:
    """An unpinned tag changes the page under a deployment that changed nothing.

    That the hash *matches* the bundle needs the network, so it lives in
    `scripts/check-scalar-sri.py`, which CI runs with FELIX_REQUIRE_SCALAR_SRI=1.
    """
    assert "@latest" not in SCALAR_JS_URL
    assert re.search(r"@scalar/api-reference@\d+\.\d+\.\d+/", SCALAR_JS_URL)
    assert SCALAR_JS_SRI.startswith("sha384-")

    html = scalar_html(openapi_url="/openapi.json", title="t")
    assert f'integrity="{SCALAR_JS_SRI}"' in html
    assert 'crossorigin="anonymous"' in html


def test_interpolated_values_cannot_break_out_of_their_tags() -> None:
    """Nothing feeds these today; `create_app` hardcodes the title. The escaping is
    what keeps that true once a title or spec path becomes caller-supplied."""
    page = scalar_html(openapi_url="/openapi.json</script><script>x=1", title="t")
    assert "</script><script>x=1" not in page
    assert "<\\/script>" in page

    titled = scalar_html(openapi_url="/openapi.json", title="</title><script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in titled
    assert "&lt;/title&gt;" in titled
