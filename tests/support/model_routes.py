"""Every manifest field that names a model route, for the provider-pin and cost-cap tests.

One table, so both checks are tested against the same inventory, and `route_naming_fields()`
finds every schema field that looks like a route so a new one cannot be left out of both. Each
entry says how the harness builds that client: `spec_model` routes are `spec.model` with the id
swapped and carry `spec.model.price`; `catalog` routes build a bare `ModelSpec(id=...)` and are
billed at catalog rates whatever the override says.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel

Kind = Literal["spec_model", "catalog"]

# Field path (as `route_naming_fields` spells it) -> (how it is priced, spec fragment naming route `r`).
ROUTE_FIELDS: dict[str, tuple[Kind, Callable[[str], dict[str, Any]]]] = {
    "model.fallbacks": ("spec_model", lambda r: {"model": {"fallbacks": [r]}}),
    "model.vision_model": ("spec_model", lambda r: {"model": {"vision_model": r}}),
    "model.confidence_escalation.escalate_to": (
        "spec_model",
        lambda r: {"model": {"confidence_escalation": {"enabled": True, "escalate_to": r}}},
    ),
    "plan_execute.planner_model": ("spec_model", lambda r: {"plan_execute": {"planner_model": r}}),
    "plan_execute.executor_model": ("spec_model", lambda r: {"plan_execute": {"executor_model": r}}),
    "heartbeat.model": (
        "spec_model",
        lambda r: {"notify": {}, "heartbeat": {"checklist": "check", "model": r}},
    ),
    "memory.capture.model": ("spec_model", lambda r: {"memory": {"capture": {"enabled": True, "model": r}}}),
    "reflect.verifier_model": ("catalog", lambda r: {"reflect": {"verifier_model": r}}),
    "guardrails.judges.model": (
        "catalog",
        lambda r: {"guardrails": {"judges": [{"name": "j", "criteria": "c", "model": r}]}},
    ),
    "content_screening.model": ("catalog", lambda r: {"content_screening": {"enabled": True, "model": r}}),
    "content_screening.image_model": (
        "catalog",
        lambda r: {"content_screening": {"enabled": True, "image_model": r}},
    ),
    "memory.consolidate.model": (
        "catalog",
        lambda r: {"memory": {"consolidate": {"enabled": True, "model": r}}},
    ),
    "http_tools.extract_model": (
        "catalog",
        lambda r: {"http_tools": [{"name": "web_fetch", "allow_any_host": True, "extract_model": r}]},
    ),
}

# Matched by the name pattern, and not a `FELIX_MODEL_ROUTES` id the run sends content to.
EXEMPT = {
    "content_screening.model_tools": "tool-name globs, not a route",
    "tools_retrieval.model": "a local embedding model",
    "procedural_memory.embedding_model": "a local embedding model",
}


def with_fragment(spec: dict[str, Any], fragment: dict[str, Any]) -> dict[str, Any]:
    """`spec` with `fragment` merged in, one level deep, so `model: {...}` keeps its `id`."""
    out = dict(spec)
    for key, value in fragment.items():
        out[key] = (
            {**out[key], **value} if isinstance(value, dict) and isinstance(out.get(key), dict) else value
        )
    return out


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    if typing.get_origin(annotation) is None:
        return [annotation] if isinstance(annotation, type) and issubclass(annotation, BaseModel) else []
    return [m for arg in typing.get_args(annotation) for m in _models_in(arg)]


def route_naming_fields() -> set[str]:
    """Every leaf of the manifest spec whose name says it may hold a model route."""
    from felix.manifests.schema import Manifest

    def walk(cls: type[BaseModel], prefix: str, seen: frozenset[type]) -> set[str]:
        found: set[str] = set()
        for name, field in cls.model_fields.items():
            nested = _models_in(field.annotation)
            for sub in nested:
                if sub not in seen:
                    found |= walk(sub, f"{prefix}{name}.", seen | {sub})
            if not nested and re.search(r"model|escalate_to|fallbacks", name):
                found.add(f"{prefix}{name}")
        return found

    return walk(Manifest.model_fields["spec"].annotation, "", frozenset())
