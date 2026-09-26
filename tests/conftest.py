"""Test isolation: the process-global stores, the ambient git environment, and the app globals.

The `memory://` twins are module-level dicts, which is the point — they are the
no-infrastructure path, not a mock layer. But they outlive a test, so state written by one
reaches the next: a manifest stored as `quick` shadows the bundled file for the rest of the
session, and a minimal one has no `auth.inbound` block, so everything downstream 401s. That
failure is silent in isolation and only shows up as unrelated tests failing together.
"""

from __future__ import annotations

import os
from pathlib import Path

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


def _repository_config() -> Path | None:
    """This checkout's shared `.git/config`, found without running git.

    Read off the filesystem because every git subprocess in `tests/` must go through
    `tests/git_fixture.py`, and that helper runs against a *fixture* repo by design. In a
    linked worktree `.git` is a file naming the worktree's git dir, whose `commondir` names
    the repository's own — where `config` lives.
    """
    dot_git = Path(__file__).resolve().parents[1] / ".git"
    if dot_git.is_dir():
        return dot_git / "config"
    if not dot_git.is_file():
        return None
    text = dot_git.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        return None
    git_dir = (dot_git.parent / text.removeprefix("gitdir:").strip()).resolve()
    common = git_dir / "commondir"
    if common.is_file():
        git_dir = (git_dir / common.read_text(encoding="utf-8").strip()).resolve()
    return git_dir / "config"


def _identity(config: Path | None) -> tuple[str, ...]:
    """The `[user]` lines of a git config, which is the part a fixture's `git config` writes."""
    if config is None or not config.is_file():
        return ()
    lines, in_user = [], False
    for raw in config.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_user = line.lower().startswith("[user")
        elif in_user and line:
            lines.append(line)
    return tuple(lines)


_REPO_CONFIG = _repository_config()


@pytest.fixture(autouse=True)
def _real_repository_identity_is_untouched(request: pytest.FixtureRequest):
    """Fail the test that writes a git identity into this repository.

    Every fixture repo sets `user.name t` / `user.email t@example.com`. One of them once did so
    against the real `.git/config` instead of its throwaway one, and from then on every commit
    made in this checkout — thirty of them, across three weeks and a dozen merged PRs — was
    authored `t <t@example.com>`. Nothing failed; the only symptom was a stranger's name on
    the squash merges, and by the time anyone asked, the history that would have named the
    test had been squashed away.

    Only the `[user]` section is compared, and nothing is rewritten: other sessions share this
    repository and legitimately write `branch.*` config mid-run, and a guard that restored the
    whole file would clobber them.
    """
    before = _identity(_REPO_CONFIG)
    yield
    after = _identity(_REPO_CONFIG)
    if after != before:
        pytest.fail(
            f"{request.node.nodeid} changed the git identity of this repository "
            f"({_REPO_CONFIG}): {before} -> {after}. A fixture's `git config` reached the real "
            "repo — route it through tests/git_fixture.py:git. Undo with "
            "`git config --local --unset user.name; git config --local --unset user.email`.",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _isolate_process_global_stores():
    """Clear the in-memory manifest store, corpus and resolver caches around every test."""
    from felix.approvals.store import reset_approvals_for_tests
    from felix.audit import store as audit_store
    from felix.documents.store import reset_documents_for_tests
    from felix.durability.fibers import reset_memory_fibers
    from felix.eval.store import reset_eval_for_tests
    from felix.jobs.store import reset_jobs_for_tests
    from felix.manifests import store as manifest_store
    from felix.manifests.resolver import clear_resolver_cache
    from felix.session.search import reset_search_index_for_tests
    from felix.session.store import _memory_session_stores
    from felix.session.thread_state import reset_thread_meta_for_tests
    from felix.session.tree import _leaf_by_thread
    from felix.skills import store as skills_store
    from felix.usage import store as usage_store

    def _clear() -> None:
        manifest_store.reset_memory_store()
        clear_resolver_cache()
        reset_memory_fibers()
        # A corpus that survives a test becomes another test's mysterious extra hit, and
        # retrieval tests assert on result *counts*, so the leak would look like a ranking bug.
        reset_documents_for_tests()
        # Skill activation is keyed by (tenant, manifest), so one test switching a skill on
        # is the next test.s starting state -- which is how a tenant-isolation assertion
        # passes alone and fails in the file, reading as flakiness rather than a leak.
        skills_store.clear_memory()
        # The session search index is another module-level list, and now that the in-memory
        # store actually writes to it, a thread's events would otherwise be found by every
        # later test that searched for them.
        reset_search_index_for_tests()
        # Thread state is three more process globals, and nothing reset them: a test reusing
        # another test's thread id inherited its transcript, its leaf pointer and its `phase`.
        # The suite was correct only because every id in it happened to be unique, and the
        # failure when one was not would have looked like a product bug rather than a leak.
        _memory_session_stores.clear()
        reset_thread_meta_for_tests()
        _leaf_by_thread.clear()
        # The management stores are the same shape of process global, and the same hazard: a
        # dataset named `smoke` written by one test was counted by another test's assertion on
        # the bundled `smoke` fixture, and it failed as an off-by-one in a file that had not
        # changed. Each store exports its own reset, so the private names stay next to the
        # globals they clear and a store refactor touches one file rather than this one.
        reset_eval_for_tests()
        reset_jobs_for_tests()
        reset_approvals_for_tests()
        # Audit and usage are the same shape again — a process buffer plus an in-memory twin —
        # and were the two this list missed. Each has *two* globals, so a test that recorded
        # an event without flushing left it in the buffer for whatever flushed next, and the
        # rows themselves outlived every test that wrote them. Two of the worker cron tests
        # found this the hard way: they passed alone and failed in the suite.
        audit_store.clear_memory()
        usage_store.clear_memory()

    _clear()
    yield
    _clear()


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
