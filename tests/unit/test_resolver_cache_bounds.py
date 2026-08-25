"""The resolver's caches forget instead of growing.

Three of the four are keyed partly by tenant — `tenant#name#version` for version blobs,
`tenant#name` for the active pointer, and `manifests/{tenant}/{name}.json` for the
tenant object — so as plain dicts they only ever grew. Every tenant that resolved a
manifest left an entry behind for the life of the process.

That is a slow leak rather than a latency problem, and it scales with the number of
tenants a deployment serves, which is the number an operator is least able to bound.

Eviction costs a re-read from Postgres or the object store, so the failure mode of a
too-small cache is "slower" while the failure mode of an unbounded one is the thing
being fixed. The bound is generous for that reason.
"""

from __future__ import annotations

import pytest
from felix.manifests import resolver as res


@pytest.fixture(autouse=True)
def _clean() -> None:
    res.clear_resolver_cache()
    yield
    res.clear_resolver_cache()


def _caches() -> dict[str, object]:
    return {
        "_version_blob_cache": res._version_blob_cache,
        "_active_pointer_cache": res._active_pointer_cache,
        "_tenant_obj_cache": res._tenant_obj_cache,
        "_global_obj_cache": res._global_obj_cache,
    }


def test_every_resolver_cache_is_bounded() -> None:
    """Enumerated rather than listed one by one, so a fifth cache added later is not
    silently unbounded — the failure this is about was four dicts nobody was counting."""
    for name, cache in _caches().items():
        assert isinstance(cache, res._BoundedCache), f"{name} is a plain dict again"


@pytest.mark.parametrize("name", sorted(_caches()))
def test_a_cache_stops_growing_at_its_bound(name: str) -> None:
    cache = _caches()[name]
    for i in range(res.MAX_CACHE_ENTRIES * 2):
        cache[f"tenant-{i}#manifest#1"] = {"v": i}
    assert len(cache) <= res.MAX_CACHE_ENTRIES, f"{name} grew to {len(cache)}"


def test_it_forgets_the_least_recently_used_entry() -> None:
    """Not merely *an* entry: evicting the hot one would turn a memory fix into a
    latency regression, because the working set is exactly what keeps being asked for."""
    cache = res._BoundedCache(maxsize=3)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3
    assert cache.get("a") == 1  # touch it, so "b" is now the oldest
    cache["d"] = 4

    assert cache.get("b") is None, "evicted the recently-used entry instead of the oldest"
    assert cache.get("a") == 1
    assert cache.get("c") == 3
    assert cache.get("d") == 4


def test_reassigning_a_key_does_not_count_twice() -> None:
    """A version blob re-resolved after eviction must not consume two slots."""
    cache = res._BoundedCache(maxsize=2)
    cache["a"] = 1
    cache["a"] = 2
    cache["b"] = 3
    assert len(cache) == 2
    assert cache.get("a") == 2


def test_invalidation_still_reaches_a_bounded_cache() -> None:
    """`invalidate_active` pops by key and `clear_resolver_cache` empties everything.

    Both are the reason this subclasses `dict` rather than wrapping one: a cache that
    silently stopped honouring invalidation would serve a stale manifest pointer after
    a rollback, which is a correctness bug wearing a performance bug's clothes.
    """
    key = res._pointer_key("acme", "quick")
    res._active_pointer_cache[key] = {"version": 1, "expires_at": 1 << 62}
    assert res._active_pointer_cache.get(key) is not None

    res.invalidate_active("acme", "quick")
    assert res._active_pointer_cache.get(key) is None, "invalidate_active no longer evicts"

    for cache in _caches().values():
        cache["x#y#1"] = {"v": 1}
    res.clear_resolver_cache()
    for name, cache in _caches().items():
        assert len(cache) == 0, f"{name} survived clear_resolver_cache"


@pytest.mark.asyncio
async def test_the_object_cache_still_serves_a_hit_without_touching_the_store() -> None:
    """`_read_object` used `in`/`[]`, which reads without maintaining recency. Routing
    it through `get` keeps the eviction order honest — this pins that the routing did
    not cost the cache hit itself."""
    reads: list[str] = []

    class _Store:
        async def get_json(self, key: str) -> dict[str, object]:
            reads.append(key)
            return {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "quick"}}

    cache = res._BoundedCache()
    first = await res._read_object(_Store(), "manifests/acme/quick.json", cache)
    second = await res._read_object(_Store(), "manifests/acme/quick.json", cache)
    assert first is not None and second is first
    assert len(reads) == 1, f"the object store was read {len(reads)} times for two resolves"
