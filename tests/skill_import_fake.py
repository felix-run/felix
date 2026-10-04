"""A fake api.github.com at the transport, for skill imports: repositories, refs that move,
commits, recursive trees and content-addressed blobs.

Shared by `tests/unit/test_skill_import.py` and `tests/e2e/test_skill_import.py`. A tree or a
commit lookup by a ref answers with whatever the ref names *now*, as GitHub does -- so a test can
move a ref between two calls and see whether an import read the commit it resolved or the ref.
Every request is recorded.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
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
    # Called once a commit lookup has answered, with the repository: a test moves a ref here.
    after_resolve: Any = None
    _counter: int = 0

    def push(
        self, repo: str, files: dict[str, bytes], *, ref: str = "main", license: str | None = "MIT"
    ) -> str:
        """Commit ``files`` (the whole tree) to ``repo`` and point ``ref`` at it."""
        state = self.repos.setdefault(repo.lower(), Repo())
        state.license = license
        self._counter += 1
        sha = hashlib.sha1(f"{repo}:{self._counter}".encode(), usedforsecurity=False).hexdigest()
        state.commits[sha] = dict(files)
        state.refs[ref] = sha
        return sha

    def client(self) -> httpx.AsyncClient:
        # The class as imported, not as looked up now: a test that rebinds `httpx.AsyncClient`
        # to reach the app must not reroute GitHub to the app as well.
        return _AsyncClient(transport=httpx.MockTransport(self.handle))

    def serve(self, monkeypatch: Any) -> None:
        """Answer the production path's GitHub client (`github.github_client`) from this fake."""
        from felix.skills import github

        monkeypatch.setattr(github, "github_client", lambda settings: self.client())

    def paths(self) -> list[str]:
        return [unquote(r.url.raw_path.decode().split("?")[0]) for r in self.requests]

    def _commit_of(self, state: Repo, ref: str) -> str | None:
        return state.refs.get(ref) or (ref if ref in state.commits else None)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "api.github.com", request.url
        if self.rate_limited:
            return httpx.Response(
                403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"}
            )
        parts = request.url.raw_path.decode().split("?")[0].split("/")[1:]
        if len(parts) < 3 or parts[0] != "repos" or f"{parts[1]}/{parts[2]}".lower() not in self.repos:
            return httpx.Response(404, json={"message": "Not Found"})
        name = f"{parts[1]}/{parts[2]}".lower()
        state, rest = self.repos[name], parts[3:]
        if not rest:
            license = {"spdx_id": state.license} if state.license else None
            return httpx.Response(200, json={"default_branch": state.default_branch, "license": license})
        if rest[0] == "commits":
            sha = self._commit_of(state, unquote(rest[1]))
            if sha is None:
                return httpx.Response(422, json={"message": "No commit found"})
            assert request.headers["accept"] == "application/vnd.github.sha"
            if self.after_resolve is not None:
                self.after_resolve(self, name)
            return httpx.Response(200, text=sha)
        if rest[:2] == ["git", "trees"]:
            sha = self._commit_of(state, unquote(rest[2]))
            if sha is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200, json={"sha": sha, "tree": _tree(state.commits[sha]), "truncated": self.truncated}
            )
        if rest[:2] == ["git", "blobs"]:
            for files in state.commits.values():
                for path, data in files.items():
                    if blob_sha(data) == rest[2]:
                        served = self.tampered.get(path, data)
                        content = base64.encodebytes(served).decode()
                        return httpx.Response(
                            200, content=json.dumps({"content": content, "encoding": "base64"})
                        )
            return httpx.Response(404, json={"message": "Not Found"})
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
