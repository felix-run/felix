"""Provider family comes from the route table, not from the model id's spelling.

`provider_family` accepted a `routes` argument that no caller ever passed, so it always
fell through to substring matching — `claude`, `gpt`, `llama`, `mistral`. That was the last
vendor sniff in the harness, and it decided whether a thread's tool calls and images get
flattened to plain text before switching models.
"""

from __future__ import annotations

from typing import Any

from felix.session.handoff import needs_handoff, provider_family

# The shape `parse_model_routes` returns: logical id -> route with a `.provider`.
_ROUTES: dict[str, Any] = {
    "claude-sonnet": {"provider": "anthropic", "model": "claude-sonnet-5"},
    "gpt-4.1": {"provider": "openai", "model": "gpt-4.1"},
    # Two providers whose names carry no vendor hint at all.
    "house-a": {"provider": "acme", "model": "a-1"},
    "house-b": {"provider": "globex", "model": "b-1"},
    # A model whose id says "claude" but which an operator routed elsewhere.
    "claude-flavoured": {"provider": "openai", "model": "gpt-4o"},
}


def test_family_is_the_routes_provider() -> None:
    assert provider_family("claude-sonnet", routes=_ROUTES) == "anthropic"
    assert provider_family("gpt-4.1", routes=_ROUTES) == "openai"
    assert provider_family("house-a", routes=_ROUTES) == "acme"


def test_the_route_wins_over_what_the_id_looks_like() -> None:
    """The sniff would call this `anthropic` on the strength of the substring."""
    assert provider_family("claude-flavoured", routes=_ROUTES) == "openai"


def test_providers_with_no_vendor_hint_in_their_name_still_resolve() -> None:
    """A substring sniff has nothing to go on here and answers `unknown` for both, so the
    switch looks like a no-op and a thread's tool calls and images get replayed to a model
    that cannot read them. The route table knows."""
    assert provider_family("house-a", routes=_ROUTES) != provider_family("house-b", routes=_ROUTES)
    assert needs_handoff("house-a", "house-b", routes=_ROUTES)


def test_same_provider_needs_no_handoff() -> None:
    routes = {**_ROUTES, "claude-haiku": {"provider": "anthropic", "model": "claude-haiku-4-5"}}
    assert not needs_handoff("claude-sonnet", "claude-haiku", routes=routes)


def test_a_real_switch_still_needs_a_handoff() -> None:
    assert needs_handoff("claude-sonnet", "gpt-4.1", routes=_ROUTES)


def test_an_id_missing_from_the_table_is_distinctly_unknown() -> None:
    assert provider_family("nowhere", routes=_ROUTES) == "unknown:nowhere"
    assert provider_family("elsewhere", routes=_ROUTES) == "unknown:elsewhere"


def test_the_same_model_is_never_a_handoff() -> None:
    assert not needs_handoff("house-a", "house-a", routes=_ROUTES)


def test_routes_are_loaded_when_the_caller_supplies_none() -> None:
    """The default path: `needs_handoff` reads the configured table itself, which is what
    it never did before."""
    assert provider_family("claude-sonnet") == "anthropic"
    assert needs_handoff("claude-sonnet", "gpt-4.1")
