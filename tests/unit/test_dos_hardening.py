"""Request-level DoS controls.

Rate limiting ran *inside* auth, so a 401 returned before the limiter was consulted and
credential guessing was unthrottled. The limiter was also always in-process (so the
effective ceiling was limit x replicas), keyed per tenant (so under `auth_mode=none`
every caller shared one bucket and any client could 429 the deployment), and never
evicted a key. The body limit trusted `Content-Length`, so a chunked request was
unbounded. `/metrics` was public. And three compute paths had no ceiling.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.security.rate_limit import (
    InMemoryRateLimiter,
    ResilientRateLimiter,
    build_rate_limit_config,
    client_key,
    should_skip_rate_limit,
)


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "memory://dos",
        "object_store": "memory",
        "allow_insecure": True,
        "auth_mode": "none",
        "redis_url": "",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


class _Req:
    def __init__(self, host: str = "1.2.3.4", headers: dict[str, str] | None = None) -> None:
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}


# --- keying ---------------------------------------------------------------------


def test_key_is_per_client_not_per_tenant() -> None:
    """Per-tenant keys meant one shared bucket under auth_mode=none."""
    s = _settings()
    assert client_key(_Req("1.2.3.4"), s) != client_key(_Req("5.6.7.8"), s)


def test_proxy_header_is_ignored_unless_trusted() -> None:
    """The header is attacker-controlled unless a proxy you operate overwrites it."""
    s = _settings()
    key = client_key(_Req("1.2.3.4", {"x-forwarded-for": "9.9.9.9"}), s)
    assert "1.2.3.4" in key


def test_trusted_proxy_header_is_used_when_configured() -> None:
    s = _settings(trusted_client_ip_header="x-forwarded-for")
    key = client_key(_Req("10.0.0.1", {"x-forwarded-for": "9.9.9.9, 10.0.0.1"}), s)
    assert "9.9.9.9" in key, "the origin client is the first entry"


def test_metrics_is_no_longer_exempt() -> None:
    """It is a scrape target with unbounded label cardinality."""
    assert should_skip_rate_limit("/metrics") is False
    assert should_skip_rate_limit("/health") is True


def test_docs_is_exempt_but_nothing_under_it_is() -> None:
    """Swagger UI's /docs/oauth2-redirect was the only route ever served under /docs/.

    It went with Swagger UI, so the prefix exemption went too — the Scalar reference is
    the one exact path, and /docs/<anything> is now an ordinary 404 that counts.
    """
    assert should_skip_rate_limit("/docs") is True
    assert should_skip_rate_limit("/docs/oauth2-redirect") is False


def test_metrics_is_no_longer_public() -> None:
    """Counter labels carry tenant-supplied manifest ids and MCP tool names."""
    from felix.auth.middleware import _is_public_path

    assert _is_public_path("/metrics") is False
    assert _is_public_path("/health") is True


# --- limiter behaviour ------------------------------------------------------------


@pytest.mark.asyncio
async def test_limit_is_enforced() -> None:
    rl = InMemoryRateLimiter()
    allowed = [await rl.hit("k", limit=3, window_seconds=60) for _ in range(5)]
    assert allowed == [True, True, True, False, False]


@pytest.mark.asyncio
async def test_keys_are_independent() -> None:
    rl = InMemoryRateLimiter()
    for _ in range(3):
        await rl.hit("a", limit=3, window_seconds=60)
    assert await rl.hit("b", limit=3, window_seconds=60) is True


@pytest.mark.asyncio
async def test_stale_keys_are_evicted() -> None:
    """`_windows` was a defaultdict with no eviction — a per-IP key spray grew it
    forever, a memory-exhaustion DoS in the component meant to prevent DoS."""
    rl = InMemoryRateLimiter()
    for i in range(50):
        await rl.hit(f"k{i}", limit=10, window_seconds=0)
    # window of 0 means every prior entry is stale on the next sweep
    await rl.hit("trigger", limit=10, window_seconds=0)
    assert len(rl._windows) < 50


@pytest.mark.asyncio
async def test_redis_failure_degrades_rather_than_failing_requests() -> None:
    """Failing the request would turn a cache blip into a full outage; skipping the
    limit would remove the control exactly when a dependency is struggling."""

    class _Broken:
        async def hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
            raise ConnectionError("redis down")

    rl = ResilientRateLimiter(primary=_Broken())
    assert await rl.hit("k", limit=2, window_seconds=60) is True
    assert await rl.hit("k", limit=2, window_seconds=60) is True
    assert await rl.hit("k", limit=2, window_seconds=60) is False, "still limited, just per-process"


def test_config_comes_from_settings() -> None:
    """The limit was hardcoded 120/60 and the backend always in-process."""
    cfg = build_rate_limit_config(_settings(rate_limit=7, rate_limit_window_seconds=30))
    assert cfg.limit == 7
    assert cfg.window_seconds == 30


# --- compute ceilings -------------------------------------------------------------


def test_nested_quantifier_patterns_are_refused() -> None:
    """`re.compile(args.query)` compiles a model-supplied pattern and runs it over every
    workspace file; `re` has no timeout and a worker thread cannot be killed."""
    from felix.tools.workspace import _reject_catastrophic

    for evil in [r"(a+)+$", r"(a*)*b", r"(\d+)*", r"([a-z]+)+"]:
        assert _reject_catastrophic(evil), evil


def test_nesting_is_detected_through_intermediate_groups() -> None:
    """`((a+))+` is just as exponential as `(a+)+`."""
    from felix.tools.workspace import _reject_catastrophic

    for evil in [r"((a+))+", r"(?:(a+))+", r"(a{2,3})+"]:
        assert _reject_catastrophic(evil), evil


def test_ordinary_patterns_are_allowed() -> None:
    from felix.tools.workspace import _reject_catastrophic

    ok = [
        r"foo.*bar",
        "TODO",
        r"^\s*def ",
        r"(abc)+",  # quantified group, no inner quantifier
        r"a{2,3}",
        r"[*+]+",  # quantifier chars inside a class are literals
        r"\(a+\)+",  # escaped parens are not groups
        r"(?:ab)+",
        r"(a)(b)+",  # sibling groups, not nested
        r"(a+)b",  # quantifier inside an unquantified group
    ]
    for pattern in ok:
        assert _reject_catastrophic(pattern) is None, pattern


def test_the_detector_is_itself_linear() -> None:
    """The first version of this check was a regex with an ambiguous alternation — i.e.
    exactly the bug it exists to catch. CodeQL flagged it; it is now a scan."""
    import time

    from felix.tools.workspace import _reject_catastrophic

    payload = "(" + "?!" * 5000
    start = time.monotonic()
    _reject_catastrophic(payload)
    assert time.monotonic() - start < 0.5


def test_calculator_exponent_is_bounded() -> None:
    """`9**9**9**9` is a few keystrokes and pins a core."""
    from felix.security.expr import evaluate_expression

    assert evaluate_expression("2**10") == 1024
    with pytest.raises(ValueError, match="too large"):
        evaluate_expression("9**9**9**9")
    with pytest.raises(ValueError, match="too large"):
        evaluate_expression("10**100000")


def test_search_query_length_is_capped() -> None:
    import pydantic
    from felix.tools.workspace import SearchFilesArgs

    with pytest.raises(pydantic.ValidationError):
        SearchFilesArgs(query="a" * 10_000)


def test_pii_card_detection_is_unchanged_by_the_rewrite() -> None:
    from felix.governance.pii import _REGEX_PATTERNS

    card = next(rx for rx, name in _REGEX_PATTERNS if name == "card")
    assert card.search("4111 1111 1111 1111")
    assert card.search("4111111111111111")
    assert not card.search("just 42 words here")


# --- middleware ordering ---------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_auth_is_rate_limited() -> None:
    """Starlette's add_middleware inserts at index 0, so auth was registered last and
    ran first — a 401 returned before the limiter was ever consulted, making credential
    guessing completely unthrottled."""
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    settings = Settings(
        database_url="memory://ratelimit",
        object_store="memory",
        auth_mode="api_key",
        auth_api_keys='{"sk-real":{"tenant_id":"t","sub":"s","scopes":["admin"]}}',
        host="127.0.0.1",
        environment="development",
        redis_url="",
        rate_limit=5,
        rate_limit_window_seconds=60,
    )
    app = create_app(settings=settings, plugins=[])
    codes: list[int] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(9):
            resp = await client.get("/manifests", headers={"authorization": "Bearer sk-wrong"})
            codes.append(resp.status_code)

    assert 401 in codes, "bad credentials should still 401"
    assert 429 in codes, "guessing was never throttled before this change"
    assert codes.index(429) >= 5, "the limit should apply after `rate_limit` attempts"


@pytest.mark.asyncio
async def test_oversized_declared_body_is_rejected() -> None:
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    settings = Settings(
        database_url="memory://body",
        object_store="memory",
        auth_mode="none",
        allow_insecure=True,
        host="127.0.0.1",
        environment="development",
        redis_url="",
    )
    app = create_app(settings=settings, plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/chat", content=b"x" * (2 * 1024 * 1024))
        assert resp.status_code == 413
