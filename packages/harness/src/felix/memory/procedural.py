"""Procedural memory — retrieve how-to snippets and optionally record new ones.

Agent-scoped whatever `spec.memory.scope` says: a procedure is how an agent works, not a fact
about the person it works for, so `remember_procedure` writes the agent's own pool and retrieval
reads it (`memory.store.MemoryPool.agent`) -- for every caller, a person-less one included. Read
through that pool rather than by manifest, so a `procedure` a person's agent stored in *their*
pool through `remember` is never shown to another.

That leaves one channel between people, on purpose and documented in `deploy/GOVERNANCE.md`: a
procedure an agent learns while working for one person is retrieved for every person that agent
works for. It is agent-written, so it never reaches a person-scope agent's *fact* reads (the
prelude, `recall`, `list_memories`), which see only operator rows of the agent pool.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from felix.config import Settings
from felix.manifests.schema import ProceduralSpec
from felix.memory import store as memory_store
from felix.patterns.types import ChatMessage
from felix.tools.types import Tool, ToolInvocationCtx, define_tool

logger = logging.getLogger("felix.memory.procedural")

_WORD = re.compile(r"[a-z0-9]+")
PROCEDURE_KIND = "procedure"


class RememberProcedureArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, description="Short name for the procedure.")
    body: str = Field(min_length=1, description="Steps / how-to text to remember.")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def query_from_user_messages(messages: list[ChatMessage]) -> str:
    return " ".join(m.content for m in messages if m.role == "user" and m.content)


def rank_procedures(
    rows: list[dict[str, Any]],
    query: str,
    top_k: int,
    *,
    embedding_model: str = "",
) -> list[dict[str, Any]]:
    if embedding_model:
        try:
            from felix.embeddings import rank_indices_by_query

            blobs = [str(r.get("content") or "") for r in rows]
            order = rank_indices_by_query(query, blobs, embedding_model)
            if order is not None:
                return [rows[i] for i in order[:top_k]]
        except Exception:
            logger.debug("procedural embedding rank failed", exc_info=True)
    q = _tokens(query)
    if not q:
        return rows[:top_k]

    def score(row: dict[str, Any]) -> int:
        return len(q & _tokens(str(row.get("content") or "")))

    ranked = sorted(rows, key=score, reverse=True)
    scored = [r for r in ranked if score(r) > 0]
    # No fallback to `ranked`: returning the top-k arbitrary rows when nothing matched
    # injected irrelevant, possibly stale procedures as instructions on every turn.
    return scored[:top_k]


async def retrieve_procedures(
    settings: Settings,
    tenant_id: str,
    *,
    manifest_id: str,
    query: str,
    spec: ProceduralSpec,
) -> str:
    if not spec.enabled:
        return ""
    try:
        rows = await memory_store.list_active(
            settings,
            tenant_id,
            scope=memory_store.MemoryPool.agent(manifest_id),
            kind=PROCEDURE_KIND,
            limit=max(spec.top_k * 10, 20),
        )
    except Exception:
        logger.debug("procedural list_active failed", exc_info=True)
        return ""
    # Ranking is keyword overlap until an embedding model is configured, at which point
    # it encodes the query and every candidate — CPU-bound, and the first call for a
    # model loads it from disk. `retrieve_procedures` is awaited while a turn is being
    # served, so that work goes to a thread rather than stalling the whole worker.
    # Unconditionally, unlike tool retrieval: this runs once per turn rather than four
    # times per loop step, so the hop is free and there is no branch here to drift from
    # the condition inside `rank_procedures`.
    picked = await asyncio.to_thread(
        rank_procedures, rows, query, spec.top_k, embedding_model=spec.embedding_model
    )
    if not picked:
        return ""
    # Escaped like the recall prelude. `remember_procedure` content is
    # f"{title}: {body}", fully attacker-chosen through an injected tool call, and it
    # was rendered raw into the same prompt the prelude is concatenated into -- the
    # sibling surface the prelude's own escaping argument applies to most directly.
    from felix.memory.capture import escape_markup

    lines = [f"- {escape_markup(str(r['content']))}" for r in picked if r.get("content")]
    if not lines:
        return ""
    return "[known procedures]\n" + "\n".join(lines)


def make_remember_procedure_tool(
    *,
    settings: Settings,
    tenant_id: str,
    manifest_id: str,
) -> Tool:
    async def handler(args: RememberProcedureArgs, _ctx: ToolInvocationCtx | None = None) -> str:
        content = f"{args.title}: {args.body}"
        row = await memory_store.put_memory(
            settings,
            tenant_id,
            pool=memory_store.MemoryPool.agent(manifest_id),
            content=content,
            kind=PROCEDURE_KIND,
            metadata={"title": args.title, "source": "remember_procedure"},
        )
        return f"remembered_procedure:{row['id']}"

    return define_tool(
        name="remember_procedure",
        description=("Store a reusable how-to so it can be retrieved on later turns (procedural memory)."),
        args=RememberProcedureArgs,
        handler=handler,
        source="memory",
    )


__all__ = [
    "PROCEDURE_KIND",
    "make_remember_procedure_tool",
    "query_from_user_messages",
    "rank_procedures",
    "retrieve_procedures",
]
