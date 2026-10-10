"""A fake api.github.com at the transport, for skill imports: repositories, refs that move,
commits, recursive trees and content-addressed blobs.

Shared by `tests/unit/test_skill_import.py` and `tests/e2e/test_skill_import.py`. A tree or a
commit lookup by a ref answers with whatever the ref names *now*, as GitHub does -- so a test can
move a ref between two calls and see whether an import read the commit it resolved or the ref.
Every request is recorded.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import httpx

_AsyncClient = httpx.AsyncClient


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()


def skill_md(name: str, description: str = "Route invoices to the right queue.", body: str = "") -> bytes:
    text = body or (
        f"# {name}\n\nUse this when an invoice arrives and must be routed.\n\n"
        "## Steps\n\n1. Read the vendor and the amount.\n2. Route large amounts to finance.\n"
    )
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{text}".encode()


@dataclass
class Repo:
    default_branch: str = "main"
    license: str | None = "MIT"
    refs: dict[str, str] = field(default_factory=dict)
    commits: dict[str, dict[str, bytes]] = field(default_factory=dict)
    # Every commit in push order (one linear history), and when each was committed (epoch ms).
    history: list[str] = field(default_factory=list)
    dates: dict[str, int] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    # A commit off the linear history, and the history commit it was built on.
    parents: dict[str, str] = field(default_factory=dict)
    # An annotated tag's object id, and the commit it points at.
    annotated: dict[str, str] = field(default_factory=dict)

    def compare(self, base: str, head: str) -> str:
        """The `compare` status of ``head`` against ``base`` over one linear history. A commit
        built on it but off it (a fork's, say) is ahead; one with no ancestor in it has diverged."""
        if head not in self.history:
            return "ahead" if self.parents.get(head) in self.history else "diverged"
        at, of = self.history.index(head), self.history.index(base)
        return "identical" if at == of else "behind" if at < of else "ahead"

    def last_changed(self, commit: str, path: str) -> str | None:
        """The newest commit up to ``commit`` that changed anything under ``path``."""

        def folder(sha: str) -> dict[str, bytes]:
            files = self.commits[sha]
            return {p: d for p, d in files.items() if not path or p == path or p.startswith(f"{path}/")}

        upto = self.history[: self.history.index(commit) + 1]
        for i in range(len(upto) - 1, -1, -1):
            if folder(upto[i]) and (i == 0 or folder(upto[i]) != folder(upto[i - 1])):
                return upto[i]
        return None


# When a push without `at` is committed: long ago, so no cooldown a test sets refuses it.
EPOCH_MS = 1_600_000_000_000


@dataclass
class FakeRepos:
    repos: dict[str, Repo] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    # Answer every call with GitHub's rate-limit refusal.
    rate_limited: bool = False
    # Mark every tree as truncated, as GitHub does past its listing limit.
    truncated: bool = False
    # Serve these bytes for a blob instead of the ones its id names.
    tampered: dict[str, bytes] = field(default_factory=dict)
    # Called once a ref or commit lookup has answered, with the repository: a test moves a ref here.
    after_resolve: Any = None
    # Answer every call with a redirect to this URL.
    redirect_to: str = ""
    # Report no history for any path (`commits?path=`).
    no_history: bool = False
    # Pad the JSON answer for these paths' blobs by this many bytes.
    padding: dict[str, int] = field(default_factory=dict)
    # Every blob read waits here first, when set (`_handle_async`).
    blob_barrier: asyncio.Barrier | None = None
    _counter: int = 0

    def push(
        self,
        repo: str,
        files: dict[str, bytes],
        *,
        ref: str = "main",
        license: str | None = "MIT",
        at: int | None = None,
    ) -> str:
        """Commit ``files`` (the whole tree) to ``repo`` at ``at`` (epoch ms) and point ``ref`` at it."""
        sha = self._commit(repo, files, license=license, at=at)
        state = self.repos[repo.lower()]
        state.history.append(sha)
        state.refs[ref] = sha
        return sha

    def fork_commit(self, repo: str, files: dict[str, bytes]) -> str:
        """A commit GitHub serves under ``repo``'s name -- it is in the fork network -- that no
        branch or tag of ``repo`` reaches: one pushed only to a fork, built on the default
        branch's tip."""
        state = self.repos[repo.lower()]
        sha = self._commit(repo, files, license=state.license, at=None)
        state.parents[sha] = state.refs[state.default_branch]
        return sha

    def tag(self, repo: str, name: str, sha: str, *, annotated: bool = False) -> None:
        """Tag ``sha``: lightweight (the ref names the commit) or annotated (a tag object does)."""
        state = self.repos[repo.lower()]
        if annotated:
            obj = hashlib.sha1(f"tag:{name}:{sha}".encode(), usedforsecurity=False).hexdigest()
            state.annotated[obj] = sha
            sha = obj
        state.tags[name] = sha

    def _commit(self, repo: str, files: dict[str, bytes], *, license: str | None, at: int | None) -> str:
        state = self.repos.setdefault(repo.lower(), Repo())
        state.license = license
        self._counter += 1
        sha = hashlib.sha1(f"{repo}:{self._counter}".encode(), usedforsecurity=False).hexdigest()
        state.commits[sha] = dict(files)
        state.dates[sha] = EPOCH_MS + self._counter * 1000 if at is None else at
        return sha

    def client(self) -> httpx.AsyncClient:
        # The class as imported, not as looked up now: a test that rebinds `httpx.AsyncClient`
        # to reach the app must not reroute GitHub to the app as well.
        return _AsyncClient(transport=httpx.MockTransport(self._handle_async))

    async def _handle_async(self, request: httpx.Request) -> httpx.Response:
        # A blob read waits at the barrier, when one is set: what lets a test hold two imports
        # until both have read the library and neither has saved.
        if self.blob_barrier is not None and "/git/blobs/" in request.url.path:
            await self.blob_barrier.wait()
        return self.handle(request)

    def serve(self, monkeypatch: Any) -> None:
        """Answer the production path's GitHub client (`github.github_client`) from this fake."""
        from felix.skills import github

        monkeypatch.setattr(github, "github_client", lambda settings: self.client())

    def paths(self) -> list[str]:
        return [unquote(r.url.raw_path.decode().split("?")[0]) for r in self.requests]

    def _commit_of(self, state: Repo, ref: str) -> str | None:
        return state.refs.get(ref) or (ref if ref in state.commits else None)

    def _resolved(self, name: str) -> None:
        if self.after_resolve is not None:
            self.after_resolve(self, name)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "api.github.com", request.url
        if self.rate_limited:
            return httpx.Response(
                403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"}
            )
        if self.redirect_to:
            return httpx.Response(301, headers={"location": self.redirect_to})
        parts = [unquote(p) for p in request.url.raw_path.decode().split("?")[0].split("/")[1:]]
        if len(parts) < 3 or parts[0] != "repos" or f"{parts[1]}/{parts[2]}".lower() not in self.repos:
            return httpx.Response(404, json={"message": "Not Found"})
        name = f"{parts[1]}/{parts[2]}".lower()
        state, rest = self.repos[name], parts[3:]
        if not rest:
            license = {"spdx_id": state.license} if state.license else None
            return httpx.Response(200, json={"default_branch": state.default_branch, "license": license})
        if rest[:2] == ["git", "ref"]:
            kind, ref = rest[2], "/".join(rest[3:])
            sha = (state.refs if kind == "heads" else state.tags).get(ref)
            if sha is None:
                return httpx.Response(404, json={"message": "Not Found"})
            self._resolved(name)
            kind_of = "tag" if sha in state.annotated else "commit"
            return httpx.Response(
                200, json={"ref": f"refs/{kind}/{ref}", "object": {"type": kind_of, "sha": sha}}
            )
        if rest[:2] == ["git", "tags"]:
            target = state.annotated.get(rest[2])
            if target is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"sha": rest[2], "object": {"type": "commit", "sha": target}})
        if rest[0] == "compare":
            base, _, head = "/".join(rest[1:]).partition("...")
            base_sha = state.refs.get(base, base)
            return httpx.Response(200, json={"status": state.compare(base_sha, head), "files": []})
        if rest == ["commits"]:
            params = request.url.params
            assert params["per_page"] == "1"
            found = None if self.no_history else state.last_changed(params["sha"], params.get("path", ""))
            if found is None:
                return httpx.Response(200, json=[])
            stamp = datetime.fromtimestamp(state.dates[found] / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            return httpx.Response(200, json=[{"sha": found, "commit": {"committer": {"date": stamp}}}])
        if rest[0] == "commits":
            # As GitHub answers: a branch or tag of that name first, then any commit in the fork
            # network by full or abbreviated id.
            named = state.refs.get(rest[1]) or state.annotated.get(
                state.tags.get(rest[1], ""), state.tags.get(rest[1])
            )
            matches = [named] if named else [s for s in state.commits if s.startswith(rest[1])]
            if len(matches) != 1:
                return httpx.Response(422, json={"message": "No commit found"})
            assert request.headers["accept"] == "application/vnd.github.sha"
            self._resolved(name)
            return httpx.Response(200, text=matches[0])
        if rest[:2] == ["git", "trees"]:
            sha = self._commit_of(state, rest[2])
            if sha is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200, json={"sha": sha, "tree": _tree(state.commits[sha]), "truncated": self.truncated}
            )
        if rest[:2] == ["git", "blobs"]:
            return self._blob(state, rest[2], raw=request.headers["accept"] == "application/vnd.github.raw")
        return httpx.Response(404, json={"message": "Not Found"})

    def _blob(self, state: Repo, sha: str, *, raw: bool) -> httpx.Response:
        for files in state.commits.values():
            for path, data in files.items():
                if blob_sha(data) != sha:
                    continue
                served = self.tampered.get(path, data)
                if raw:
                    return httpx.Response(200, content=served)
                body = json.dumps({"content": base64.encodebytes(served).decode(), "encoding": "base64"})
                # Whitespace after the JSON: still valid, and longer than any cap on the answer.
                return httpx.Response(200, content=body + " " * self.padding.get(path, 0))
        return httpx.Response(404, json={"message": "Not Found"})


def _tree(files: dict[str, bytes]) -> list[dict[str, Any]]:
    dirs = {"/".join(p.split("/")[:i]) for p in files for i in range(1, p.count("/") + 1)}
    entries: list[dict[str, Any]] = [{"path": d, "type": "tree", "sha": "0" * 40} for d in sorted(dirs)]
    entries += [
        {"path": p, "type": "blob", "sha": blob_sha(data), "size": len(data)}
        for p, data in sorted(files.items())
    ]
    return sorted(entries, key=lambda e: e["path"])


__all__ = ["FakeRepos", "Repo", "blob_sha", "skill_md"]
