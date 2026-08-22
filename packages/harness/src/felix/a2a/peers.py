"""Outbound A2A peer tools — message/send to peer agent URLs."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from felix.manifests.schema import A2APeerRef
from felix.security.ssrf import assert_safe_outbound_url
from felix.tools.types import Tool, ToolInvocationCtx, define_tool

logger = logging.getLogger("felix.a2a.peers")


class PeerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, description="Message to send to the peer agent.")
    manifest: str | None = Field(default=None, description="Optional peer manifest name override.")


def _auth_headers(auth: str) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if not auth:
        return headers
    if auth.lower().startswith("bearer ") or auth.lower().startswith("basic "):
        headers["authorization"] = auth
    else:
        headers["authorization"] = f"Bearer {auth}"
    return headers


def _extract_peer_text(body: dict[str, Any]) -> str:
    if body.get("error"):
        err = body["error"]
        return f"[peer_error] {err.get('message') or err}"
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    if not isinstance(result, dict):
        return str(body)
    # A2A task status message
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    message = status.get("message") if isinstance(status, dict) else None
    if isinstance(message, dict):
        parts = message.get("parts")
        if isinstance(parts, list):
            texts = [str(p.get("text") or p.get("content") or "") for p in parts if isinstance(p, dict)]
            joined = "\n".join(t for t in texts if t)
            if joined:
                return joined
    if result.get("final"):
        final = result["final"]
        if isinstance(final, dict):
            return str(final.get("content") or final)
        return str(final)
    if result.get("content"):
        return str(result["content"])
    return str(result)


def make_peer_tool(ref: A2APeerRef, *, allow_http: bool = False) -> Tool:
    assert_safe_outbound_url(ref.url, allow_http=allow_http)
    url = ref.url.rstrip("/")
    # Accept bare host or /a2a path.
    endpoint = url if url.endswith("/a2a") else f"{url}/a2a"

    async def handler(args: PeerArgs, _ctx: ToolInvocationCtx | None = None) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": args.message}],
                },
            },
        }
        if args.manifest:
            payload["params"]["manifest"] = args.manifest  # type: ignore[index]
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            resp = await client.post(endpoint, json=payload, headers=_auth_headers(ref.auth))
            resp.raise_for_status()
            body = resp.json()
        if not isinstance(body, dict):
            return str(body)
        return _extract_peer_text(body)

    return define_tool(
        name=f"peer__{ref.name}",
        description=f"Send a message to peer agent '{ref.name}' at {ref.url}.",
        args=PeerArgs,
        handler=handler,
        is_peer=True,
        source=f"peer:{ref.name}",
        transport="a2a",
    )


def tools_from_peers(
    refs: list[A2APeerRef],
    *,
    allow_http: bool = False,
) -> list[Tool]:
    out: list[Tool] = []
    for ref in refs:
        try:
            out.append(make_peer_tool(ref, allow_http=allow_http))
        except Exception:
            logger.warning("failed to bind peer tool %s", ref.name, exc_info=True)
    return out


__all__ = ["make_peer_tool", "tools_from_peers"]
