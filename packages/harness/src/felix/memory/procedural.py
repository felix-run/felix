"""Procedural memory — retrieve how-to snippets and optionally record new ones."""

from __future__ import annotations

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
    return (scored or ranked)[:top_k]


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
            manifest_id=manifest_id,
            kind=PROCEDURE_KIND,
            limit=max(spec.top_k * 10, 20),
        )
    except Exception:
        logger.debug("procedural list_active failed", exc_info=True)
        return ""
    picked = rank_procedures(rows, query, spec.top_k, embedding_model=spec.embedding_model)
    if not picked:
        return ""
    lines = [f"- {r['content']}" for r in picked if r.get("content")]
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
            content=content,
            kind=PROCEDURE_KIND,
            manifest_id=manifest_id,
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
