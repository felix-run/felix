"""Independent reads on the reattach path run together, not one after another.

`_build_thread_snapshot` issued five sequential awaits against four different stores —
`get_events`, `get_thread_meta`, the stored leaf, `peek_steer_count`, `lease_status` — none
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

# Five reads, and since felix-run/felix#529 a sixth: the durable run in flight on the thread.
CONCURRENT = 6


@pytest.mark.asyncio
async def test_the_snapshot_reads_run_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix import steer as steer_mod
    from felix.durability import runs as runs_mod
    from felix.session import lease as lease_mod
    from felix.session import store as store_mod
    from felix.session import thread_state as ts_mod
    from felix.session.snapshot import gather_thread_snapshot

    barrier = asyncio.Barrier(CONCURRENT)

    async def _rendezvous(result: Any) -> Any:
        """Block until every other read has also started."""
        await asyncio.wait_for(barrier.wait(), timeout=2.0)
        return result

    class _Session:
        async def get_events(self) -> list[Any]:
            return await _rendezvous([])

        async def resolve_leaf(self) -> str | None:
            return await _rendezvous(None)

    class _Store:
        def open(self, _thread: str) -> _Session:
            return _Session()

    monkeypatch.setattr(store_mod, "get_session_store", lambda *a, **k: _Store())
    monkeypatch.setattr(ts_mod, "get_thread_meta", lambda **k: _rendezvous({}))
    monkeypatch.setattr(steer_mod, "peek_steer_count", lambda *a, **k: _rendezvous(0))
    monkeypatch.setattr(lease_mod, "lease_status", lambda *a, **k: _rendezvous({}))
    monkeypatch.setattr(runs_mod, "active_durable_run", lambda *a, **k: _rendezvous(None))

    snapshot = await gather_thread_snapshot(settings=object(), tenant_id="t", thread="t:thread")
    assert snapshot["id"] == "t:thread"


@pytest.mark.asyncio
async def test_the_snapshot_still_carries_what_each_read_provides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fanning out must not shuffle which result feeds which field — the five reads
    return different shapes and `gather` returns them positionally."""
    from felix import steer as steer_mod
    from felix.durability import runs as runs_mod
    from felix.session import lease as lease_mod
    from felix.session import store as store_mod
    from felix.session import thread_state as ts_mod
    from felix.session.snapshot import gather_thread_snapshot

    class _Session:
        async def get_events(self) -> list[Any]:
            return []

        async def resolve_leaf(self) -> str | None:
            return "leaf-42"

    class _Store:
        def open(self, _thread: str) -> _Session:
            return _Session()

    async def _meta(**_k: Any) -> dict[str, Any]:
        return {"session_name": "named", "phase": "turn", "revision": 7}

    async def _steer(*_a: Any, **_k: Any) -> int:
        return 3

    async def _lease(*_a: Any, **_k: Any) -> dict[str, bool]:
        return {"attached": True, "locked": False}

    monkeypatch.setattr(store_mod, "get_session_store", lambda *a, **k: _Store())
    monkeypatch.setattr(ts_mod, "get_thread_meta", _meta)
    monkeypatch.setattr(steer_mod, "peek_steer_count", _steer)
    monkeypatch.setattr(lease_mod, "lease_status", _lease)

    async def _run(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"resume_token": "fib-9", "status": "running", "expires_at": 99}

    monkeypatch.setattr(runs_mod, "active_durable_run", _run)

    snap = await gather_thread_snapshot(settings=object(), tenant_id="t", thread="t:thread")
    assert snap["name"] == "named"
    assert snap["phase"] == "turn"
    assert snap["revision"] == 7
    assert snap["leafId"] == "leaf-42"
    assert snap["queuedSteerCount"] == 3
    assert snap["attached"] is True
    assert snap["locked"] is False
    assert snap["activeRun"] == {"resumeToken": "fib-9", "status": "running", "expiresAt": 99}


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


def _models_request(settings: Any) -> Any:
    class _App:
        state = type("S", (), {"settings": settings})()

    return type("R", (), {"app": _App()})()


def _resolves_to_nothing(monkeypatch: pytest.MonkeyPatch, oc: Any) -> None:
    async def _resolve(_settings: Any, _tenant: str, _name: str, **_k: Any) -> Any:
        class _Resolved:
            manifest = None

        return _Resolved()

    monkeypatch.setattr(oc, "resolve_tenant_manifest", _resolve)
    monkeypatch.setattr(oc, "_auth", lambda _r: type("A", (), {"tenant_id": "t"})())


@pytest.mark.asyncio
async def test_the_models_listing_includes_the_tenants_published_manifests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manifest published under a new name was callable and invisible.

    `/chat` and the resolver found it, but this listing read the bundled directory only,
    so a client building its picker from `/v1/models` could never select it. Bundled
    names keep their order; a stored manifest shadowing a bundled one is listed once.
    """
    from felix.manifests import store as manifest_store
    from felix_api.routes import openai_compat as oc

    monkeypatch.setattr(oc, "list_bundled", lambda: ["cowork", "quick"])
    seen_tenants: list[str] = []

    async def _list_active(_settings: Any, tenant_id: str) -> list[dict[str, Any]]:
        seen_tenants.append(tenant_id)
        return [{"name": "zeta-inline"}, {"name": "quick"}, {"name": "cowork-inline"}]

    monkeypatch.setattr(manifest_store, "list_active", _list_active)
    _resolves_to_nothing(monkeypatch, oc)

    result = await oc.list_models(_models_request(type("S", (), {"bundled_only": False})()))
    assert [row["id"] for row in result["data"]] == ["cowork", "quick", "cowork-inline", "zeta-inline"]
    assert seen_tenants == ["t"], "the store must be read for the caller's tenant only"


@pytest.mark.asyncio
async def test_bundled_only_does_not_list_stored_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under `bundled_only` the resolver never reads the store, so neither does the listing:
    the catalogue names what a request can actually reach."""
    from felix.manifests import store as manifest_store
    from felix_api.routes import openai_compat as oc

    monkeypatch.setattr(oc, "list_bundled", lambda: ["quick"])

    async def _list_active(_settings: Any, _tenant_id: str) -> list[dict[str, Any]]:
        raise AssertionError("the store must not be consulted under bundled_only")

    monkeypatch.setattr(manifest_store, "list_active", _list_active)
    _resolves_to_nothing(monkeypatch, oc)

    result = await oc.list_models(_models_request(type("S", (), {"bundled_only": True})()))
    assert [row["id"] for row in result["data"]] == ["quick"]


@pytest.mark.asyncio
async def test_an_unreadable_store_leaves_the_bundled_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store being down is not a reason for `/v1/models` to fail: it degrades to the
    bundled list, the same rule `return_exceptions=True` keeps for one bad manifest."""
    from felix.manifests import store as manifest_store
    from felix_api.routes import openai_compat as oc

    monkeypatch.setattr(oc, "list_bundled", lambda: ["quick", "deep"])

    async def _list_active(_settings: Any, _tenant_id: str) -> list[dict[str, Any]]:
        raise ConnectionError("postgres is down")

    monkeypatch.setattr(manifest_store, "list_active", _list_active)
    _resolves_to_nothing(monkeypatch, oc)

    result = await oc.list_models(_models_request(type("S", (), {"bundled_only": False})()))
    assert [row["id"] for row in result["data"]] == ["quick", "deep"]
