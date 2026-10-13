"""The workflow definition: structure on parse, the graph through `problems()`.

Each graph test starts from `_triage()`, a workflow that has no problems, and breaks one thing,
so a check that stopped firing fails its own test rather than passing on a graph that was
broken some other way.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.workflows import kinds
from felix.workflows.check import check, problems, resolve_ports
from felix.workflows.ports import Port, describe, port_mismatch
from felix.workflows.schema import WorkflowSpec
from pydantic import BaseModel, ValidationError

_LABEL = {"type": "string", "enum": ["bug", "feature"]}


def _triage(**overrides: Any) -> dict[str, Any]:
    """trigger.issue → classify (agent, structured) → approve → post (tool); classify.label → route."""
    spec: dict[str, Any] = {
        "name": "Triage issue",
        "inputs": {"issue": {"type": "string"}},
        "steps": [
            {
                "id": "classify",
                "kind": "agent_turn",
                "config": {
                    "manifest": "quick",
                    "prompt": "Classify this issue: {{ issue }}",
                    "output_schema": {
                        "type": "object",
                        "properties": {"label": _LABEL, "summary": {"type": "string"}},
                        "required": ["label", "summary"],
                    },
                },
            },
            {"id": "approve", "kind": "approval", "config": {"prompt": "Post this summary?"}},
            {
                "id": "post",
                "kind": "tool_call",
                "config": {
                    "manifest": "quick",
                    "tool": "github__add_comment",
                    "input_schema": {
                        "type": "object",
                        "properties": {"body": {"type": "string"}, "issue": {"type": "integer"}},
                        "required": ["body", "issue"],
                    },
                    "args": {"issue": 7},
                },
            },
            {"id": "route", "kind": "branch", "config": {"cases": ["bug", "feature"]}},
        ],
        "edges": [
            _edge("trigger", "issue", "classify", "issue"),
            _edge("classify", "summary", "approve", "payload"),
            _edge("classify", "summary", "post", "body"),
            _edge("approve", "approved", "post", "when"),
            _edge("classify", "label", "route", "value"),
        ],
    }
    spec.update(overrides)
    return spec


def _edge(src: str, src_port: str, dst: str, dst_port: str) -> dict[str, Any]:
    return {"source": {"step": src, "port": src_port}, "target": {"step": dst, "port": dst_port}}


def _codes(spec: dict[str, Any]) -> list[str]:
    return [p.code for p in problems(WorkflowSpec.model_validate(spec))]


def test_the_baseline_has_no_problems() -> None:
    assert problems(WorkflowSpec.model_validate(_triage())) == []


# --- structure ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("steps", "needle"),
    [
        ([{"id": "a", "kind": "branch"}, {"id": "a", "kind": "branch"}], "used twice"),
        ([{"id": "trigger", "kind": "branch"}], "reserved"),
        ([{"id": "Has-Caps", "kind": "branch"}], "pattern"),
    ],
)
def test_step_ids_are_unique_unreserved_and_plain(steps: list[dict[str, Any]], needle: str) -> None:
    with pytest.raises(ValidationError, match=needle):
        WorkflowSpec.model_validate({"name": "w", "steps": steps})


def test_an_unknown_top_level_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="extra"):
        WorkflowSpec.model_validate({"name": "w", "stages": []})


def test_an_unknown_kind_still_parses_and_is_reported() -> None:
    # A stored workflow whose plugin kind went away must load so the editor can say so.
    spec = _triage()
    spec["steps"][1]["kind"] = "plugin_only"
    found = problems(WorkflowSpec.model_validate(spec))
    assert [(p.code, p.step) for p in found] == [("unknown_kind", "approve")]


def test_a_config_the_kind_refuses_still_parses_and_is_reported() -> None:
    spec = _triage()
    spec["steps"][1]["config"] = {"ttl_seconds": 99999}
    found = problems(WorkflowSpec.model_validate(spec))
    assert [(p.code, p.step) for p in found] == [("invalid_config", "approve")]
    assert "ttl_seconds" in found[0].message


# --- the graph ------------------------------------------------------------------------------


def test_a_cycle_is_reported_with_its_steps() -> None:
    spec = _triage()
    spec["edges"].append(_edge("post", "result", "classify", "when"))
    found = [p for p in problems(WorkflowSpec.model_validate(spec)) if p.code == "cycle"]
    assert len(found) == 1
    assert "approve" in found[0].message and "classify" in found[0].message and "post" in found[0].message
    assert "route" not in found[0].message, "a step downstream of the cycle is behind it, not on it"


def test_a_required_input_left_unwired_is_reported() -> None:
    spec = _triage()
    spec["edges"] = [e for e in spec["edges"] if e["target"] != {"step": "post", "port": "body"}]
    found = problems(WorkflowSpec.model_validate(spec))
    assert [(p.code, p.step, p.port) for p in found] == [("unconnected_input", "post", "body")]


def test_a_fixed_arg_is_not_an_input() -> None:
    # `issue` is required by the tool and fixed in `args`, so nothing has to feed it.
    ports, _ = resolve_ports(WorkflowSpec.model_validate(_triage()).steps[2])
    assert ports is not None
    assert [p.name for p in ports.inputs] == ["body", "when"]


def test_two_edges_into_one_data_input_are_reported() -> None:
    spec = _triage()
    spec["edges"].append(_edge("trigger", "issue", "post", "body"))
    found = problems(WorkflowSpec.model_validate(spec))
    assert [(p.code, p.step, p.port) for p in found] == [("duplicate_input", "post", "body")]


def test_many_control_edges_may_feed_when() -> None:
    spec = _triage()
    spec["edges"].append(_edge("route", "bug", "post", "when"))
    assert _codes(spec) == []


def test_a_mistyped_edge_is_reported_with_both_types() -> None:
    spec = _triage(inputs={"issue": {"type": "array", "items": {"type": "object"}}})
    found = problems(WorkflowSpec.model_validate(spec))
    assert [p.code for p in found] == []  # agent prompt variables accept anything
    spec["edges"].append(_edge("trigger", "issue", "route", "value"))
    spec["edges"] = [e for e in spec["edges"] if e["source"] != {"step": "classify", "port": "label"}]
    found = problems(WorkflowSpec.model_validate(spec))
    assert [(p.code, p.step, p.port) for p in found] == [("type_mismatch", "route", "value")]
    assert "array<object> → string" in found[0].message


def test_data_cannot_feed_when_and_control_cannot_feed_data() -> None:
    spec = _triage()
    spec["edges"].append(_edge("classify", "summary", "route", "when"))
    assert _codes(spec) == ["type_mismatch"]
    spec = _triage()
    spec["edges"] = [e for e in spec["edges"] if e["target"] != {"step": "approve", "port": "payload"}]
    spec["edges"].append(_edge("route", "bug", "approve", "payload"))
    assert _codes(spec) == ["type_mismatch"]


@pytest.mark.parametrize(
    ("edge", "code"),
    [
        (_edge("nowhere", "x", "post", "when"), "unknown_step"),
        (_edge("classify", "summary", "nowhere", "x"), "unknown_step"),
        (_edge("classify", "summary", "trigger", "issue"), "unknown_step"),
        (_edge("classify", "verdict", "post", "when"), "unknown_port"),
        (_edge("classify", "summary", "post", "title"), "unknown_port"),
    ],
)
def test_edges_to_things_that_do_not_exist_are_reported(edge: dict[str, Any], code: str) -> None:
    spec = _triage()
    spec["edges"].append(edge)
    assert _codes(spec) == [code]


# --- the built-in kinds ---------------------------------------------------------------------


def test_agent_turn_ports_follow_the_prompt_and_the_output_schema() -> None:
    plain = kinds.AgentTurnConfig(manifest="quick", prompt="{{a}} then {{ b }} then {{a}}")
    inputs, outputs = kinds.get_step_kind("agent_turn").ports(plain)  # type: ignore[union-attr]
    assert [p.name for p in inputs] == ["a", "b"]
    assert all(p.required for p in inputs)
    assert [(p.name, p.schema) for p in outputs] == [("text", {"type": "string"})]

    structured = WorkflowSpec.model_validate(_triage()).steps[0]
    ports, _ = resolve_ports(structured)
    assert ports is not None
    assert [p.name for p in ports.outputs] == ["output", "label", "summary"]
    assert ports.outputs[1].schema == _LABEL


@pytest.mark.parametrize(
    ("config", "needle"),
    [
        ({"manifest": "quick", "prompt": "x", "output_schema": {"type": "array"}}, "object"),
        (
            {
                "manifest": "quick",
                "prompt": "x",
                "output_schema": {"type": "object", "properties": {"output": {}}},
            },
            "whole object",
        ),
        ({"manifest": "quick", "prompt": "{{ when }}"}, "reserved"),
    ],
)
def test_agent_turn_refuses_configs_that_cannot_become_ports(config: dict[str, Any], needle: str) -> None:
    with pytest.raises(ValidationError, match=needle):
        kinds.AgentTurnConfig.model_validate(config)


def test_branch_ports_are_true_false_or_its_cases_plus_default() -> None:
    kind = kinds.get_step_kind("branch")
    assert kind is not None
    inputs, outputs = kind.ports(kinds.BranchConfig())
    assert inputs[0].schema == {"type": "boolean"}
    assert [(p.name, p.control) for p in outputs] == [("true", True), ("false", True)]
    _, outputs = kind.ports(kinds.BranchConfig(cases=["bug", "feature"]))
    assert [p.name for p in outputs] == ["bug", "feature", "default"]
    with pytest.raises(ValidationError, match="default"):
        kinds.BranchConfig(cases=["default"])


def test_a_registered_kind_is_checked_like_a_built_in(monkeypatch: pytest.MonkeyPatch) -> None:
    class Wait(BaseModel):
        seconds: int

    monkeypatch.setitem(
        kinds._kinds, "wait", kinds.StepKind(config=Wait, ports=lambda _c: ([], []), label="Wait")
    )
    spec = _triage()
    spec["steps"].append({"id": "pause", "kind": "wait", "config": {"seconds": 5}})
    assert _codes(spec) == []
    spec["steps"][-1]["config"] = {}
    assert _codes(spec) == ["invalid_config"]


# --- port types -----------------------------------------------------------------------------


def _data(schema: dict[str, Any]) -> Port:
    return Port("p", schema)


@pytest.mark.parametrize(
    ("source", "target", "ok"),
    [
        ({}, {"type": "string"}, True),
        ({"type": "string"}, {}, True),
        ({"type": "integer"}, {"type": "number"}, True),
        ({"type": ["string", "null"]}, {"type": "string"}, False),
        ({"type": "string"}, {"type": ["string", "null"]}, True),
        ({"enum": [1, 2]}, {"type": "string"}, False),
        ({"type": "number"}, {"type": "integer"}, False),
        (_LABEL, {"type": "string"}, True),
        ({"type": "string"}, _LABEL, False),
        ({"enum": ["bug"]}, _LABEL, True),
        ({"enum": ["bug", "chore"]}, _LABEL, False),
        (
            {"type": "array", "items": {"type": "string"}},
            {"type": "array", "items": {"type": "string"}},
            True,
        ),
        (
            {"type": "array", "items": {"type": "string"}},
            {"type": "array", "items": {"type": "integer"}},
            False,
        ),
        ({"type": "object"}, {"type": "object", "required": ["a"]}, True),
        ({"type": "object", "properties": {"b": {}}}, {"type": "object", "required": ["a"]}, False),
        (
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "properties": {"a": {"type": "boolean"}}, "required": ["a"]},
            False,
        ),
    ],
)
def test_port_compatibility(source: dict[str, Any], target: dict[str, Any], ok: bool) -> None:
    assert (port_mismatch(_data(source), _data(target)) is None) is ok


def test_deeply_nested_schemas_do_not_recurse_forever() -> None:
    deep: dict[str, Any] = {"type": "string"}
    for _ in range(5000):
        deep = {"type": "array", "items": deep}
    assert port_mismatch(_data(deep), _data(deep)) is None
    # A mismatch describes both types, so this is the path that recursed through `describe`.
    reason = port_mismatch(_data(deep), _data({"type": "string"}))
    assert reason is not None and reason.endswith("→ string") and "…" in reason


@pytest.mark.parametrize(
    ("schema", "shown"),
    [
        ({}, "any"),
        ({"type": "string"}, "string"),
        ({"type": "array", "items": {"type": "object"}}, "array<object>"),
        (_LABEL, "enum<bug|feature>"),
        ({"type": ["string", "null"]}, "null|string"),
    ],
)
def test_describe(schema: dict[str, Any], shown: str) -> None:
    assert describe(schema) == shown


def test_an_optional_tool_input_is_a_port_nothing_has_to_feed() -> None:
    config = kinds.ToolCallConfig(
        manifest="quick",
        tool="search",
        input_schema={
            "type": "object",
            "properties": {"q": {"type": "string"}, "limit": {}},
            "required": ["q"],
        },
    )
    inputs, _ = kinds.get_step_kind("tool_call").ports(config)  # type: ignore[union-attr]
    assert [(p.name, p.required) for p in inputs] == [("q", True), ("limit", False)]


def test_a_tool_input_keeps_the_tool_s_own_name() -> None:
    # MCP tools name arguments their own way; the step has to take them as they are.
    config = kinds.ToolCallConfig(
        manifest="quick",
        tool="github__get_pull_request",
        input_schema={"properties": {"pullNumber": {"type": "integer"}, "repo-name": {}, "x" * 64: {}}},
    )
    inputs, _ = kinds.get_step_kind("tool_call").ports(config)  # type: ignore[union-attr]
    assert [p.name for p in inputs] == ["pullNumber", "repo-name", "x" * 64]


@pytest.mark.parametrize(
    ("make", "needle"),
    [
        (lambda: WorkflowSpec.model_validate({"name": "w", "inputs": {"Issue Body": {}}}), "input name"),
        (
            lambda: kinds.ToolCallConfig(
                manifest="quick", tool="t", input_schema={"properties": {"when": {}}}
            ),
            "reserved",
        ),
        (
            lambda: kinds.ToolCallConfig(
                manifest="quick", tool="t", input_schema={"properties": {"x" * 65: {}}}
            ),
            "1 to 64",
        ),
        (
            lambda: kinds.ToolCallConfig(
                manifest="quick", tool="t", input_schema={"properties": {"a\nb": {}}}
            ),
            "control character",
        ),
        (lambda: kinds.BranchConfig(cases=["bug", "bug"]), "distinct"),
        (
            lambda: WorkflowSpec.model_validate(
                {"name": "w", "steps": [{"id": f"s{i}", "kind": "branch"} for i in range(201)]}
            ),
            "200",
        ),
    ],
)
def test_names_and_sizes_that_cannot_become_ports_are_refused(make: Any, needle: str) -> None:
    with pytest.raises(ValidationError, match=needle):
        make()


@pytest.mark.parametrize(
    ("edges", "on_cycle"),
    [
        # a ⇄ b → c → d ⇄ e: c sits between two cycles and is on neither.
        ([("a", "b"), ("b", "a"), ("b", "c"), ("c", "d"), ("d", "e"), ("e", "d")], "a, b, d, e"),
        ([("a", "a"), ("a", "b")], "a"),
    ],
)
def test_only_steps_on_a_cycle_are_named(edges: list[tuple[str, str]], on_cycle: str) -> None:
    names = sorted({n for e in edges for n in e})
    spec = {
        "name": "w",
        "steps": [{"id": n, "kind": "approval"} for n in names],
        "edges": [_edge(src, "approved", dst, "when") for src, dst in edges],
    }
    found = problems(WorkflowSpec.model_validate(spec))
    assert [p.message for p in found] == [f"these steps wait on each other: {on_cycle}"]


@pytest.mark.parametrize(
    "input_schema",
    [
        {"properties": {"a": {}}, "required": [["a"]]},
        {"properties": {"a": {}}, "required": 5},
        {"properties": ["a"]},
    ],
)
def test_a_malformed_tool_schema_is_a_problem_not_a_crash(input_schema: dict[str, Any]) -> None:
    spec = _triage()
    spec["steps"][2]["config"]["input_schema"] = input_schema
    assert "invalid_config" in _codes(spec)


def test_a_malformed_target_schema_does_not_crash_the_type_check() -> None:
    source = _data({"type": "object", "properties": {"a": {"type": "string"}}})
    for target in (
        {"type": "object", "properties": ["a"], "required": ["a"]},
        {"type": "object", "required": "a"},
        {"type": "object", "required": [["a"]]},
    ):
        assert port_mismatch(source, _data(target)) is None


@pytest.mark.parametrize("where", ["config", "ports"])
def test_a_kind_that_raises_is_that_steps_problem(monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    from pydantic import model_validator

    class Broken(BaseModel):
        @model_validator(mode="before")
        @classmethod
        def _boom(cls, v: Any) -> Any:
            if where == "config":
                raise TypeError("plugin bug")
            return v

    def ports(_c: Any) -> Any:
        raise KeyError("plugin bug")

    monkeypatch.setitem(kinds._kinds, "broken", kinds.StepKind(config=Broken, ports=ports, label="Broken"))
    spec = _triage()
    spec["steps"].append({"id": "odd", "kind": "broken"})
    found = problems(WorkflowSpec.model_validate(spec))
    assert [(p.code, p.step) for p in found] == [("kind_error", "odd")]


def test_check_returns_the_ports_it_resolved_beside_the_problems() -> None:
    spec = _triage()
    spec["steps"][1]["kind"] = "plugin_only"
    ports, found = check(WorkflowSpec.model_validate(spec))
    assert set(ports) == {"trigger", "classify", "approve", "post", "route"}
    assert ports["approve"] is None, "a step whose ports cannot be known maps to None"
    assert [p.name for p in ports["trigger"].outputs] == ["issue"]  # type: ignore[union-attr]
    assert [p.name for p in ports["post"].inputs] == ["body", "when"]  # type: ignore[union-attr]
    assert [p.code for p in found] == ["unknown_kind"]


def test_a_wide_and_deep_schema_is_checked_in_bounded_time() -> None:
    import time

    # Three distinct required fields sharing one subtree, sixteen levels down: 3**16 comparisons
    # without the node budget, from a single edge.
    node: dict[str, Any] = {"type": "string"}
    for _ in range(16):
        node = {
            "type": "object",
            "properties": {"a": node, "b": node, "c": node},
            "required": ["a", "b", "c"],
        }
    started = time.monotonic()
    port_mismatch(_data(node), _data(node))
    assert time.monotonic() - started < 1.0


def test_a_long_enum_is_shown_cut_short() -> None:
    shown = describe({"enum": [f"value{i}" * 50 for i in range(10_000)]})
    assert len(shown) < 500
    assert shown.endswith("|…>")


@pytest.mark.parametrize(
    "make",
    [
        lambda: WorkflowSpec.model_validate({"name": "w", "inputs": {"issue\n": {}}}),
        lambda: kinds.BranchConfig(cases=["bug\n"]),
        lambda: kinds.ToolCallConfig(manifest="quick", tool="t", input_schema={"properties": {"body\n": {}}}),
    ],
)
def test_a_trailing_newline_does_not_pass_a_name_check(make: Any) -> None:
    with pytest.raises(ValidationError):
        make()


def test_a_definition_over_the_size_limit_is_refused() -> None:
    from felix.workflows.schema import MAX_DEFINITION_BYTES

    big = {"type": "string", "description": "x" * MAX_DEFINITION_BYTES}
    with pytest.raises(ValidationError, match="limit"):
        WorkflowSpec.model_validate({"name": "w", "inputs": {"issue": big}})


def test_a_tool_call_saved_before_it_named_a_manifest_reads_as_a_problem() -> None:
    # `manifest` became required on `tool_call` once the step could run; a row stored without it
    # still loads, and the check says what to fix.
    spec = _triage()
    del spec["steps"][2]["config"]["manifest"]
    found = problems(WorkflowSpec.model_validate(spec))
    assert [(p.code, p.step) for p in found] == [("invalid_config", "post")]
    assert "manifest" in found[0].message
