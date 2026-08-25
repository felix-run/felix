"""Credentials are parsed once per configuration, not once per request.

`_parse_api_keys` re-parsed `FELIX_AUTH_API_KEYS` as JSON on every authenticated
request in `api_key` mode, and the match was then a loop of `constant_time_equal` over
every configured key — constant time per comparison but linear in how many exist.
`parse_verifiers` did the same on every request in `jwt` mode.

Measured with fifty keys configured, per request:

    parse                        15.58 µs
    scan to a miss                5.88 µs
    ---------------------------  --------
    after (parse + O(1) lookup)   0.38 µs

and `parse_verifiers` with five verifiers went 2.29 µs → 0.08 µs.

The security property is the part worth holding still: the presented token is still
compared with `constant_time_equal`, against the whole token. Hashing only chooses
*which* configured key to compare against, so the comparison that decides the outcome
is unchanged.
"""

from __future__ import annotations

import hashlib
import json

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
    """Tolerant of the index not existing, so tests that *can* run against an older
    version do, and fail rather than error. An error says the test could not run."""
    for name in ("_parse_api_keys", "_api_key_index"):
        fn = getattr(mw, name, None)
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()
    if hasattr(parse_verifiers, "cache_clear"):
        parse_verifiers.cache_clear()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- the lookup still resolves the same principals ----------------------------------


def test_every_configured_key_still_resolves_to_its_own_metadata() -> None:
    index = mw._api_key_index(RAW)
    for token, meta in KEYS.items():
        entry = index.get(_digest(token))
        assert entry is not None, f"{token[:12]}… no longer resolves"
        assert entry[0] == token
        assert entry[1]["sub"] == meta["sub"]
        assert entry[1]["tenant_id"] == meta["tenant_id"]


def test_an_unknown_token_resolves_to_nothing() -> None:
    assert mw._api_key_index(RAW).get(_digest("not-a-configured-key")) is None


def test_a_token_that_is_a_prefix_of_a_real_key_does_not_match() -> None:
    """A digest lookup is exact where a sloppy comparison might not be."""
    prefix = next(iter(KEYS))[:20]
    assert mw._api_key_index(RAW).get(_digest(prefix)) is None


def test_malformed_configuration_yields_no_keys_rather_than_raising() -> None:
    for raw in ("", "   ", "not json", "[1,2,3]", '"a string"'):
        assert mw._api_key_index(raw) == {}, f"{raw!r} should configure no keys"


def test_metadata_that_is_not_an_object_becomes_an_empty_mapping() -> None:
    """The old loop coerced a non-dict value with `meta if isinstance(meta, dict) else {}`;
    the index has to keep doing that or a string value would reach `.get('scopes')`."""
    index = mw._api_key_index(json.dumps({"k" * 30: "not-an-object"}))
    entry = index.get(_digest("k" * 30))
    assert entry is not None and entry[1] == {}


# --- the caching, and what it must not break ----------------------------------------


def test_the_configuration_is_parsed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    parses = []
    real = json.loads
    monkeypatch.setattr(json, "loads", lambda s, **k: (parses.append(1), real(s, **k))[1])

    for _ in range(20):
        mw._api_key_index(RAW)
    assert len(parses) <= 1, f"parsed the key configuration {len(parses)} times for 20 requests"


def test_changing_the_configuration_invalidates_it() -> None:
    """Keyed on the raw settings string, so nothing has to remember to invalidate."""
    assert mw._api_key_index(RAW).get(_digest("rotated-key-cccccccccccccccccccc")) is None
    rotated = json.dumps({"rotated-key-cccccccccccccccccccc": {"sub": "carol"}})
    entry = mw._api_key_index(rotated).get(_digest("rotated-key-cccccccccccccccccccc"))
    assert entry is not None, "a rotated configuration was not picked up"
    # And the old configuration still resolves to the old keys.
    assert mw._api_key_index(RAW).get(_digest("live-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaa")) is not None


def test_a_mutated_parse_cannot_seed_the_credential_table() -> None:
    """Only the index is cached, and it builds from a fresh parse.

    Caching both would make this order-dependent: mutate the parse before the index is
    built and the injected key authenticates; mutate it after and nothing happens. An
    authentication bug reachable only in one call order is the worst kind to hunt.
    """
    mutated = mw._parse_api_keys(RAW)
    mutated["injected-key"] = {"sub": "attacker", "tenant_id": "acme", "scopes": ["admin"]}

    # Built *after* the mutation, which is the order that would expose a shared parse.
    assert mw._api_key_index(RAW).get(_digest("injected-key")) is None
    assert mw._parse_api_keys(RAW) is not mutated, "the parse is shared between callers"


def test_verifiers_are_parsed_once_and_reparsed_on_change() -> None:
    spec = "self:https://issuer.example"
    a = parse_verifiers(spec)
    b = parse_verifiers(spec)
    assert a is b, "the verifier list is rebuilt per request"
    assert parse_verifiers("self:https://other.example") is not a
