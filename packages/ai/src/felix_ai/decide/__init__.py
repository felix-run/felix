"""Decision providers: typed questions in, calibrated answers out.

See `types.py` for the vocabulary. The built-in backends are Jev over TypeSafe's API and
over Cloudflare Workers AI; `llm.py` answers the same questions with a chat model and is
registered by the harness, which is what can build one.

Factories take `(model_id, wire_model, options, settings)`. `options` is the provider's
entry in `FELIX_MODEL_PROVIDER_OPTIONS`, already parsed — a decision provider shares its
credential with the model provider of the same name rather than growing a second setting.
`settings` is opaque here; only the timeout is read from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from felix_ai.decide.registry import (
    DecisionProviderFactory,
    get_decision_provider,
    list_decision_providers,
    register_decision_provider,
    reset_decision_provider_registry,
)
from felix_ai.decide.types import (
    MAX_CHOICE_OPTIONS,
    Answer,
    Choice,
    ChoiceAnswer,
    DecisionProvider,
    DecisionResult,
    Noul,
    NoulAnswer,
    Question,
    Score,
    ScoreAnswer,
)
from felix_ai.decide.typesafe import JevDecider

# Jev answers in well under a second; a decision that takes longer than this has failed,
# and the caller's fallback is better than waiting out the model timeout meant for
# generation. Overridable per provider with a `timeout_seconds` option.
DEFAULT_DECISION_TIMEOUT_S = 15.0


def _timeout(options: Mapping[str, str], settings: Any) -> float:
    raw = options.get("timeout_seconds")
    if raw:
        return float(raw)
    return min(DEFAULT_DECISION_TIMEOUT_S, float(getattr(settings, "model_timeout_seconds", 0) or 1e9))


def _typesafe(model_id: str, wire_model: str, options: Mapping[str, str], settings: Any) -> JevDecider:
    # Refused at build rather than sent: TypeSafe has no unauthenticated mode, so a missing
    # key is a 401 on every turn that the consumer quietly falls back from.
    if not options.get("api_key") and not options.get("base_url"):
        raise ValueError(
            "decision provider 'typesafe' needs api_key — set it in "
            'FELIX_MODEL_PROVIDER_OPTIONS, e.g. {"typesafe": {"api_key": "..."}}'
        )
    return JevDecider.typesafe(
        model_id=model_id,
        wire_model=wire_model,
        api_key=options.get("api_key", ""),
        timeout_s=_timeout(options, settings),
        base_url=options.get("base_url", ""),
    )


def _workers_ai(model_id: str, wire_model: str, options: Mapping[str, str], settings: Any) -> JevDecider:
    return JevDecider.workers_ai(
        model_id=model_id,
        wire_model=wire_model,
        api_key=options.get("api_key", ""),
        account_id=options.get("account_id", ""),
        gateway_id=options.get("gateway_id", ""),
        timeout_s=_timeout(options, settings),
        base_url=options.get("base_url", ""),
    )


BUILTIN_DECISION_PROVIDERS: dict[str, DecisionProviderFactory] = {
    "typesafe": _typesafe,
    "workers_ai": _workers_ai,
}


__all__ = [
    "BUILTIN_DECISION_PROVIDERS",
    "DEFAULT_DECISION_TIMEOUT_S",
    "MAX_CHOICE_OPTIONS",
    "Answer",
    "Choice",
    "ChoiceAnswer",
    "DecisionProvider",
    "DecisionProviderFactory",
    "DecisionResult",
    "JevDecider",
    "Noul",
    "NoulAnswer",
    "Question",
    "Score",
    "ScoreAnswer",
    "get_decision_provider",
    "list_decision_providers",
    "register_decision_provider",
    "reset_decision_provider_registry",
]
