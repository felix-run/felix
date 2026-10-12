"""A fake Slack at the transport, and Slack's request signing, for the channels tests.

Shared by `tests/unit/test_channels.py` and `tests/e2e/test_slack_channel.py`. `FakeSlack.client`
replaces `felix_channels.slack.slack_http_client`, so every `chat.postMessage` the plugin makes is
recorded with its full URL, its bearer and its body; `fail` makes Slack answer 500.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


def slack_signature(body: bytes, secret: str, ts: int) -> str:
    """Slack's `X-Slack-Signature` for `body` at `ts`, as Slack computes it."""
    return "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()


def signed_headers(body: bytes, secret: str, *, ts: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    return {
        "content-type": "application/json",
        "x-slack-request-timestamp": str(ts),
        "x-slack-signature": slack_signature(body, secret, ts),
    }


@dataclass
class FakeSlack:
    posts: list[dict[str, Any]] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    fail: bool = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        self.tokens.append(request.headers.get("authorization", ""))
        self.posts.append(json.loads(request.content))
        if self.fail:
            return httpx.Response(500, json={"ok": False, "error": "internal_error"})
        return httpx.Response(200, json={"ok": True, "ts": "1700000000.000200"})

    def client(self, settings: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


__all__ = ["POST_MESSAGE_URL", "FakeSlack", "signed_headers", "slack_signature"]
