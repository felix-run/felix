"""Decision providers — the harness half: routes, credentials, and metering.

`felix_ai.decide` defines what a decision is and speaks the wire. What needs `Settings`
lives here: resolving `FELIX_DECISION_ROUTES`, handing each provider its entry from
`FELIX_MODEL_PROVIDER_OPTIONS`, building the `llm` backend over a routed chat model, and
metering every decision through `record_usage` so it counts against `limits.max_cost_usd`
like any other model call.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from felix_ai.decide import (
    BUILTIN_DECISION_PROVIDERS,
    DecisionProvider,
    DecisionResult,
    Question,
    get_decision_provider,
    list_decision_providers,
    register_decision_provider,
)
from felix_ai.decide.llm import LLMDecider
from felix_ai.decide.types import validate_answers
from felix_ai.types import ChatMessage, ModelChatResult, ModelRoute

from felix.config import DEFAULT_DECISION_ROUTES, Settings, get_settings
from felix.context import try_get_context
from felix.observability.metrics import record_counter

logger = logging.getLogger("felix.decisions")


@lru_cache(maxsize=32)
def _parse_routes_cached(raw: str) -> dict[str, ModelRoute]:
    from felix.patterns.model import parse_route_overlay

    return parse_route_overlay(raw, DEFAULT_DECISION_ROUTES, "FELIX_DECISION_ROUTES")


def parse_decision_routes(settings: Settings | None = None) -> dict[str, ModelRoute]:
    """Logical decider id -> route, with `FELIX_DECISION_ROUTES` overlaid on the defaults."""
    settings = settings or get_settings()
    return dict(_parse_routes_cached(settings.decision_routes or ""))


def _llm_factory(model_id: str, wire_model: str, options: Mapping[str, str], settings: Any) -> LLMDecider:
    """`{"provider": "llm", "model": "<FELIX_MODEL_ROUTES id>"}` — decide with a chat model."""
    from felix.manifests.schema import ModelSpec
    from felix.patterns.model import build_one_model

    model = build_one_model(settings, ModelSpec(id=wire_model), wire_model)
    return LLMDecider(model, model_id=model_id)


def register_builtin_deciders() -> None:
    """Register every built-in decision provider. Idempotent, last-write-wins."""
    for name, factory in BUILTIN_DECISION_PROVIDERS.items():
        register_decision_provider(name, factory)
    register_decision_provider("llm", _llm_factory)


@dataclass
class MeteredDecider:
    """A decision provider whose every call is recorded against the run's budgets.

    The one place usage is recorded, so a consumer cannot forget to — the defect this repo
    has shipped twice for chat patterns (`test_a_pattern_that_reaches_a_model_records_the_usage`).
    """

    inner: DecisionProvider
    model_id: str
    # `spec.decider.min_confidence`, carried with the decider so every consumer reads the
    # manifest's value rather than a default of its own.
    min_confidence: float = 0.5

    @property
    def wire_model(self) -> str:
        return self.inner.wire_model

    async def decide(
        self,
        state: str | Mapping[str, Any] | list[Any],
        questions: Mapping[str, Question],
        *,
        purpose: str = "",
    ) -> DecisionResult:
        labels = {"decider": self.model_id, "purpose": purpose or "unspecified"}
        try:
            result = await self.inner.decide(state, questions)
        except Exception:
            record_counter("felix_decisions", {**labels, "outcome": "error"})
            raise
        # Metered before it is checked: a malformed answer was still paid for.
        self._record(result, purpose)
        try:
            validate_answers(questions, result.answers)
        except ValueError:
            record_counter("felix_decisions", {**labels, "outcome": "invalid"})
            raise
        record_counter("felix_decisions", {**labels, "outcome": "ok"})
        return result

    def _record(self, result: DecisionResult, purpose: str) -> None:
        from felix.patterns.model import record_usage

        ctx = try_get_context()
        record_usage(
            ModelChatResult(message=ChatMessage(role="assistant", content=""), usage=result.usage),
            manifest_id=(ctx.manifest_id if ctx is not None else "") or "",
            model_id=self.model_id,
            wire_model_id=self.inner.wire_model,
            meta={"kind": "decision", "purpose": purpose} if purpose else {"kind": "decision"},
        )


def latest_request(messages: Sequence[Any], limit: int = 4_000) -> str | None:
    """The newest user turn's text, or `None` when it carries none (an image alone).

    What every consumer states its decision about. Not an earlier turn as a stand-in: a
    decision about the wrong request is worse than the fallback, and gets cached as right.
    """
    users = [m for m in messages if getattr(m, "role", "") == "user"]
    if not users:
        return None
    content = getattr(users[-1], "content", None)
    if not isinstance(content, str) or not content.strip():
        return None
    return content[:limit]


def build_decider(settings: Settings, logical_id: str, *, min_confidence: float = 0.5) -> MeteredDecider:
    """The metered decision provider for one `FELIX_DECISION_ROUTES` id.

    Raises `ValueError` for an unknown id or provider, or one its factory cannot build (a
    missing credential): `manifests/builder.py:bind_decider` lets that fail the compile.
    Failures of a *call* are the consumer's to fall back from.
    """
    from felix.patterns.model import parse_provider_options

    route = parse_decision_routes(settings).get(logical_id)
    if route is None:
        raise ValueError(f"decider {logical_id!r} is not in FELIX_DECISION_ROUTES")
    factory = get_decision_provider(route.provider)
    if factory is None:
        raise ValueError(
            f"Unknown decision provider {route.provider!r} — registered: "
            f"{', '.join(list_decision_providers()) or '(none)'}"
        )
    options = parse_provider_options(settings).get(route.provider, {})
    return MeteredDecider(
        inner=factory(logical_id, route.model, options, settings),
        model_id=logical_id,
        min_confidence=min_confidence,
    )


__all__ = [
    "MeteredDecider",
    "build_decider",
    "latest_request",
    "list_decision_providers",
    "parse_decision_routes",
    "register_builtin_deciders",
    "register_decision_provider",
]
