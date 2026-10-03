"""Model calls the skill quality loop makes outside a request: the improvement and the evaluation.

Both run in the worker, where there is no request to meter against. `tenant_job` installs a
`RequestContext` for the skill's tenant, as `memory/consolidation.py` does for its pass, so every
call is billed to that tenant through `record_model_usage` -- the turn's own meter -- and the
stores it touches bind that tenant under RLS. `ask` is one metered turn.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from felix.config import Settings


def route_id(settings: Settings, configured: str) -> str:
    """The `FELIX_MODEL_ROUTES` id a setting names, or the default route when it is empty."""
    return (configured or "").strip() or settings.default_model_id


def build_route(settings: Settings, configured: str) -> tuple[Any, str]:
    """The client for a configured route (through `build_model`, so it is traced and carries
    its price) and the route id it resolved to, which the job records."""
    from felix.manifests.schema import ModelSpec
    from felix.patterns.model import build_model

    rid = route_id(settings, configured)
    return build_model(settings, ModelSpec(id=rid)), rid


@asynccontextmanager
async def tenant_job(settings: Settings, tenant_id: str, principal: str) -> AsyncIterator[None]:
    """Run the enclosed calls as ``principal`` in ``tenant_id``: metered to that tenant."""
    from felix.context import AuthContext, RequestContext, async_run_with_context

    auth = AuthContext(tenant_id=tenant_id, principal_sub=principal, anonymous=False)
    async with async_run_with_context(RequestContext(settings=settings, auth=auth)):
        yield


async def ask(model: Any, *, system: str, user: str, kind: str) -> str:
    """One turn with no tools, metered before the answer is read: a reply that is thrown away
    was still paid for."""
    from felix.patterns.model import ModelChatOptions, record_model_usage
    from felix.patterns.types import ChatMessage

    messages = [ChatMessage(role="system", content=system)] if system else []
    messages.append(ChatMessage(role="user", content=user))
    result = await model.chat(messages, [], ModelChatOptions(isolate_cache=True))
    record_model_usage(result, model, meta={"kind": kind})
    return str(result.message.content or "")


__all__ = ["ask", "build_route", "route_id", "tenant_job"]
