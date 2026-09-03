"""Felix model layer — wire formats, model catalog, and the provider contract.

This package may not import `felix`; `tests/unit/test_invariants.py` enforces it. That is
what makes "Felix is model-agnostic" a structural property rather than a claim: anything
the harness needs to inject arrives as a Protocol (`ToolSchema`, `ModelConfig`) or through
an explicit sink (`felix_ai.observability`, `felix_ai.context`).
"""

from __future__ import annotations

from felix_ai.catalog import (
    ModelCatalogEntry,
    ModelPricing,
    ModelQuirks,
    all_entries,
    clamp_effort,
    entry_for,
    is_priced,
    known_entry_for,
)
from felix_ai.providers import ProviderSpec, builtin_provider_specs
from felix_ai.registry import (
    ModelProviderFactory,
    get_model_provider,
    list_model_providers,
    register_model_provider,
)
from felix_ai.types import (
    ChatMessage,
    ContentBlock,
    ImageAttachment,
    ModelChatOptions,
    ModelChatResult,
    ModelClient,
    ModelConfig,
    ModelProvider,
    ModelRoute,
    Role,
    StopReason,
    StreamDelta,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from felix_ai.wire import (
    DEFAULT_CONNECT_TIMEOUT_S,
    MODEL_MAX_RETRIES,
    AnthropicMessagesClient,
    HttpModelClient,
    ModelGatewayError,
    OpenAICompletionsClient,
    apply_anthropic_thinking_cache,
    apply_openai_thinking_cache,
    map_stop,
    parse_tool_arguments,
    post_with_retry,
    reasoning_effort_from_budget,
    tool_json_schema,
)

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "MODEL_MAX_RETRIES",
    "AnthropicMessagesClient",
    "ChatMessage",
    "ContentBlock",
    "HttpModelClient",
    "ImageAttachment",
    "ModelCatalogEntry",
    "ModelChatOptions",
    "ModelChatResult",
    "ModelClient",
    "ModelConfig",
    "ModelGatewayError",
    "ModelPricing",
    "ModelProvider",
    "ModelProviderFactory",
    "ModelQuirks",
    "ModelRoute",
    "OpenAICompletionsClient",
    "ProviderSpec",
    "Role",
    "StopReason",
    "StreamDelta",
    "TokenUsage",
    "ToolCall",
    "ToolSchema",
    "all_entries",
    "apply_anthropic_thinking_cache",
    "apply_openai_thinking_cache",
    "builtin_provider_specs",
    "clamp_effort",
    "entry_for",
    "get_model_provider",
    "is_priced",
    "known_entry_for",
    "list_model_providers",
    "map_stop",
    "parse_tool_arguments",
    "post_with_retry",
    "reasoning_effort_from_budget",
    "register_model_provider",
    "tool_json_schema",
]
