"""Session JSONL export for eval artifacts and sharing."""

from __future__ import annotations

import json
from typing import Any

from felix.session.types import SessionEvent


def event_to_export_row(ev: SessionEvent) -> dict[str, Any]:
    md = dict(ev.metadata or {})
    row: dict[str, Any] = {
        "seq": ev.seq,
        "ts": ev.ts,
        "kind": ev.kind,
        "role": ev.role,
        "content": ev.content,
        "event_id": md.get("event_id"),
        "parent_id": md.get("parent_id"),
    }
    if ev.tool_call_id:
        row["tool_call_id"] = ev.tool_call_id
    if ev.name:
        row["name"] = ev.name
    if ev.tool_calls:
        row["tool_calls"] = ev.tool_calls
    # Drop tree linkage duplicates already promoted.
    rest = {k: v for k, v in md.items() if k not in {"event_id", "parent_id"}}
    if rest:
        row["metadata"] = rest
    return row


def events_to_jsonl(events: list[SessionEvent]) -> str:
    lines = [json.dumps(event_to_export_row(e), default=str) for e in events]
    return "\n".join(lines) + ("\n" if lines else "")


__all__ = ["event_to_export_row", "events_to_jsonl"]
