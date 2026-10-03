"""What the skill quality loop's worker jobs share: their model calls, their lease, their deadline.

Both jobs run in the worker, where there is no request to meter against. `tenant_job` installs a
`RequestContext` for the skill's tenant, as `memory/consolidation.py` does for its pass, so every
call is billed to that tenant through `record_model_usage` -- the turn's own meter -- and the
stores it touches bind that tenant under RLS. `ask` is one metered turn, capped at the route's
`max_tokens`.

`Lease` is how a job keeps its claim: it heartbeats between model calls, and when the heartbeat
does not land -- the claim lapsed and another worker took the job -- the job stops at once rather
than spend more model calls on work whose result it can no longer record. `deadline` bounds a job
by wall clock (`FELIX_SKILL_JOB_DEADLINE_SECONDS`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from felix.config import Settings

if TYPE_CHECKING:
    from felix_ai.types import ModelClient

now_ms = lambda: int(time.time() * 1000)


class LeaseLost(Exception):
    """The job's claim no longer holds: another worker took it, or it was finished elsewhere."""


class DeadlineExceeded(Exception):
    """The job ran past `FELIX_SKILL_JOB_DEADLINE_SECONDS`."""


class Lease:
    """A claimed job's hold on its row. ``beat`` heartbeats and says whether the claim held."""

    def __init__(self, beat: Callable[[int], Awaitable[bool]]) -> None:
        self._beat = beat

    async def keep(self) -> None:
        """Heartbeat, or raise `LeaseLost`."""
        if not await self._beat(now_ms()):
            raise LeaseLost("the claim was taken over")


def route_id(settings: Settings, configured: str) -> str:
    """The `FELIX_MODEL_ROUTES` id a setting names, or the default route when it is empty."""
    return (configured or "").strip() or settings.default_model_id


def build_route(settings: Settings, configured: str, *, max_tokens: int) -> tuple[ModelClient, str]:
    """The client for a configured route (through `build_model`, so it is traced and carries its
    price), capped at ``max_tokens`` per call, and the route id it resolved to."""
    from felix.manifests.schema import ModelSpec
    from felix.patterns.model import build_model

    rid = route_id(settings, configured)
    return build_model(settings, ModelSpec(id=rid, max_tokens=max_tokens)), rid


@asynccontextmanager
async def tenant_job(settings: Settings, tenant_id: str, principal: str) -> AsyncIterator[None]:
    """Run the enclosed calls as ``principal`` in ``tenant_id``: metered to that tenant, and cut
    off at the job deadline (`DeadlineExceeded`)."""
    from felix.context import AuthContext, RequestContext, async_run_with_context

    auth = AuthContext(tenant_id=tenant_id, principal_sub=principal, anonymous=False)
    try:
        async with asyncio.timeout(settings.skill_job_deadline_seconds):
            async with async_run_with_context(RequestContext(settings=settings, auth=auth)):
                yield
    except TimeoutError as exc:
        raise DeadlineExceeded(
            f"deadline_exceeded: the job ran past {settings.skill_job_deadline_seconds}s"
        ) from exc


async def ask(model: ModelClient, *, system: str, user: str, kind: str) -> str:
    """One turn with no tools, metered before the answer is read: a reply that is thrown away
    was still paid for."""
    from felix.patterns.model import ModelChatOptions, record_model_usage
    from felix.patterns.types import ChatMessage

    messages = [ChatMessage(role="system", content=system)] if system else []
    messages.append(ChatMessage(role="user", content=user))
    result = await model.chat(messages, [], ModelChatOptions(isolate_cache=True))
    record_model_usage(result, model, meta={"kind": kind})
    return str(result.message.content or "")


__all__ = ["DeadlineExceeded", "Lease", "LeaseLost", "ask", "build_route", "route_id", "tenant_job"]
