"""Test isolation for the process-global stores.

The `memory://` twins are module-level dicts, which is the point — they are the
no-infrastructure path, not a mock layer. But they outlive a test, so state written by one
reaches the next: a manifest stored as `quick` shadows the bundled file for the rest of the
session, and a minimal one has no `auth.inbound` block, so everything downstream 401s. That
failure is silent in isolation and only shows up as unrelated tests failing together.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_process_global_stores():
    """Clear the in-memory manifest store and the resolver caches around every test."""
    from felix.manifests import store as manifest_store
    from felix.manifests.resolver import clear_resolver_cache

    manifest_store.reset_memory_store()
    clear_resolver_cache()
    yield
    manifest_store.reset_memory_store()
    clear_resolver_cache()
