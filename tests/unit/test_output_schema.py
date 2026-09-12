"""Structured output: what a schema must be, and what each wire does with it.

`spec.output_schema` says the agent's answer is a JSON document of a stated shape, and the
provider is what enforces it. The two wires have no common mechanism — OpenAI has
`response_format`, Anthropic has nothing and must be handed a tool it is required to call — so
the risk sits in three places, and this file covers all three:

* the schema itself, which arrives from a *client* on `/v1` and is therefore unbounded input;
* whether the request actually makes the provider enforce the shape, rather than mentioning it;
* whether the Anthropic answer is folded back into text, since without that the react loop
  sees a call to a tool no manifest bound and answers with a tool error.

The cross-wire half is in `tests/conformance/test_model_provider.py`, which runs the same
assertions against both. What is here is the per-wire detail a contract cannot state.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix_ai.output_schema import (
    MAX_DEPTH,
    MAX_NODES,
    InvalidOutputSchema,
    is_strict,
    validate_output_schema,
)
from felix_ai.types import ChatMessage, ModelRoute, ToolCall
from felix_ai.wire.anthropic_messages import (
    STRUCTURED_OUTPUT_TOOL,
    AnthropicMessagesClient,
    apply_anthropic_output_schema,
    fold_structured_output,
)
from felix_ai.wire.openai_completions import openai_response_format

STRICT: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _nested(levels: int) -> dict[str, Any]:
    """An object nested `levels` schema levels deep through `properties`.

    Each level is two JSON nodes — the object and its `properties` map — so the node depth
    `validate_output_schema` bounds is about twice this.
    """
    schema: dict[str, Any] = {"type": "object", "properties": {"leaf": {"type": "string"}}}
    for _ in range(levels - 1):
        schema = {"type": "object", "properties": {"inner": schema}}
    return schema


# --- what a schema must be ------------------------------------------------------------------


def test_a_valid_schema_is_returned_unchanged() -> None:
    """Returned, not rewritten. A schema quietly normalised into something acceptable is the
    defect shape this repo produces most: the author believes the contract they wrote is the
    contract being enforced."""
    assert validate_output_schema(STRICT) is STRICT


def test_a_pydantic_style_schema_with_local_refs_is_accepted() -> None:
    """`model_json_schema()` is how a caller will realistically produce one of these, and it
    emits `$defs` plus `#/$defs/...` references. Rejecting refs outright would reject the
    normal case."""
    schema = {
        "type": "object",
        "properties": {"item": {"$ref": "#/$defs/Item"}},
        "required": ["item"],
        "additionalProperties": False,
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {"sku": {"type": "string"}},
                "required": ["sku"],
                "additionalProperties": False,
            }
        },
    }
    assert validate_output_schema(schema) is schema
    assert is_strict(schema) is True


@pytest.mark.parametrize(
    ("schema", "names"),
    [
        ("not a schema", "JSON Schema object"),
        ({"type": "array", "items": {"type": "string"}}, "root"),
        ({"type": "object"}, "properties"),
        ({"type": "object", "properties": {}}, "properties"),
    ],
)
def test_a_schema_no_provider_would_accept_is_refused(schema: Any, names: str) -> None:
    """Each of these is a provider 400 otherwise — relayed back to the caller as
    `invalid_request_error` two hops from the manifest that caused it.

    `match` rather than a bare `raises`: the message is what the manifest author acts on, and
    four cases that only assert "some InvalidOutputSchema" all pass on the first check alone.
    """
    with pytest.raises(InvalidOutputSchema, match=names):
        validate_output_schema(schema)


def test_a_remote_ref_is_refused() -> None:
    """A `$ref` to a URL asks the provider to fetch a host of the caller's choosing while
    holding the caller's schema — outbound traffic Felix pays for and cannot see. The local
    form above is what makes this a check on the separator rather than on the feature."""
    with pytest.raises(InvalidOutputSchema, match="local"):
        validate_output_schema(
            {
                "type": "object",
                "properties": {"x": {"$ref": "https://example.invalid/schema.json"}},
            }
        )


def test_a_schema_nested_past_the_limit_is_refused_without_recursing() -> None:
    """Depth is walked with an explicit stack, so deep client input is a 422 rather than a
    `RecursionError` — which is an unhandled 500 and, on the `/v1` surface, free to send."""
    # Deeper than any provider will honour, and still accepted here: this is a DoS bound, not
    # a second opinion on what the provider should allow.
    deep_but_fine = _nested(10)
    assert validate_output_schema(deep_but_fine) is deep_but_fine
    with pytest.raises(InvalidOutputSchema, match="nested"):
        validate_output_schema(_nested(MAX_DEPTH * 3))


def test_a_schema_with_too_many_nodes_is_refused() -> None:
    """The body is re-serialised on every turn of the loop, so one accepted request would
    otherwise buy `recursion_limit` copies of however much schema the client sent."""
    wide = {
        "type": "object",
        "properties": {f"f{i}": {"type": "string"} for i in range(MAX_NODES + 10)},
    }
    with pytest.raises(InvalidOutputSchema, match="too large"):
        validate_output_schema(wide)


def test_a_schema_made_large_by_leaves_is_refused() -> None:
    """The bound has to count leaves, not containers. A twenty-thousand-string `enum` is three
    dicts deep and megabytes wide, so a walk that counted only dicts and lists would let
    through exactly the schemas the per-turn re-serialisation cost is about."""
    enum_heavy = {
        "type": "object",
        "properties": {"choice": {"type": "string", "enum": [f"v{i}" for i in range(MAX_NODES * 2)]}},
    }
    with pytest.raises(InvalidOutputSchema, match="too large"):
        validate_output_schema(enum_heavy)


# --- OpenAI's strict subset -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "strict"),
    [
        (STRICT, True),
        # Open object: the model may add keys, so the shape is not guaranteed.
        ({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}, False),
        # Optional property: strict mode has no notion of one.
        (
            {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False},
            False,
        ),
    ],
)
def test_strict_is_reported_for_the_whole_schema(schema: dict[str, Any], strict: bool) -> None:
    assert is_strict(schema) is strict


def test_a_nested_open_object_is_not_strict() -> None:
    """The check a shallow implementation gets wrong. OpenAI applies the rule at every level,
    so a closed root with an open child is a 400 under `strict: true` — which is worse than
    the non-strict request, because it fails rather than degrading."""
    schema = {
        "type": "object",
        "properties": {"inner": {"type": "object", "properties": {"a": {"type": "string"}}}},
        "required": ["inner"],
        "additionalProperties": False,
    }
    assert is_strict(schema) is False


def test_the_openai_response_format_asks_for_a_guarantee_when_it_can() -> None:
    fmt = openai_response_format(STRICT)
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] is STRICT
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["name"], "OpenAI requires a name on the schema"


def test_an_endpoint_that_has_not_claimed_strict_is_sent_no_strict_key() -> None:
    """Twelve providers speak this wire and `strict` is an OpenAI extension. Sent as `false` it
    is still an unknown key to a server that validates its request body — a 400 on every
    request, which would make this feature an outage for the eleven rows nobody has verified.
    The schema still goes; only the guarantee is absent."""
    fmt = openai_response_format(STRICT, allow_strict=False)
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] is STRICT
    assert "strict" not in fmt["json_schema"]


@pytest.mark.parametrize(
    ("provider", "expect_strict_key"),
    [("openai", True), ("groq", False), ("a-plugins-own-provider", False)],
)
def test_the_endpoint_decides_strict_not_only_the_schema(provider: str, expect_strict_key: bool) -> None:
    """The wiring, not the function. `openai_response_format` takes `allow_strict`, and the
    test above passes it by hand — so nothing pinned `_body` reading it off the provider row.
    Point that at `True` and eleven endpoints get an OpenAI-only key on every request.

    An unknown provider is a plugin's, which has claimed nothing, so it gets the safe answer.
    """
    from felix_ai.wire.openai_completions import OpenAICompletionsClient

    client = OpenAICompletionsClient(
        model_id="m",
        route=ModelRoute(provider=provider, model="m-1"),
        settings=type("_S", (), {"model_timeout_seconds": 30})(),
        spec=None,
        base_url="https://example.invalid/v1",
        api_key="k",
    )
    body = client._body([ChatMessage(role="user", content="hi")], [], 0.0, None, output_schema=STRICT)
    json_schema = body["response_format"]["json_schema"]
    assert json_schema["schema"] is STRICT, "the schema itself goes to every provider"
    assert ("strict" in json_schema) is expect_strict_key


def test_a_loose_schema_drops_to_non_strict_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    """Asking for `strict: true` on a schema outside the subset is a 400, not a looser
    constraint — so the request has to be made non-strict. Silently is not an option: the
    caller's guarantee just disappeared."""
    loose = {"type": "object", "properties": {"a": {"type": "string"}}}
    with caplog.at_level("WARNING", logger="felix_ai.wire.openai_completions"):
        fmt = openai_response_format(loose)
    assert fmt["json_schema"]["strict"] is False
    assert "strict" in caplog.text


# --- Anthropic, which has no response_format ------------------------------------------------


def test_the_schema_becomes_a_tool_the_model_must_call() -> None:
    body: dict[str, Any] = {"model": "claude-sonnet-5", "messages": []}
    apply_anthropic_output_schema(body, STRICT)
    assert body["tool_choice"] == {"type": "tool", "name": STRUCTURED_OUTPUT_TOOL}
    assert [t["name"] for t in body["tools"]] == [STRUCTURED_OUTPUT_TOOL]
    assert body["tools"][0]["input_schema"] is STRICT


def test_real_tools_survive_and_the_choice_relaxes_to_any() -> None:
    """Naming this tool would stop the model calling the others, and in a react loop the
    structured answer is the last turn rather than the only one. `any` keeps every tool
    reachable while still forbidding a plain-text ending."""
    body: dict[str, Any] = {
        "model": "claude-sonnet-5",
        "messages": [],
        "tools": [{"name": "calculator", "input_schema": {"type": "object", "properties": {}}}],
    }
    apply_anthropic_output_schema(body, STRICT)
    assert body["tool_choice"] == {"type": "any"}
    assert [t["name"] for t in body["tools"]] == ["calculator", STRUCTURED_OUTPUT_TOOL]


def test_extended_thinking_downgrades_to_offering_the_schema(caplog: pytest.LogCaptureFixture) -> None:
    """Anthropic rejects any `tool_choice` but `auto` while `thinking` is set, so a forced
    choice there is a 400 on every request — the agent would answer nothing at all. The
    schema can only be offered, and the operator has to be told that it is not a guarantee."""
    body: dict[str, Any] = {
        "model": "claude-sonnet-5",
        "messages": [],
        "thinking": {"type": "enabled", "budget_tokens": 4096},
    }
    with caplog.at_level("WARNING", logger="felix_ai.wire.anthropic_messages"):
        apply_anthropic_output_schema(body, STRICT)
    assert body["tool_choice"] == {"type": "auto"}
    assert "thinking" in caplog.text


def _anthropic_client(spec: Any) -> AnthropicMessagesClient:
    return AnthropicMessagesClient(
        model_id="claude-sonnet-5",
        route=ModelRoute(provider="anthropic", model="claude-sonnet-5"),
        settings=type("_S", (), {"model_timeout_seconds": 30})(),
        spec=spec,
        base_url="https://example.invalid",
        api_key="k",
    )


def test_the_thinking_downgrade_is_decided_after_the_thinking_pass() -> None:
    """The guarantee above lives in the *order* of two calls in `_body`, not in the branch it
    tests. Swap `apply_anthropic_thinking_cache` and `apply_anthropic_output_schema` and every
    test in this file still passes, while every request from a thinking-enabled agent with an
    output schema becomes a provider 400 — the schema would force a `tool_choice` before
    anything had written `thinking` onto the body.

    So this drives the real `_body`, which is the only thing that pins the order.
    """
    spec = type(
        "_Spec", (), {"cache": False, "thinking_budget": 8192, "temperature": 0, "max_tokens": None}
    )()
    body = _anthropic_client(spec)._body(
        [ChatMessage(role="user", content="hi")], [], 0.0, 1024, output_schema=STRICT
    )
    assert body.get("thinking"), "the spec must actually have turned thinking on"
    assert body["tool_choice"] == {"type": "auto"}, "a forced choice with thinking on is a 400"
    assert any(t["name"] == STRUCTURED_OUTPUT_TOOL for t in body["tools"])


def test_without_thinking_the_same_body_forces_the_tool() -> None:
    """The counterpart, so the test above cannot pass by never forcing anything."""
    spec = type(
        "_Spec", (), {"cache": False, "thinking_budget": None, "temperature": 0, "max_tokens": None}
    )()
    body = _anthropic_client(spec)._body(
        [ChatMessage(role="user", content="hi")], [], 0.0, 1024, output_schema=STRICT
    )
    assert "thinking" not in body
    assert body["tool_choice"] == {"type": "tool", "name": STRUCTURED_OUTPUT_TOOL}


def test_a_structured_call_folds_back_into_the_turns_text() -> None:
    call = ToolCall(id="t1", name=STRUCTURED_OUTPUT_TOOL, args={"answer": "4"})
    content, tool_calls, stop = fold_structured_output("", [call], "tool_use")
    assert json.loads(content) == {"answer": "4"}
    assert tool_calls == []
    assert stop == "end_turn", "the loop must not run another turn for a tool that does not exist"


def test_prose_alongside_the_structured_call_is_replaced() -> None:
    """A caller holding a schema calls `json.loads` on the content, and a preamble breaks
    that — so the arguments replace the text rather than joining it."""
    call = ToolCall(id="t1", name=STRUCTURED_OUTPUT_TOOL, args={"answer": "4"})
    content, _, _ = fold_structured_output("Here you go: ", [call], "tool_use")
    assert json.loads(content) == {"answer": "4"}


def test_a_truncated_turn_is_not_reported_as_a_finished_answer() -> None:
    """The fold may only rewrite `tool_use`. A turn cut off mid-arguments stops for
    `max_tokens`, and `parse_tool_arguments` answers a half-written document with `{}` rather
    than raising — so mapping every stop to `end_turn` would hand the caller a well-formed
    `"{}"` as a complete answer *and* silence react's truncation quarantine, which is the one
    thing that would otherwise catch it."""
    call = ToolCall(id="t1", name=STRUCTURED_OUTPUT_TOOL, args={})
    content, tool_calls, stop = fold_structured_output("", [call], "max_tokens")
    assert stop == "max_tokens", "a truncated turn must stay truncated"
    assert tool_calls == [], "the reserved tool must still never reach the loop"
    assert content == "{}"


def test_the_reserved_tool_name_cannot_be_shadowed() -> None:
    """`fold_structured_output` matches on the name alone, so a manifest that bound a tool
    called `felix_structured_output` would have its call swallowed and re-emitted as the
    turn's answer. Silent and unlikely is the pair that earns a raise over a comment."""
    body: dict[str, Any] = {
        "model": "claude-sonnet-5",
        "messages": [],
        "tools": [{"name": STRUCTURED_OUTPUT_TOOL, "input_schema": {"type": "object"}}],
    }
    with pytest.raises(ValueError, match=STRUCTURED_OUTPUT_TOOL):
        apply_anthropic_output_schema(body, STRICT)


def test_a_real_tool_call_in_the_same_turn_wins() -> None:
    """That turn is the loop continuing, not answering. The premature structured call is
    dropped — it must never reach the loop, since nothing bound a tool by that name — and the
    schema is asked for again on the turn that ends the run."""
    real = ToolCall(id="t1", name="calculator", args={"expression": "2+2"})
    early = ToolCall(id="t2", name=STRUCTURED_OUTPUT_TOOL, args={"answer": "?"})
    content, tool_calls, stop = fold_structured_output("thinking", [real, early], "tool_use")
    assert content == "thinking"
    assert tool_calls == [real]
    assert stop == "tool_use"


def test_a_turn_with_no_structured_call_is_untouched() -> None:
    """The path taken whenever the schema was only offered — extended thinking above — and the
    path every unstructured turn in the repo takes."""
    real = ToolCall(id="t1", name="calculator", args={})
    assert fold_structured_output("hello", [real], "tool_use") == ("hello", [real], "tool_use")
    assert fold_structured_output("hello", [], "end_turn") == ("hello", [], "end_turn")


# --- which patterns may declare one ----------------------------------------------------------


ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _settings() -> Any:
    from felix.config import Settings

    return Settings(database_url="memory://output-schema", object_store="memory", auth_mode="none")


def _spec(pattern: str) -> dict[str, Any]:
    return {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": f"schema-{pattern}"},
        "spec": {"pattern": pattern, "tools": [], "output_schema": ANSWER_SCHEMA},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["react", "deep"])
async def test_a_pattern_that_honours_the_schema_compiles(pattern: str) -> None:
    """`react` reads `ctx["output_schema"]` outright; `deep` has no branch in
    `_DelegatingAgent._run`, so it forwards to the inner react agent built from the same
    context and composes nothing of its own afterwards."""
    from felix.manifests.builder import build_agent
    from felix.tools.provider import InMemoryToolProvider

    agent = await build_agent(_spec(pattern), tools=InMemoryToolProvider(), settings=_settings())
    assert agent is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["router", "parallel", "groupchat", "reflect", "plan_execute"])
async def test_a_pattern_that_cannot_honour_the_schema_is_refused(pattern: str) -> None:
    """Refused at compile, not dropped at runtime.

    Every pattern receives `output_schema` in its build context and only two read it, so
    without this a manifest declares an answer contract, validates, compiles, runs, and returns
    free text. `plan_execute` and `reflect` are worse than silent: the schema reaches the inner
    react agent, shaping an intermediate turn, while the synthesis turn the caller actually
    sees goes through `_DelegatingAgent` with no options at all.

    Flipping one of these to supported means threading `output_schema` onto that answering
    turn first — at which point this parametrisation is what says so.
    """
    from felix.manifests.builder import build_agent
    from felix.tools.provider import InMemoryToolProvider

    with pytest.raises(ValueError, match=r"does not support spec\.output_schema"):
        await build_agent(_spec(pattern), tools=InMemoryToolProvider(), settings=_settings())


def test_the_honouring_set_is_declared_by_the_registry_not_a_name_list() -> None:
    """The pattern registry is open, so the check cannot be a list of names in the manifest
    schema — a plugin's pattern has to be able to say yes for itself. This asserts the seam
    exists and defaults to no, which is the safe direction for a field nobody read.

    It registers two throwaway names and removes exactly those. An earlier version called
    `reset_pattern_registry()` and reloaded `felix.patterns` to put the builtins back, which
    does not re-run `register_pattern` in the already-imported `felix.patterns.react` — so
    `react` vanished from the registry and twelve unrelated tests failed after this one.
    """
    from felix.patterns import registry

    def _noop(ctx: Any) -> Any:  # pragma: no cover - never built
        raise AssertionError("this pattern is only ever asked about, never built")

    registry.register_pattern("plugin-quiet", _noop)
    registry.register_pattern("plugin-shaped", _noop, honours_output_schema=True)
    try:
        assert registry.honours_output_schema("plugin-quiet") is False, "the default must be no"
        assert registry.honours_output_schema("plugin-shaped") is True
        assert registry.honours_output_schema("never-registered") is False
        # The builtins are the live answer, not a copy of the list in this file.
        assert registry.honours_output_schema("react") is True
        assert registry.honours_output_schema("plan_execute") is False
    finally:
        for name in ("plugin-quiet", "plugin-shaped"):
            registry._patterns.pop(name, None)
