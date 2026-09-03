"""Wire formats — one module per protocol a provider can speak."""

from __future__ import annotations

from felix_ai.wire.anthropic_messages import (
    AnthropicMessagesClient,
    apply_anthropic_thinking_cache,
)
from felix_ai.wire.base import (
    HttpModelClient,
    map_stop,
    parse_tool_arguments,
    tool_json_schema,
)
from felix_ai.wire.openai_completions import (
    OpenAICompletionsClient,
    apply_openai_thinking_cache,
    reasoning_effort_from_budget,
)
from felix_ai.wire.transport import (
    DEFAULT_CONNECT_TIMEOUT_S,
    MODEL_MAX_RETRIES,
    ModelGatewayError,
    post_with_retry,
)

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "MODEL_MAX_RETRIES",
    "AnthropicMessagesClient",
    "HttpModelClient",
    "ModelGatewayError",
    "OpenAICompletionsClient",
    "apply_anthropic_thinking_cache",
    "apply_openai_thinking_cache",
    "map_stop",
    "parse_tool_arguments",
    "post_with_retry",
    "reasoning_effort_from_budget",
    "tool_json_schema",
]
