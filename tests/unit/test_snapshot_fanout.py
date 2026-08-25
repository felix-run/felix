"""Independent reads on the reattach path run together, not one after another.

`_build_thread_snapshot` issued five sequential awaits against four different stores —
`get_events`, `get_thread_meta`, `load_leaf`, `peek_steer_count`, `lease_status` — none
of which depends on another. It runs on `GET /chat/sessions/{id}`, on both lease
endpoints, and on every cold SSE reconnect, so it sits exactly where latency is most
visible in the product.

Measured against a real Postgres (the `memory://` twin has no round trips, so it hides
this entirely):

    serial    p50 2.66 ms   p95 3.48 ms
    gathered  p50 1.38 ms   p95 1.56 ms

and that is a *local* database. Serial pays five round trips where gathered pays about
one, so the gap widens with network latency rather than narrowing.

Concurrency is asserted with a barrier rather than a stopwatch: a timing test passes on
a fast machine for the wrong reason, and fails on a slow one for another wrong reason.
If the reads run in series the barrier is never satisfied and the test times out.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

CONCURRENT = 5


@pytest.mark.asyncio
async def test_the_snapshot_reads_run_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix import steer as steer_mod
    from felix.session import lease as lease_mod
    from felix.session import thread_state as ts_mod
    from felix_api.routes import chat as chat_mod

    barrier = asyncio.Barrier(CONCURRENT)

    async def _rendezvous(result: Any) -> Any:
        """Block until every other read has also started."""
        await asyncio.wait_for(barrier.wait(), timeout=2.0)
        return result

    class _Session:
        async def get_events(self) -> list[Any]:
            return await _rendezvous([])

    class _Store:
        def open(self, _thread: str) -> _Session:
            return _Session()

    monkeypatch.setattr(chat_mod, "get_session_store", lambda *a, **k: _Store())
    monkeypatch.setattr(ts_mod, "get_thread_meta", lambda **k: _rendezvous({}))
    monkeypatch.setattr(ts_mod, "load_leaf", lambda **k: _rendezvous(None))
    monkeypatch.setattr(steer_mod, "peek_steer_count", lambda *a, **k: _rendezvous(0))
    monkeypatch.setattr(lease_mod, "lease_status", lambda *a, **k: _rendezvous({}))

    snapshot = await chat_mod._build_thread_snapshot(settings=object(), tenant_id="t", thread="t:thread")
    assert snapshot["id"] == "t:thread"


@pytest.mark.asyncio
async def test_the_snapshot_still_carries_what_each_read_provides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fanning out must not shuffle which result feeds which field — the five reads
    return different shapes and `gather` returns them positionally."""
    from felix import steer as steer_mod
    from felix.session import lease as lease_mod
    from felix.session import thread_state as ts_mod
    from felix_api.routes import chat as chat_mod

    class _Session:
        async def get_events(self) -> list[Any]:
            return []

    class _Store:
        def open(self, _thread: str) -> _Session:
            return _Session()

    async def _meta(**_k: Any) -> dict[str, Any]:
        return {"session_name": "named", "phase": "turn", "revision": 7}

    async def _leaf(**_k: Any) -> str:
        return "leaf-42"

    async def _steer(*_a: Any, **_k: Any) -> int:
        return 3

    async def _lease(*_a: Any, **_k: Any) -> dict[str, bool]:
        return {"attached": True, "locked": False}

    monkeypatch.setattr(chat_mod, "get_session_store", lambda *a, **k: _Store())
    monkeypatch.setattr(ts_mod, "get_thread_meta", _meta)
    monkeypatch.setattr(ts_mod, "load_leaf", _leaf)
    monkeypatch.setattr(steer_mod, "peek_steer_count", _steer)
    monkeypatch.setattr(lease_mod, "lease_status", _lease)

    snap = await chat_mod._build_thread_snapshot(settings=object(), tenant_id="t", thread="t:thread")
    assert snap["name"] == "named"
    assert snap["phase"] == "turn"
    assert snap["revision"] == 7
    assert snap["leafId"] == "leaf-42"
    assert snap["queuedSteerCount"] == 3
    assert snap["attached"] is True
    assert snap["locked"] is False


# --- /v1/models ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_unresolvable_manifest_does_not_empty_the_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`return_exceptions=True` is what preserves the old per-manifest `except`.

    Without it the first failure propagates out of `gather` and the whole listing 500s,
    where before it degraded to a name-only entry for the one bad manifest.
    """
    from felix_api.routes import openai_compat as oc

    names = ["quick", "broken", "deep"]
    monkeypatch.setattr(oc, "list_bundled", lambda: names)

    async def _resolve(_settings: Any, _tenant: str, name: str, **_k: Any) -> Any:
        if name == "broken":
            raise RuntimeError("manifest is malformed")

        class _Resolved:
            manifest = None

        return _Resolved()

    monkeypatch.setattr(oc, "resolve_tenant_manifest", _resolve)
    monkeypatch.setattr(oc, "_auth", lambda _r: type("A", (), {"tenant_id": "t"})())

    class _App:
        state = type("S", (), {"settings": object()})()

    result = await oc.list_models(type("R", (), {"app": _App()})())
    listed = [row["id"] for row in result["data"]]
    assert listed == names, f"expected every manifest listed in order, got {listed}"


@pytest.mark.asyncio
async def test_the_models_listing_keeps_manifest_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gather` preserves positional order, but the pairing back onto names is the part
    that could silently drift — `zip(..., strict=True)` makes a length mismatch loud."""
    from felix_api.routes import openai_compat as oc

    names = [f"m{i}" for i in range(8)]
    monkeypatch.setattr(oc, "list_bundled", lambda: names)

    async def _resolve(_settings: Any, _tenant: str, name: str, **_k: Any) -> Any:
        # Reverse-ordered delays: a serial implementation would return in call order
        # anyway, so this only proves the pairing survives out-of-order completion.
        await asyncio.sleep((8 - int(name[1:])) * 0.001)

        class _Resolved:
            manifest = None

        return _Resolved()

    monkeypatch.setattr(oc, "resolve_tenant_manifest", _resolve)
    monkeypatch.setattr(oc, "_auth", lambda _r: type("A", (), {"tenant_id": "t"})())

    class _App:
        state = type("S", (), {"settings": object()})()

    result = await oc.list_models(type("R", (), {"app": _App()})())
    assert [row["id"] for row in result["data"]] == names
