"""Test isolation: the process-global stores, the ambient git environment, and the app globals.

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
GIT_REDIRECTS = frozenset(
    (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
    )
)


@pytest.fixture(autouse=True, scope="session")
def _scrub_ambient_git_environment():
    """No test in this suite ever wants an inherited git redirect."""
    saved = {name: os.environ.pop(name) for name in GIT_REDIRECTS if name in os.environ}
    try:
        yield
    finally:
        # Restored, because an in-process runner (an IDE, a wrapper calling pytest.main())
        # shares this process — losing GIT_DIR permanently would be a surprise the suite has
        # no business causing.
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def _isolate_process_global_stores():
    """Clear the in-memory manifest store and the resolver caches around every test."""
    from felix.durability.fibers import reset_memory_fibers
    from felix.manifests import store as manifest_store
    from felix.manifests.resolver import clear_resolver_cache

    manifest_store.reset_memory_store()
    clear_resolver_cache()
    reset_memory_fibers()
    yield
    manifest_store.reset_memory_store()
    clear_resolver_cache()
    reset_memory_fibers()


@pytest.fixture(autouse=True)
def _reset_app_globals():
    """Undo the process globals that booting the API populates.

    `create_application()` fills the `get_settings` lru_cache and runs plugin discovery. The
    registry is replaced rather than having its `_loaded` flag cleared: `load_plugins` gates on
    that flag and every `register_*` *appends*, so clearing it while `_plugins` and
    `_startup_hooks` stay populated makes the next discovery register everything twice —
    startup hooks included, which then run twice. Inert in the lean CI venv, where
    `installed_plugins()` is empty; not inert under `make install-full`.
    """
    yield
    from felix import plugins as felix_plugins
    from felix.config import get_settings

    get_settings.cache_clear()
    felix_plugins._registry = felix_plugins.PluginRegistry()
