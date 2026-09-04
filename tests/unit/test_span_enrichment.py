"""What a tracing backend needs from a generation span, beyond that it happened.

Reviewing the first real export against Memoturn found four gaps, three of them silent:

* `FELIX_OTEL_CAPTURE_CONTENT` was defined in `config.py` and read nowhere. It was
  documented in `.env.example`, the Helm values, `docs/OBSERVABILITY.md` and the CHANGELOG
  as though it worked — a setting that looks like a control and is not one, which is the
  defect shape `.claude/rules/felix-invariants.md` names first.
* Cache tokens went out as `felix.usage.*` only. Backends read the `gen_ai.usage.*` names,
  so they were dropped — and `usage_with_cost` counts them, so a backend's cost silently
  disagrees with Felix's own as soon as prompt caching is on.
* Nothing carried a session or a caller, so a multi-turn conversation arrived as N
  unrelated traces.
* Probe and scrape endpoints were traced: 270 of 372 traces in the first run were
  `GET /health` or `GET /metrics`, against 3 chats.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext
from felix.patterns.model import _identity_attrs, _traced
from felix_ai.types import ChatMessage, ModelChatResult, ModelRoute, TokenUsage

ROOT = Path(__file__).resolve().parents[2]
ROUTE = ModelRoute(provider="anthropic", model="claude-sonnet-4-6")


class _Client:
    model_id = "sonnet"
    route = ROUTE

    async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="forty-five"),
            stop_reason="end_turn",
            usage=TokenUsage(input=10, output=4, cache_creation=7, cache_read=3),
        )


async def _run(monkeypatch: pytest.MonkeyPatch, settings: Settings, messages: Any) -> Any:
    """Capture the span a real call produces, with `settings` as the active context."""
    from felix.patterns import model as model_mod

    spans: list[Any] = []
    original = model_mod.make_span

    def _spy(name: str, attributes: Any = None) -> Any:
        span = original(name, attributes)
        spans.append(span)
        return span

    monkeypatch.setattr(model_mod, "make_span", _spy)
    monkeypatch.setattr(model_mod, "get_settings", lambda: settings)
    await _traced(_Client()).chat(messages, [])
    return spans[0]


def _settings(**kw: Any) -> Settings:
    return Settings(database_url="memory://span-enrich", object_store="memory", **kw)


@pytest.mark.asyncio
async def test_cache_tokens_use_the_names_backends_read(monkeypatch: pytest.MonkeyPatch) -> None:
    span = await _run(monkeypatch, _settings(), [])
    assert span.attributes["gen_ai.usage.cache_creation_input_tokens"] == 7
    assert span.attributes["gen_ai.usage.cache_read_input_tokens"] == 3
    # The Felix-namespaced pair stays for anything already reading it.
    assert span.attributes["felix.usage.cache_creation"] == 7


@pytest.mark.asyncio
async def test_content_is_absent_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tracing backend is an egress destination; prompts do not leave without consent."""
    span = await _run(monkeypatch, _settings(), [ChatMessage(role="user", content="hi")])
    assert "gen_ai.input.messages" not in span.attributes
    assert "gen_ai.output.messages" not in span.attributes


@pytest.mark.asyncio
async def test_capture_content_actually_captures(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression that matters: the flag was inert while documented as working."""
    span = await _run(
        monkeypatch,
        _settings(otel_capture_content=True),
        [ChatMessage(role="user", content="what is 15 times 3?")],
    )
    payload = json.loads(span.attributes["gen_ai.input.messages"])
    assert payload[0]["content"] == "what is 15 times 3?"
    out = json.loads(span.attributes["gen_ai.output.messages"])
    assert out[0]["content"] == "forty-five"


@pytest.mark.asyncio
async def test_captured_content_redacts_configured_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Content on a span is masked to exactly the audit row's standard — no more.

    `redact_json` replaces values Felix *knows* are secrets, meaning the ones hydrated from
    the configured secrets backend. That covers the common real case: a provider key or a
    credential that reached the prompt through a system prompt or a tool result.

    It is substring replacement over a known list, **not** pattern detection, so a
    credential a user types into a chat is exported verbatim. That is the honest boundary
    of this flag and the reason it defaults to off; see docs/OBSERVABILITY.md.
    """
    configured = "sk-ant-api03-" + "b" * 40
    monkeypatch.setattr("felix.secrets.collected_secret_values", lambda: [configured])
    span = await _run(
        monkeypatch,
        _settings(otel_capture_content=True),
        [ChatMessage(role="user", content=f"the key is {configured}")],
    )
    payload = span.attributes["gen_ai.input.messages"]
    assert configured not in payload
    assert "[REDACTED]" in payload


def test_session_identity_is_always_emitted() -> None:
    """An opaque thread id, and what turns a conversation into one session."""
    ctx = RequestContext(settings=_settings(), auth=AuthContext(), thread_id="thread-7")
    attrs = _identity_attrs(ctx)
    assert attrs["session.id"] == "thread-7"
    assert attrs["gen_ai.conversation.id"] == "thread-7"


def test_caller_identity_is_gated() -> None:
    auth = AuthContext(principal_sub="user-42", tenant_id="acme", anonymous=False)
    on = _identity_attrs(RequestContext(settings=_settings(), auth=auth, thread_id="t"))
    assert on["user.id"] == "user-42"
    assert on["felix.tenant.id"] == "acme"

    off = _identity_attrs(
        RequestContext(settings=_settings(otel_capture_identity=False), auth=auth, thread_id="t")
    )
    assert "user.id" not in off, "identity was exported with FELIX_OTEL_CAPTURE_IDENTITY=false"
    # The session id is not identity and stays either way.
    assert off["session.id"] == "t"


def test_anonymous_is_not_recorded_as_a_user() -> None:
    """`anonymous` is the default subject, not a person — it would be one fake user."""
    ctx = RequestContext(settings=_settings(), auth=AuthContext(), thread_id="t")
    assert "user.id" not in _identity_attrs(ctx)


def test_probe_and_scrape_endpoints_are_not_traced() -> None:
    """73% of the first real export was health checks and Prometheus scrapes."""
    from felix.observability.tracing import _EXCLUDED_SPANS, _EXCLUDED_URLS

    for path in ("health", "live", "ready", "metrics"):
        assert path in _EXCLUDED_URLS, f"{path} would be traced on every probe"
    # ASGI emits a child span per receive/send event: 552 of 936 observations.
    assert set(_EXCLUDED_SPANS) == {"send", "receive"}


def test_every_otel_setting_is_read_somewhere() -> None:
    """The check that would have caught `otel_capture_content` shipping inert.

    A setting defined in `config.py`, documented in `.env.example` and the Helm values, and
    read by no code, is indistinguishable from a working feature until someone looks for
    its effect in a backend.
    """
    config = (ROOT / "packages/harness/src/felix/config.py").read_text()
    declared = {
        node.target.id
        for node in ast.walk(ast.parse(config))
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id.startswith(("otel_", "metrics_"))
    }
    assert declared, "no otel settings found; this test needs rewriting"

    sources = [
        p.read_text()
        for root in (ROOT / "packages", ROOT / "apps")
        for p in root.rglob("*.py")
        if p.name != "config.py"
    ]
    unread = sorted(name for name in declared if not any(name in text for text in sources))
    assert not unread, (
        f"settings declared and documented but read by nothing: {unread} — "
        "each is a control that looks present and does nothing"
    )
