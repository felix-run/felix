"""Parse an SSE response body the way a client reads it."""

from __future__ import annotations

import json
from typing import Any


def sse_blocks(body: str) -> list[tuple[int | None, dict[str, Any]]]:
    """Every data frame as `(id, payload)`, keeping the `id:` the frame carried."""
    out: list[tuple[int | None, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        event_id: int | None = None
        payload: dict[str, Any] | None = None
        for line in block.splitlines():
            if line.startswith("id: "):
                event_id = int(line[4:])
            elif line.startswith("data: ") and line[6:] != "[DONE]":
                payload = json.loads(line[6:])
        if payload is not None:
            out.append((event_id, payload))
    return out


def sse_event_names(body: str) -> list[str]:
    return [str(p.get("event")) for _, p in sse_blocks(body)]


def sse_payloads(body: str) -> list[dict[str, Any]]:
    """Every `data:` payload in order, decoded, without the `[DONE]` terminator."""
    return [payload for _, payload in sse_blocks(body)]
