"""`publish_commits` — publish workspace commits through GitHub's Git Data API, from the harness.

Everything here runs against a **real git repository** and a **real HTTP server** on loopback
through the real egress-guarded client. The fake GitHub is honest where it matters: it hashes
the blobs it is sent and builds the tree it is asked for with git itself, so the tool's
tree-sha integrity check is tested against what was actually sent rather than a canned answer.
It is also strict where GitHub is: a tree entry naming a blob that was neither uploaded nor in
the base tree, a `type` that does not fit its `mode`, a non-base64 blob, a ref update that is
not a fast-forward of the current tip, and creating a ref that exists are all 422s — the
workspace's object database is the fake's storage, so without those checks a tool that forgot
to upload a blob would still pass.

What the tests pin, in the order the module docstring promises it:

* the call sequence — blobs, a tree on the parent's tree with deletions as `sha: null`, one
  commit on the right parent, then a created or fast-forwarded ref;
* the refusals — a branch outside the prefix, a head that does not contain the remote tip;
* the approval row shows the diff, computed from `head_sha`, while the call signature and the
  arguments the tool runs with never include it;
* an approval binds content: the same branch with a different `head_sha` is a new approval;
* a preview that fails refuses the call and writes no row — an approval over no preview binds
  content nobody saw;
* a remote branch that moves under the approval, or mid-publish, is never overwritten;
* a hostile `.git/config` / `.gitattributes` makes the harness's git run nothing. Of the
  overrides in `_GIT_PRELUDE`, `log.showSignature`, `--no-textconv` and `--no-ext-diff` are each
  load-bearing here; `core.fsmonitor`, `core.hooksPath` and `core.pager` guard against commands
  the tool does not run today (nothing reads the index, writes a ref, or pages to a tty), so
  removing one alone leaves this test green;
* the #307 regression — a one-line change to a 100+ KiB file puts a small preview in the row
  and none of the file in the model's context.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from felix.approvals import store as approvals_store
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.builder import PREVIEW_ARG, apply_approvals
from felix.manifests.schema import ApprovalRule, GithubPublishSpec
from felix.side_events import requested_on
from felix.tools import github_publish as gp
from felix.tools.errors import read_tool_error_code
from felix.tools.types import Tool, ToolInvocationCtx, define_tool, is_wrapper_deny, tool_output_content
from pydantic import ValidationError

from tests.support.git_fixture import git as fixture_git
from tests.support.loopback_http import Request, respond, serve

REPO = "felix-run/felix"
TOKEN = "t0k"
TENANT = "default"
BIG_MARKER = "UNCHANGED-MIDDLE-OF-THE-BIG-FILE"


# --- a real repository -----------------------------------------------------------------


_IDENTITY = {
    "GIT_AUTHOR_NAME": "Felix",
    "GIT_AUTHOR_EMAIL": "felix@example.invalid",
    "GIT_COMMITTER_NAME": "Felix",
    "GIT_COMMITTER_EMAIL": "felix@example.invalid",
}


def git(ws: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """`tests/support/git_fixture.py:git` with a commit identity; never an ambient git environment."""
    return fixture_git(ws, *args, extra={**_IDENTITY, **(env or {})})


def commit(ws: Path, message: str) -> str:
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", message)
    return git(ws, "rev-parse", "HEAD").strip()


def _big_changelog(extra: str = "") -> str:
    lines = [f"- entry {i}: something that happened in release {i}" for i in range(2500)]
    lines.insert(1200, BIG_MARKER)
    return "# Changelog\n\n## [Unreleased]\n" + extra + "\n".join(lines) + "\n"


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("hello\n")
    (root / "old.txt").write_text("to be deleted\n")
    (root / "CHANGELOG.md").write_text(_big_changelog())
    commit(root, "base")
    return root


def feature_commit(ws: Path) -> str:
    git(ws, "switch", "-q", "-c", "felix/307-publish")
    (ws / "CHANGELOG.md").write_text(_big_changelog("- Added publish_commits.\n"))
    (ws / "new.txt").write_text("brand new\n")
    (ws / "old.txt").unlink()
    return commit(ws, "Publish commits from the harness\n\nCloses #307.")


# --- a fake GitHub that is honest about hashes -------------------------------------------


_MODE_TYPE = {"100644": "blob", "100755": "blob", "120000": "blob", "160000": "commit", "040000": "tree"}


class Unprocessable(Exception):
    """A request GitHub answers with 422."""


class FakeGitHub:
    def __init__(self, ws: Path, refs: dict[str, str]) -> None:
        self.ws = ws
        self.refs = dict(refs)
        self.calls: list[tuple[str, str, Any]] = []
        self.auth: list[str] = []
        self.blob_bytes = 0
        self.uploaded: set[str] = set()
        self.commits: dict[str, dict[str, Any]] = {}
        self.wrong_tree = False
        # Runs after a commit is created and before the ref moves: another pusher, mid-publish.
        self.on_commit: Callable[[], None] | None = None
        self.rejected: list[str] = []

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.calls if m != "GET"]

    def bodies(self, method: str, path: str) -> list[Any]:
        return [b for m, p, b in self.calls if m == method and p == path]

    def _git(self, *args: str, env: dict[str, str] | None = None) -> str:
        """The fake's own git, inert against the hostile-config test's workspace."""
        return git(self.ws, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args, env=env)

    def _base_blobs(self, tree: str) -> set[str]:
        """Blob shas reachable from `tree` — what GitHub already has without an upload."""
        listing = self._git("ls-tree", "-r", tree)
        return {line.split("\t", 1)[0].split(" ")[2] for line in listing.splitlines() if line}

    def _tree(self, body: dict[str, Any]) -> str:
        known = self.uploaded | self._base_blobs(body["base_tree"])
        for e in body["tree"]:
            if _MODE_TYPE.get(e["mode"]) != e["type"]:
                raise Unprocessable(f"type {e['type']!r} does not fit mode {e['mode']!r} at {e['path']}")
            if e["sha"] is not None and e["type"] == "blob" and e["sha"] not in known:
                raise Unprocessable(f"tree.sha {e['sha']} is not a valid blob ({e['path']})")
        index = self.ws.parent / "fake-index"
        env = {"GIT_INDEX_FILE": str(index)}
        self._git("read-tree", body["base_tree"], env=env)
        for e in body["tree"]:
            if e["sha"] is None:
                self._git("update-index", "--force-remove", "--", e["path"], env=env)
            else:
                self._git(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{e['mode']},{e['sha']},{e['path']}",
                    env=env,
                )
        sha = self._git("write-tree", env=env).strip()
        index.unlink()
        return sha

    async def __call__(self, req: Request, writer: asyncio.StreamWriter) -> None:
        self.auth.append(req.headers.get("authorization", ""))
        prefix = f"/repos/{REPO}"
        assert req.path.startswith(prefix), req.path
        path = req.path[len(prefix) :]
        body = json.loads(req.body) if req.body else None
        self.calls.append((req.method, path, body))
        try:
            status, data = self._route(req.method, path, body)
        except Unprocessable as exc:
            self.rejected.append(str(exc))
            status, data = "422 Unprocessable Entity", {"message": str(exc)}
        respond(writer, json.dumps(data).encode(), status=status, ctype="application/json")

    def _route(self, method: str, path: str, body: Any) -> tuple[str, Any]:
        if method == "GET" and path.startswith("/git/ref/heads/"):
            name = path[len("/git/ref/heads/") :]
            if name in self.refs:
                return "200 OK", {
                    "ref": f"refs/heads/{name}",
                    "object": {"sha": self.refs[name], "type": "commit"},
                }
            return "404 Not Found", {"message": "Not Found"}
        if method == "POST" and path == "/git/blobs":
            if body.get("encoding") != "base64":
                raise Unprocessable(f"encoding {body.get('encoding')!r}: this client only sends base64")
            data = base64.b64decode(body["content"], validate=True)
            self.blob_bytes += len(data)
            sha = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            self.uploaded.add(sha)
            return "201 Created", {"sha": sha}
        if method == "POST" and path == "/git/trees":
            sha = self._tree(body)
            return "201 Created", {"sha": "0" * 40 if self.wrong_tree else sha}
        if method == "POST" and path == "/git/commits":
            sha = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
            self.commits[sha] = body
            if self.on_commit is not None:
                self.on_commit()
            return "201 Created", {"sha": sha}
        if method == "POST" and path == "/git/refs":
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.refs:
                raise Unprocessable("Reference already exists")
            self.refs[name] = body["sha"]
            return "201 Created", {"ref": body["ref"]}
        if method == "PATCH" and path.startswith("/git/refs/heads/"):
            name = path[len("/git/refs/heads/") :]
            tip = self.refs.get(name)
            new = self.commits.get(body["sha"])
            if tip is None:
                raise Unprocessable("Reference does not exist")
            if new is None or tip not in new["parents"]:
                raise Unprocessable("Update is not a fast forward")
            self.refs[name] = body["sha"]
            return "200 OK", {"ref": path}
        raise Unprocessable(f"unexpected {method} {path}")


# --- binding and context ------------------------------------------------------------------


def _spec(**kw: Any) -> GithubPublishSpec:
    base: dict[str, Any] = {
        "repo": REPO,
        "auth": "secret:GITHUB_MCP_TOKEN",
        "base": "main",
        "branch_prefix": "felix/",
    }
    base.update(kw)
    return GithubPublishSpec(**base)


def _tool(api: str) -> Tool:
    return gp.tool_from_github_publish(_spec(), token=TOKEN, allow_http=True, api_base=api)


def _settings(ws: Path) -> Settings:
    return Settings(workspace_root=str(ws), allow_insecure=True, auth_mode="none", environment="development")


def _req(ws: Path, thread: str = "default:t-publish") -> RequestContext:
    return RequestContext(
        settings=_settings(ws),
        auth=AuthContext(tenant_id=TENANT),
        manifest_id="contributor",
        thread_id=thread,
    )


async def _call(tool: Tool, ws: Path, args: dict[str, Any]) -> Any:
    async with async_run_with_context(_req(ws)):
        return await tool.executor.execute(args, ToolInvocationCtx(tool_call_id="c1"))


@pytest.fixture(autouse=True)
def _clean_approvals() -> Iterator[None]:
    approvals_store.reset_approvals_for_tests()
    yield
    approvals_store.reset_approvals_for_tests()


# --- publishing ----------------------------------------------------------------------------


async def test_a_new_branch_is_published_as_blobs_tree_commit_ref(ws: Path) -> None:
    base = git(ws, "rev-parse", "HEAD").strip()
    head = feature_commit(ws)
    fake = FakeGitHub(ws, {"main": base})
    async with serve(fake) as api:
        out = await _call(_tool(api), ws, {"branch": "felix/307-publish", "head_sha": head})

    text = tool_output_content(out)
    assert read_tool_error_code(out) is None, text
    assert [m for m, _ in fake.writes()] == ["POST", "POST", "POST", "POST", "POST"], fake.writes()
    (tree,) = fake.bodies("POST", "/git/trees")
    assert tree["base_tree"] == git(ws, "rev-parse", f"{base}^{{tree}}").strip()
    entries = {e["path"]: e for e in tree["tree"]}
    assert set(entries) == {"CHANGELOG.md", "new.txt", "old.txt"}
    assert entries["old.txt"]["sha"] is None, "a deleted file must be a null-sha entry"
    (commit_body,) = fake.bodies("POST", "/git/commits")
    assert commit_body["parents"] == [base]
    assert commit_body["tree"] == git(ws, "rev-parse", f"{head}^{{tree}}").strip()
    assert commit_body["message"].startswith("Publish commits from the harness")
    (ref,) = fake.bodies("POST", "/git/refs")
    assert ref["ref"] == "refs/heads/felix/307-publish"
    assert fake.refs["felix/307-publish"] == ref["sha"]
    assert set(fake.auth) == {f"Bearer {TOKEN}"}

    assert ref["sha"] in text and "3 file(s)" in text
    assert f"https://github.com/{REPO}/compare/main...felix/307-publish" in text
    assert BIG_MARKER not in text and len(text) < 500


async def test_a_repository_in_a_hosted_sandbox_is_published_through_the_gateway(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the hosted backend the thread's repository is in its sandbox: every git call of the
    plan and the upload goes to the gateway's `git` op, and the result is the same publish."""
    import shutil

    from felix.tools import workspace_hosted

    from tests.support.workspace_gateway_fake import TOKEN as GATEWAY_TOKEN
    from tests.support.workspace_gateway_fake import URL, FakeGateway

    base = git(ws, "rev-parse", "HEAD").strip()
    head = feature_commit(ws)
    gateway = FakeGateway(root=tmp_path / "sandboxes")
    sandbox = gateway.sandbox("default/thread-key")
    shutil.copytree(ws, sandbox, symlinks=True)
    hosted = Settings(
        workspace_root=str(ws), workspace_backend="hosted", workspace_gateway_url=URL,
        workspace_gateway_token=GATEWAY_TOKEN,
    )  # fmt: skip
    monkeypatch.setattr(workspace_hosted, "gateway_client", lambda settings: gateway.client())
    root = workspace_hosted.HostedRoot(hosted, "default/thread-key")
    monkeypatch.setattr(workspace_hosted, "hosted_checkout_root", lambda: root)

    fake = FakeGitHub(sandbox, {"main": base})
    async with serve(fake) as api:
        out = await _call(_tool(api), ws, {"branch": "felix/307-publish", "head_sha": head})

    text = tool_output_content(out)
    assert read_tool_error_code(out) is None, text
    (commit_body,) = fake.bodies("POST", "/git/commits")
    assert commit_body["parents"] == [base]
    assert commit_body["tree"] == git(ws, "rev-parse", f"{head}^{{tree}}").strip()
    assert fake.refs["felix/307-publish"] in text
    assert {op for _, op in gateway.calls} == {"git"}


async def test_an_existing_branch_is_fast_forwarded_from_its_remote_tip(ws: Path) -> None:
    base = git(ws, "rev-parse", "HEAD").strip()
    first = feature_commit(ws)
    (ws / "second.txt").write_text("more\n")
    head = commit(ws, "A second change")
    fake = FakeGitHub(ws, {"main": base, "felix/307-publish": first})
    async with serve(fake) as api:
        out = await _call(_tool(api), ws, {"branch": "felix/307-publish", "head_sha": head})

    assert read_tool_error_code(out) is None, tool_output_content(out)
    (commit_body,) = fake.bodies("POST", "/git/commits")
    assert commit_body["parents"] == [first], "the parent is the branch's remote tip, not base"
    (tree,) = fake.bodies("POST", "/git/trees")
    assert [e["path"] for e in tree["tree"]] == ["second.txt"]
    (patch,) = fake.bodies("PATCH", "/git/refs/heads/felix/307-publish")
    assert patch["force"] is False
    assert not fake.bodies("POST", "/git/refs")


async def test_several_local_commits_are_published_as_one_titled_commit(ws: Path) -> None:
    base = git(ws, "rev-parse", "HEAD").strip()
    feature_commit(ws)
    (ws / "second.txt").write_text("more\n")
    head = commit(ws, "A second change")
    fake = FakeGitHub(ws, {"main": base})
    async with serve(fake) as api:
        out = await _call(
            _tool(api), ws, {"branch": "felix/307-publish", "head_sha": head, "title": "Publish it"}
        )

    assert read_tool_error_code(out) is None, tool_output_content(out)
    (commit_body,) = fake.bodies("POST", "/git/commits")
    assert commit_body["message"] == ("Publish it\n\n- Publish commits from the harness\n- A second change\n")


async def test_a_head_that_does_not_contain_the_remote_tip_is_refused(ws: Path) -> None:
    base = git(ws, "rev-parse", "HEAD").strip()
    (ws / "elsewhere.txt").write_text("someone else's\n")
    remote_only = commit(ws, "Pushed by someone else")
    git(ws, "reset", "-q", "--hard", base)
    head = feature_commit(ws)
    fake = FakeGitHub(ws, {"main": base, "felix/307-publish": remote_only})
    async with serve(fake) as api:
        out = await _call(_tool(api), ws, {"branch": "felix/307-publish", "head_sha": head})

    text = tool_output_content(out)
    assert read_tool_error_code(out) == "invalid_arguments", text
    assert "does not contain the remote felix/307-publish" in text
    assert fake.writes() == [], "a refused publish wrote to GitHub"


async def test_a_remote_tip_the_workspace_never_fetched_says_to_fetch(ws: Path) -> None:
    head = feature_commit(ws)
    fake = FakeGitHub(ws, {"main": "f" * 40})
    async with serve(fake) as api:
        out = await _call(_tool(api), ws, {"branch": "felix/307-publish", "head_sha": head})

    text = tool_output_content(out)
    assert read_tool_error_code(out) == "invalid_arguments", text
    assert "git" in text and "fetch" in text and "f" * 40 in text
    assert fake.writes() == []


@pytest.mark.parametrize(
    "branch,why",
    [
        ("main", "must start with 'felix/'"),
        ("feature/x", "must start with 'felix/'"),
        ("felix/../main", "not a valid branch name"),
        ("felix/a b", "not a valid branch name"),
        ("felix/x.lock", "not a valid branch name"),
        # URL-significant: each would read as a different path, query or fragment in the ref URL.
        ("felix/a%2fb", "not a valid branch name"),
        ("felix/a?b", "not a valid branch name"),
        ("felix/a#b", "not a valid branch name"),
        ("felix/a\nb", "not a valid branch name"),
        # `$` matches before a trailing newline; and the branch is not stripped into validity.
        ("felix/ab\n", "not a valid branch name"),
        (" felix/ab", "must start with 'felix/'"),
        ("felix/.x", "not a valid branch name"),
    ],
    ids=lambda v: repr(v) if isinstance(v, str) and len(v) < 16 else None,
)
async def test_branches_outside_the_prefix_or_malformed_are_refused(ws: Path, branch: str, why: str) -> None:
    head = git(ws, "rev-parse", "HEAD").strip()
    fake = FakeGitHub(ws, {"main": head})
    async with serve(fake) as api:
        out = await _call(_tool(api), ws, {"branch": branch, "head_sha": head})
    assert read_tool_error_code(out) == "invalid_arguments", tool_output_content(out)
    assert why in tool_output_content(out)
    assert fake.calls == [], "a refused branch reached GitHub"


async def test_an_unknown_or_short_sha_is_refused(ws: Path) -> None:
    head = git(ws, "rev-parse", "HEAD").strip()
    fake = FakeGitHub(ws, {"main": head})
    async with serve(fake) as api:
        short = await _call(_tool(api), ws, {"branch": "felix/x", "head_sha": head[:12]})
        unknown = await _call(_tool(api), ws, {"branch": "felix/x", "head_sha": "a" * 40})
    assert read_tool_error_code(short) == read_tool_error_code(unknown) == "invalid_arguments"
    assert "full 40-character sha" in tool_output_content(short)
    assert "is not a commit in the workspace" in tool_output_content(unknown)
    assert fake.writes() == []


async def test_a_tree_that_is_not_the_head_tree_is_never_committed(ws: Path) -> None:
    base = git(ws, "rev-parse", "HEAD").strip()
    head = feature_commit(ws)
    fake = FakeGitHub(ws, {"main": base})
    fake.wrong_tree = True
    async with serve(fake) as api:
        out = await _call(_tool(api), ws, {"branch": "felix/307-publish", "head_sha": head})
    assert "refusing to commit it" in tool_output_content(out)
    assert not fake.bodies("POST", "/git/commits") and not fake.bodies("POST", "/git/refs")


def test_the_git_subprocess_environment_carries_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_MCP_TOKEN", "the-token-value")
    monkeypatch.setenv("FELIX_ANTHROPIC_API_KEY", "another-secret")
    env = gp._git_env()
    assert "the-token-value" not in env.values() and "another-secret" not in env.values()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "core.hooksPath=/dev/null" in gp._GIT_PRELUDE and "core.fsmonitor=false" in gp._GIT_PRELUDE


def _hostile_config(ws: Path, markers: Path) -> list[str]:
    """Point every config-driven program git might run at a script that leaves a marker.

    The workspace's `.git/config` and `.gitattributes` are agent-writable, and the git this tool
    runs is in the API process — so any of these executing is code from the workspace running as
    the harness, with whatever it can read. Returns the marker names, one per vector.
    """
    markers.mkdir()
    names = ["fsmonitor", "external", "textconv", "hook", "gpg", "pager"]
    scripts = {}
    for name in names:
        script = markers / f"{name}.sh"
        # textconv and gpg must still produce output for git to carry on past them.
        script.write_text(f'#!/bin/sh\ntouch "{markers}/ran-{name}"\ncat "$1" 2>/dev/null\nexit 0\n')
        script.chmod(0o755)
        scripts[name] = str(script)
    hooks = markers / "hooks"
    hooks.mkdir()
    for hook in ("pre-commit", "post-checkout", "post-index-change", "reference-transaction", "post-rewrite"):
        target = hooks / hook
        target.write_text(f'#!/bin/sh\ntouch "{markers}/ran-hook"\n')
        target.chmod(0o755)
    (ws / ".gitattributes").write_text("* diff=evil\n")
    # In the workspace's config, after any commit the test still makes: the fixture's own git
    # would run the hooks otherwise.
    for key, value in (
        ("core.fsmonitor", scripts["fsmonitor"]),
        ("diff.external", scripts["external"]),
        ("diff.evil.textconv", scripts["textconv"]),
        ("core.hooksPath", str(hooks)),
        ("gpg.program", scripts["gpg"]),
        ("log.showSignature", "true"),
        ("core.pager", scripts["pager"]),
    ):
        git(ws, "config", key, value)
    return names


def _signed_commit(ws: Path, tree_of: str, parent: str, subject: str) -> str:
    """A commit carrying a `gpgsig` header, so `log.showSignature` has something to verify."""
    tree = git(ws, "rev-parse", f"{tree_of}^{{tree}}").strip()
    body = (
        f"tree {tree}\nparent {parent}\n"
        "author Felix <felix@example.invalid> 1700000000 +0000\n"
        "committer Felix <felix@example.invalid> 1700000000 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEEAAAA\n -----END PGP SIGNATURE-----\n"
        f"\n{subject}\n"
    )
    raw = ws.parent / "signed-commit"
    raw.write_text(body)
    return git(ws, "hash-object", "-t", "commit", "-w", str(raw)).strip()


async def test_a_hostile_repository_config_runs_nothing(ws: Path) -> None:
    """fsmonitor, an external diff, a textconv driver, hooks, gpg and a pager — each named by the
    workspace's own config — must not run during a preview or a publish."""
    base = git(ws, "rev-parse", "HEAD").strip()
    feature = feature_commit(ws)
    # Every vector needs content to act on: a text diff for textconv and diff.external, a signed
    # commit for log.showSignature to hand to gpg.
    head = _signed_commit(ws, feature, base, "Publish commits from the harness")
    markers = ws.parent / "markers"
    names = _hostile_config(ws, markers)
    fake = FakeGitHub(ws, {"main": base})
    args = {"branch": "felix/307-publish", "head_sha": head}
    async with serve(fake) as api:
        tool = _tool(api)
        async with async_run_with_context(_req(ws)):
            preview = await tool.executor.preview(args)
        out = await _call(tool, ws, args)

    assert "diff --git a/CHANGELOG.md" in preview and "+- Added publish_commits." in preview
    assert read_tool_error_code(out) is None, tool_output_content(out)
    ran = sorted(p.name for p in markers.glob("ran-*"))
    assert ran == [], f"the workspace's config made the harness's git run: {ran} (of {names})"


# --- the approval preview ------------------------------------------------------------------


def _gated(tool: Tool, ttl: int = 5, *, one_shot: bool = False) -> Tool:
    """Gate `tool` with one rule whose flags each test states. Not contributor.yaml's `publish`
    rule (one-shot, principal-bound): the tests that need a reusable grant say so."""
    rule = ApprovalRule(id="publish", tools=[gp.TOOL_NAME, "probe"], ttl_seconds=ttl, one_shot=one_shot)
    return apply_approvals([tool], [rule], "contributor")[0]


def _sig(args: dict[str, Any]) -> str:
    """The signature `apply_approvals` computes, re-derived here on purpose."""
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:32]


async def _run_with_decision(
    tool: Tool,
    ws: Path,
    args: dict[str, Any],
    *,
    decision: str = "approved",
    edit: bool = False,
    before_decide: Callable[[], None] | None = None,
) -> tuple[Any, dict[str, Any], RequestContext]:
    """Call the gated tool; when its approval row appears, record it and decide it.

    `before_decide` runs while the person is reading the row — the window in which the world
    the preview described can change."""
    from felix.approvals.interrupt import signal_decision

    req = _req(ws)
    seen: dict[str, Any] = {}

    async def _decide() -> None:
        for _ in range(200):
            pending = await approvals_store.list_approvals(req.settings, TENANT, status="pending")
            if pending:
                break
            await asyncio.sleep(0.02)
        else:
            return
        (row,) = pending
        seen.update(row)
        if before_decide is not None:
            before_decide()
        edited = dict(row["args"]) if edit else None
        await approvals_store.decide(
            req.settings, TENANT, row["id"], decision=decision, decided_by="op", edited_args=edited
        )
        await signal_decision(row["id"], decision, edited_args=edited)

    helper = asyncio.create_task(_decide())
    async with async_run_with_context(req):
        out = await tool.executor.execute(args, ToolInvocationCtx(tool_call_id="c1"))
    await helper
    return out, seen, req


async def test_the_approval_row_and_frame_carry_the_diff_but_the_signature_does_not(ws: Path) -> None:
    base = git(ws, "rev-parse", "HEAD").strip()
    head = feature_commit(ws)
    fake = FakeGitHub(ws, {"main": base})
    args = {"branch": "felix/307-publish", "head_sha": head}
    async with serve(fake) as api:
        out, row, req = await _run_with_decision(_gated(_tool(api)), ws, args)

    assert read_tool_error_code(out) is None, tool_output_content(out)
    preview = row["args"][PREVIEW_ARG]
    assert "diff --git a/CHANGELOG.md b/CHANGELOG.md" in preview
    assert "+- Added publish_commits." in preview
    assert "diff --git a/old.txt b/old.txt" in preview and "deleted file mode" in preview
    assert "new branch from " + base in preview
    (frame,) = requested_on(req.extras, "approval_required")
    assert frame["args"][PREVIEW_ARG] == preview
    assert row["call_signature"] == _sig(args), "the preview leaked into the call signature"


async def test_a_one_line_change_to_a_large_file_stays_small_everywhere(ws: Path) -> None:
    """#307: push_files carried the whole 196 KiB CHANGELOG as an argument, into the model's
    context and the approval row. Now the file goes to GitHub and nowhere else."""
    size = (ws / "CHANGELOG.md").stat().st_size
    assert size > 100 * 1024, "the fixture must be the large-file case"
    base = git(ws, "rev-parse", "HEAD").strip()
    head = feature_commit(ws)
    fake = FakeGitHub(ws, {"main": base})
    args = {"branch": "felix/307-publish", "head_sha": head}
    async with serve(fake) as api:
        out, row, _ = await _run_with_decision(_gated(_tool(api)), ws, args)

    text = tool_output_content(out)
    assert read_tool_error_code(out) is None, text
    assert fake.blob_bytes > size, "the file did go to GitHub"
    preview = row["args"][PREVIEW_ARG]
    assert "+- Added publish_commits." in preview, "the preview must show the change it summarises"
    assert len(preview.encode()) < 4 * 1024, len(preview)
    for where in (text, json.dumps(args), preview):
        assert BIG_MARKER not in where


async def test_a_large_diff_is_truncated_in_the_preview_and_says_so(ws: Path) -> None:
    base = git(ws, "rev-parse", "HEAD").strip()
    git(ws, "switch", "-q", "-c", "felix/big")
    (ws / "CHANGELOG.md").write_text("rewritten\n")
    head = commit(ws, "Rewrite the changelog")
    fake = FakeGitHub(ws, {"main": base})
    async with serve(fake) as api:
        executor = _tool(api).executor
        async with async_run_with_context(_req(ws)):
            preview = await executor.preview({"branch": "felix/big", "head_sha": head})

    assert "CHANGELOG.md" in preview.split("diff --git", 1)[0], "--stat must precede the diff"
    assert f"[diff truncated at {gp.MAX_PREVIEW_BYTES} bytes" in preview
    assert len(preview.encode()) < gp.MAX_PREVIEW_BYTES + 4096


async def test_an_approval_for_one_head_does_not_authorize_another(ws: Path) -> None:
    """`head_sha` is content-addressed and in the signature, so an approval binds the content.
    The same branch with a different head is a different call and waits for its own decision.

    The grant is reusable and lives 30s, so it is still live and spendable when the second call
    arrives: only the signature can keep it out. (With a 1s TTL the first grant could expire
    first and the test would pass with `head_sha` dropped from the signature.)"""
    base = git(ws, "rev-parse", "HEAD").strip()
    first = feature_commit(ws)
    fake = FakeGitHub(ws, {"main": base})
    first_args = {"branch": "felix/307-publish", "head_sha": first}
    async with serve(fake) as api:
        gated = _gated(_tool(api), ttl=30, one_shot=False)
        out, first_row, req = await _run_with_decision(gated, ws, first_args)
        assert read_tool_error_code(out) is None, tool_output_content(out)
        published = len(fake.writes())
        # Point the remote branch at a commit the workspace has, so that were the second call
        # authorized it would publish — a refusal must come from the approval, not from a stale tip.
        fake.refs["felix/307-publish"] = first

        (ws / "sneaky.txt").write_text("not what was approved\n")
        second = commit(ws, "Something else")
        second_args = {"branch": "felix/307-publish", "head_sha": second}
        denied, second_row, _ = await _run_with_decision(gated, ws, second_args, decision="denied")

    # Control: the first grant was live and unspent throughout — it is still found now.
    live = await approvals_store.find_approved(
        req.settings,
        TENANT,
        manifest_id="contributor",
        tool_name=gp.TOOL_NAME,
        call_signature=_sig(first_args),
    )
    assert live is not None and live["id"] == first_row["id"]
    assert second_row, "the second head ran on the first approval instead of asking for its own"
    assert second_row["id"] != first_row["id"]
    assert second_row["call_signature"] == _sig(second_args) != first_row["call_signature"]
    assert "[approval denied]" in tool_output_content(denied), tool_output_content(denied)
    assert len(fake.writes()) == published, "the second head was published on the first approval"


async def test_edited_args_lose_the_preview_before_the_tool_runs(ws: Path) -> None:
    ran: list[dict[str, Any]] = []

    async def _probe(args: dict[str, Any]) -> str:
        ran.append(dict(args))
        return "ok"

    async def _preview(args: dict[str, Any]) -> str:
        return f"would do {args['x']}"

    tool = define_tool(name="probe", description="p", handler=_probe)
    tool.approval_preview = _preview
    out, row, _ = await _run_with_decision(_gated(tool), ws, {"x": 1}, edit=True)

    assert tool_output_content(out) == "ok"
    assert row["args"] == {"x": 1, PREVIEW_ARG: "would do 1"}
    assert ran == [{"x": 1}], "the approver's echo of the preview reached the tool as an argument"


async def test_reusing_a_grant_with_edited_args_strips_the_preview(ws: Path) -> None:
    """The `find_approved` path: an approver's `edited_args` echo the row, preview and all, and a
    later identical call runs on them. The preview must not reach the tool there either."""
    ran: list[dict[str, Any]] = []

    async def _probe(args: dict[str, Any]) -> str:
        ran.append(dict(args))
        return "ok"

    async def _preview(args: dict[str, Any]) -> str:
        return f"would do {args['x']}"

    tool = define_tool(name="probe", description="p", handler=_probe)
    tool.approval_preview = _preview
    gated = _gated(tool, ttl=30, one_shot=False)
    _, row, req = await _run_with_decision(gated, ws, {"x": 1}, edit=True)
    assert row["args"] == {"x": 1, PREVIEW_ARG: "would do 1"}

    async with async_run_with_context(req):
        again = await gated.executor.execute({"x": 1}, ToolInvocationCtx(tool_call_id="c2"))

    assert tool_output_content(again) == "ok"
    assert len(await approvals_store.list_approvals(req.settings, TENANT, status=None)) == 1, (
        "the reuse asked again"
    )
    assert ran == [{"x": 1}, {"x": 1}], "the approver's echo of the preview reached the tool on reuse"


async def test_a_one_shot_grant_is_spent_by_the_call_that_waited_for_it(ws: Path) -> None:
    """The waiting call used to run without spending the grant, so one replay ran on it too."""
    ran: list[dict[str, Any]] = []

    async def _probe(args: dict[str, Any]) -> str:
        ran.append(dict(args))
        return "ok"

    gated = _gated(define_tool(name="probe", description="p", handler=_probe), ttl=30, one_shot=True)
    out, first_row, _ = await _run_with_decision(gated, ws, {"x": 1})
    assert tool_output_content(out) == "ok"
    replay, second_row, _ = await _run_with_decision(gated, ws, {"x": 1}, decision="denied")

    assert second_row, "the replay ran on a spent one_shot grant instead of asking again"
    assert second_row["id"] != first_row["id"]
    assert "[approval denied]" in tool_output_content(replay)
    assert ran == [{"x": 1}]


async def _no_row_and_refused(tool: Tool, ws: Path, args: dict[str, Any]) -> str:
    req = _req(ws)
    async with async_run_with_context(req):
        out = await tool.executor.execute(args, ToolInvocationCtx(tool_call_id="c1"))
    assert is_wrapper_deny(out), tool_output_content(out)
    assert await approvals_store.list_approvals(req.settings, TENANT, status=None) == [], (
        "a row without a preview"
    )
    return tool_output_content(out)


async def test_a_failing_preview_refuses_the_call_and_writes_no_row(ws: Path) -> None:
    ran: list[dict[str, Any]] = []

    async def _probe(args: dict[str, Any]) -> str:
        ran.append(dict(args))
        return "ran"

    async def _broken(args: dict[str, Any]) -> str:
        raise RuntimeError("git is gone")

    tool = define_tool(name="probe", description="p", handler=_probe)
    tool.approval_preview = _broken
    text = await _no_row_and_refused(_gated(tool), ws, {"x": 1})

    assert text.startswith("[approval preview failed] tool=probe rule=publish")
    assert "RuntimeError: git is gone" in text
    assert ran == []


async def test_a_preview_that_times_out_refuses_the_call(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.manifests import builder

    monkeypatch.setattr(builder, "_PREVIEW_TIMEOUT_S", 0.05)

    async def _slow(args: dict[str, Any]) -> str:
        await asyncio.sleep(5)
        return "never"

    tool = define_tool(name="probe", description="p", handler=lambda args: "ran")
    tool.approval_preview = _slow
    text = await _no_row_and_refused(_gated(tool), ws, {"x": 1})
    assert "[approval preview failed]" in text and "timed out" in text


async def test_a_head_that_is_not_a_commit_yet_gets_no_approval(ws: Path) -> None:
    """The review's case: name a sha before committing it, so the preview cannot be computed,
    then commit it while the row waits. Refused up front, it never becomes an approval."""
    base = git(ws, "rev-parse", "HEAD").strip()
    fake = FakeGitHub(ws, {"main": base})
    async with serve(fake) as api:
        text = await _no_row_and_refused(
            _gated(_tool(api)), ws, {"branch": "felix/307-publish", "head_sha": "a" * 40}
        )
    assert "[approval preview failed]" in text and "is not a commit in the workspace" in text
    assert fake.writes() == []


async def test_a_tool_without_a_preview_keeps_a_preview_argument(ws: Path) -> None:
    """`preview` is only reserved on a tool that computes one."""
    ran: list[dict[str, Any]] = []

    async def _probe(args: dict[str, Any]) -> str:
        ran.append(dict(args))
        return "ok"

    tool = define_tool(name="probe", description="p", handler=_probe)
    _, row, _ = await _run_with_decision(_gated(tool), ws, {PREVIEW_ARG: "mine"}, edit=True)
    assert row["args"] == {PREVIEW_ARG: "mine"}
    assert ran == [{PREVIEW_ARG: "mine"}]


# --- the remote moves under an approval ----------------------------------------------------


async def test_a_branch_that_moves_while_the_approval_waits_is_not_overwritten(ws: Path) -> None:
    """The preview said "fast-forward from X"; by the time the person approves, someone pushed.
    The publish re-reads the tip, finds it is not in `head_sha`, and writes nothing."""
    base = git(ws, "rev-parse", "HEAD").strip()
    (ws / "elsewhere.txt").write_text("someone else's\n")
    remote_only = commit(ws, "Pushed by someone else")
    git(ws, "reset", "-q", "--hard", base)
    first = feature_commit(ws)
    (ws / "second.txt").write_text("more\n")
    head = commit(ws, "A second change")
    fake = FakeGitHub(ws, {"main": base, "felix/307-publish": first})

    def _someone_pushes() -> None:
        fake.refs["felix/307-publish"] = remote_only

    async with serve(fake) as api:
        out, row, _ = await _run_with_decision(
            _gated(_tool(api)),
            ws,
            {"branch": "felix/307-publish", "head_sha": head},
            before_decide=_someone_pushes,
        )

    assert f"fast-forward from {first}" in row["args"][PREVIEW_ARG]
    assert read_tool_error_code(out) == "invalid_arguments", tool_output_content(out)
    assert "does not contain the remote felix/307-publish" in tool_output_content(out)
    assert fake.writes() == []
    assert fake.refs["felix/307-publish"] == remote_only


async def test_a_branch_that_moves_mid_publish_is_not_fast_forwarded_over(ws: Path) -> None:
    """Between the plan and the PATCH: the commit is built on the old tip, so the ref update is
    not a fast-forward of the new one, `force: false` makes GitHub refuse it, and the tool says
    the publish failed rather than reporting success."""
    base = git(ws, "rev-parse", "HEAD").strip()
    first = feature_commit(ws)
    (ws / "second.txt").write_text("more\n")
    head = commit(ws, "A second change")
    fake = FakeGitHub(ws, {"main": base, "felix/307-publish": first})
    moved = "e" * 40

    def _someone_pushes() -> None:
        fake.refs["felix/307-publish"] = moved

    fake.on_commit = _someone_pushes
    async with serve(fake) as api:
        out = await _call(_tool(api), ws, {"branch": "felix/307-publish", "head_sha": head})

    assert read_tool_error_code(out) == "provider_error", tool_output_content(out)
    assert "422" in tool_output_content(out) and "published" not in tool_output_content(out)
    assert fake.rejected == ["Update is not a fast forward"]
    assert fake.refs["felix/307-publish"] == moved, "the ref update was applied over the new tip"


async def test_a_branch_created_mid_publish_is_not_replaced(ws: Path) -> None:
    """The new-branch arm of the same race: `POST /git/refs` on a ref that now exists fails."""
    base = git(ws, "rev-parse", "HEAD").strip()
    head = feature_commit(ws)
    fake = FakeGitHub(ws, {"main": base})
    theirs = "d" * 40

    def _someone_creates_it() -> None:
        fake.refs["felix/307-publish"] = theirs

    fake.on_commit = _someone_creates_it
    async with serve(fake) as api:
        out = await _call(_tool(api), ws, {"branch": "felix/307-publish", "head_sha": head})

    assert read_tool_error_code(out) == "provider_error", tool_output_content(out)
    assert fake.rejected == ["Reference already exists"]
    assert fake.refs["felix/307-publish"] == theirs


# --- schema and binding ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"repo": "not-a-repo"},
        {"repo": "felix-run/.."},
        {"auth": "ghp_literaltoken"},
        {"branch_prefix": ""},
        {"branch_prefix": "bad prefix/"},
        {"base": "felix/main"},
        {"base": "a..b"},
    ],
)
def test_the_spec_refuses_what_it_cannot_enforce(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _spec(**override)


def _manifest() -> dict[str, Any]:
    return {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": "publisher"},
        "spec": {
            "pattern": "react",
            "github_publish": {
                "repo": REPO,
                "auth": "secret:FELIX_TEST_PUBLISH_TOKEN",
                "base": "main",
                "branch_prefix": "felix/",
            },
            "approvals": [{"id": "publish", "tools": [gp.TOOL_NAME], "ttl_seconds": 5}],
        },
    }


async def test_the_compile_binds_it_with_its_preview_through_the_governance_stack(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.manifests.builder import build_agent
    from felix.tools.builtins import default_tool_provider

    monkeypatch.setenv("FELIX_TEST_PUBLISH_TOKEN", "t1")
    agent = await build_agent(_manifest(), default_tool_provider(), settings=_settings(ws))
    tool = next((t for t in agent.tools if t.name == gp.TOOL_NAME), None)
    assert tool is not None, "spec.github_publish bound nothing"
    assert tool.approval_preview is not None, "the preview did not survive the governance stack"
    assert tool.replay_safe is False


async def test_a_call_through_the_compiled_agent_opens_an_approval_with_the_diff(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound tool *is* gated, not merely still carrying a preview: a call through the
    compiled agent writes a pending row under the manifest's rule and publishes nothing until
    it is decided. Dropping the approvals wrapper would publish here with no row."""
    from felix.manifests.builder import build_agent
    from felix.tools.builtins import default_tool_provider

    base = git(ws, "rev-parse", "HEAD").strip()
    head = feature_commit(ws)
    fake = FakeGitHub(ws, {"main": base})
    monkeypatch.setenv("FELIX_TEST_PUBLISH_TOKEN", "t1")
    async with serve(fake) as api:
        real = gp.tool_from_github_publish
        # The compile binds api.github.com; aim the same binder at the fake.
        monkeypatch.setattr(
            gp, "tool_from_github_publish", lambda spec, **kw: real(spec, **{**kw, "api_base": api})
        )
        agent = await build_agent(_manifest(), default_tool_provider(), settings=_settings(ws))
        tool = next(t for t in agent.tools if t.name == gp.TOOL_NAME)
        out, row, _ = await _run_with_decision(
            tool, ws, {"branch": "felix/307-publish", "head_sha": head}, decision="denied"
        )

    assert row, "a publish_commits call through the compiled agent opened no approval"
    assert row["tool_name"] == gp.TOOL_NAME and row["rule_id"] == "publish"
    assert "+- Added publish_commits." in row["args"][PREVIEW_ARG]
    assert "[approval denied]" in tool_output_content(out), tool_output_content(out)
    assert fake.writes() == []


async def test_an_unresolvable_token_binds_no_tool(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.manifests.builder import build_agent
    from felix.tools.builtins import default_tool_provider

    monkeypatch.delenv("FELIX_TEST_PUBLISH_TOKEN", raising=False)
    agent = await build_agent(_manifest(), default_tool_provider(), settings=_settings(ws))
    assert gp.TOOL_NAME not in {t.name for t in agent.tools}
