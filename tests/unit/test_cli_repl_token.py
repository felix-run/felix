"""`felix chat` picks its bearer: `--token` wins, else the login saved for `--base`, else none.

Driven through the `felix` Typer app, so the argument parsing and the header it builds are the
ones a person gets. The REPL sends one line, then input ends with an `EOFError`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from felix_client.login import LoginToken, save_token

_BASE = "https://felix.test"


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
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers)
        return httpx.Response(200, json={"final": {"role": "assistant", "content": "hi"}})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    # One line, so one turn reaches the server, then the end of input.
    lines = iter(["hello"])

    def one_line(_: str = "") -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr("builtins.input", one_line)
    return headers


def _run(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    from felix_cli.main import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["chat", *args])
    assert result.exit_code == 0, result.output


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
