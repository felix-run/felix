"""Port types: what a step's ports are, and whether one can feed another.

A port is named and typed by a JSON Schema (`{}` accepts or produces anything). A *control* port
carries no data, only "this way was taken". Every step also has the implicit control input `when`.
The compatibility check is advisory and deliberately partial; see `port_mismatch`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Port names are referenced from edges and, later, from templates.
PORT_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
# The implicit control input every step has.
WHEN = "when"


@dataclass(frozen=True, slots=True)
class Port:
    name: str
    # JSON Schema of the value; `{}` accepts or produces anything. Ignored on a control port.
    schema: dict[str, Any]
    control: bool = False
    # An input the step cannot run without. Meaningless on an output.
    required: bool = False


@dataclass(frozen=True, slots=True)
class StepPorts:
    inputs: tuple[Port, ...]
    outputs: tuple[Port, ...]


# --- compatibility --------------------------------------------------------------------------


def _types(schema: dict[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {t for t in declared if isinstance(t, str)}
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return {_json_type(v) for v in enum}
    return set()


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return "array" if isinstance(value, list) else "object"


# The type check walks an author's schemas, so it is bounded twice over and treats anything past
# either bound as compatible (it is advisory). Depth stops a `RecursionError`. The node budget
# stops width: a `required` list naming a field k times would otherwise re-walk its subtree k
# times at every level, k**depth comparisons from one edge, all on the event loop.
_MAX_TYPE_DEPTH = 16
_MAX_TYPE_NODES = 2000
# How much of an enum a type is shown with: problem messages are built per edge, and a 1 MB enum
# copied into each of a thousand of them is a gigabyte.
_MAX_ENUM_SHOWN = 8
_MAX_VALUE_SHOWN = 40


def _shown(value: str) -> str:
    """An author's name as a message quotes it: a field name inside a schema has no length rule."""
    return value if len(value) <= _MAX_VALUE_SHOWN else value[:_MAX_VALUE_SHOWN] + "…"


def describe(schema: dict[str, Any], _depth: int = 0) -> str:
    """A port type as the editor shows it: `string`, `array<object>`, `enum<bug|feature>`.

    Nesting past `_MAX_TYPE_DEPTH` is shown as `…`: this runs on an author's schema, inside the
    mismatch message, and must not recurse as deep as the schema does.
    """
    if _depth > _MAX_TYPE_DEPTH:
        return "…"
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        shown = [str(v)[:_MAX_VALUE_SHOWN] for v in enum[:_MAX_ENUM_SHOWN]]
        more = "|…" if len(enum) > _MAX_ENUM_SHOWN else ""
        return "enum<" + "|".join(shown) + more + ">"
    types = _types(schema)
    if not types:
        return "any"
    if types == {"array"}:
        items = schema.get("items")
        return f"array<{describe(items, _depth + 1) if isinstance(items, dict) else 'any'}>"
    return "|".join(sorted(types))


class _Budget:
    __slots__ = ("left",)

    def __init__(self) -> None:
        self.left = _MAX_TYPE_NODES


def _schema_mismatch(
    source: dict[str, Any], target: dict[str, Any], depth: int, budget: _Budget
) -> str | None:
    budget.left -= 1
    if not source or not target or depth > _MAX_TYPE_DEPTH or budget.left < 0:
        return None
    src_types, dst_types = _types(source), _types(target)
    if src_types and dst_types:
        accepted = dst_types | ({"integer"} if "number" in dst_types else set())
        if not src_types <= accepted:
            return f"{describe(source)} → {describe(target)}"
    if _enum_narrows(source, target):
        return f"{describe(source)} → {describe(target)}"
    if src_types == {"array"} and dst_types == {"array"}:
        src_items, dst_items = source.get("items"), target.get("items")
        if (
            isinstance(src_items, dict)
            and isinstance(dst_items, dict)
            and _schema_mismatch(src_items, dst_items, depth + 1, budget) is not None
        ):
            return f"{describe(source)} → {describe(target)}"
    if src_types == {"object"} and dst_types == {"object"}:
        return _object_mismatch(source, target, depth, budget)
    return None


def _enum_narrows(source: dict[str, Any], target: dict[str, Any]) -> bool:
    """The target takes only listed values and the source may produce one it does not list."""
    dst_enum = target.get("enum")
    if not isinstance(dst_enum, list) or not dst_enum:
        return False
    src_enum = source.get("enum")
    return not isinstance(src_enum, list) or not set(map(repr, src_enum)) <= set(map(repr, dst_enum))


def _object_mismatch(
    source: dict[str, Any], target: dict[str, Any], depth: int, budget: _Budget
) -> str | None:
    """A field the target requires that the source does not declare, or declares as another type.

    A source that declares no `properties` at all is an object of unknown shape and passes.
    """
    src_props = source.get("properties")
    if not isinstance(src_props, dict):
        return None
    dst_props = target.get("properties")
    if not isinstance(dst_props, dict):
        dst_props = {}
    required = target.get("required")
    names = [n for n in required if isinstance(n, str)] if isinstance(required, list) else []
    for name in dict.fromkeys(names):
        if name not in src_props:
            return f"{describe(source)} has no field {_shown(name)!r}"
        src_field, dst_field = src_props[name], dst_props.get(name)
        if isinstance(src_field, dict) and isinstance(dst_field, dict):
            inner = _schema_mismatch(src_field, dst_field, depth + 1, budget)
            if inner is not None:
                return f"field {_shown(name)!r}: {inner}"
    return None


def port_mismatch(source: Port, target: Port) -> str | None:
    """Why `source` cannot feed `target`, or `None` when it can.

    Deliberately partial: it compares the parts of JSON Schema a port type is drawn from
    (`type`, `enum`, `items`, `properties`/`required`) and accepts whatever it cannot judge,
    because a refused edge the author knows is fine is worse than a run that fails on a value
    the harness then reports exactly.
    """
    if source.control != target.control:
        return (
            "a control port can only feed a control port"
            if source.control
            else ("a data port cannot feed a control port")
        )
    if source.control:
        return None
    return _schema_mismatch(source.schema, target.schema, 0, _Budget())


# --- values -------------------------------------------------------------------------------

# How many of an array's items a value check reads, per level.
_MAX_ITEMS_CHECKED = 1000


def _value_type(value: Any) -> str:
    """A value's JSON Schema type: as `_json_type`, but `7.0` is an integer, as JSON Schema says."""
    if isinstance(value, float) and value.is_integer():
        return "integer"
    return _json_type(value)


def value_mismatch(schema: dict[str, Any], value: Any, _depth: int = 0) -> str | None:
    """Why `value` is not what `schema` declares, or `None`.

    A deliberately partial check, for a value from outside the harness (a trigger's payload)
    before it becomes a workflow input: `type`, `enum`, an object's `required` and declared
    `properties`, an array's `items`. Anything else the schema says is not checked, and a schema
    nested past `_MAX_TYPE_DEPTH` is accepted from there down. Never a step's own output: that is
    the step's to shape.
    """
    if _depth > _MAX_TYPE_DEPTH:
        return None
    got = _value_type(value)
    enum = schema.get("enum")
    # By type as well as equality: Python has `True == 1` and `1 == 1.0`, JSON does not.
    if (
        isinstance(enum, list)
        and enum
        and not any(member == value and _value_type(member) == got for member in enum)
    ):
        return "is not one of the allowed values"
    allowed = _types(schema) if "type" in schema else set()
    if allowed and got not in allowed and not (got == "integer" and "number" in allowed):
        return f"is {got}, not {'|'.join(sorted(allowed))}"
    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            absent = [str(k) for k in required if isinstance(k, str) and k not in value]
            if absent:
                return f"lacks {_shown(absent[0])}"
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, sub in properties.items():
                if name in value and isinstance(sub, dict):
                    reason = value_mismatch(sub, value[name], _depth + 1)
                    if reason is not None:
                        return f"{_shown(str(name))} {reason}"
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for item in value[:_MAX_ITEMS_CHECKED]:
            reason = value_mismatch(schema["items"], item, _depth + 1)
            if reason is not None:
                return f"has an item that {reason}"
    return None


__all__ = [
    "PORT_NAME_PATTERN",
    "WHEN",
    "Port",
    "StepPorts",
    "describe",
    "port_mismatch",
    "value_mismatch",
]
