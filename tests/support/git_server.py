"""A bare repository served over git's dumb HTTP protocol, for the checkout tests.

`serve` (the `git_server` fixture in `tests/conftest.py`) serves `acme/widgets.git` (two commits on main) from a local thread, points
`felix.repos.checkouts.CLONE_BASE` at it, and records the `Authorization` header of every request
— so a test can see the token reach the server and then check it was kept nowhere.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from felix.repos import checkouts


def _git(cwd: Path, *args: str, bare: bool = False) -> str:
    """git through `tests/support/git_fixture.py`, immune to an ambient GIT_DIR / GIT_WORK_TREE. A bare
    repository is its own git directory, so `bare` points GIT_DIR at `cwd` itself."""
    from tests.support.git_fixture import git

    extra = {"GIT_DIR": str(cwd), "GIT_WORK_TREE": str(cwd)} if bare else None
    return git(cwd, "-c", "user.name=t", "-c", "user.email=t@t", *args, extra=extra)


class _Server:
    def __init__(self, root: Path) -> None:
        self.headers: list[str] = []
        recorder = self

        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self) -> None:
                recorder.headers.append(self.headers.get("Authorization", ""))
                super().do_GET()

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Server]:
    """`acme/widgets.git` with two commits on main, served the way a dumb HTTP remote is."""
    work = tmp_path / "src"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "README.md").write_text("hello\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "one")
    (work / "app.py").write_text("print(1)\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "two")
    served = tmp_path / "served"
    (served / "acme").mkdir(parents=True)
    _git(work, "clone", "-q", "--bare", str(work), str(served / "acme" / "widgets.git"))
    _git(served / "acme" / "widgets.git", "update-server-info", bare=True)
    srv = _Server(served)
    monkeypatch.setattr(checkouts, "CLONE_BASE", srv.base)
    yield srv
    srv.httpd.shutdown()
