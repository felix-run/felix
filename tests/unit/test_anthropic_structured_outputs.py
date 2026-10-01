"""Anthropic structured output by model: native format, forced tool, or offered tool.

Which route a schema takes is decided per model from two catalog quirks. The failure each test
here guards is a provider 400, which is the whole agent answering nothing:

* `forced_tool_choice` — Fable 5.1, Mythos 5.1, Opus 5.5 and Sonnet 5.5 reject `tool_choice`
  `any` and `tool` outright, so a forced schema tool on those models failed every structured
  turn, with or without extended thinking;
* `structured_outputs` — `output_config.format` on a model without it is a 400 the other way,
  and a schema outside the native subset is a 400 even on a model with it.

The cross-wire statement (a caller gets JSON text and `end_turn` on every route) is in
`tests/conformance/test_model_provider.py`, which runs it against a native and a tool arm.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix_ai.catalog import all_entries, entry_for
from felix_ai.decide.llm import output_schema_for
from felix_ai.decide.types import Choice, Noul, Score
from felix_ai.output_schema import anthropic_native_misfit, fits_anthropic_native
from felix_ai.types import ChatMessage, ModelRoute, ToolSchema
from felix_ai.wire.anthropic_messages import (
    STRUCTURED_OUTPUT_TOOL,
    AnthropicMessagesClient,
    apply_anthropic_output_schema,
)

STRICT: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["answer", "confidence"],
    "additionalProperties": False,
}
# Valid for the tool route and refused by the native one: a string constraint.
BOUNDED: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string", "maxLength": 80}},
    "required": ["answer"],
    "additionalProperties": False,
}

NATIVE_MODEL = "claude-sonnet-5"
# Every model the provider answers `400 tool_choice: type "tool" and "any" are not supported`.
UNFORCEABLE = ("claude-fable-5-1", "claude-mythos-5-1", "claude-opus-5-5", "claude-sonnet-5-5")


class _Spec:
    def __init__(self, thinking_budget: int | None = None) -> None:
        self.cache = False
        self.thinking_budget = thinking_budget
        self.temperature = 0
        self.max_tokens = None


class _Tool:
    name = "calculator"
    description = "adds"
    args_schema: dict[str, Any] = {"type": "object", "properties": {"x": {"type": "number"}}}
    raw_input_schema = None


def _body(
    model: str, schema: dict[str, Any] | None, *, thinking: int | None = None, tools: bool = False
) -> dict[str, Any]:
    """The real `_body`, which is the only thing that pins the thinking pass running first."""
    client = AnthropicMessagesClient(
        model_id=model,
        route=ModelRoute(provider="anthropic", model=model),
        settings=type("_S", (), {"model_timeout_seconds": 30})(),
        spec=_Spec(thinking),
        base_url="https://example.invalid",
        api_key="k",
    )
    bound: list[ToolSchema] = [_Tool()] if tools else []
    return client._body([ChatMessage(role="user", content="hi")], bound, 0.0, 1024, output_schema=schema)


# --- the catalog --------------------------------------------------------------------------------


def test_each_point_release_that_refuses_forcing_says_so() -> None:
    """`claude-sonnet-5-5` used to resolve to the `claude-sonnet-5` key — same price, but that
    entry forces — so it needs a key of its own; each of the four must resolve to an entry that
    does not force. Asserted by id, not by key, since lookup is longest-substring."""
    for model in UNFORCEABLE:
        quirks = entry_for(model).quirks
        assert quirks.forced_tool_choice is False, model
        assert quirks.structured_outputs is True, model
    assert entry_for("claude-sonnet-5").quirks.forced_tool_choice is True
    assert entry_for("claude-opus-5").quirks.forced_tool_choice is True


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-sonnet-4-20250514",
    ],
)
def test_models_without_native_outputs_are_not_sent_them(model: str) -> None:
    assert entry_for(model).quirks.structured_outputs is False


@pytest.mark.parametrize("model", ["claude-opus-6", "claude-fable-7", "claude-nova-1", "some-gateway-model"])
def test_an_unvouched_id_neither_forces_nor_sends_a_native_format(model: str) -> None:
    """A family key or `_DEFAULT` answers for models nobody has described yet, and the newest
    ones are the ones that refuse forcing. An offered schema is a weaker answer; a 400 is none."""
    quirks = entry_for(model).quirks
    assert quirks.forced_tool_choice is False
    assert quirks.structured_outputs is False


# --- the native route ---------------------------------------------------------------------------


def test_a_native_model_gets_output_config_and_no_schema_tool() -> None:
    body = _body(NATIVE_MODEL, STRICT)
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": STRICT}
    assert "tool_choice" not in body
    assert "tools" not in body


def test_the_native_format_joins_effort_rather_than_replacing_it() -> None:
    """The thinking pass writes `output_config.effort` first. Assigning `output_config` over it
    would silently drop the operator's thinking depth on every structured turn."""
    body = _body(NATIVE_MODEL, STRICT, thinking=8192)
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": STRICT},
    }
    assert "tool_choice" not in body, "native output needs no choice, and thinking forbids a forced one"


def test_real_tools_stay_bound_and_unforced_on_the_native_route() -> None:
    body = _body(NATIVE_MODEL, STRICT, tools=True)
    assert [t["name"] for t in body["tools"]] == ["calculator"]
    assert "tool_choice" not in body
    assert body["output_config"]["format"]["schema"] is STRICT


def test_a_schema_outside_the_subset_falls_back_to_the_forced_tool(caplog: pytest.LogCaptureFixture) -> None:
    """Sonnet 5 has native outputs and accepts forcing, so `maxLength` costs only the route."""
    body = _body(NATIVE_MODEL, BOUNDED)
    assert "format" not in (body.get("output_config") or {})
    assert body["tool_choice"] == {"type": "tool", "name": STRUCTURED_OUTPUT_TOOL}
    assert body["tools"][-1]["input_schema"] is BOUNDED


def test_outside_the_subset_with_thinking_the_schema_is_offered_and_the_reason_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="felix_ai.wire.anthropic_messages"):
        body = _body(NATIVE_MODEL, BOUNDED, thinking=8192)
    assert body["tool_choice"] == {"type": "auto"}
    assert "maxLength" in caplog.text, "the author needs to know which keyword cost the guarantee"


def test_a_model_without_native_outputs_keeps_the_forced_tool() -> None:
    body = _body("claude-sonnet-4-5", STRICT)
    assert "output_config" not in body
    assert body["tool_choice"] == {"type": "tool", "name": STRUCTURED_OUTPUT_TOOL}


def test_the_reserved_name_is_refused_on_the_native_route_too() -> None:
    """Whether a manifest compiles must not depend on which model its route names."""
    body: dict[str, Any] = {
        "model": NATIVE_MODEL,
        "messages": [],
        "tools": [{"name": STRUCTURED_OUTPUT_TOOL, "input_schema": {"type": "object"}}],
    }
    with pytest.raises(ValueError, match=STRUCTURED_OUTPUT_TOOL):
        apply_anthropic_output_schema(body, STRICT)


# --- never forcing a model that refuses it ------------------------------------------------------


def _unforceable_keys() -> list[str]:
    return sorted(k for k, e in all_entries().items() if not e.quirks.forced_tool_choice)


def test_the_never_force_sweep_covers_the_models_that_refuse() -> None:
    """The sweep below is driven by the catalog, so it would pass vacuously on a catalog that
    marked nothing. Pin that the four named models are in it."""
    assert set(UNFORCEABLE) <= set(_unforceable_keys())


@pytest.mark.parametrize("key", _unforceable_keys())
@pytest.mark.parametrize("schema", [None, STRICT, BOUNDED], ids=["no-schema", "native", "bounded"])
@pytest.mark.parametrize("thinking", [None, 8192], ids=["no-thinking", "thinking"])
@pytest.mark.parametrize("tools", [False, True], ids=["no-tools", "tools"])
def test_a_model_that_refuses_forcing_is_never_forced(
    key: str, schema: dict[str, Any] | None, thinking: int | None, tools: bool
) -> None:
    body = _body(key, schema, thinking=thinking, tools=tools)
    assert (body.get("tool_choice") or {}).get("type") not in {"any", "tool"}, body.get("tool_choice")


# --- the subset checker -------------------------------------------------------------------------


def _obj(**properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


FITS: dict[str, dict[str, Any]] = {
    "flat": STRICT,
    "optional-field": {**_obj(a={"type": "string"}), "required": []},
    "nested": _obj(inner=_obj(n={"type": "integer"})),
    "array-of-objects": _obj(rows={"type": "array", "items": _obj(k={"type": "string"})}),
    "enum-const-anyof": _obj(
        kind={"enum": ["a", "b"]},
        fixed={"const": 1},
        maybe={"anyOf": [{"type": "string"}, {"type": "null"}]},
    ),
    "nullable-type-list": _obj(x={"type": ["string", "null"]}),
    "supported-format": _obj(
        at={"type": "string", "format": "date-time"}, who={"type": "string", "format": "email"}
    ),
    "annotations": {**_obj(a={"type": "string", "description": "d", "title": "t"}), "$schema": "x"},
    "defs-non-recursive": {
        **_obj(item={"$ref": "#/$defs/Item"}),
        "$defs": {"Item": _obj(name={"type": "string"})},
    },
}

MISFITS: dict[str, tuple[dict[str, Any], str]] = {
    "maxLength": (_obj(a={"type": "string", "maxLength": 5}), "maxLength"),
    "minLength": (_obj(a={"type": "string", "minLength": 1}), "minLength"),
    "pattern": (_obj(a={"type": "string", "pattern": "^x$"}), "pattern"),
    "minimum": (_obj(a={"type": "integer", "minimum": 0}), "minimum"),
    "multipleOf": (_obj(a={"type": "number", "multipleOf": 2}), "multipleOf"),
    "minItems": (_obj(a={"type": "array", "items": {"type": "string"}, "minItems": 1}), "minItems"),
    "uniqueItems": (
        _obj(a={"type": "array", "items": {"type": "string"}, "uniqueItems": True}),
        "uniqueItems",
    ),
    "oneOf": (_obj(a={"oneOf": [{"type": "string"}, {"type": "integer"}]}), "oneOf"),
    "open-root": ({**STRICT, "additionalProperties": True}, "additionalProperties"),
    "unclosed-root": (
        {k: v for k, v in STRICT.items() if k != "additionalProperties"},
        "additionalProperties",
    ),
    "unclosed-nested": (
        _obj(inner={"type": "object", "properties": {"x": {"type": "string"}}}),
        "additionalProperties",
    ),
    "schema-valued-additional": (
        {**STRICT, "additionalProperties": {"type": "string"}},
        "additionalProperties",
    ),
    "unsupported-format": (_obj(a={"type": "string", "format": "regex"}), "regex"),
    "recursive-def": (
        {
            **_obj(node={"$ref": "#/$defs/Node"}),
            "$defs": {"Node": _obj(child={"anyOf": [{"$ref": "#/$defs/Node"}, {"type": "null"}]})},
        },
        "recursive",
    ),
    "mutually-recursive": (
        {
            **_obj(a={"$ref": "#/$defs/A"}),
            "$defs": {"A": _obj(b={"$ref": "#/$defs/B"}), "B": _obj(a={"$ref": "#/$defs/A"})},
        },
        "recursive",
    ),
    "root-ref": (_obj(again={"$ref": "#"}), "'#'"),
}


@pytest.mark.parametrize("schema", list(FITS.values()), ids=list(FITS))
def test_schemas_in_the_native_subset_fit(schema: dict[str, Any]) -> None:
    assert anthropic_native_misfit(schema) is None
    assert fits_anthropic_native(schema)


@pytest.mark.parametrize(("schema", "reason"), list(MISFITS.values()), ids=list(MISFITS))
def test_schemas_outside_the_native_subset_do_not_and_say_why(schema: dict[str, Any], reason: str) -> None:
    misfit = anthropic_native_misfit(schema)
    assert misfit is not None
    assert reason in misfit
    assert not fits_anthropic_native(schema)


def test_the_checker_reports_and_never_repairs() -> None:
    schema = _obj(a={"type": "string", "maxLength": 5})
    before = json.dumps(schema, sort_keys=True)
    anthropic_native_misfit(schema)
    assert json.dumps(schema, sort_keys=True) == before


# --- the decider's schema ----------------------------------------------------------------------


def test_the_llm_deciders_schema_fits_the_native_subset() -> None:
    """The LLM decider asks every chat model for its answer through `output_schema`. Numeric
    bounds on a score or a probability put that schema outside the native subset, so on the
    models that refuse forcing the decider's answer would only ever be *offered* a shape."""
    schema = output_schema_for(
        {
            "pick": Choice(instructions="which", criteria={"a": None, "b": None}),
            "grade": Score(instructions="how good", levels=("bad", "ok", "good")),
            "likely": Noul(instructions="is it"),
        }
    )
    assert anthropic_native_misfit(schema) is None
    assert schema["properties"]["grade"]["enum"] == [0, 1, 2]


@pytest.mark.parametrize(
    "model_id", ["claude-3-5-sonnet-latest", "claude-3-7-sonnet-20250219", "claude-3-haiku-20240307"]
)
def test_a_claude_3_id_keeps_the_forced_route(model_id: str) -> None:
    # Only the bare `claude` key matched these, and it neither forces nor goes native, so a route
    # pinned to a 3.x model quietly lost its structured-output guarantee.
    from felix_ai.catalog import entry_for

    quirks = entry_for(model_id).quirks
    assert quirks.forced_tool_choice is True
    assert quirks.structured_outputs is False


@pytest.mark.parametrize("value", [3, -1, 7, 2.01])
def test_a_score_outside_the_offered_levels_is_refused(value: float) -> None:
    from felix_ai.decide.llm import _answer
    from felix_ai.decide.types import Score

    with pytest.raises(ValueError, match="offered levels"):
        _answer(Score(instructions="rate", levels=("low", "mid", "high")), value)


@pytest.mark.parametrize("value", [0, 1.5, 2])
def test_a_score_inside_the_range_is_kept_fractional_included(value: float) -> None:
    from felix_ai.decide.llm import _answer
    from felix_ai.decide.types import Score

    assert _answer(Score(instructions="rate", levels=("low", "mid", "high")), value).score == value
