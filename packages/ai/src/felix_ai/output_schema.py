"""Checking a JSON Schema before it becomes a provider's output contract.

A schema reaches this harness from two sides. `spec.output_schema` is operator input, written
once by whoever deploys the manifest; `response_format` on `/v1/chat/completions` is *client*
input, arriving with every request. Both end up serialised into the provider request body on
every turn of the loop, so both come through here.

What is checked is the set of shapes whose only other failure mode is a provider `400` —
relayed back through two hops as `invalid_request_error`, which tells the author nothing about
which part of their schema was wrong — plus bounds, since a client-supplied schema is otherwise
unbounded in size and depth.

What is deliberately *not* checked is whether the schema is valid JSON Schema in full. This is
not a validator; the provider is. Reimplementing draft 2020-12 here would be a second opinion
that can disagree with the one that actually decides.
"""

from __future__ import annotations

import json
from typing import Any

# Bounds against pathological input, not opinions about schema design — a provider's own
# nesting limit is stricter than this and is the one that shapes a schema (OpenAI strict mode
# allows five levels of *schema* nesting). Both counts are over raw JSON nodes, which is what
# the walk below can actually see: one schema level is two or three nodes deep once
# `properties` is counted, so 32 is roughly a dozen levels of nesting. The node limit is what
# stops one accepted request buying `recursion_limit` re-serialisations of a megabyte of
# schema, since the body is rebuilt on every turn of the loop.
MAX_DEPTH = 32
MAX_NODES = 1_000
# The bound the two above were described as providing and do not: node count is orthogonal to
# size, and 900 KB of schema fits in six nodes — one string value is one node. That is what
# actually gets re-serialised into the provider request on every turn of the loop, and on
# Anthropic the schema is a *tool definition*, so it sits inside the prefix the cache
# breakpoint covers: a per-request schema also destroys the conversation's prompt cache and
# bills every turn at the full write rate.
MAX_BYTES = 32 * 1024

# Keywords that take part in resolving a reference, and so decide what a `#`-prefixed pointer
# resolves *against*. `$schema` is deliberately absent: it names a dialect, every schema
# pydantic emits carries an https one, and rejecting it would reject the ordinary case.
_REFERENCE_KEYWORDS = ("$ref", "$id", "$dynamicRef")


class InvalidOutputSchema(ValueError):
    """A schema that cannot be an output contract, with the reason a client can act on.

    Its own type so a route can answer 422 for exactly this and not for the next
    `ValueError` the model layer grows underneath it.
    """


def validate_output_schema(schema: Any) -> dict[str, Any]:
    """The schema unchanged, or `InvalidOutputSchema` naming what is wrong with it.

    Returns rather than mutates: a schema quietly rewritten to something acceptable is the
    defect shape this repo produces most — the caller believes the contract they wrote is the
    contract being enforced. If a schema needs changing, its author changes it.
    """
    if not isinstance(schema, dict):
        raise InvalidOutputSchema(f"output_schema must be a JSON Schema object, not {type(schema).__name__}")
    if schema.get("type") != "object":
        # Both wires need an object at the root and for the same reason: OpenAI's
        # `json_schema` response format and Anthropic's tool `input_schema` are each
        # specified as objects. A bare `{"type": "array"}` is a 400 on either.
        raise InvalidOutputSchema('output_schema must have "type": "object" at its root')
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise InvalidOutputSchema('output_schema must declare a non-empty "properties" object')

    # Before the walk, because the walk is per-node and this is about bytes.
    try:
        size = len(json.dumps(schema).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise InvalidOutputSchema("output_schema is not JSON-serialisable") from exc
    if size > MAX_BYTES:
        raise InvalidOutputSchema(f"output_schema is too large ({size} bytes, limit {MAX_BYTES})")

    nodes = 0
    # Iterative, with the depth carried alongside each node: a recursive walk over
    # attacker-supplied nesting is a `RecursionError` — an unhandled 500 — rather than the
    # 422 this function exists to produce.
    stack: list[tuple[Any, int]] = [(schema, 1)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        # Both bounds are checked before the branch below, because that branch `continue`s on
        # a leaf — which is how the node bound came to count every leaf and fire on none.
        if nodes > MAX_NODES:
            raise InvalidOutputSchema(f"output_schema is too large (over {MAX_NODES} nodes)")
        if depth > MAX_DEPTH:
            raise InvalidOutputSchema(f"output_schema is nested deeper than {MAX_DEPTH} levels")
        if isinstance(node, dict):
            for keyword in _REFERENCE_KEYWORDS:
                target = node.get(keyword)
                # Only a *string* value is the keyword being used; `{"properties": {"$ref":
                # {...}}}` is a property that happens to be named `$ref`, which pydantic emits
                # for a field aliased that way and which the first version of this rejected
                # with a message about remote references.
                if isinstance(target, str) and not target.startswith("#"):
                    # A remote reference asks the provider to fetch a URL of the caller's
                    # choosing while holding the caller's schema — a request Felix would be
                    # paying for and could not see. Checking `$ref` alone was not enough:
                    # `$id` is precisely the keyword that redefines what a `#` pointer resolves
                    # against. Local refs into `$defs` are what `model_json_schema()` emits and
                    # are the whole reason references are allowed at all.
                    raise InvalidOutputSchema(
                        f'output_schema may only use local "{keyword}" values, not {target!r}'
                    )
            children: list[Any] = list(node.values())
        elif isinstance(node, list):
            children = list(node)
        else:
            # Counted, not walked. A twenty-thousand-string `enum` is three dicts and twenty
            # thousand leaves, so a bound that counted only containers would let the largest
            # schemas through — and size, not nesting, is what the per-turn re-serialisation
            # costs.
            continue
        stack.extend((child, depth + 1) for child in children)
    return schema


def is_strict(schema: dict[str, Any]) -> bool:
    """Whether the schema is already in OpenAI's strict subset.

    Strict mode is the only setting under which OpenAI *guarantees* the response matches the
    schema, and it requires every object to be closed (`additionalProperties: false`) with
    every declared property listed in `required`. A schema with one optional field is outside
    it, and asking for strict anyway is a 400 rather than a looser constraint.

    Reported rather than repaired, for the reason in `validate_output_schema`: the repair
    OpenAI documents is to make optional fields `["T", "null"]` unions, which changes what the
    caller's own parser will see. The wire logs the drop to non-strict instead, so the author
    can decide.
    """
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "object":
                properties = node.get("properties")
                if not isinstance(properties, dict):
                    return False
                if node.get("additionalProperties") is not False:
                    return False
                required = node.get("required")
                if not isinstance(required, list) or set(required) != set(properties):
                    return False
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return True


__all__ = [
    "MAX_BYTES",
    "MAX_DEPTH",
    "MAX_NODES",
    "InvalidOutputSchema",
    "is_strict",
    "validate_output_schema",
]
