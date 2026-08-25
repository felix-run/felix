"""Credentials are parsed once per configuration, not once per request.

`_parse_api_keys` re-parsed `FELIX_AUTH_API_KEYS` as JSON on every authenticated
request in `api_key` mode; `parse_verifiers` did the same on every request in `jwt`
mode. Measured with fifty keys configured, the parse is 15.58 µs per request against
5.46 µs for the whole constant-time scan — so the parse was the cost, and it is the
part that is now cached.

The audit also suggested replacing the scan with a hashed index. That was implemented
and then dropped: at five keys the scan is 0.58 µs and at ten it is 1.13 µs, which is
the shape real deployments have, and a SHA-256 of a credential is something every later
reader has to reason about. CodeQL read it as password hashing, which it is not — but
code that needs the argument is worse than code that does not, for one microsecond.

What the scan gained instead is the removal of its early `break`. Stopping at the match
made total time depend on *which* key matched, which is the leak `constant_time_equal`
exists to close one level down.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest
from felix.auth import middleware as mw
from felix.auth.jwt import parse_verifiers

KEYS = {
    "live-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaa": {"sub": "alice", "tenant_id": "acme", "scopes": ["x"]},
    "live-key-bbbbbbbbbbbbbbbbbbbbbbbbbbbb": {"sub": "bob", "tenant_id": "other", "scopes": []},
}
RAW = json.dumps(KEYS)


@pytest.fixture(autouse=True)
def _clear() -> None:
    """Tolerant of the caches not existing, so tests that can run against an older
    version do, and fail rather than error. An error says the test could not run."""
    for fn in (getattr(mw, "_parse_api_keys", None), parse_verifiers):
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()


# --- the lookup still resolves the same principals ----------------------------------


def test_every_configured_key_still_resolves_to_its_own_metadata() -> None:
    parsed = mw._parse_api_keys(RAW)
    for token, meta in KEYS.items():
        assert parsed.get(token) is not None, f"{token[:12]} no longer resolves"
        assert parsed[token]["sub"] == meta["sub"]
        assert parsed[token]["tenant_id"] == meta["tenant_id"]


def test_an_unknown_token_resolves_to_nothing() -> None:
    assert mw._parse_api_keys(RAW).get("not-a-configured-key") is None


def test_malformed_configuration_yields_no_keys_rather_than_raising() -> None:
    for raw in ("", "   ", "not json", "[1,2,3]", '"a string"'):
        assert mw._parse_api_keys(raw) == {}, f"{raw!r} should configure no keys"


# --- the caching ---------------------------------------------------------------------


def test_the_configuration_is_parsed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    parses: list[int] = []
    real = json.loads
    monkeypatch.setattr(json, "loads", lambda s, **k: (parses.append(1), real(s, **k))[1])

    for _ in range(20):
        mw._parse_api_keys(RAW)
    assert len(parses) <= 1, f"parsed the key configuration {len(parses)} times for 20 requests"


def test_changing_the_configuration_invalidates_it() -> None:
    """Keyed on the raw settings string, so nothing has to remember to invalidate."""
    assert mw._parse_api_keys(RAW).get("rotated-key-cccccccccccccccccccc") is None
    rotated = json.dumps({"rotated-key-cccccccccccccccccccc": {"sub": "carol"}})
    assert mw._parse_api_keys(rotated).get("rotated-key-cccccccccccccccccccc") is not None
    # And the old configuration still resolves to the old keys.
    assert mw._parse_api_keys(RAW).get("live-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaa") is not None


def test_verifiers_are_parsed_once_and_reparsed_on_change() -> None:
    spec = "self:https://issuer.example"
    a = parse_verifiers(spec)
    assert parse_verifiers(spec) is a, "the verifier list is rebuilt per request"
    assert parse_verifiers("self:https://other.example") is not a


# --- what the match must keep doing --------------------------------------------------


def test_the_match_does_not_stop_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every configured key is compared on every request.

    Breaking at the match makes a request's duration depend on the matched key's
    position in the configuration -- a leak one level above the constant-time
    comparison that exists to prevent exactly this.
    """
    import asyncio

    from felix.config import Settings

    compared: list[str] = []
    real = mw.constant_time_equal
    monkeypatch.setattr(mw, "constant_time_equal", lambda a, b: (compared.append(b), real(a, b))[1])

    first = next(iter(KEYS))
    settings = Settings(
        database_url="memory://auth",
        auth_mode="api_key",
        auth_api_keys=RAW,
        allow_insecure=True,
    )

    class _Req:
        url = type("U", (), {"path": "/chat"})()
        headers: ClassVar[dict[str, str]] = {"authorization": f"Bearer {first}"}

    result = asyncio.run(mw.authenticate_request(_Req(), settings))
    assert getattr(result, "anonymous", None) is False, "the matching key should authenticate"
    assert compared == list(KEYS), f"compared {len(compared)} of {len(KEYS)} keys: {compared}"


def test_the_cached_table_is_shared_so_nothing_may_mutate_it() -> None:
    """`lru_cache` hands out the same dict every time.

    The only caller iterates, so nothing mutates it today -- but a shared credential
    table one request could edit would be an authentication bug rather than a
    performance one. Asserted structurally so a future caller that wants to modify it
    has to notice.
    """
    import ast
    import inspect

    assert mw._parse_api_keys(RAW) is mw._parse_api_keys(RAW), "the table is not actually shared"

    tree = ast.parse(inspect.getsource(mw.authenticate_request).lstrip())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", "") in {"pop", "setdefault", "update", "clear"}:
            owner = getattr(node.func.value, "id", "") or getattr(node.func.value, "attr", "")
            assert "key" not in owner.lower(), f"line {node.lineno} mutates the credential table"
