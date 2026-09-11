"""Four posture gaps on the authenticated edge.

The rate-limit key read the *leftmost* X-Forwarded-For entry, which is the one the client
wrote; a JWT deployment whose verifier takes the tenant from a claim accepted any claimed
tenant unless the operator remembered FELIX_ALLOWED_TENANTS; `exp` was checked with zero
clock leeway; and a remote JWKS past its TTL made every token from that issuer fail with
nothing but a per-request warning while `/ready` stayed green.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from felix.auth.jwt import (
    JWKS_RETRY_S,
    JWKS_TTL_MS,
    JWT_LEEWAY_S,
    _jwks_url,
    _remote_jwks,
    parse_verifiers,
    run_jwks_refresh_loop,
    verify_jwt,
)
from felix.config import Settings
from felix.health import check_readiness
from felix.security.rate_limit import client_key, forwarded_client
from joserfc import jwk, jwt

_KEY = jwk.RSAKey.generate_key(2048)
_PUB = json.dumps({"keys": [_KEY.as_dict(private=False)]})
_ISS = "https://felix.local"


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "memory://posture",
        "object_store": "memory",
        "allow_insecure": True,
        "auth_mode": "none",
        "redis_url": "",
        "environment": "development",
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


# --- proxy hops -------------------------------------------------------------------


class _Req:
    def __init__(self, host: str, headers: dict[str, str]) -> None:
        self.client = type("C", (), {"host": host})()
        self.headers = headers


@pytest.mark.parametrize(
    ("raw", "hops", "expected"),
    [
        ("6.6.6.6, 9.9.9.9", 1, "9.9.9.9"),  # the one proxy appended the peer it saw
        ("6.6.6.6, 9.9.9.9, 10.0.0.2", 2, "9.9.9.9"),  # edge appended the client, inner the edge
        ("9.9.9.9", 1, "9.9.9.9"),  # single-valued header (cf-connecting-ip)
        (" 6.6.6.6 ,, 9.9.9.9 ", 1, "9.9.9.9"),  # whitespace and empty entries
        ("9.9.9.9:4123", 1, "9.9.9.9"),  # a port suffix is tolerated
        ("[2001:db8::1]:4123", 1, "2001:db8::1"),
        ("2001:db8::1", 1, "2001:db8::1"),
        ("9.9.9.9", 2, ""),  # fewer entries than proxies: the chain described did not write it
        ("", 1, ""),
        ("evil, 9.9.9.9", 2, ""),  # the chosen entry is not an address
        ("<script>", 1, ""),
    ],
)
def test_the_client_is_counted_from_the_right_and_must_be_an_address(
    raw: str, hops: int, expected: str
) -> None:
    assert forwarded_client(raw, hops=hops) == expected


def test_a_client_supplied_entry_cannot_choose_its_own_bucket() -> None:
    """One client, one bucket, however many addresses it lists before the proxy's entry."""
    s = _settings(trusted_client_ip_header="x-forwarded-for")
    keys = {
        client_key(_Req("10.0.0.1", {"x-forwarded-for": f"{fake}, 9.9.9.9"}), s)
        for fake in ("1.1.1.1", "2.2.2.2", "3.3.3.3")
    }
    assert keys == {"ip:9.9.9.9"}


def test_a_repeated_header_line_is_joined_before_it_is_read() -> None:
    """HAProxy adds a line rather than extending the list; `get` returns the first line,
    which is the client's — the leftmost-entry bug one field line over."""
    from starlette.datastructures import Headers

    s = _settings(trusted_client_ip_header="x-forwarded-for")
    req = _Req("10.0.0.1", {})
    req.headers = Headers(raw=[(b"x-forwarded-for", b"1.1.1.1"), (b"x-forwarded-for", b"9.9.9.9")])
    assert client_key(req, s) == "ip:9.9.9.9"


def test_an_untrusted_header_falls_back_to_the_socket_peer_not_the_first_entry() -> None:
    """Overcounted hops must not restore the spoof; the peer is the one address a client
    cannot choose."""
    s = _settings(trusted_client_ip_header="x-forwarded-for", trusted_proxy_hops=3)
    keys = {
        client_key(_Req("10.0.0.1", {"x-forwarded-for": f"{fake}, 9.9.9.9"}), s)
        for fake in ("1.1.1.1", "2.2.2.2")
    }
    assert keys == {"ip:10.0.0.1"}


def test_hops_setting_reaches_the_key() -> None:
    s = _settings(trusted_client_ip_header="x-forwarded-for", trusted_proxy_hops=2)
    key = client_key(_Req("10.0.0.1", {"x-forwarded-for": "1.1.1.1, 9.9.9.9, 10.0.0.2"}), s)
    assert key == "ip:9.9.9.9"


def test_hops_below_one_is_refused() -> None:
    with pytest.raises(ValueError):
        _settings(trusted_proxy_hops=0)


# --- tenant posture ---------------------------------------------------------------


def _jwt_settings(**over: object) -> Settings:
    # `redis_url` because `validate_runtime` also refuses an empty one outside
    # development; these tests are about which *tenant posture* is refused, so the
    # earlier guard must not be what fires.
    base: dict[str, object] = {
        "auth_mode": "jwt",
        "jwks_public": _PUB,
        "allow_insecure": False,
        "host": "0.0.0.0",
        "redis_url": "redis://127.0.0.1:6379/0",
    }
    return _settings(**{**base, **over})


def test_claim_mode_without_an_allowlist_is_refused_outside_development() -> None:
    s = _jwt_settings(environment="production", jwt_verifiers=f"self:{_ISS}")
    with pytest.raises(RuntimeError, match="FELIX_ALLOWED_TENANTS"):
        s.validate_runtime()


def test_the_allowlist_satisfies_it() -> None:
    _jwt_settings(
        environment="production", jwt_verifiers=f"self:{_ISS}", allowed_tenants="acme"
    ).validate_runtime()


def test_a_pinned_tenant_needs_no_allowlist() -> None:
    """`fixed` and `issuer` modes never read the claim, so there is nothing to constrain."""
    for mode in ("fixed:acme", "issuer"):
        _jwt_settings(environment="production", jwt_verifiers=f"self:{_ISS};tenant={mode}").validate_runtime()


def test_development_keeps_the_old_behaviour() -> None:
    _jwt_settings(environment="development", jwt_verifiers=f"self:{_ISS}").validate_runtime()


def test_an_allowlist_of_separators_alone_is_empty_here_as_it_is_at_runtime() -> None:
    """`allowed_tenants()` drops blanks; a value that passed startup and was empty at
    request time would be the exact state the guard exists to make unbootable."""
    with pytest.raises(RuntimeError, match="FELIX_ALLOWED_TENANTS"):
        _jwt_settings(
            environment="production", jwt_verifiers=f"self:{_ISS}", allowed_tenants=" , ,"
        ).validate_runtime()


def test_issuer_mode_verifiers_that_collapse_into_one_tenant_are_refused_everywhere() -> None:
    """The first host label is the tenant; two Cognito pools or two Keycloak realms differ
    only in the path and would share every session and memory row."""
    spec = (
        "cognito:https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AAAA;aud=app;tenant=issuer,"
        "cognito:https://cognito-idp.us-east-1.amazonaws.com/us-east-1_BBBB;aud=app;tenant=issuer"
    )
    with pytest.raises(RuntimeError, match="collapse into one tenant"):
        _jwt_settings(environment="development", jwt_verifiers=spec).validate_runtime()
    single = "cognito:https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AAAA;aud=app;tenant=issuer"
    _jwt_settings(environment="development", jwt_verifiers=single).validate_runtime()


def test_an_issuer_derived_tenant_colliding_with_a_pinned_one_is_refused() -> None:
    spec = "self:https://keycloak.example/realms/acme;tenant=issuer,cognito:https://idp.example;aud=a;tenant=fixed:keycloak"
    with pytest.raises(RuntimeError, match="collapse into one tenant"):
        _jwt_settings(environment="development", jwt_verifiers=spec).validate_runtime()
    # Two pinned verifiers naming one tenant is a stated intent (an IdP migration), not a collision.
    twice = f"self:{_ISS};tenant=fixed:acme,cognito:https://idp.example;aud=a;tenant=fixed:acme"
    _jwt_settings(environment="production", jwt_verifiers=twice).validate_runtime()


def test_a_plugin_auth_mode_gets_the_same_guard() -> None:
    from felix.auth.jwt import uses_jwt_verifiers

    plugin = _settings(auth_mode="acme-sso", jwt_verifiers=f"self:{_ISS}", jwks_public=_PUB)
    assert uses_jwt_verifiers(plugin) is True
    assert uses_jwt_verifiers(_settings(auth_mode="api_key", jwt_verifiers=f"self:{_ISS}")) is False
    assert uses_jwt_verifiers(_settings(auth_mode="jwt", jwt_verifiers="")) is False


def test_a_verifier_list_that_parses_to_nothing_is_refused() -> None:
    """A typo'd scheme was one startup warning, a green /ready with no jwks row, and a 401
    on every request."""
    with pytest.raises(RuntimeError, match="no entry parsed"):
        _jwt_settings(
            environment="development", jwt_verifiers="oidc:https://idp.example;aud=app"
        ).validate_runtime()


def test_api_key_mode_ignores_leftover_verifiers() -> None:
    _settings(
        auth_mode="api_key",
        environment="production",
        jwt_verifiers=f"self:{_ISS}",
        redis_url="redis://127.0.0.1:6379/0",
    ).validate_runtime()


def test_one_claim_mode_verifier_among_pinned_ones_still_trips_it() -> None:
    spec = f"self:{_ISS};tenant=fixed:acme,cognito:https://idp.example;aud=app"
    with pytest.raises(RuntimeError, match="FELIX_ALLOWED_TENANTS"):
        _jwt_settings(environment="staging", jwt_verifiers=spec).validate_runtime()


# --- leeway -------------------------------------------------------------------------


def _mint(**claims: object) -> str:
    return jwt.encode({"alg": "RS256"}, {"iss": _ISS, "sub": "user-1", "tenant_id": "t1", **claims}, _KEY)


def _verify(token: str):
    return verify_jwt(token, parse_verifiers(f"self:{_ISS}"), jwks_public=_PUB, settings=_settings())


def test_a_token_expired_within_the_leeway_is_accepted() -> None:
    """Zero leeway rejected a valid token whenever the issuer's clock ran a second ahead."""
    result = _verify(_mint(exp=int(time.time()) - JWT_LEEWAY_S // 2))
    assert getattr(result, "principal", None) is not None, result


def test_a_token_expired_past_the_leeway_is_still_expired() -> None:
    result = _verify(_mint(exp=int(time.time()) - JWT_LEEWAY_S - 30))
    assert getattr(result, "reason", None) == "expired"


def test_leeway_is_a_minute_not_a_loophole() -> None:
    assert 0 < JWT_LEEWAY_S <= 120


# --- verifier usability on /ready ---------------------------------------------------


# A remote-scheme verifier (Cloudflare Access) and a second one at another issuer, so the
# "one of several is out" case is a real two-issuer deployment rather than a contrivance.
_REMOTE = "access:team.cloudflareaccess.com;aud=app"
_REMOTE_CFG = parse_verifiers(_REMOTE)[0]
_REMOTE_URL = _jwks_url(_REMOTE_CFG)
# How `unusable_verifiers` labels it. Derived rather than written out again: the assertion
# is that the row names the verifier the way the code does, not that a hostname appears.
_REMOTE_LABEL = f"{_REMOTE_CFG.scheme}:{_REMOTE_CFG.issuer}"
_OTHER = "cognito:https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AAAA;aud=app;tenant=fixed:acme"
_OTHER_URL = _jwks_url(parse_verifiers(_OTHER)[0])


@pytest.fixture
def clean_jwks_cache():
    saved = dict(_remote_jwks)
    _remote_jwks.clear()
    try:
        yield
    finally:
        _remote_jwks.clear()
        _remote_jwks.update(saved)


def _probe(report, name: str):
    return next((p for p in report.probes if p.name == name), None)


def _fresh(url: str, *, age_ms: int = 0) -> None:
    key_set = jwk.KeySet.import_key_set(json.loads(_PUB))
    _remote_jwks[url] = (key_set, int(time.time() * 1000) - age_ms)


@pytest.mark.asyncio
async def test_a_remote_issuer_never_fetched_is_not_ready(clean_jwks_cache: None) -> None:
    """Every token from that issuer 401s; database and Redis green is not "ready"."""
    report = await check_readiness(_settings(auth_mode="jwt", jwt_verifiers=_REMOTE), max_age_s=0)
    jwks = _probe(report, "jwks")
    assert jwks is not None and jwks.ok is False
    assert _REMOTE_LABEL in jwks.detail and "never fetched" in jwks.detail
    assert report.ready is False


@pytest.mark.asyncio
async def test_a_stale_key_set_is_not_ready_and_says_how_old(clean_jwks_cache: None) -> None:
    _fresh(_REMOTE_URL, age_ms=JWKS_TTL_MS + 1000)
    report = await check_readiness(_settings(auth_mode="jwt", jwt_verifiers=_REMOTE), max_age_s=0)
    jwks = _probe(report, "jwks")
    assert jwks is not None and jwks.ok is False and "stale" in jwks.detail
    assert report.ready is False


@pytest.mark.asyncio
async def test_a_fresh_key_set_is_ready(clean_jwks_cache: None) -> None:
    _fresh(_REMOTE_URL)
    report = await check_readiness(_settings(auth_mode="jwt", jwt_verifiers=_REMOTE), max_age_s=0)
    jwks = _probe(report, "jwks")
    assert jwks is not None and jwks.ok is True
    assert report.ready is True


@pytest.mark.asyncio
async def test_one_unusable_issuer_among_usable_ones_degrades_rather_than_downs(
    clean_jwks_cache: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A stale issuer taking every pod off the Service would down the issuers that still
    work too; the pod stays ready, and says which issuer is out."""
    _fresh(_OTHER_URL)
    with caplog.at_level("WARNING", logger="felix.health"):
        report = await check_readiness(
            _settings(auth_mode="jwt", jwt_verifiers=f"{_REMOTE},{_OTHER}"), max_age_s=0
        )
    jwks = _probe(report, "jwks")
    assert jwks is not None and jwks.ok is True and _REMOTE_LABEL in jwks.detail
    assert report.ready is True
    assert any(_REMOTE_LABEL in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_two_verifiers_on_one_issuer_both_stale_is_not_ready(clean_jwks_cache: None) -> None:
    """One issuer serving two apps is two verifiers on one key set; keyed by issuer they
    counted as one, and "one of two unusable" read as degraded while nothing verified."""
    spec = "cognito:https://idp.example;aud=app1;tenant=fixed:a,cognito:https://idp.example;aud=app2;tenant=fixed:b"
    report = await check_readiness(_settings(auth_mode="jwt", jwt_verifiers=spec), max_age_s=0)
    assert report.ready is False


@pytest.mark.asyncio
async def test_a_shared_issuer_without_an_audience_is_unusable_not_fresh(clean_jwks_cache: None) -> None:
    """`verify_jwt` refuses it one line in; "fresh" would be the green-everything-401 shape."""
    spec = "access:team.cloudflareaccess.com"
    _fresh(_jwks_url(parse_verifiers(spec)[0]))
    report = await check_readiness(_settings(auth_mode="jwt", jwt_verifiers=spec), max_age_s=0)
    jwks = _probe(report, "jwks")
    assert jwks is not None and jwks.ok is False and "audience" in jwks.detail


@pytest.mark.asyncio
async def test_a_local_key_that_does_not_import_is_unusable(clean_jwks_cache: None) -> None:
    report = await check_readiness(
        _settings(auth_mode="jwt", jwt_verifiers=f"self:{_ISS}", jwks_public="not a key"), max_age_s=0
    )
    jwks = _probe(report, "jwks")
    assert jwks is not None and jwks.ok is False and "FELIX_JWKS_PUBLIC" in jwks.detail
    good = await check_readiness(
        _settings(auth_mode="jwt", jwt_verifiers=f"self:{_ISS}", jwks_public=_PUB), max_age_s=0
    )
    assert _probe(good, "jwks").ok is True  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_no_probe_outside_jwt_mode(clean_jwks_cache: None) -> None:
    """The refresh loop runs only under `auth_mode=jwt`, so a leftover verifier elsewhere
    would fail forever. The row is absent rather than a vacuous ok."""
    report = await check_readiness(_settings(auth_mode="api_key", jwt_verifiers=_REMOTE), max_age_s=0)
    assert _probe(report, "jwks") is None
    assert report.ready is True


@pytest.mark.asyncio
async def test_the_public_route_reports_it_without_the_issuer(clean_jwks_cache: None) -> None:
    """The detail names the issuer; `/ready` is anonymous and drops details."""
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app(settings=_settings(auth_mode="jwt", jwt_verifiers=_REMOTE), plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["jwks"]["ok"] is False
    assert "detail" not in body["checks"]["jwks"]


# --- refresh backoff ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_refresh_retries_soon_instead_of_next_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At 600s against a 900s TTL one failed tick aged the set past the TTL before the
    next attempt: one IdP blip, five minutes of 401s on every replica."""
    import felix.auth.jwt as jwt_mod

    sleeps: list[float] = []
    outcomes = iter([0, 1, 1])

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 4:
            raise asyncio.CancelledError

    async def fake_refresh(settings) -> int:
        return next(outcomes)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(jwt_mod, "refresh_all_jwks", fake_refresh)
    with pytest.raises(asyncio.CancelledError):
        await run_jwks_refresh_loop(_settings(auth_mode="jwt", jwt_verifiers=_REMOTE), interval_s=300.0)
    assert sleeps == [300.0, JWKS_RETRY_S, 300.0, 300.0]
    assert JWKS_RETRY_S * 3 < JWKS_TTL_MS / 1000, "several retries fit inside one TTL"
