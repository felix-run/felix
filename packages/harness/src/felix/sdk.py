"""Thin Python client for Felix HTTP surfaces."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FelixClient:
    """Minimal async client: prompt, stream, steer, follow_up, fork, rewind, abort, …"""

    base_url: str
    api_key: str | None = None
    tenant_header: str | None = None
    timeout: float = 120.0
    _model: str | None = None
    _thread_id: str | None = None
    _manifest: str = "quick"
    _listeners: list[Callable[[dict[str, Any]], Any]] = field(default_factory=list)

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        if self.tenant_header:
            headers["x-felix-tenant"] = self.tenant_header
        return headers

    def set_model(self, model_id: str) -> None:
        self._model = model_id

    def set_manifest(self, manifest: str) -> None:
        self._manifest = manifest

    def set_thread(self, thread_id: str | None) -> None:
        self._thread_id = thread_id

    def subscribe(self, listener: Callable[[dict[str, Any]], Any]) -> Callable[[], None]:
        self._listeners.append(listener)

        def _unsub() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsub

    def _emit(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            listener(event)

    async def prompt(
        self,
        text: str,
        *,
        manifest: str | None = None,
        thread_id: str | None = None,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body = {
            "manifest": manifest or self._manifest,
            "messages": messages or [{"role": "user", "content": text}],
            "thread_id": thread_id if thread_id is not None else self._thread_id,
            "model": model or self._model,
        }
        body = {k: v for k, v in body.items() if v is not None}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            self._emit({"event": "prompt_result", "data": data})
            return data

    async def stream(
        self,
        text: str,
        *,
        manifest: str | None = None,
        thread_id: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        body = {
            "manifest": manifest or self._manifest,
            "messages": [{"role": "user", "content": text}],
            "thread_id": thread_id if thread_id is not None else self._thread_id,
            "model": model or self._model,
        }
        body = {k: v for k, v in body.items() if v is not None}
        async with (
            httpx.AsyncClient(timeout=self.timeout) as client,
            client.stream(
                "POST",
                f"{self.base_url.rstrip('/')}/chat/stream",
                headers=self._headers(),
                json=body,
            ) as resp,
        ):
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                self._emit(event)
                yield event

    async def steer(self, text: str, *, thread_id: str | None = None) -> dict[str, Any]:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for steer")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/steer",
                headers=self._headers(),
                json={"thread_id": tid, "text": text, "kind": "steer"},
            )
            resp.raise_for_status()
            return resp.json()

    async def follow_up(self, text: str, *, thread_id: str | None = None) -> dict[str, Any]:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for follow_up")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/steer",
                headers=self._headers(),
                json={"thread_id": tid, "text": text, "kind": "follow_up"},
            )
            resp.raise_for_status()
            return resp.json()

    async def fork(
        self,
        new_thread_id: str,
        *,
        thread_id: str | None = None,
        from_event_id: str | None = None,
    ) -> dict[str, Any]:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for fork")
        body: dict[str, Any] = {
            "thread_id": tid,
            "new_thread_id": new_thread_id,
        }
        if from_event_id:
            body["from_event_id"] = from_event_id
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/fork",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            return resp.json()

    async def rewind(self, event_id: str, *, thread_id: str | None = None) -> dict[str, Any]:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for rewind")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/rewind",
                headers=self._headers(),
                json={"thread_id": tid, "event_id": event_id},
            )
            resp.raise_for_status()
            return resp.json()

    async def abort(self, *, thread_id: str | None = None) -> dict[str, Any]:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for abort")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/abort",
                headers=self._headers(),
                json={"thread_id": tid},
            )
            resp.raise_for_status()
            return resp.json()

    async def continue_run(
        self,
        *,
        thread_id: str | None = None,
        manifest: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for continue")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/continue",
                headers=self._headers(),
                json={
                    "thread_id": tid,
                    "manifest": manifest or self._manifest,
                    "model": model or self._model,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def set_thinking(
        self,
        thinking_level: str,
        *,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for set_thinking")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/thinking",
                headers=self._headers(),
                json={"thread_id": tid, "thinking_level": thinking_level},
            )
            resp.raise_for_status()
            return resp.json()

    async def compact(
        self,
        *,
        thread_id: str | None = None,
        manifest: str | None = None,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for compact")
        body: dict[str, Any] = {
            "thread_id": tid,
            "manifest": manifest or self._manifest,
        }
        if instructions:
            body["instructions"] = instructions
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/compact",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            return resp.json()

    async def snapshot(self, *, thread_id: str | None = None) -> dict[str, Any]:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for snapshot")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url.rstrip('/')}/chat/sessions/{tid}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def list_sessions(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url.rstrip('/')}/chat/sessions",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def append_custom(
        self,
        content: str,
        *,
        thread_id: str | None = None,
        in_context: bool = False,
        metadata: dict[str, Any] | None = None,
        role: str = "system",
    ) -> dict[str, Any]:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for append_custom")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/sessions/custom",
                headers=self._headers(),
                json={
                    "thread_id": tid,
                    "content": content,
                    "in_context": in_context,
                    "metadata": metadata or {},
                    "role": role,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def export_session(self, *, thread_id: str | None = None) -> str:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for export_session")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url.rstrip('/')}/chat/sessions/{tid}/export",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.text

    async def acquire_lease(
        self,
        holder_id: str,
        *,
        thread_id: str | None = None,
        mode: str = "exclusive",
        ttl_seconds: float = 300.0,
        token: str | None = None,
    ) -> dict[str, Any]:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for acquire_lease")
        body: dict[str, Any] = {
            "thread_id": tid,
            "holder_id": holder_id,
            "mode": mode,
            "ttl_seconds": ttl_seconds,
        }
        if token:
            body["token"] = token
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/sessions/lease",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            return resp.json()

    async def release_lease(
        self,
        *,
        thread_id: str | None = None,
        holder_id: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for release_lease")
        body: dict[str, Any] = {"thread_id": tid}
        if holder_id:
            body["holder_id"] = holder_id
        if token:
            body["token"] = token
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/sessions/lease/release",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            return resp.json()

    async def resolve_ui(
        self,
        request_id: str,
        *,
        value: Any = None,
        cancelled: bool = False,
        note: str = "",
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/ui",
                headers=self._headers(),
                json={
                    "request_id": request_id,
                    "value": value,
                    "cancelled": cancelled,
                    "note": note,
                },
            )
            resp.raise_for_status()
            return resp.json()


__all__ = ["FelixClient"]
