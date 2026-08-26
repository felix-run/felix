"""Thin Python client for Felix HTTP surfaces."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

# Statuses a durable run will not move on from.
RUN_TERMINAL = frozenset({"completed", "failed", "expired", "cancelled"})

# Poll pacing for a durable run. Same shape as the server's resume stream: start
# responsive, decay while nothing is happening. A durable run exists because it may
# take a while, so a fixed rate is wrong at one end or the other.
RUN_POLL_FLOOR_SECONDS = 0.5
RUN_POLL_CEILING_SECONDS = 5.0
RUN_POLL_FACTOR = 1.5


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
        wait_s: float | None = None,
    ) -> dict[str, Any]:
        """Send a prompt and return the answer.

        A manifest with `spec.execution.mode: durable` answers 202 with a
        `resume_token` rather than a result, because the run is handed to a worker.
        This used to return that envelope as though it were the answer, so a caller
        switching a manifest to durable got `{"status": "accepted", ...}` where the
        content had been -- no error, just the wrong shape.

        `wait_s` bounds the wait. `None` means "until the run finishes or its own TTL
        expires"; `0` returns the 202 immediately, for a caller that wants to hold the
        token and poll on its own schedule.
        """
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
            if resp.status_code == 202 and data.get("resume_token") and wait_s != 0:
                self._emit({"event": "run_accepted", "data": data})
                data = await self._await_run(client, data, wait_s=wait_s)
            self._emit({"event": "prompt_result", "data": data})
            return data

    async def _await_run(
        self,
        client: httpx.AsyncClient,
        accepted: dict[str, Any],
        *,
        wait_s: float | None,
    ) -> dict[str, Any]:
        """Poll a durable run to completion, emitting progress as it goes.

        Backs off the way the server's own streams do rather than holding a fixed 1 Hz:
        a durable run is durable because it may take a while, and a client that polls a
        finished-in-30-seconds run at the same rate as an hour-long one is wrong for one
        of them.

        Three ways to stop, and the caller can tell which happened: the run reaches a
        terminal status, the run's own `expires_at` passes, or `wait_s` runs out. The
        last returns the acceptance envelope with `status: "waiting"` -- the token is
        still good, so giving up waiting is not the same as the run failing.
        """
        token = str(accepted.get("resume_token") or "")
        url = f"{self.base_url.rstrip('/')}/chat/runs/{token}"
        expires_at = float(accepted.get("expires_at") or 0) or None

        waited = 0.0
        delay = RUN_POLL_FLOOR_SECONDS
        last_status = ""
        while True:
            resp = await client.get(url, headers=self._headers())
            if resp.status_code == 404:
                return {**accepted, "status": "expired", "error": f"run_not_found:{token}"}
            resp.raise_for_status()
            run = resp.json()
            status = str(run.get("status") or "")
            if status != last_status:
                last_status = status
                self._emit({"event": "run_status", "data": run})
            if status in RUN_TERMINAL:
                return run
            if expires_at is not None and time.time() * 1000 >= expires_at:
                return {**run, "status": "expired"}
            if wait_s is not None and waited >= wait_s:
                # Not a failure. The run is still going and the token still resolves.
                return {**accepted, "status": "waiting", "waited_s": waited}
            if wait_s is not None:
                delay = min(delay, max(0.0, wait_s - waited))
            await asyncio.sleep(delay)
            waited += delay
            delay = min(delay * RUN_POLL_FACTOR, RUN_POLL_CEILING_SECONDS)

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

    async def tool_result(
        self,
        tool_call_id: str,
        content: Any = "",
        *,
        thread_id: str | None = None,
        error: bool = False,
    ) -> dict[str, Any]:
        """Answer a ``tool_request`` frame, whose tool runs on the client.

        A ``tool_request`` is a real round trip inside the model loop: the run is
        parked until the result is posted, so a client that receives one and has no
        way to reply hangs for as long as the lease allows rather than failing. Pass
        ``error=True`` to report a failed execution — that still unblocks the run,
        which silence does not.
        """
        tid = thread_id if thread_id is not None else self._thread_id
        if not tid:
            raise ValueError("thread_id required for tool_result")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/tool_result",
                headers=self._headers(),
                json={
                    "thread_id": tid,
                    "tool_call_id": tool_call_id,
                    "content": content,
                    "error": error,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def list_approvals(
        self,
        *,
        status: str | None = "pending",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Approvals awaiting a decision.

        The `approval_required` frame carries the id, but a caller only sees that
        frame while it is attached. A durable run, or one whose stream dropped, has
        no such frame to read — polling here is the only way to find what it is
        waiting on. Needs the ``approvals:read`` scope, so a 403 is a narrow key
        rather than an empty queue.
        """
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url.rstrip('/')}/approvals",
                headers=self._headers(),
                params=params,
            )
            resp.raise_for_status()
            return resp.json()

    async def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        note: str = "",
        edited_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Approve or deny a gated tool call, releasing the run that is waiting.

        The counterpart to `resolve_ui`, for the other interrupt that holds a run
        open. Without it a manifest that gates its writes could be driven from a
        browser and not from here, which is backwards: an unattended caller is the
        one that most needs to answer, because no one is watching it park.

        `edited_args` approves a *modified* call — the operator's third option
        between yes and no, and the reason the decision carries a body at all.
        Needs the ``approvals:write`` scope.
        """
        body: dict[str, Any] = {
            "decision": "approved" if approved else "denied",
            "note": note,
        }
        if edited_args is not None:
            body["edited_args"] = edited_args
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/approvals/{approval_id}/decide",
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
