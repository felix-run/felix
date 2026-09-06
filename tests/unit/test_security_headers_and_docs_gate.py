"""Every response carries the browser headers; the API reference is behind auth.

The API is JSON and SSE served to programs, but every response can be loaded by a
browser and the docs page is one. Nothing set `nosniff`, `DENY` or a referrer policy, and
`/docs`, `/openapi.json` and `/redoc` — a map of every route, management ones included —
were public in every auth mode and exempt from the rate limiter, which under `api_key` left
three paths where a credential could be guessed unthrottled. `/redoc` is gone: one
reference surface, with one CSP.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from felix.config import Settings
from felix_api.app import create_app
from httpx import ASGITransport, AsyncClient

KEYS = '{"sk-a": {"tenant_id": "acme", "sub": "a", "scopes": ["*"]}}'


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "memory://headers",
        "object_store": "memory",
        "allow_insecure": True,
        "auth_mode": "none",
        "host": "127.0.0.1",
        "environment": "development",
        "rate_limit": 100_000,
    }
    base.update(kw)
    return Settings(**base)


def _client(settings: Settings, **transport: Any) -> AsyncClient:
    app = create_app(settings=settings, plugins=[])
    return AsyncClient(transport=ASGITransport(app=app, **transport), base_url="http://test")


@pytest.mark.asyncio
async def test_every_response_carries_the_browser_headers() -> None:
    async with _client(_settings()) as client:
        ok = await client.get("/health")
        missing = await client.get("/no/such/route")
    for resp in (ok, missing):
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert resp.headers["cache-control"] == "no-store"
        assert "strict-transport-security" not in resp.headers, "HSTS on a plaintext response pins a policy"


@pytest.mark.asyncio
async def test_a_401_and_a_413_carry_them_too() -> None:
    async with _client(_settings(auth_mode="api_key", auth_api_keys=KEYS)) as client:
        denied = await client.get("/manifests")
        assert denied.status_code == 401
        assert denied.headers["x-content-type-options"] == "nosniff"
        too_big = await client.post(
            "/chat",
            content=b"x" * (3 * 1024 * 1024),
            headers={"content-type": "application/json", "authorization": "Bearer sk-a"},
        )
        assert too_big.status_code == 413
        assert too_big.headers["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_hsts_only_over_tls_and_only_from_a_trusted_proxy_claim() -> None:
    settings = _settings()
    app = create_app(settings=settings, plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        resp = await client.get("/health")
    assert resp.headers["strict-transport-security"] == "max-age=15552000; includeSubDomains"

    # A forwarded-proto claim is attacker-controlled unless a proxy you operate sets it.
    async with _client(settings) as client:
        spoofed = await client.get("/health", headers={"x-forwarded-proto": "https"})
    assert "strict-transport-security" not in spoofed.headers

    trusted = _settings(trusted_client_ip_header="x-forwarded-for")
    async with _client(trusted) as client:
        forwarded = await client.get("/health", headers={"x-forwarded-proto": "https"})
        # The proxy appends; its entry is the last one and the client's is the first.
        client_first = await client.get("/health", headers={"x-forwarded-proto": "https, http"})
        proxy_last = await client.get("/health", headers={"x-forwarded-proto": "http, https"})
        # A proxy that adds a second header line instead of extending the list.
        two_lines = await client.get(
            "/health", headers=[("x-forwarded-proto", "https"), ("x-forwarded-proto", "http")]
        )
    assert "strict-transport-security" in forwarded.headers
    assert "strict-transport-security" not in client_first.headers
    assert "strict-transport-security" in proxy_last.headers
    assert "strict-transport-security" not in two_lines.headers

    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings=_settings(hsts_max_age_seconds=0), plugins=[])),
        base_url="https://test",
    ) as client:
        off = await client.get("/health")
    assert "strict-transport-security" not in off.headers


@pytest.mark.asyncio
async def test_include_subdomains_is_its_own_switch() -> None:
    """On an apex hostname it pins every sibling for 180 days; dropping it must not mean
    dropping HSTS."""
    app = create_app(settings=_settings(hsts_include_subdomains=False), plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        resp = await client.get("/health")
    assert resp.headers["strict-transport-security"] == "max-age=15552000"


@pytest.mark.asyncio
async def test_a_raw_mounted_app_that_sends_no_headers_key_still_gets_them() -> None:
    """`headers` is optional in the ASGI spec; a plugin's raw mount may omit it."""

    async def bare(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok"})

    app = create_app(settings=_settings(), plugins=[])
    app.mount("/bare", bare)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/bare/x")
    assert resp.status_code == 200 and resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_the_docs_page_has_a_csp_whose_nonce_covers_both_scripts() -> None:
    async with _client(_settings()) as client:
        resp = await client.get("/docs")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp)
    assert nonce, csp
    assert resp.text.count(f'nonce="{nonce.group(1)}"') == 2, "the bundle and the inline config both carry it"
    assert "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in csp.split("style-src")[0]
    script_src = re.search(r"script-src ([^;]+)", csp)
    assert script_src and "'strict-dynamic'" in script_src.group(1)
    assert "https://" not in script_src.group(1), "a host allowlist would let any package on the CDN run"
    assert "object-src 'none'" in csp
    async with _client(_settings()) as client:
        again = await client.get("/docs")
    assert again.headers["content-security-policy"] != csp, "a fresh nonce per response"


@pytest.mark.asyncio
async def test_the_reference_is_behind_auth_unless_the_operator_opens_it() -> None:
    authed = _settings(auth_mode="api_key", auth_api_keys=KEYS)
    async with _client(authed) as client:
        for path in ("/docs", "/openapi.json"):
            assert (await client.get(path)).status_code == 401, path
        assert (
            await client.get("/openapi.json", headers={"authorization": "Bearer sk-a"})
        ).status_code == 200
        assert (await client.get("/health")).status_code == 200, "probes stay public"

    opened = _settings(auth_mode="api_key", auth_api_keys=KEYS, docs_public=True)
    async with _client(opened) as client:
        for path in ("/docs", "/openapi.json"):
            assert (await client.get(path)).status_code == 200, path

    async with _client(_settings()) as client:
        assert (await client.get("/docs")).status_code == 200, "auth_mode=none is public"


def test_opening_the_reference_on_an_authenticated_deployment_is_warned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A legitimate choice, so a warning rather than a refusal — but not a silent one."""
    with caplog.at_level("WARNING", logger="felix.config"):
        _settings(
            auth_mode="api_key", auth_api_keys=KEYS, docs_public=True, environment="production"
        ).validate_runtime()
        warned = [r for r in caplog.records if "FELIX_DOCS_PUBLIC" in r.getMessage()]
    assert len(warned) == 1

    caplog.clear()
    with caplog.at_level("WARNING", logger="felix.config"):
        _settings(auth_mode="api_key", auth_api_keys=KEYS, docs_public=True).validate_runtime()
        _settings(auth_mode="none", docs_public=True, environment="production").validate_runtime()
    assert not [r for r in caplog.records if "FELIX_DOCS_PUBLIC" in r.getMessage()], (
        "development, or nothing to open"
    )
