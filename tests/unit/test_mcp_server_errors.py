"""What an MCP client is told when `tools/list` or `tools/call` fails.

The server relayed `str(exc)` -- whatever a driver, a path or a compile happened to say -- to the
caller as the JSON-RPC error message, and a tool's own raise as `error: <exc>` content. Both are the
operator's business: the client gets a fixed message with the request id, and the log gets the
traceback. A manifest name the caller sent that does not resolve is still named back to it.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from felix.mcp import server
from felix_api.composition import compose

from tests.support.factories import make_settings

# Internal detail a client must not see; not credential-shaped, so the secret scan stays quiet.
LEAK = "relation felix_internal_events on db-internal.svc"


async def _call(settings: Any, method: str, **params: Any) -> dict[str, Any]:
    return await server.handle_rpc(
        settings=settings, tools=compose(settings), method=method, params=params, rpc_id=1
    )


@pytest.mark.parametrize("method", ["tools/list", "tools/call"])
# A `KeyError` is a `LookupError` too: only a manifest that does not resolve is named back.
@pytest.mark.parametrize("raised", [RuntimeError, KeyError])
async def test_a_compile_failure_is_not_relayed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, method: str, raised: type[Exception]
) -> None:
    async def boom(**_: Any) -> Any:
        raise raised(LEAK)

    monkeypatch.setattr(server, "_compiled_tools", boom)
    with caplog.at_level(logging.ERROR, logger="felix.mcp.server"):
        out = await _call(make_settings(), method, name="calculator")
    assert out["error"]["code"] == -32000
    assert out["error"]["message"].startswith("internal error")
    assert LEAK not in str(out)
    logged = [r.exc_info for r in caplog.records if r.exc_info]
    assert logged and logged[0][0] is raised, "the cause was not logged"


async def test_a_raising_tool_is_not_relayed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _Raises:
        async def execute(self, args: Any, ctx: Any) -> Any:
            raise OSError(f"/srv/felix/{LEAK}")

        transport = "builtin"

    tool = SimpleNamespace(name="leaky", description="", executor=_Raises(), args_schema=None, fatal=False)

    async def compiled(**_: Any) -> Any:
        return [tool], object()

    async def passthrough(_manifest: Any, args: dict[str, Any], _settings: Any) -> dict[str, Any]:
        return args

    monkeypatch.setattr(server, "_compiled_tools", compiled)
    monkeypatch.setattr("felix.governance.tool_screening.screen_tool_arguments", passthrough)
    with caplog.at_level(logging.ERROR):
        out = await _call(make_settings(), "tools/call", name="leaky", arguments={})
    assert out["result"]["isError"] is True
    text = out["result"]["content"][0]["text"]
    # Named by its code, then the fixed message with the request id: `ToolRunner.for_client`.
    assert text.startswith("[error/") and "internal error" in text, text
    assert LEAK not in str(out)
    logged = [r.exc_info for r in caplog.records if r.exc_info]
    assert logged and logged[0][0] is OSError, "the tool's raise was not logged"


async def test_an_unknown_manifest_is_still_named() -> None:
    out = await _call(make_settings(), "tools/list", manifest="no-such-agent")
    assert out["error"] == {"code": -32004, "message": "Unknown manifest: no-such-agent"}


@pytest.mark.parametrize("method", ["tools/list", "tools/call"])
async def test_a_refusal_written_for_the_caller_is_relayed(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """The same rule as the HTTP API (`felix.errors.client_safe_message`): a type whose message is
    written for a client keeps it, so a manifest author learns which secret ref to drop."""
    from felix.secrets import SecretNotPermittedError

    detail = "secret:DOCS_TOKEN is not permitted for this tenant's manifests"

    async def refused(**_: Any) -> Any:
        raise SecretNotPermittedError(detail)

    monkeypatch.setattr(server, "_compiled_tools", refused)
    out = await _call(make_settings(), method, name="calculator")
    assert out["error"] == {"code": -32000, "message": detail}
