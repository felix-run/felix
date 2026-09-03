"""Compaction compacted against a fixed 128K regardless of the model's real window.

`spec.session.context_window_tokens` carries a schema default of 128000 and pydantic
fills it in whether or not the operator wrote it, so `runtime.py` could not tell a
declared value from a default. A manifest on a 1M-context model therefore compacted at
128K minus reserve — summarising away seven eighths of the window it was paying for, and
spending a summarisation call to do it.
"""

from __future__ import annotations

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
    assert "context_window_tokens" not in spec.model_fields_set
    assert _context_window_for_manifest(_Manifest("claude-opus-5"), spec) == 1_000_000


def test_undeclared_window_on_a_200k_model_stays_200k() -> None:
    assert _context_window_for_manifest(_Manifest("claude-sonnet-4-5"), SessionSpec()) == 200_000


def test_declared_window_wins_over_the_model() -> None:
    """An operator who set the value meant it — including setting it lower deliberately."""
    spec = SessionSpec(context_window_tokens=64_000)
    assert "context_window_tokens" in spec.model_fields_set
    assert _context_window_for_manifest(_Manifest("claude-opus-5"), spec) == 64_000


def test_declared_value_equal_to_the_default_is_still_honoured() -> None:
    """Writing 128000 explicitly is a decision, not an absent field."""
    spec = SessionSpec(context_window_tokens=128_000)
    assert _context_window_for_manifest(_Manifest("claude-opus-5"), spec) == 128_000


def test_manifest_without_a_model_falls_back_to_the_default() -> None:
    class _Bare:
        spec = None

    assert _context_window_for_manifest(_Bare(), SessionSpec()) == 128_000


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
