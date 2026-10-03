"""Shared fixtures for the skill quality loop's tests: a scripted model per route, and a library.

`ScriptedRoutes` registers the scripted provider (`felix_ai.providers.scripted`) under one name
and gives each logical route its own queue of turns, so a test scripts "the improver says this,
the judge scores that" without caring how the calls interleave. Every call's messages are kept,
which is the only evidence of what a model was *shown* -- the fencing assertions read them.

A route that runs out of turns raises instead of inventing one, as `tests/e2e/conftest.py`
does: an invented `ok` would read as a plausible answer and a test would pass on it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from felix.config import Settings
from felix.skills import library
from felix.skills.format import serialize_skill_md
from felix_ai.providers.scripted import ScriptedClient, ScriptedTurn

TENANT = "acme"
NAME = "invoice-triage"
DESCRIPTION = "Route incoming invoices to the right queue."
BODY = """# Invoice triage

Use this when an invoice arrives.

## Steps

1. Read the vendor and the amount.
2. Route amounts over the limit to finance.
"""
# What the routes are called. Distinct names so a test can tell which route a call took.
IMPROVER, ANSWERER, JUDGE = "skill-improver-route", "skill-answer-route", "skill-judge-route"


def skill_md(name: str = NAME, body: str = BODY, description: str = DESCRIPTION) -> str:
    return serialize_skill_md({"name": name, "description": description}, f"\n{body}")


def bundle(name: str = NAME, body: str = BODY, **extra: str) -> dict[str, str]:
    return {"SKILL.md": skill_md(name, body), **extra}


def judged(score: float, reason: str = "fine") -> ScriptedTurn:
    """A judge turn in the shape `eval.compare.llm_judge_score` parses."""
    return ScriptedTurn(content=json.dumps({"score": score, "reason": reason}))


@dataclass
class ScriptedRoutes:
    queues: dict[str, list[ScriptedTurn]] = field(default_factory=dict)
    #: `(route, messages)` for every call, in order.
    calls: list[tuple[str, list[Any]]] = field(default_factory=list)

    def push(self, route: str, *turns: ScriptedTurn | str) -> None:
        self.queues.setdefault(route, []).extend(
            t if isinstance(t, ScriptedTurn) else ScriptedTurn(content=t) for t in turns
        )

    def prompts(self, route: str) -> list[list[Any]]:
        return [m for r, m in self.calls if r == route]

    def texts(self, route: str) -> list[str]:
        """Every message the route was shown, each call's messages joined."""
        return ["\n".join(str(m.content) for m in messages) for messages in self.prompts(route)]

    def factory(self) -> Any:
        def build(model_id: str, route: Any, spec: Any, settings: Any) -> ScriptedClient:
            queue = self.queues.setdefault(model_id, [])
            client = ScriptedClient(model_id=model_id, route=route, script=queue)
            inner, recorded = client.chat, self.calls

            async def chat(messages: Any, tools: Any, opts: Any = None) -> Any:
                if not queue:
                    raise AssertionError(f"route {model_id} ran out of scripted turns")
                recorded.append((model_id, list(messages)))
                return await inner(messages, tools, opts)

            client.chat = chat  # type: ignore[method-assign]
            return client

        return build


@contextmanager
def scripted_routes() -> Iterator[ScriptedRoutes]:
    """Register the scripted provider for the duration, restoring the registry after -- a
    snapshot, so a plugin's provider survives, as `tests/e2e/conftest.py` explains."""
    from felix_ai import registry

    saved = dict(registry._providers)
    routes = ScriptedRoutes()
    registry.register_model_provider("scripted", routes.factory())
    try:
        yield routes
    finally:
        registry._providers.clear()
        registry._providers.update(saved)


def routed_settings(tmp_path: Any, **kw: Any) -> Settings:
    """In-memory stores, and every quality-loop route pointed at the script."""
    routes = {r: {"provider": "scripted", "model": "claude-sonnet-5"} for r in (IMPROVER, ANSWERER, JUDGE)}
    base: dict[str, Any] = {
        "database_url": "memory://skill-quality",
        "object_store": "memory",
        "data_dir": str(tmp_path),
        "model_routes": json.dumps(routes),
        "default_model_id": ANSWERER,
        "skill_improve_model": IMPROVER,
        "skill_eval_model": ANSWERER,
        "skill_eval_judge_model": JUDGE,
    }
    return Settings(**{**base, **kw})


def object_store(settings: Settings) -> Any:
    from felix.storage import get_object_store

    return get_object_store(settings)


async def published(settings: Settings, files: dict[str, str] | None = None, tenant: str = TENANT) -> str:
    """Save ``files`` as an operator draft and publish it; the version."""
    store = object_store(settings)
    row = await library.save_draft(
        settings,
        tenant,
        files=files or bundle(),
        provenance=library.DraftProvenance(source="operator", author="ops"),
        object_store=store,
    )
    await library.publish(
        settings, tenant, str(row["name"]), str(row["version"]), by="ops", object_store=store
    )
    return str(row["version"])


__all__ = [
    "ANSWERER",
    "BODY",
    "DESCRIPTION",
    "IMPROVER",
    "JUDGE",
    "NAME",
    "TENANT",
    "ScriptedRoutes",
    "bundle",
    "judged",
    "object_store",
    "published",
    "routed_settings",
    "scripted_routes",
    "skill_md",
]
