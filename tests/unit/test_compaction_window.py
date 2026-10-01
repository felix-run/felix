"""Compaction compacts against the declared window, or else the model's own.

`spec.session.context_window_tokens` defaulted to 128000, so a manifest on a 1M-context model
compacted at 128K minus reserve — summarising away seven eighths of the window it was paying
for. `runtime.py` then told a written value from the default through `model_fields_set`; the
default is `None` now, so the value itself says which it is, and the pin hash can see it.
"""

from __future__ import annotations

from typing import Any

from felix.manifests.schema import SessionSpec
from felix.runtime import _context_window_for_manifest


class _Model:
    def __init__(self, model_id: str) -> None:
        self.id = model_id


class _Spec:
    def __init__(self, model_id: str) -> None:
        self.model = _Model(model_id)


class _Manifest:
    def __init__(self, model_id: str) -> None:
        self.spec = _Spec(model_id)


def test_undeclared_window_follows_the_model() -> None:
    spec = SessionSpec()
    assert spec.context_window_tokens is None, "unset means the model's window, visibly"
    assert _context_window_for_manifest(_Manifest("claude-opus-5"), spec) == 1_000_000


def test_undeclared_window_on_a_200k_model_stays_200k() -> None:
    assert _context_window_for_manifest(_Manifest("claude-sonnet-4-5"), SessionSpec()) == 200_000


def test_declared_window_wins_over_the_model() -> None:
    """An operator who set the value meant it — including setting it lower deliberately."""
    spec = SessionSpec(context_window_tokens=64_000)
    assert _context_window_for_manifest(_Manifest("claude-opus-5"), spec) == 64_000


def test_declared_value_equal_to_the_default_is_still_honoured() -> None:
    """Writing 128000 explicitly is a decision, not an absent field."""
    spec = SessionSpec(context_window_tokens=128_000)
    assert _context_window_for_manifest(_Manifest("claude-opus-5"), spec) == 128_000


def test_manifest_without_a_model_uses_the_default_models_window() -> None:
    """A manifest naming no model runs on `default_model_id`, so that model's window applies.

    This used to return a flat 128000 -- every bundled manifest, since none names a model --
    so each compacted at 128K on a 200K model and `/v1/models` listed 128K for all of them.
    """
    from felix.config import Settings

    class _Bare:
        spec = None

    on_200k = Settings(database_url="memory://cw", default_model_id="claude-sonnet-4-5")
    assert _context_window_for_manifest(_Bare(), SessionSpec(), on_200k) == 200_000
    on_1m = Settings(database_url="memory://cw", default_model_id="claude-opus-5")
    assert _context_window_for_manifest(_Bare(), SessionSpec(), on_1m) == 1_000_000
    # A declared window still wins over the default model's.
    declared = SessionSpec(context_window_tokens=64_000)
    assert _context_window_for_manifest(_Bare(), declared, on_1m) == 64_000


def test_a_default_with_no_model_at_all_still_answers() -> None:
    """No model named and no default configured is the one case the fixed number is for."""
    from felix.config import Settings

    class _Bare:
        spec = None

    unset = Settings(database_url="memory://cw", default_model_id="")
    assert _context_window_for_manifest(_Bare(), SessionSpec(), unset) == 128_000


def test_missing_session_spec_is_not_an_error() -> None:
    assert _context_window_for_manifest(_Manifest("claude-opus-5"), None) == 1_000_000


def test_a_logical_route_id_resolves_to_the_wire_models_window() -> None:
    """`spec.model.id` is a *logical* route name in every bundled manifest, and feeding
    that to the catalog matched only the loose `claude-sonnet` family key, whose entry is
    200K — so a manifest on the default route compacted against 200K instead of the 1M it
    pays for. (128K is what an id matching *nothing* would have got.)"""
    from felix.config import Settings
    from felix.patterns.model import parse_model_routes

    # `claude-sonnet` is a route key, not a wire id; it resolves to claude-sonnet-5 (1M).
    route = parse_model_routes(Settings(database_url="memory://cw")).get("claude-sonnet")
    assert route is not None and route.model == "claude-sonnet-5"
    assert _context_window_for_manifest(_Manifest("claude-sonnet"), SessionSpec()) == 1_000_000


def _real(**session: Any) -> Any:
    from felix.manifests.loader import parse_manifest

    spec = {"model": {"id": "claude-opus-5"}, **({"session": session} if session else {})}
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "listed"}, "spec": spec}
    )


def test_the_model_listing_reports_the_window_compaction_uses() -> None:
    """The listing read the session field — whose 128000 default meant every manifest said 128K
    — and otherwise looked the window up by the manifest's *name*."""
    from felix.usage.catalog import catalog_from_manifest

    assert catalog_from_manifest("listed", _real())["felix"]["contextWindow"] == 1_000_000
    declared = catalog_from_manifest("listed", _real(context_window_tokens=64_000))
    assert declared["felix"]["contextWindow"] == 64_000


def test_the_listing_reports_the_default_models_window_for_a_manifest_naming_none(
    monkeypatch: Any,
) -> None:
    """The listing only asked for the window when a manifest had a `model` block; without one
    it looked the window up by the manifest's name, which is 128K for any name."""
    from felix.config import get_settings
    from felix.manifests.loader import parse_manifest
    from felix.usage.catalog import catalog_from_manifest

    monkeypatch.setenv("FELIX_DEFAULT_MODEL_ID", "claude-sonnet-4-5")
    get_settings.cache_clear()
    try:
        bare = parse_manifest(
            {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "bare"}, "spec": {}}
        )
        assert catalog_from_manifest("bare", bare)["felix"]["contextWindow"] == 200_000
    finally:
        get_settings.cache_clear()


def test_writing_a_window_and_omitting_it_hash_differently() -> None:
    """They compact differently, so the compile pin must be able to tell them apart. With a
    128000 default it could not: the written value was dropped as a default."""
    from felix.manifests.pin import manifest_content_hash

    assert manifest_content_hash(_real()) != manifest_content_hash(_real(context_window_tokens=128_000))
