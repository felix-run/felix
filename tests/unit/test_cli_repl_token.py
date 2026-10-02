"""`clients/cli.py` picks its bearer: `--token` wins, else the login saved for `--base`, else none.

Driven through `main()` with `sys.argv`, so the argument parsing and the header it builds are the
ones a person gets. The REPL loop is ended at the first prompt by an `EOFError`.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from felix_client.login import LoginToken, save_token

_REPL = Path(__file__).resolve().parents[2] / "clients" / "cli.py"
_BASE = "https://felix.test"


def _load_repl() -> Any:
    spec = importlib.util.spec_from_file_location("felix_repl_under_test", _REPL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[httpx.Headers]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_token(
        LoginToken(
            access_token="saved-token",
            tenant="acme",
            scopes=(),
            expires_at=time.time() + 3600,
            base_url=_BASE,
        )
    )
    headers: list[httpx.Headers] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers)
        return httpx.Response(200, json={"status": "ok"})

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    def no_input(_: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr("builtins.input", no_input)
    return headers


def _run(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["cli.py", *args])
    _load_repl().main()


def test_the_saved_login_for_this_server_is_sent(
    sent: list[httpx.Headers], monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(monkeypatch, "--base", _BASE)
    assert sent[0]["authorization"] == "Bearer saved-token"


def test_an_explicit_token_wins(sent: list[httpx.Headers], monkeypatch: pytest.MonkeyPatch) -> None:
    _run(monkeypatch, "--base", _BASE, "--token", "explicit")
    assert sent[0]["authorization"] == "Bearer explicit"


def test_another_servers_saved_login_is_not_sent(
    sent: list[httpx.Headers], monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(monkeypatch, "--base", "https://elsewhere.test")
    assert "authorization" not in sent[0]
