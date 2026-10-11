"""felix.prompts — named prompt templates for chat expansion."""

from felix.prompts.templates import (
    PromptUnavailable,
    expand_named_prompt,
    expand_template,
    find_prompt_spec,
    resolve_prompt_body,
)

__all__ = [
    "PromptUnavailable",
    "expand_named_prompt",
    "expand_template",
    "find_prompt_spec",
    "resolve_prompt_body",
]
