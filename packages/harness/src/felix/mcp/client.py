"""Outbound MCP client — discover remote tools and bind them as Felix Tools, over the official SDK.

The `mcp` SDK speaks the protocol: Streamable HTTP or stdio, either era (it probes `server/discover`
and falls back to the legacy `initialize` handshake). Felix decides what it may reach: an HTTP
server through an egress-guarded httpx2 client (`safe_httpx2_client`), a stdio server only under the
stdio allowlist (`felix.mcp.stdio`). Every discovery and every call is its own short session.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import httpx2
from felix_ai.types import ImageAttachment
from felix_ai.wire.base import data_url
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import Implementation
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from felix import __version__
from felix.bounded_cache import BoundedCache
from felix.logging_setup import loggable
from felix.manifests.schema import McpServerRef
from felix.manifests.tool_match import matches_any, unmatched_patterns
from felix.observability.metrics import record_counter
from felix.security.ssrf import assert_safe_outbound_url
from felix.timeouts import DEFAULT_CONNECT_TIMEOUT_S, timeout_seconds
from felix.tools.errors import ToolError, ToolErrorCode
from felix.tools.types import Tool, ToolInvocationCtx, ToolOutput, ToolOutputDict, define_tool

logger = logging.getLogger("felix.mcp.client")

DEFAULT_MCP_TIMEOUT_S = 30.0
# A whole session -- handshake, listing pages, the call, closing it -- in multiples of the
# per-request timeout. Each request is bounded on its own; without this a server answering every
# page just in time, or dribbling the session DELETE, held a compile or a call for as long as it liked.
SESSION_BUDGET_FACTOR = 3
# A server's tool listing, page by page; a server that never stops paginating stops here.
MAX_TOOL_PAGES = 20


def _timeout_s(ref: McpServerRef) -> float:
    """Per-server request timeout in seconds."""
    return timeout_seconds(ref.timeout_ms, default_s=DEFAULT_MCP_TIMEOUT_S)


def _headers(auth: str) -> dict[str, str]:
    """The `Authorization` a ref's resolved ``auth`` becomes; the SDK sets the rest."""
    if not auth:
        return {}
    if auth.lower().startswith("bearer ") or auth.lower().startswith("basic "):
        return {"authorization": auth}
    return {"authorization": f"Bearer {auth}"}


def _call_mode(handshake: dict[str, Any]) -> str:
    """How a call should open a session, given what discovery negotiated: straight to the legacy
    handshake for a legacy server, sparing each call the `server/discover` probe it would fail."""
    return "legacy" if handshake.get("protocolVersion") in HANDSHAKE_PROTOCOL_VERSIONS else "auto"


def _leaves(exc: BaseException) -> list[BaseException]:
    """The exceptions an anyio task-group nest is made of, depth first."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def _timed_out() -> ToolError:
    """A slow server, as the tool runner should see it: TIMEOUT. A `RuntimeError` would infer
    INTERNAL, and a `TimeoutError` -- by its name -- USER_ABORTED."""
    return ToolError(ToolErrorCode.TIMEOUT, "MCP request timed out")


def _from_mcp_error(exc: MCPError) -> Exception:
    """A JSON-RPC error as the tool runner should see it.

    The SDK reports its own read timeout as `MCPError(REQUEST_TIMEOUT, "Request '<method>' timed
    out")`, which is `_timed_out()`. Any other error -- a server's own `-32001` included, which is
    Felix's inbound-auth refusal -- names its code and message.
    """
    from mcp_types import REQUEST_TIMEOUT

    if exc.code == REQUEST_TIMEOUT and exc.data is None and _SDK_TIMEOUT.fullmatch(exc.message):
        return _timed_out()
    return RuntimeError(f"MCP error {exc.code}: {exc.message}")


_SDK_TIMEOUT = re.compile(r"Request '[^']*' timed out")


def _the_failure(exc: BaseException) -> BaseException:
    """What one failed session means, out of the `ExceptionGroup` the SDK's task groups wrap it in
    (or as raised, when it was not wrapped). ``exc`` itself when nothing here reclassifies it.

    A server's JSON-RPC error becomes what `_from_mcp_error` makes of it; a transport timeout stays
    a timeout, so `infer_error_code` still says TIMEOUT and retry logic keyed on it is unchanged;
    anything else is its first leaf. The wrapping alone read as "unhandled errors in a TaskGroup",
    with the server's message lost.
    """
    leaves = _leaves(exc)
    for leaf in leaves:
        if isinstance(leaf, MCPError):
            return _from_mcp_error(leaf)
    for leaf in leaves:
        if isinstance(leaf, httpx2.TimeoutException):
            return leaf
        if isinstance(leaf, TimeoutError):
            return _timed_out()
    if isinstance(exc, BaseExceptionGroup):
        for leaf in leaves:
            if isinstance(leaf, Exception):
                return leaf
    return exc


@asynccontextmanager
async def _session(ref: McpServerRef, *, allow_http: bool, mode: str = "auto") -> AsyncIterator[Client]:
    """One SDK client session with ``ref``'s server. A failure leaves unwrapped from the SDK's
    task-group `ExceptionGroup`s and classified by `_the_failure`: a server's JSON-RPC error as a
    `RuntimeError` naming its code and message, as it did before the SDK, and a timeout -- the
    session deadline's included -- as a timeout."""
    wait = _timeout_s(ref)
    client_info = Implementation(name="felix", version=__version__)
    try:
        async with asyncio.timeout(wait * SESSION_BUDGET_FACTOR):
            async with _sdk_client(
                ref, allow_http=allow_http, mode=mode, wait=wait, client_info=client_info
            ) as c:
                yield c
    except BaseException as exc:
        failure = _the_failure(exc)
        if failure is exc:
            raise
        raise failure from None


@asynccontextmanager
async def _sdk_client(
    ref: McpServerRef, *, allow_http: bool, mode: str, wait: float, client_info: Implementation
) -> AsyncIterator[Client]:
    """The SDK client for ``ref``'s transport: stdio under the allowlist, HTTP through the guard."""
    if ref.transport == "stdio":
        from felix.mcp.stdio import stdio_transport

        async with Client(
            stdio_transport(ref),
            mode=mode,
            read_timeout_seconds=wait,
            client_info=client_info,
            cache=None,
        ) as client:
            yield client
        return
    # Syntactic only — scheme, internal names, IP literals. The resolving check is the
    # transport's, which connects to the address it validated.
    assert_safe_outbound_url(ref.url, allow_http=allow_http, resolve=False)
    from felix.security.egress_httpx2 import safe_httpx2_client

    http = safe_httpx2_client(
        allow_http=allow_http,
        timeout=httpx2.Timeout(wait, connect=DEFAULT_CONNECT_TIMEOUT_S),
        headers=_headers(ref.auth),
    )
    async with (
        http,
        Client(
            streamable_http_client(ref.url, http_client=http),
            mode=mode,
            read_timeout_seconds=wait,
            client_info=client_info,
            cache=None,
        ) as client,
    ):
        yield client


async def _tool_pages(client: Client) -> AsyncIterator[list[Any]]:
    """The session's tool listing, page by page, at most `MAX_TOOL_PAGES` of them."""
    cursor: str | None = None
    for _ in range(MAX_TOOL_PAGES):
        page = await client.list_tools(cursor=cursor)
        yield list(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return


async def _list_tools(client: Client) -> list[dict[str, Any]]:
    tools = [
        t.model_dump(by_alias=True, mode="json", exclude_none=True)
        async for page in _tool_pages(client)
        for t in page
    ]
    return [t for t in tools if t.get("name")]


async def _list_until(client: Client, name: str) -> None:
    """Page through the session's tool listing until ``name`` has been seen (or the pages end)."""
    async for page in _tool_pages(client):
        if any(t.name == name for t in page):
            return


async def list_remote_tools(
    ref: McpServerRef,
    *,
    allow_http: bool = False,
    handshake: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The server's tools, as wire dicts. ``handshake``, when given, receives what the session
    negotiated: `protocolVersion`, and the server's `instructions` when it sent any."""
    async with _session(ref, allow_http=allow_http) as client:
        if handshake is not None:
            handshake["protocolVersion"] = client.protocol_version
            if client.instructions:
                handshake["instructions"] = client.instructions
        return await _list_tools(client)


def _bind_remote_tool(
    ref: McpServerRef,
    remote: dict[str, Any],
    *,
    allow_http: bool,
    name_prefix: bool = True,
    mode: str = "auto",
) -> Tool:
    remote_name = str(remote["name"])
    local_name = f"{ref.name}__{remote_name}" if name_prefix else remote_name
    description = str(remote.get("description") or f"MCP tool {remote_name} via {ref.name}")
    schema = (
        remote.get("inputSchema")
        or remote.get("input_schema")
        or {
            "type": "object",
            "properties": {},
        }
    )

    async def handler(args: dict[str, Any], _ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        try:
            return await call(args)
        except ToolError, httpx2.TimeoutException:
            raise  # a timeout, kept a timeout for `infer_error_code`
        except Exception as exc:
            # Returned, not raised: a failed call reads as `[mcp_error]` output, which audit and
            # eval count as a failure (`FAILURE_CONTENT_PREFIXES`) and screening and masking read
            # like any output. Timeouts above stay raised so the runner's TIMEOUT handling sees them.
            return f"[mcp_error] {exc}"

    async def call(args: dict[str, Any]) -> ToolOutput:
        async with _session(ref, allow_http=allow_http, mode=mode) as client:
            # Listed first, in this session: the SDK validates a result against the tool's output
            # schema and, for a tool it has not seen listed, lists *after* the call to find it. A
            # failure there would turn a call whose side effects already happened into an error the
            # model retries. Listing first also gives the SDK the tool's `x-mcp-header` map.
            await _list_until(client, remote_name)
            result = await client.call_tool(remote_name, args or {})
        return _tool_result(
            result.model_dump(by_alias=True, mode="json", exclude_none=True), tool_name=local_name
        )

    return define_tool(
        name=local_name,
        description=description,
        handler=handler,
        raw_input_schema=schema if isinstance(schema, dict) else None,
        source=f"mcp:{ref.name}",
        transport="mcp",
    )


def _tool_result(result: Any, *, tool_name: str = "mcp") -> ToolOutput:
    """A `tools/call` result: its text blocks, and its image blocks as images the model sees.

    An image block used to be dropped, and a result with no text fell through to `str()` of the
    whole result -- base64 in the transcript. As a tool image it is checked here, before the
    screener reads it (`screenable_tool_images`), and then goes the way of any untrusted tool's.
    """
    from felix.tools.tool_images import screenable_tool_images

    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return str(result)
    texts: list[str] = []
    found: list[ImageAttachment] = []
    for block in result["content"]:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            texts.append(str(block["text"]))
        elif block.get("type") == "image" and isinstance(block.get("data"), str) and block["data"]:
            # The label is not trusted: the check reads the type from the bytes.
            found.append(ImageAttachment(url=data_url("application/octet-stream", block["data"])))
    if not texts and not found:
        return str(result)
    images, notes = screenable_tool_images(found, tool_name=tool_name)
    text = "\n".join([*texts, *notes])
    if result.get("isError"):
        # Kept with or without text: an error that returned only an image is still an error.
        text = f"[mcp_error] {text}".rstrip()
    return ToolOutputDict(content=text, attachments=images) if images else text


# Instructions reach the system prompt; a server's is at most this long there.
MAX_INSTRUCTIONS_CHARS = 1000


def server_guidance(ref: McpServerRef, handshake: dict[str, Any], *, manifest_id: str = "") -> str:
    """The line of tool guidance a server's ``instructions`` become, or "" for none.

    Only when the manifest opted in for this server. Collapsed to one line and capped, because
    it is one entry in a list, and dropped whole when the injection markers flag it: this text
    is written by the server and read as the system prompt, which no tool output ever is.
    """
    raw = handshake.get("instructions") if ref.use_instructions else None
    if not isinstance(raw, str) or not raw.strip():
        return ""
    from felix.governance.content_screening import _INJECTION

    text = " ".join(raw.split())
    if any(rx.search(text) for rx in _INJECTION):
        logger.warning("MCP server %s: instructions flagged by the injection markers; not used", ref.name)
        record_counter("felix_mcp_instructions", {"manifest_id": manifest_id, "outcome": "flagged"})
        return ""
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        text = text[: MAX_INSTRUCTIONS_CHARS - 1].rstrip() + "…"
    record_counter("felix_mcp_instructions", {"manifest_id": manifest_id, "outcome": "used"})
    return f"{ref.name} (from the server): {text}"


def _allowlist_patterns(ref: McpServerRef) -> list[str]:
    """`ref.tools` as patterns over the *remote* names.

    Every other `tools:` list in a manifest — approvals, policies, screening — is written over
    the *bound* name, `github__issue_write`. An author copying a name from the approval block
    into this list would otherwise bind nothing and get a warning. Accept both spellings: a
    pattern carrying this server's own prefix is the same pattern without it.
    """
    prefix = f"{ref.name}__"
    return [p[len(prefix) :] if p.startswith(prefix) else p for p in ref.tools]


# A server's `initialize` result and `tools/list`, remembered briefly. The agent is compiled per
# request, and discovery was two round trips per server on every one of them -- a process spawn
# per RPC for stdio -- before the first token. Keyed by a digest of the whole ref (URL or
# command, its resolved auth, timeouts) and `allow_http`, never the raw credential; two refs
# that hash alike reach the same server as the same caller. Failures are not remembered.
DISCOVERY_TTL_S = 60.0
_DISCOVERY_CONCURRENCY = 8
_DISCOVERY = BoundedCache(256, ttl_s=DISCOVERY_TTL_S)


def clear_discovery_cache() -> None:
    _DISCOVERY.clear()


def _discovery_key(ref: McpServerRef, allow_http: bool, identity: str = "") -> str:
    """`identity` (a person's `tenant|person`, for `auth: person:<provider>`) stands in for the
    token: a person's token changes at each refresh, and their tool list does not."""
    dumped = ref.model_dump_json(exclude={"auth"}) if identity else ref.model_dump_json()
    raw = f"{dumped}|{allow_http}|{identity}".encode()
    return hashlib.sha256(raw).hexdigest()


async def _discover(
    ref: McpServerRef, *, allow_http: bool, identity: str = ""
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """`list_remote_tools` for one server, through the discovery cache. Copies out, so a
    caller's edits never reach the next compile."""
    key = _discovery_key(ref, allow_http, identity)
    hit = _DISCOVERY.get(key)
    if hit is not None:
        return copy.deepcopy(hit)
    handshake: dict[str, Any] = {}
    remotes = await list_remote_tools(ref, allow_http=allow_http, handshake=handshake)
    # Only an answer with tools is remembered: a server that failed raised, and one that answered
    # with none may have tools a moment later, so caching it would leave it toolless for the TTL.
    if remotes:
        _DISCOVERY[key] = copy.deepcopy((remotes, handshake))
    return remotes, handshake


@dataclass(frozen=True)
class McpBinding:
    """What binding a manifest's MCP servers produced: the tools, the servers that could not be
    reached or started, which bound none (`felix.run_sources` says so to the model and the run), and
    the servers that answered and bound none -- they list no tools, or none their `tools:` allowlist
    matches -- which a `required: true` server may not do either."""

    tools: list[Tool]
    unavailable: list[str]
    empty: list[str] = field(default_factory=list)


async def tools_from_mcp_servers(
    refs: list[McpServerRef],
    *,
    allow_http: bool = False,
    manifest_id: str = "",
    identities: Sequence[str] | None = None,
) -> list[Tool]:
    """`bind_mcp_servers`' tools alone."""
    return (
        await bind_mcp_servers(refs, allow_http=allow_http, manifest_id=manifest_id, identities=identities)
    ).tools


async def bind_mcp_servers(
    refs: list[McpServerRef],
    *,
    allow_http: bool = False,
    manifest_id: str = "",
    identities: Sequence[str] | None = None,
) -> McpBinding:
    """Discover and bind tools from each MCP server ref. `identities`, by position, names the
    person whose token a server carries (`person_auth`), so their discovery is cached as theirs.

    Servers are asked concurrently: one slow server used to delay every server after it, and
    the compile waits for all of them either way. Binding keeps the manifest's server order.
    A server that cannot be reached binds nothing rather than failing the compile, and is named
    in the binding's `unavailable`.
    """
    # Bounded: a manifest may name up to `MAX_REFS` servers, and each discovery is a connection
    # or a process spawn.
    gate = asyncio.Semaphore(_DISCOVERY_CONCURRENCY)

    who = list(identities) if identities is not None else [""] * len(refs)

    async def discover(ref: McpServerRef, identity: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        async with gate:
            return await _discover(ref, allow_http=allow_http, identity=identity)

    found = await asyncio.gather(
        *(discover(r, i) for r, i in zip(refs, who, strict=True)), return_exceptions=True
    )
    out: list[Tool] = []
    unavailable: list[str] = []
    empty: list[str] = []
    for ref, identity, result in zip(refs, who, found, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result
            if identity:
                # A person's token is masked per compile, not in the process-wide list the log
                # formatter knows; an error that echoed it would reach the log whole.
                logger.warning(
                    "failed to list MCP tools from %s error=%s",
                    loggable(ref.name, limit=100),
                    type(result).__name__,
                )
            else:
                logger.warning(
                    "failed to list MCP tools from %s", loggable(ref.name, limit=100), exc_info=result
                )
            unavailable.append(ref.name)
            continue
        remotes, handshake = result
        if ref.tools:
            patterns = _allowlist_patterns(ref)
            names = [str(r["name"]) for r in remotes]
            missing = unmatched_patterns(patterns, names)
            if missing:
                # The server renamed or dropped a tool the manifest names. Not fatal — the
                # rest still bind — but the manifest author wrote that name for a reason, and
                # it is the same inert-rule shape `builder.py` counts for approvals and
                # policies, so it lands on the same counter an operator already watches.
                logger.warning("MCP server %s lists no tool matching %s", ref.name, missing)
                record_counter(
                    "felix_rule_targets_nothing",
                    {"manifest_id": manifest_id, "kind": "mcp_allowlist", "rule": ref.name},
                )
            remotes = [r for r in remotes if matches_any(patterns, str(r["name"]))]
        guidance = server_guidance(ref, handshake, manifest_id=manifest_id)
        bound = len(out)
        for remote in remotes:
            try:
                tool = _bind_remote_tool(ref, remote, allow_http=allow_http, mode=_call_mode(handshake))
                # One line per server; the prompt section lists an identical line once.
                out.append(replace(tool, prompt_guidance=guidance) if guidance else tool)
            except Exception:
                logger.debug("skip remote tool %s from %s", remote.get("name"), ref.name, exc_info=True)
        if len(out) == bound:
            empty.append(ref.name)
    return McpBinding(tools=out, unavailable=unavailable, empty=empty)


__all__ = [
    "DEFAULT_MCP_TIMEOUT_S",
    "McpBinding",
    "bind_mcp_servers",
    "list_remote_tools",
    "tools_from_mcp_servers",
]
