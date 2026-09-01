"""Test isolation for the process-global stores.

The `memory://` twins are module-level dicts, which is the point — they are the
no-infrastructure path, not a mock layer. But they outlive a test, so state written by one
reaches the next: a manifest stored as `quick` shadows the bundled file for the rest of the
session, and a minimal one has no `auth.inbound` block, so everything downstream 401s. That
failure is silent in isolation and only shows up as unrelated tests failing together.
"""

from __future__ import annotations

import os

import pytest

# Variables that redirect git away from the repository a command names. `git -C <dir>` does
# NOT override these — the environment wins — so a fixture building a throwaway repo with
# `git -C <tmpdir> init && add -A && commit` writes into whatever these point at. That
# happened: a review run with GIT_DIR exported committed twice into this repository and moved
# `refs/heads/<branch>` and `refs/remotes/origin/main` onto fixture commits, with no file
# changed and `git status` as the only symptom.
#
# Scrubbing them from the parent process, once, is what makes the hazard impossible rather
# than merely detected: every subprocess inherits the clean environment however it spells its
# git call. `tests/git_fixture.py` still scrubs per-call as belt-and-braces, and an invariant
# still requires tests to use it — but neither is the load-bearing defense any more.
#
# An allowlist would be better still, and is not available: git has no "ignore all ambient
# configuration" switch, so this enumerates. Erring wide is cheap here — the suite never wants
# any of these.
_GIT_REDIRECTS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
)


@pytest.fixture(autouse=True, scope="session")
def _scrub_ambient_git_environment():
    """No test in this suite ever wants an inherited git redirect."""
    for name in _GIT_REDIRECTS:
        os.environ.pop(name, None)
    yield


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
