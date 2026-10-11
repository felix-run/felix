"""POST /chat, /chat/stream, steer/follow-up, fork/rewind — REST + SSE agent surface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from felix.auth.mgmt import SCOPE_APPROVALS_READ, holds_mgmt_scopes
from felix.context import AuthContext, RequestContext, async_run_with_context, get_context, try_get_context
from felix.governance.inbound import INBOUND_SCREENED_EXTRA
from felix.governance.screening import InboundScreeningError
from felix.idempotency import (
    IdempotencyConflict,
    IdempotencyStore,
    StoredResponse,
    once,
    principal_scope,
    request_fingerprint,
    valid_key,
)
from felix.logging_setup import loggable
from felix.manifests.schema import PermissionModeName
from felix.patterns.model import ModelGatewayError
from felix.patterns.types import ChatMessage, InvokeInput
from felix.runtime import build_tenant_agent, prepare_tenant_invoke, resolve_tenant_manifest
from felix.session.snapshot import gather_thread_snapshot
from felix.session.store import get_session_store
from felix.session.tree import APPEND_ORIGIN_EXTRA, get_leaf, stored_leaf, sync_leaf
from felix.session.types import GetEventsOpts
from felix.steer import enqueue
from felix.thread_ids import effective_thread_id
from felix.tools.client_bridge import MAX_TOOL_CALL_ID
from felix.ui.ask_user import LIVE_STREAM_EXTRA
from felix.workspace_files import TREE_DEFAULT_LIMIT, TREE_MAX_LIMIT
from pydantic import BaseModel, Field, field_validator, model_validator

from felix_api.errors import client_safe_message, log_gateway_error
from felix_api.routes._sse import (
    DONE,
    HEARTBEAT,
    KEEP_ALIVE,
    error_frame,
    frame,
    is_resume_point,
    sse_response,
    with_heartbeat,
)
from felix_api.routes._streaming import (
    durable_run_gen,
    replay_stream_gen,
    request_events,
    resume_stream_gen,
    stream_cursor,
)

logger = logging.getLogger("felix_api.routes.chat")

router = APIRouter(tags=["Threads"])


def _http_from_invoke_prep(exc: Exception) -> HTTPException | None:
    from felix.governance.inbound import InboundScreeningError
    from felix.manifests.inbound_auth import InboundAuthError
    from felix.manifests.loader import ManifestParseError
    from felix.manifests.pin import ManifestDriftError

    # This decides the status code; `client_safe_message` decides the wording. They
    # were the same decision here and in three other shapes across two modules, which
    # is how a message that was safe in one place got copied to one where it was not.
    if isinstance(exc, InboundAuthError | InboundScreeningError):
        return HTTPException(status_code=exc.status_code, detail=client_safe_message(exc))
    if isinstance(exc, ManifestDriftError):
        return HTTPException(status_code=409, detail=client_safe_message(exc))
    from felix.durability.fibers import RunInProgress

    if isinstance(exc, RunInProgress):
        # The enqueue's own check, which a send racing another past `_refuse_if_run_in_flight`
        # reaches: the advisory lock admitted one of them.
        return HTTPException(status_code=409, detail=f"run_in_progress:{exc.resume_token}")
    from felix_ai.providers.base import ProviderConfigError

    if isinstance(exc, ProviderConfigError):
        # The manifest is fine; the deployment has not configured the provider its model
        # routes to. Unavailable, like a missing secret, not a server fault.
        return HTTPException(status_code=503, detail=client_safe_message(exc))
    from felix.secrets import SecretNotFoundError

    if isinstance(exc, SecretNotFoundError):
        # A manifest's `secret:` ref names a secret this deployment does not hold.
        return HTTPException(status_code=503, detail=client_safe_message(exc))
    if isinstance(exc, ValueError) and str(exc).startswith(
        ("unknown checkpointer", "memory.checkpointer is")
    ):
        # A manifest stored before `memory.checkpointer` was implemented can name a
        # value that is now rejected — `agentcore`, `sqlite`, `do` were all inert.
        # `PUT /manifests` refuses new ones, but existing rows only fail here, and
        # unmapped that is a 500 with a traceback on every request for the manifest.
        return HTTPException(status_code=422, detail=client_safe_message(exc, authored_for_clients=True))
    from felix.durability.webhooks import WebhookEndpointError

    if isinstance(exc, WebhookEndpointError):
        # The manifest names an endpoint this deployment has not registered for the tenant.
        return HTTPException(status_code=422, detail=client_safe_message(exc, authored_for_clients=True))
    if isinstance(exc, ManifestParseError):
        # Same shape one step earlier: a row stored before a schema tightening no longer
        # validates. `PUT /manifests` refuses new ones with a 400; without this the existing
        # rows answer 500 "internal error" on every request, which is indistinguishable from
        # an outage and sends the operator looking for one.
        return HTTPException(status_code=422, detail=client_safe_message(exc))
    return None


class ChatRequest(BaseModel):
    model_config = {"extra": "forbid"}

    manifest: str = Field(description="Manifest name to invoke.")
    messages: list[dict[str, Any]] = Field(default_factory=list)
    thread_id: str | None = Field(
        default=None,
        description="Optional thread-id suffix; server prefixes the tenant id.",
    )
    model: str | None = Field(
        default=None,
        description="Optional mid-session model override (allowlisted against manifest fallbacks).",
    )
    # Named prompt from manifest ``spec.prompts``; expands into a user message.
    template: str | None = None
    template_args: list[str] = Field(default_factory=list)


class SteerRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: Literal["steer", "follow_up"] = "steer"


def _one_line_path(value: str | None) -> str | None:
    """A workspace path a note may quote: the path is quoted into text the model reads, and a
    control character -- a newline above all -- would let it carry a line of its own."""
    if value is not None and any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("path must not contain control characters")
    return value


class WorkspaceEditedRequest(BaseModel):
    """The operator changed a workspace file directly. Structure only: the server writes the note."""

    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    path: str = Field(min_length=1, max_length=4096, description="Workspace path the operator changed.")
    op: Literal["write", "delete", "rename"] = "write"
    bytes: int | None = Field(default=None, ge=0, description="Size after a `write`. Not sent for `delete`.")
    to_path: str | None = Field(
        default=None, min_length=1, max_length=4096, description="New path. Required for `rename`, only."
    )

    @field_validator("path", "to_path")
    @classmethod
    def _one_line(cls, value: str | None) -> str | None:
        return _one_line_path(value)

    @model_validator(mode="after")
    def _shape_matches_op(self) -> WorkspaceEditedRequest:
        if self.op == "rename" and self.to_path is None:
            raise ValueError("to_path is required for op 'rename'")
        if self.op != "rename" and self.to_path is not None:
            raise ValueError("to_path is only accepted for op 'rename'")
        if self.op == "delete" and self.bytes is not None:
            raise ValueError("bytes is not accepted for op 'delete'")
        return self


class WorkspaceEditedOut(BaseModel):
    status: Literal["queued", "recorded"] = Field(
        description="`queued`: a run is in flight and reads the note before its next model call. "
        "`recorded`: no run is, so the note was appended to the thread for the next one."
    )
    thread_id: str
    event_id: str | None = Field(default=None, description="The session entry's id, when `recorded`.")


# The read and write caps the workspace tools hold a model to, so the file pane can open exactly
# what an agent can and save nothing an agent's `read_file` could not read back whole.
WORKSPACE_FILE_MAX_BYTES = 512_000


class WorkspaceWriteRequest(BaseModel):
    """Replace one workspace file with the operator's text, from the file pane."""

    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    path: str = Field(min_length=1, max_length=4096, description="Workspace path to write.")
    content: str = Field(description=f"UTF-8 text, at most {WORKSPACE_FILE_MAX_BYTES:,} bytes encoded.")
    expected_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="The digest `GET /chat/workspace/file` returned. When set, the write is refused "
        "(409 `workspace_changed`) unless the file still has it; a missing file never does.",
    )
    manifest: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="The manifest a thread that has never run will run under. Ignored once it has.",
    )

    @field_validator("path")
    @classmethod
    def _one_line(cls, value: str) -> str:
        return _one_line_path(value) or value


class WorkspaceDeleteRequest(BaseModel):
    """Remove one workspace file, from the file pane."""

    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    path: str = Field(min_length=1, max_length=4096, description="Workspace path of the file to delete.")
    expected_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="The digest `GET /chat/workspace/file` returned. When set, the delete is refused "
        "(409 `workspace_changed`) unless the file still has it.",
    )
    manifest: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="The manifest a thread that has never run will run under. Ignored once it has.",
    )

    @field_validator("path")
    @classmethod
    def _one_line(cls, value: str) -> str:
        return _one_line_path(value) or value


class WorkspaceRenameRequest(BaseModel):
    """Move one workspace file to another path in the same workspace, from the file pane."""

    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    path: str = Field(min_length=1, max_length=4096, description="Workspace path of the file to move.")
    to_path: str = Field(
        min_length=1,
        max_length=4096,
        description="Where it goes. Must not exist; missing directories on the way are made.",
    )
    expected_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="The digest `GET /chat/workspace/file` returned. When set, the rename is refused "
        "(409 `workspace_changed`) unless the file still has it.",
    )
    manifest: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="The manifest a thread that has never run will run under. Ignored once it has.",
    )

    @field_validator("path", "to_path")
    @classmethod
    def _one_line(cls, value: str) -> str:
        return _one_line_path(value) or value


class WorkspaceTreeEntryOut(BaseModel):
    path: str
    type: Literal["file", "dir"]
    bytes: int | None = Field(default=None, description="A file's size. Absent for a directory.")


class WorkspaceTreeOut(BaseModel):
    root_kind: Literal["scoped", "checkout"] = Field(
        description="`checkout`: the thread's repository checkout, whatever the manifest's scope. "
        "`scoped`: the manifest's `spec.workspace.scope` directory."
    )
    scope: Literal["thread", "tenant", "deployment"] | None = Field(
        description="The manifest's workspace scope; null when a checkout overrides it."
    )
    manifest: str | None = Field(
        description="The manifest whose scope decided the workspace; null when none resolved "
        "(the thread is then on `thread` scope, the narrowest)."
    )
    entries: list[WorkspaceTreeEntryOut]
    truncated: bool


class WorkspaceFileOut(BaseModel):
    path: str
    bytes: int
    sha256: str = Field(description="Of the bytes, whatever the encoding. Send it back as `expected_sha256`.")
    encoding: Literal["utf-8", "base64"]
    content: str


class WorkspaceWriteOut(BaseModel):
    status: Literal["queued", "recorded"] = Field(
        description="Where the edit note went: as `/workspace/edited`."
    )
    path: str = Field(description="The path written, normalised as the workspace tools report it.")
    bytes: int
    sha256: str
    event_id: str | None = Field(default=None, description="The note's session entry, when `recorded`.")


class WorkspaceDeleteOut(BaseModel):
    status: Literal["queued", "recorded"] = Field(
        description="Where the delete note went: as `/workspace/edited`."
    )
    path: str = Field(description="The path deleted, normalised as the workspace tools report it.")
    event_id: str | None = Field(default=None, description="The note's session entry, when `recorded`.")


class WorkspaceRenameOut(BaseModel):
    status: Literal["queued", "recorded"] = Field(
        description="Where the rename note went: as `/workspace/edited`."
    )
    path: str = Field(description="The path the file was at, normalised as the workspace tools report it.")
    to_path: str = Field(description="The path it is at now, normalised the same way.")
    sha256: str | None = Field(
        description="Of the file's bytes, which the move did not change: send it back as `expected_sha256` "
        "at the new path. Null for a file over the 512,000-byte read cap."
    )
    event_id: str | None = Field(default=None, description="The note's session entry, when `recorded`.")


class WorkspaceChangedOut(BaseModel):
    detail: Literal["workspace_changed"]
    sha256: str | None = Field(
        description="The file's digest now; null when it is missing, or (with `bytes`) over the read cap."
    )
    bytes: int | None = Field(default=None, description="The file's size now; null when it is missing.")


class WorkspaceErrorOut(BaseModel):
    detail: str


_WORKSPACE_READ_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {
        "model": WorkspaceErrorOut,
        "description": "`invalid_thread_id`, `invalid_path` (absolute, escaping, through a symlink), "
        "`reserved_path` (inside `.git` or `.felix-scopes`), `not_a_file`.",
    },
    404: {
        "model": WorkspaceErrorOut,
        "description": "`not_found`: no such file. "
        "`unknown_manifest`: the `manifest` named does not resolve.",
    },
    409: {
        "model": WorkspaceErrorOut,
        "description": "`workspace_unavailable`: the thread's checkout is cloning, failed or expired, "
        "or its scope is one this tenant may not use.",
    },
    503: {
        "model": WorkspaceErrorOut,
        "description": "`workspace_not_configured` (no FELIX_WORKSPACE_ROOT), `workspace_unavailable` "
        "(the hosted workspace gateway did not answer).",
    },
}


class ToolResultRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    # Capped for the same reason `thread_id` is: both are interpolated into a waiter name,
    # which becomes a Redis key held for an hour. See `client_bridge.MAX_TOOL_CALL_ID`.
    tool_call_id: str = Field(min_length=1, max_length=MAX_TOOL_CALL_ID)
    content: str | dict[str, Any] | list[Any] = ""
    error: bool = False


class ForkRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1, description="Source thread suffix.")
    new_thread_id: str = Field(
        min_length=1,
        description="Destination thread suffix. Must name a thread that does not exist yet (409 otherwise).",
    )
    from_event_id: str | None = None


class RewindRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    summarize: bool | None = None
    instructions: str | None = None
    manifest: str | None = None


class SessionNameRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=256)


class PermissionModeRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    mode: PermissionModeName


class ThinkingRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    thinking_level: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class AbortRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)


class ContinueRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    manifest: str = Field(min_length=1)
    model: str | None = None


class CompactRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    manifest: str = Field(min_length=1)
    instructions: str | None = None


class AskRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=8000)


class LabelRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    label: str | None = None


class FeedbackRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    # `None` clears a rating, the way a `None` label clears a label.
    rating: Literal["up", "down"] | None = None
    note: str = Field(default="", max_length=1000)


class CustomEntryRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    content: str = ""
    # When true, the entry is included in model context (custom_message semantics).
    in_context: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    role: Literal["user", "assistant", "system"] = "system"


class LeaseRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    holder_id: str = Field(min_length=1, max_length=128)
    mode: Literal["exclusive", "shared"] = "exclusive"
    ttl_seconds: float = Field(default=300.0, ge=5, le=86400)
    token: str | None = None


class LeaseReleaseRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    holder_id: str | None = None
    token: str | None = None


class ChatRefusalOut(BaseModel):
    """A refusal from a chat route: `detail` is a stable code, as every chat refusal's is."""

    detail: str


class LeaseObserverOut(BaseModel):
    holder_id: str
    expires_at: int = Field(description="Epoch milliseconds when this observer's own hold lapses.")


class LeaseStatusOut(BaseModel):
    locked: bool = Field(description="An exclusive holder is driving the thread.")
    attached: bool = Field(description="Anyone holds the thread, exclusively or as an observer.")
    holder_id: str | None = Field(description="The exclusive holder; null when only observers hold it.")
    mode: Literal["exclusive", "shared"] | None
    observers: int = Field(description="How many observer holds are live.")
    observer_holds: list[LeaseObserverOut] = Field(description="Every live observer, by holder id.")
    expires_at: int | None = Field(
        description="Epoch ms: the exclusive hold's expiry, or the last observer's when there is none."
    )
    token_hint: str | None = Field(description="The exclusive hold's token prefix.")


class LeaseAcquireOut(BaseModel):
    ok: bool
    renewed: bool = Field(description="This holder already had this hold; its expiry was extended.")
    token: str = Field(description="This hold's own token. An observer's is never the exclusive one.")
    mode: Literal["exclusive", "shared"] = Field(description="The mode this hold was granted in.")
    held_by_other: bool = Field(
        description="Another holder drives the thread: a `shared` hold granted here is read-only."
    )
    status: LeaseStatusOut
    snapshot: dict[str, Any]


class LeaseReleaseOut(BaseModel):
    ok: bool
    released: bool
    status: LeaseStatusOut
    snapshot: dict[str, Any]


# A client that holds a lease may present its token on the routes that drive a thread, and
# the server then refuses it unless the token is the exclusive hold's: an observer's is
# `409 lease_read_only`, one whose hold another holder has since taken is `409 lease_held`.
# Optional by default. Leases were advisory before this header existed, and a caller that
# never took one -- a script, the OpenAI surface, an older client -- keeps working as it did.
# `FELIX_LEASE_ENFORCE=strict` makes it binding: without the header a driving request is
# refused while another holder has the thread exclusively (`lease.driving_refusal`).
# Checked on every route below that starts a turn, answers one, or writes the thread's log or
# settings. Not on `/chat/fork`, which only reads its source (forking is how an observer takes
# its own branch), nor on `/chat/sessions/feedback`: it writes the rating into thread metadata
# and the audit log, but appends nothing to the session log and moves no leaf -- a rating of a
# reply, not a turn, so an observer may give one.
LEASE_TOKEN_HEADER = "x-felix-lease-token"
LeaseToken = Annotated[
    str | None,
    Header(
        alias=LEASE_TOKEN_HEADER,
        description="This caller's session-lease token. When present, the request is refused "
        "(409 `lease_read_only` / `lease_held`) unless it is the thread's exclusive hold.",
    ),
]


# The refusal every route that checks `X-Felix-Lease-Token` can answer, said once.
LEASE_REFUSALS: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ChatRefusalOut,
        "description": "`lease_read_only`: the `X-Felix-Lease-Token` presented is an observer's. "
        "`lease_held`: another holder has the thread exclusively. Only sent when the header is, "
        "unless `FELIX_LEASE_ENFORCE=strict`, where a request without it is `lease_held` too.",
    }
}

# `POST /chat` starts a turn, so it can also be refused for the run already on the thread.
CHAT_REFUSALS: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ChatRefusalOut,
        "description": LEASE_REFUSALS[409]["description"]
        + " `run_in_progress:<resume_token>`: the thread has a durable run in flight; watch it at "
        "`GET /chat/runs/{resume_token}` and send once it has finished.",
    }
}


async def _refuse_if_run_in_flight(request: Request, tenant_id: str, thread: str | None) -> None:
    """`409 run_in_progress:<resume_token>` when `thread` has a durable run in flight.

    A second send would run beside it -- durable or not, it appends to the same log -- and
    neither run sees the other's work until a whole tool batch lands, so each re-does it
    (felix-run/felix#529). The token is the run to watch instead. Refused before a transient
    turn as well as a durable one; the durable enqueue checks again under a per-thread lock,
    so two sends racing past this cannot both start a run.

    Called *after* an `Idempotency-Key` is judged, never before: a resend of the message that
    started the run is answered by reattaching to it, not by this refusal.
    """
    if thread is None:
        return
    from felix.durability.runs import active_durable_run

    active = await active_durable_run(request.app.state.settings, tenant_id, thread)
    if active is not None:
        raise HTTPException(status_code=409, detail=f"run_in_progress:{active['resume_token']}")


async def _refuse_unless_driver(request: Request, thread: str | None, lease_token: str | None) -> None:
    """409 when the caller may not drive `thread`: see `lease.driving_refusal`."""
    from felix.session.lease import driving_refusal

    refusal = await driving_refusal(
        thread, lease_token, enforce=getattr(request.app.state.settings, "lease_enforce", "advisory")
    )
    if refusal:
        raise HTTPException(status_code=409, detail=refusal)


class UiResponseRequest(BaseModel):
    model_config = {"extra": "forbid"}

    # Required: the prompt's waiter is scoped to its thread, which is what ties an answer to
    # the tenant that was asked. The `ui_request` frame carries it.
    thread_id: str = Field(min_length=1)
    # A server-minted `token_urlsafe(12)` is 16 characters; the cap bounds the waiter key a
    # caller can make the server hold, as `MAX_TOOL_CALL_ID` does for tool results.
    request_id: str = Field(min_length=1, max_length=64)
    value: Any = None
    cancelled: bool = False
    note: str = ""


def _auth_from_request(request: Request) -> AuthContext:
    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth
    return AuthContext()


def _caller_messages(raw: list[dict[str, Any]]) -> list[ChatMessage]:
    """The request's messages, parsed, with images only where a caller's own can be: user turns."""
    from felix.patterns.model_vision import caller_images_on_user_turns
    from felix_ai.types import MessageFormatError

    try:
        parsed = [ChatMessage.model_validate(m) for m in raw]
    except MessageFormatError as exc:
        # The caller's mistake, said as one: before, this was a 500 with a stack trace.
        raise HTTPException(status_code=422, detail=f"messages: {client_safe_message(exc)}") from exc
    return caller_images_on_user_turns(parsed)


def _refuse_unseeable_images(
    manifest: Any, model_id: str | None, messages: list[ChatMessage], settings: Any
) -> None:
    """422 for a turn carrying an image that no route of this agent can see.

    Before the agent is built and before a stream opens, so the person who attached the
    picture hears that it cannot be read rather than getting an answer about something else.
    """
    from felix.patterns.model_vision import unseeable_image_problem

    problem = unseeable_image_problem(manifest, messages, settings, model_id)
    if problem:
        raise HTTPException(status_code=422, detail=problem)


def _allowlisted_model(manifest: Any, model_id: str | None, settings: Any = None) -> str | None:
    if not model_id:
        return None
    from felix.config import DEFAULT_MODEL_ROUTES, get_settings

    spec = getattr(getattr(manifest, "spec", None), "model", None)
    primary = getattr(spec, "id", None)
    fallbacks = list(getattr(spec, "fallbacks", None) or [])
    allowed: set[str] = set(fallbacks)
    if primary:
        allowed.add(primary)
    cfg = settings or get_settings()
    allowed.add(cfg.default_model_id)
    allowed.update(DEFAULT_MODEL_ROUTES.keys())
    # Also accept provider/model keys present in FELIX_MODEL_ROUTES overrides via parse.
    try:
        from felix.patterns.model import parse_model_routes

        allowed.update(parse_model_routes(cfg).keys())
    except Exception:
        pass
    if model_id not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"model_not_allowlisted:{model_id}",
        )
    return model_id


async def _apply_template(
    messages: list[ChatMessage],
    *,
    manifest: Any,
    template: str | None,
    template_args: list[str],
    settings: Any,
    tenant_id: str,
    object_store: Any | None = None,
) -> list[ChatMessage]:
    if not template:
        return messages
    from felix.prompts import expand_named_prompt

    try:
        text = await expand_named_prompt(
            manifest,
            template,
            template_args,
            object_store=object_store,
            workspace_root=getattr(settings, "workspace_root", None),
            tenant_id=tenant_id,
        )
    except LookupError as exc:
        # Names the template the caller asked for, which the caller supplied.
        raise HTTPException(
            status_code=404, detail=client_safe_message(exc, authored_for_clients=True)
        ) from exc
    return [*messages, ChatMessage(role="user", content=text)]


IDEMPOTENCY_HEADER = "idempotency-key"


@router.post("", responses=CHAT_REFUSALS)
@router.post("/", responses=CHAT_REFUSALS)
async def chat(body: ChatRequest, request: Request, lease_token: LeaseToken = None) -> Any:
    """Run a turn. With an `Idempotency-Key`, run it once per key per principal.

    A client that times out and retries otherwise runs the turn twice — two model
    calls, two usage rows, two session events. The first request claims the key and
    stores its response on completion; a retry with the same key and body gets that
    response back with `Idempotent-Replayed: true`; the same key with a different body
    is `422 idempotency_key_reused`; a retry while the first is still running is
    `409 idempotency_in_progress`. A failed attempt releases the key so the retry runs.
    """
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if body.thread_id and thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    # Before the idempotency claim: an observer's request is refused, not stored as the key's answer.
    await _refuse_unless_driver(request, thread, lease_token)
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if key is None:
        status, payload = await _chat_turn(body, request)
        return JSONResponse(payload, status_code=status)
    if not valid_key(key):
        raise HTTPException(status_code=400, detail="invalid_idempotency_key")

    async def run() -> StoredResponse:
        status, payload = await _chat_turn(body, request)
        return StoredResponse(status, payload)

    try:
        response, replayed = await once(
            request.app.state.idempotency_store,
            principal_scope(auth.tenant_id, auth.principal_sub, skill_owner=auth.skill_owner),
            key,
            request_fingerprint("/chat", body.model_dump(mode="json")),
            run,
        )
    except IdempotencyConflict as exc:
        status = 409 if exc.kind == "in_progress" else 422
        raise HTTPException(
            status_code=status, detail=f"idempotency_{'in_progress' if status == 409 else 'key_reused'}"
        ) from exc
    headers = {"idempotent-replayed": "true"} if replayed else None
    return JSONResponse(response.body, status_code=response.status, headers=headers)


async def _chat_turn(body: ChatRequest, request: Request) -> tuple[int, dict[str, Any]]:
    """The turn itself: `(202, accepted)` for a durable manifest, `(200, result)` otherwise."""
    settings = request.app.state.settings
    tools = request.app.state.tools
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if body.thread_id and thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    if not (body.manifest or "").strip():
        raise HTTPException(status_code=400, detail="manifest_required")
    # Inside the turn `once` runs, so a refusal frees the key rather than becoming its answer.
    await _refuse_if_run_in_flight(request, auth.tenant_id, thread)

    try:
        resolved = await resolve_tenant_manifest(settings, auth.tenant_id, body.manifest, thread_id=thread)
        await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        if isinstance(exc, (LookupError, ValueError)):
            raise HTTPException(status_code=404, detail=f"unknown_manifest:{body.manifest}") from exc
        raise
    model_id = _allowlisted_model(resolved.manifest, body.model, settings)
    messages = _caller_messages(body.messages)
    messages = await _apply_template(
        messages,
        manifest=resolved.manifest,
        template=body.template,
        template_args=body.template_args,
        settings=settings,
        tenant_id=auth.tenant_id,
        object_store=getattr(request.app.state, "object_store", None),
    )
    if not messages:
        raise HTTPException(status_code=400, detail="messages_or_template_required")
    _refuse_unseeable_images(resolved.manifest, model_id, messages, settings)
    try:
        from felix.governance.inbound import apply_inbound_screening

        messages = await apply_inbound_screening(resolved.manifest, messages, settings)
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        raise
    execution = getattr(getattr(resolved.manifest, "spec", None), "execution", None)
    if execution is not None and getattr(execution, "mode", "transient") == "durable":
        from felix.durability.runs import start_durable_chat
        from felix.manifests.pin import pin_fields_for

        try:
            payload = await start_durable_chat(
                settings,
                auth.tenant_id,
                manifest_id=body.manifest,
                messages=messages,
                thread_id=thread,
                model_id=model_id,
                execution=execution,
                # With the sub-agent digest: a durable run carrying stored authority is pinned on
                # resume whatever the manifest says, so its children are part of what it runs.
                pin=await pin_fields_for(
                    settings, auth.tenant_id, resolved.manifest, version=resolved.version
                ),
            )
        except Exception as exc:
            http = _http_from_invoke_prep(exc)
            if http is not None:
                raise http from exc
            raise
        return 202, payload

    req_ctx = RequestContext(
        settings=settings,
        auth=auth,
        manifest_id=body.manifest,
        thread_id=thread,
        # Screened above, before the stream opened or the durable run was enqueued.
        extras={INBOUND_SCREENED_EXTRA: True},
    )
    async with async_run_with_context(req_ctx):
        try:
            agent = await build_tenant_agent(
                settings,
                manifest=resolved.manifest,
                sub_agents=resolved.sub_agents,
                tools=tools,
                tenant_id=auth.tenant_id,
                skill_owner=auth.skill_owner,
            )
            result = await agent.invoke(
                InvokeInput(
                    messages=messages,
                    thread_id=thread,
                    model_id=model_id,
                    tenant_id=auth.tenant_id,
                )
            )
        except ModelGatewayError as exc:
            # `body` is an upstream response: neither trusted nor single-line.
            # CodeQL does not flag it -- its taint source is the network rather than
            # the request -- but a gateway body is shaped by prompt content, which
            # makes it as forgeable as anything the client sends directly.
            log_gateway_error(logger, exc)
            raise HTTPException(status_code=502, detail=client_safe_message(exc)) from exc
        except Exception as exc:
            http = _http_from_invoke_prep(exc)
            if http is not None:
                raise http from exc
            raise

    final = result.final
    return 200, {
        "messages": [m.model_dump() for m in result.messages],
        "final": final.model_dump() if hasattr(final, "model_dump") else final,
        "thread_id": thread,
        "model": model_id,
        "leaf_id": get_leaf(thread) if thread else None,
        "approvals": await _approvals_raised(settings, auth.tenant_id, req_ctx.extras),
    }


async def _approvals_raised(settings: Any, tenant_id: str, extras: dict[str, Any]) -> list[dict[str, Any]]:
    """Every approval this run asked for, with how it ended.

    A non-streaming caller has no `approval_required` frame to read, and used to wait out the
    rule's TTL and receive a denial with nothing saying an approval had been requested. Each
    entry is the frame the stream would have sent, plus `status` read back from the row once
    the run is over: `approved`, `denied`, or `expired` — nobody answered in time. The gate now
    closes that row as `denied` with the note `timeout`, and `expired` is kept for it rather than
    folding it into `denied`, because nobody chose it; a pending row past its deadline (one left
    by a harness that predates the write-back) still reads the same way. While a request is
    still blocked, `GET /approvals?thread_id=` is where to find the id to decide.
    """
    from felix.approvals.store import TIMEOUT_NOTE, get_approval
    from felix.side_events import requested_on

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    now = int(time.time() * 1000)
    for raised in requested_on(extras, "approval_required"):
        approval_id = str(raised.get("approval_id") or "")
        if not approval_id or approval_id in seen:
            continue
        seen.add(approval_id)
        row = await get_approval(settings, tenant_id, approval_id)
        status = str((row or {}).get("status") or "unknown")
        expires_at = (row or {}).get("expires_at")
        timed_out = status == "denied" and (row or {}).get("decision_note") == TIMEOUT_NOTE
        if timed_out or (status == "pending" and expires_at is not None and int(expires_at) < now):
            status = "expired"
        out.append({**raised, "status": status})
    return out


def _safe_filename(thread_id: str) -> str:
    """A thread id reduced to characters that cannot escape a quoted header parameter.

    The id is interpolated into `filename="..."`. `effective_thread_id` rejects `:` and `#`,
    which makes header splitting look unreachable — but it permits `"`, and one quote ends
    the parameter early and starts attacker-controlled header text. Allowlist rather than
    escape: a filename has no need of anything outside this set.
    """
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in thread_id)[:128] or "session"


@router.get("/runs/{resume_token}")
async def chat_run(resume_token: str, request: Request) -> dict[str, Any]:
    """Poll a durable chat fiber started with ``spec.execution.mode: durable``."""
    from felix.durability.runs import get_durable_run

    auth = _auth_from_request(request)
    row = await get_durable_run(request.app.state.settings, auth.tenant_id, resume_token)
    if row is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return row


@router.get("/stream/{thread_id}")
async def chat_stream_resume(request: Request, thread_id: str) -> StreamingResponse:
    """Reattach to a thread after a dropped connection or a page refresh.

    A client that loses `POST /chat/stream` mid-turn previously had nothing to come
    back to: no `id:` on the frames, no route to reconnect to, and the run itself torn
    down on disconnect — deliberately, so a hung-up client does not keep burning
    tokens. This does not change that. The old run is still gone; what a client gets
    back is the thread as it now stands, and then anything that lands afterwards.

    Cold reconnect (no `Last-Event-ID`) opens with a `snapshot` frame carrying the
    transcript. A warm one replays only the session events after that cursor. Both
    then tail the session log, which is shared state, so this works regardless of
    which replica served the original turn.

    It also announces what the thread's run is blocked on -- `tool_request` for a client
    tool, and `approval_required` for a caller with `approvals:read` -- each once per
    stream, and stays open past the idle limit while a durable run is in flight.
    """
    settings = request.app.state.settings
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")

    header = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    try:
        after = int(header) if header not in (None, "") else None
    except ValueError:
        after = None

    poll = float(getattr(settings, "stream_resume_poll_seconds", 1.0) or 1.0)
    return sse_response(
        resume_stream_gen(
            settings=settings,
            tenant_id=auth.tenant_id,
            thread=thread,
            after=after,
            poll=poll,
            poll_max=max(poll, float(getattr(settings, "stream_resume_poll_max_seconds", 10.0) or 10.0)),
            idle_limit=float(getattr(settings, "stream_resume_idle_seconds", 300.0) or 300.0),
            # As on `POST /chat/stream`: the thread is a name the caller chose, so what its run
            # is blocked on is read only by a caller `GET /approvals` would answer.
            may_read_approvals=holds_mgmt_scopes(settings, auth.scopes, SCOPE_APPROVALS_READ),
        )
    )


# `POST /chat/stream` under an `Idempotency-Key`: one turn per key, whatever the client retries.
#
# A streamed send that failed in the client -- the network dropped, a proxy answered 5xx -- may
# or may not have reached the turn, and the client cannot tell which. Resent with the same key,
# it never runs a second turn or appends a second user message:
#
# - the first request is still streaming: `409 idempotency_in_progress`. The thread is the
#   resume token -- `GET /chat/stream/{thread_id}` reattaches to it as to any dropped stream;
# - the first request is over: the events *it* appended -- stamped with its origin, so nothing
#   else the thread got meanwhile -- are replayed (`replay_stream_gen`), then its error frame if
#   it ended in one, with `Idempotent-Replayed: true`, and nothing runs. A durable run is
#   reattached to instead (`durable_run_gen`), since its run outlives any one request;
# - the first request appended nothing of its own (it failed before the turn began, or the
#   client left before the body ran): the key is released, so the retry runs;
# - the same key with a different body: `422 idempotency_key_reused`.
#
# Scoped to the principal *and the thread*, with `FELIX_IDEMPOTENCY_TTL_SECONDS`, in the store
# `POST /chat` uses. The key is claimed after the request has passed inbound auth and screening,
# so a retry is judged as the first request was before it is told anything. (No 422 entry: it
# would replace the validation error's schema; `idempotency_key_reused` is in the docstring.)
STREAM_IDEMPOTENCY_REFUSALS: dict[int | str, dict[str, Any]] = {
    400: {
        "model": ChatRefusalOut,
        "description": "`invalid_idempotency_key`, or `idempotency_key_requires_thread_id`: a streamed "
        "send is deduplicated per thread, so an `Idempotency-Key` needs a `thread_id`.",
    },
    409: {
        "model": ChatRefusalOut,
        "description": "`lease_read_only` / `lease_held`: the `X-Felix-Lease-Token` presented does not "
        "drive the thread (or, under `FELIX_LEASE_ENFORCE=strict`, none was and another holder does). "
        "`idempotency_in_progress`: a request with this `Idempotency-Key` is still streaming; "
        "reattach with `GET /chat/stream/{thread_id}`. "
        "`run_in_progress:<resume_token>`: the thread has a durable run in flight; watch it at "
        "`GET /chat/runs/{resume_token}` and send once it has finished.",
    },
}


@dataclass(slots=True)
class _HeldStreamKey:
    """A `POST /chat/stream` that won its key, and settles it exactly once when the stream ends.

    ``origin`` stamps every event the turn appends (`tree.APPEND_ORIGIN_EXTRA`), which is how
    the settle and a later replay tell this request's events from anything else the thread got
    meanwhile. ``error`` is the stream's own error frame, if it ended in one, kept for the replay.
    """

    store: IdempotencyStore
    scope: str
    key: str
    token: str
    thread: str
    from_seq: int | None
    origin: str = field(default_factory=lambda: secrets.token_hex(12))
    error: dict[str, Any] | None = None
    settled: bool = False

    async def settle(self, settings: Any, tenant_id: str) -> None:
        """Store the key's answer, or free it. Once: later calls return at once.

        Two paths reach this -- the body's own end (`guard`) and the response's
        (`sse_response(on_close=...)`, for a client gone before the body ran) -- and the second
        must not overwrite the first, or release a key the first stored.

        This request appended nothing: the turn never began, so the key is released and a
        resend runs. It appended anything -- a whole turn, or the user message of one a
        disconnect tore down -- and the key is stored, so a resend replays that rather than
        sending the message again. A log that cannot be read counts as appended: replaying too
        little beats a second turn.
        """
        if self.settled:
            return
        self.settled = True
        try:
            mine = await request_events(settings, tenant_id, self.thread, self.origin, self.from_seq)
            appended = bool(mine)
        except Exception:
            logger.warning(
                "idempotent stream: reading thread=%s failed", loggable(self.thread), exc_info=True
            )
            appended = True
        if not appended:
            await self.store.release(self.scope, self.key, self.token)
            return
        record = {"mode": "turn", "thread_id": self.thread, "from_seq": self.from_seq, "origin": self.origin}
        if self.error:
            record["error"] = self.error
        await self.store.finish(self.scope, self.key, self.token, StoredResponse(200, record))

    async def guard(self, settings: Any, tenant_id: str, stream: AsyncGenerator[str]) -> AsyncIterator[str]:
        """``stream``, noting an error frame, and settling the key when it ends.

        The inner stream is closed before the settle, so the turn it drives has stopped
        appending when its events are counted; both shielded, since a disconnect cancels this.
        """
        try:
            async for chunk in stream:
                if chunk.startswith("event: error"):
                    self.error = _error_of(chunk)
                yield chunk
        finally:

            async def close_then_settle() -> None:
                try:
                    await stream.aclose()
                finally:
                    await self.settle(settings, tenant_id)

            await asyncio.shield(close_then_settle())


def _error_of(chunk: str) -> dict[str, Any] | None:
    """The `{"message", "type"}` of an `error_frame`, read back for a replay to re-emit."""
    for line in chunk.splitlines():
        if line.startswith("data: "):
            try:
                error = json.loads(line[len("data: ") :]).get("error")
            except ValueError, AttributeError:
                return None
            return dict(error) if isinstance(error, dict) else None
    return None


async def _claim_stream_key(
    request: Request, auth: AuthContext, thread: str, key: str, body: ChatRequest
) -> _HeldStreamKey | StreamingResponse:
    """Claim ``key`` for this streamed send, or answer the resend of one that already ran."""
    settings = request.app.state.settings
    store: IdempotencyStore = request.app.state.idempotency_store
    caller = principal_scope(auth.tenant_id, auth.principal_sub, skill_owner=auth.skill_owner)
    scope = f"{caller}#stream#{thread}"
    try:
        claim = await store.claim(
            scope, key, request_fingerprint("/chat/stream", body.model_dump(mode="json"))
        )
    except IdempotencyConflict as exc:
        # The Redis store raises rather than answers when the key keeps changing under it.
        status = 409 if exc.kind == "in_progress" else 422
        raise HTTPException(
            status_code=status, detail=f"idempotency_{'in_progress' if status == 409 else 'key_reused'}"
        ) from exc
    if claim.kind == "in_progress":
        raise HTTPException(status_code=409, detail="idempotency_in_progress")
    if claim.kind == "mismatch":
        raise HTTPException(status_code=422, detail="idempotency_key_reused")
    if claim.kind == "replay" and claim.stored is not None:
        stored = claim.stored.body
        replayed = {"idempotent-replayed": "true"}
        if stored.get("mode") == "durable":
            return sse_response(
                durable_run_gen(
                    settings=settings,
                    tenant_id=auth.tenant_id,
                    accepted=dict(stored.get("accepted") or {}),
                    from_seq=stored.get("from_seq"),
                    may_read_approvals=holds_mgmt_scopes(settings, auth.scopes, SCOPE_APPROVALS_READ),
                ),
                headers=replayed,
            )
        return sse_response(
            replay_stream_gen(
                settings=settings,
                tenant_id=auth.tenant_id,
                thread=thread,
                origin=str(stored.get("origin") or ""),
                from_seq=stored.get("from_seq"),
                error=stored.get("error"),
            ),
            headers=replayed,
        )
    # Read before the turn can append: it bounds the scan for this request's events.
    return _HeldStreamKey(
        store=store,
        scope=scope,
        key=key,
        token=claim.token,
        thread=thread,
        from_seq=await stream_cursor(settings, auth.tenant_id, thread),
    )


@router.post("/stream", responses=STREAM_IDEMPOTENCY_REFUSALS)
async def chat_stream(
    body: ChatRequest, request: Request, lease_token: LeaseToken = None
) -> StreamingResponse:
    """Run a turn as an SSE stream. With an `Idempotency-Key` and a `thread_id`, once per key.

    A resend under the same key never runs a second turn: while the first is still streaming it
    is `409 idempotency_in_progress` (reattach with `GET /chat/stream/{thread_id}`); after, it
    replays the events the first request itself appended as `session_event` frames, then the
    first stream's `event: error` frame if it ended in one, then `[DONE]`, with
    `Idempotent-Replayed: true` -- or reattaches to the durable run it started. A first request
    that appended nothing of its own frees the key. The same key with a different body is
    `422 idempotency_key_reused`. Scoped to the principal and the thread.
    """
    settings = request.app.state.settings
    tools = request.app.state.tools
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if body.thread_id and thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if key is not None:
        if not valid_key(key):
            raise HTTPException(status_code=400, detail="invalid_idempotency_key")
        if thread is None:
            # A threadless stream writes no log, so there is nothing a retry could be answered from.
            raise HTTPException(status_code=400, detail="idempotency_key_requires_thread_id")
    if not (body.manifest or "").strip():
        raise HTTPException(status_code=400, detail="manifest_required")
    if key is None:
        # Before the manifest is compiled and the input screened: there is no key whose
        # resend this could be, so nothing to judge first.
        await _refuse_if_run_in_flight(request, auth.tenant_id, thread)

    try:
        resolved = await resolve_tenant_manifest(settings, auth.tenant_id, body.manifest, thread_id=thread)
        await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        if isinstance(exc, (LookupError, ValueError)):
            raise HTTPException(status_code=404, detail=f"unknown_manifest:{body.manifest}") from exc
        raise
    model_id = _allowlisted_model(resolved.manifest, body.model, settings)
    messages = _caller_messages(body.messages)
    messages = await _apply_template(
        messages,
        manifest=resolved.manifest,
        template=body.template,
        template_args=body.template_args,
        settings=settings,
        tenant_id=auth.tenant_id,
        object_store=getattr(request.app.state, "object_store", None),
    )
    if not messages:
        raise HTTPException(status_code=400, detail="messages_or_template_required")
    _refuse_unseeable_images(resolved.manifest, model_id, messages, settings)
    try:
        from felix.governance.inbound import apply_inbound_screening

        messages = await apply_inbound_screening(resolved.manifest, messages, settings)
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        raise
    held: _HeldStreamKey | None = None
    if key is not None and thread is not None:
        claimed = await _claim_stream_key(request, auth, thread, key, body)
        if isinstance(claimed, StreamingResponse):
            return claimed
        held = claimed
        # After the claim: a resend of the message that started the run was answered above,
        # by reattaching. Anything else on a thread with a run in flight is a second run.
        try:
            await _refuse_if_run_in_flight(request, auth.tenant_id, thread)
        except HTTPException:
            held.settled = True
            await held.store.release(held.scope, held.key, held.token)
            raise
    execution = getattr(getattr(resolved.manifest, "spec", None), "execution", None)
    if execution is not None and getattr(execution, "mode", "transient") == "durable":
        from felix.durability.runs import start_durable_chat
        from felix.manifests.pin import pin_fields_for

        # Captured *before* the enqueue, not inside the stream: the fiber may be claimed
        # and start appending the moment the row lands, and a cursor read after that has
        # already skipped the turns the stream exists to report.
        #
        # `if thread else 0` rather than letting `stream_cursor` answer for both: it
        # returns None for "no thread" *and* for "the head read failed", and those want
        # opposite handling. A run with no thread of its own gets a freshly minted fiber
        # thread, which is empty, so 0 is exactly right; a failed read means the start
        # point is unknown, and `durable_tail` declines to tail rather than replaying the
        # thread's entire history as this run's progress.
        from_seq = await stream_cursor(settings, auth.tenant_id, thread) if thread else 0
        try:
            accepted = await start_durable_chat(
                settings,
                auth.tenant_id,
                manifest_id=body.manifest,
                messages=messages,
                thread_id=thread,
                model_id=model_id,
                execution=execution,
                # With the sub-agent digest: a durable run carrying stored authority is pinned on
                # resume whatever the manifest says, so its children are part of what it runs.
                pin=await pin_fields_for(
                    settings, auth.tenant_id, resolved.manifest, version=resolved.version
                ),
            )
        except Exception as exc:
            if held is not None:
                held.settled = True
                await held.store.release(held.scope, held.key, held.token)
            http = _http_from_invoke_prep(exc)
            if http is not None:
                raise http from exc
            raise
        if held is not None:
            # Settled at once: the run is enqueued and outlives this request, so a retry is
            # answered by reattaching to it, never by a second enqueue.
            held.settled = True
            record = {"mode": "durable", "accepted": accepted, "from_seq": from_seq}
            await held.store.finish(held.scope, held.key, held.token, StoredResponse(200, record))
        return sse_response(
            durable_run_gen(
                settings=settings,
                tenant_id=auth.tenant_id,
                accepted=accepted,
                from_seq=from_seq,
                # Gated on the *management* scope, not on this route's auth. `thread_id` is
                # client-supplied, so the run's thread is a question the caller chose rather
                # than one they necessarily own — nothing in Felix binds a thread to a
                # principal, and `GET /chat/stream/{thread_id}` demonstrates that already.
                # Without the check, a chat-scoped caller could name any thread in the tenant
                # and read the tool names, arguments and gate reasons it is blocked on,
                # which `GET /approvals` would have refused them. A caller without the scope
                # gets the transcript and the answer, exactly as before this existed.
                may_read_approvals=holds_mgmt_scopes(settings, auth.scopes, SCOPE_APPROVALS_READ),
            )
        )

    req_ctx = RequestContext(
        settings=settings,
        auth=auth,
        manifest_id=body.manifest,
        thread_id=thread,
        # Screened above, before the stream opened or the durable run was enqueued.
        # `LIVE_STREAM_EXTRA`: a person is reading this stream, so `ask_user` may ask them.
        # `APPEND_ORIGIN_EXTRA`: under an `Idempotency-Key`, what this turn appends is stamped,
        # so the key's settle and a resend's replay find exactly this request's events.
        extras={
            INBOUND_SCREENED_EXTRA: True,
            LIVE_STREAM_EXTRA: True,
            **({APPEND_ORIGIN_EXTRA: held.origin} if held is not None else {}),
        },
    )

    async def event_gen():

        try:
            async with async_run_with_context(req_ctx):
                agent = await build_tenant_agent(
                    settings,
                    manifest=resolved.manifest,
                    sub_agents=resolved.sub_agents,
                    tools=tools,
                    tenant_id=auth.tenant_id,
                    skill_owner=auth.skill_owner,
                )
                stream = agent.stream_events(
                    InvokeInput(
                        messages=messages,
                        thread_id=thread,
                        model_id=model_id,
                        tenant_id=auth.tenant_id,
                    )
                )
                cursor: int | None = None
                # Closed with this generator, not left to the garbage collector: a resend's key
                # is settled once this closes, and the turn the heartbeat pump drives must have
                # stopped appending by then.
                async with contextlib.aclosing(with_heartbeat(stream)) as events:
                    async for event in events:
                        if event is HEARTBEAT:
                            # A comment frame keeps proxies and load balancers from closing
                            # an idle connection during a long tool call; clients ignore it.
                            yield KEEP_ALIVE
                            continue
                        payload = event.model_dump() if hasattr(event, "model_dump") else event
                        # `id:` is the session log's own cursor, so it still means
                        # something to the *next* connection — a per-connection counter
                        # would not. Only structural frames carry one: they are the points
                        # a reconnect can resume from, and they are rare, where deltas
                        # arrive per token and would cost a query each. Frames without an
                        # `id:` leave `lastEventId` untouched, which is exactly the
                        # semantics wanted here.
                        if thread and is_resume_point(str(payload.get("event") or "")):
                            # Re-read rather than trust the cached value: a structural
                            # frame is where an append may just have happened. Consecutive
                            # structural frames with nothing appended between them return
                            # the same number, which is correct and costs one small query.
                            fresh = await stream_cursor(settings, auth.tenant_id, thread)
                            if fresh is not None:
                                cursor = fresh
                            yield frame(payload, cursor=cursor)
                        else:
                            yield frame(payload)
        except asyncio.CancelledError:
            # The client hung up. Nothing to send; let the cancellation propagate so the
            # run is torn down instead of continuing to burn model tokens.
            raise
        except ModelGatewayError as exc:
            # Typed like the non-streaming 502, with the upstream body kept to the log.
            log_gateway_error(logger, exc)
            yield error_frame(client_safe_message(exc), kind="model_gateway_error")
        except InboundScreeningError as exc:
            # Refused inside the run, after the 200 -- a `user_prompt_submit` hook: typed, so a
            # client can show it as a refusal rather than an outage.
            refusal = str(getattr(exc, "code", "content_screening_denied"))
            yield error_frame(client_safe_message(exc), kind=refusal)
        except Exception as exc:
            # Without this the body simply stopped under an already-sent 200 OK, with no
            # error event and no [DONE] — the client could not tell success from failure.
            logger.exception("chat stream failed thread=%s", loggable(thread, limit=80))
            yield error_frame(client_safe_message(exc))
        yield DONE

    if held is not None:
        return sse_response(
            held.guard(settings, auth.tenant_id, event_gen()),
            on_close=lambda: held.settle(settings, auth.tenant_id),
        )
    return sse_response(event_gen())


@router.post("/steer", responses=LEASE_REFUSALS)
async def chat_steer(body: SteerRequest, request: Request, lease_token: LeaseToken = None) -> dict[str, Any]:
    """Queue a steer (interrupt remaining tools) or follow-up (after idle) message."""
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    return await enqueue(auth.tenant_id, thread, kind=body.kind, text=body.text)


@router.post("/workspace/edited", responses=LEASE_REFUSALS)
async def chat_workspace_edited(
    body: WorkspaceEditedRequest, request: Request, lease_token: LeaseToken = None
) -> WorkspaceEditedOut:
    """Tell the agent the operator edited, deleted or renamed a workspace file directly.

    A run in flight reads it before its next model call, without cancelling any tool call
    (`status: queued`, and a `workspace_note` frame on that run's stream); otherwise it is
    appended to the thread for the next run (`status: recorded`). Either way it lands in the
    session log once, as an in-context `custom` entry with `metadata.type: workspace_edit`.
    The text the model reads is written by the server from these fields.
    """
    from felix.workspace_notes import WorkspaceNote

    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    note = WorkspaceNote(path=body.path, op=body.op, bytes=body.bytes, to_path=body.to_path)
    status, event_id = await _deliver_workspace_note(request, auth.tenant_id, thread, note)
    return WorkspaceEditedOut(status=status, thread_id=thread, event_id=event_id)


async def _deliver_workspace_note(
    request: Request, tenant_id: str, thread: str, note: Any
) -> tuple[Literal["queued", "recorded"], str | None]:
    """Queue `note` for the run in flight on `thread`, or append it for the next one.

    The one path `/workspace/edited` and the file pane's write, delete and rename all take, so a
    change from the pane reaches the agent exactly as a client's own report of one does.
    """
    from felix.session.tree import annotate_and_append
    from felix.session.types import AppendableEvent
    from felix.workspace_notes import enqueue_if_running

    if await enqueue_if_running(tenant_id, thread, note):
        return "queued", None
    ids = await annotate_and_append(
        get_session_store(request.app.state.settings, tenant_id=tenant_id).open(thread),
        [AppendableEvent(kind="custom", role="user", content=note.text(), metadata=note.metadata())],
        sync=True,
    )
    return "recorded", ids[-1] if ids else None


def _workspace_refusal(exc: Exception, *, missing_dir_is_404: bool = True) -> HTTPException | None:
    """The HTTP answer for a workspace operation that raised `exc`, or None for one it does not map.

    Codes only, never the exception's text: a "workspace_root" message names directories on the
    host (`workspace_root does not exist: /srv/...`), which a client has no business reading.
    """
    from felix.tools.workspace import NotAFileError
    from felix.tools.workspace_hosted import GatewayUnavailable

    if isinstance(exc, NotAFileError | IsADirectoryError):
        return HTTPException(status_code=400, detail="not_a_file")
    if isinstance(exc, GatewayUnavailable | TimeoutError):
        return HTTPException(status_code=503, detail="workspace_unavailable")
    if isinstance(exc, ValueError):
        message = str(exc)
        if message.startswith("workspace_root") and "not configured" in message:
            return HTTPException(status_code=503, detail="workspace_not_configured")
        if message.startswith("workspace_root"):
            return HTTPException(status_code=409, detail="workspace_unavailable")
        return HTTPException(status_code=400, detail="invalid_path")
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail="not_found")
    if isinstance(exc, NotADirectoryError):
        # A component on the way is a file: nothing is at the path to read, and nothing can be put there.
        return HTTPException(
            status_code=404 if missing_dir_is_404 else 400,
            detail="not_found" if missing_dir_is_404 else "invalid_path",
        )
    if isinstance(exc, OSError):
        logger.warning("workspace file operation failed: %s", type(exc).__name__, exc_info=True)
        return HTTPException(status_code=500, detail="workspace_io_error")
    return None


async def _thread_workspace(
    request: Request, thread_id: str, manifest: str | None
) -> tuple[AuthContext, Any]:
    """The caller's tenant and the workspace `thread_id` works in (`felix.workspace_files`)."""
    from felix.workspace_files import UnknownManifest, resolve_thread_workspace

    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    try:
        workspace = await resolve_thread_workspace(
            request.app.state.settings, auth.tenant_id, thread, manifest
        )
    except UnknownManifest:
        raise HTTPException(status_code=404, detail="unknown_manifest") from None
    except ValueError as exc:
        refusal = _workspace_refusal(exc)
        assert refusal is not None
        raise refusal from None
    return auth, workspace


def _pane_parts(path: str) -> list[str]:
    """`path` as the tools would walk it; 400 for one they refuse or the pane may not touch."""
    from felix.tools.workspace import workspace_parts
    from felix.tools.workspace_backend import pane_hides

    try:
        parts = workspace_parts(path)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_path") from None
    if not parts:
        raise HTTPException(status_code=400, detail="not_a_file")
    if any(pane_hides(part) for part in parts):
        raise HTTPException(status_code=400, detail="reserved_path")
    return parts


_PaneThread = Annotated[
    str, Query(min_length=1, description="The thread's id suffix, as everywhere on /chat.")
]
_PaneManifest = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=256,
        description="The manifest a thread that has never run will run under. Ignored once it has: "
        "the manifest its newest turn ran under decides the workspace.",
    ),
]


@router.get("/workspace/tree", responses={200: {"model": WorkspaceTreeOut}, **_WORKSPACE_READ_ERRORS})
async def chat_workspace_tree(
    request: Request,
    thread_id: _PaneThread,
    limit: int = Query(default=TREE_DEFAULT_LIMIT, ge=1, le=TREE_MAX_LIMIT),
    manifest: _PaneManifest = None,
) -> dict[str, Any]:
    """Every file and directory in the thread's workspace, for the operator's file pane.

    The workspace is the one the agent's next turn works in: the thread's repository checkout
    when it has one (`root_kind: checkout`), otherwise the directory its manifest's
    `spec.workspace.scope` names (`root_kind: scoped`). Recursive, in pre-order and case-folded
    name order, following no symlink (a link is not listed); `.git` and `.felix-scopes` are left
    out. `truncated` when the walk stopped short of the whole tree -- the limit, a directory too
    large to read whole, very deep nesting, or its time budget. A workspace nothing has written to
    yet lists no entries rather than failing.
    """
    from felix.tools.workspace_backend import get_workspace_backend

    _auth, workspace = await _thread_workspace(request, thread_id, manifest)
    backend = get_workspace_backend(request.app.state.settings)
    try:
        tree = await backend.tree(workspace.scope, limit)
    except (ValueError, OSError) as exc:
        refusal = _workspace_refusal(exc)
        if refusal is None:
            raise
        raise refusal from None
    return {
        "root_kind": workspace.root_kind,
        "scope": workspace.scope_name,
        "manifest": workspace.manifest,
        "entries": tree.entries,
        "truncated": tree.truncated,
    }


@router.get(
    "/workspace/file",
    response_model=WorkspaceFileOut,
    responses={
        **_WORKSPACE_READ_ERRORS,
        413: {"model": WorkspaceErrorOut, "description": "`too_large`: over the 512,000-byte read cap."},
    },
)
async def chat_workspace_file(
    request: Request,
    thread_id: _PaneThread,
    path: Annotated[str, Query(min_length=1, max_length=4096)],
    manifest: _PaneManifest = None,
) -> WorkspaceFileOut:
    """One workspace file, whole, from the workspace `GET /chat/workspace/tree` lists.

    `encoding: utf-8` when the bytes decode cleanly, otherwise `base64`. `sha256` is of the bytes;
    send it back as `expected_sha256` on `POST /chat/workspace/write` to refuse a save over a file
    the agent has changed since. Files over the workspace tools' read cap (512,000 bytes) are
    refused whole rather than cut.
    """
    import base64
    import hashlib

    from felix.tools.workspace_backend import get_workspace_backend

    _pane_parts(path)
    _auth, workspace = await _thread_workspace(request, thread_id, manifest)
    backend = get_workspace_backend(request.app.state.settings)
    try:
        read = await backend.read_file(workspace.scope, path, 0, WORKSPACE_FILE_MAX_BYTES)
    except (ValueError, OSError) as exc:
        refusal = _workspace_refusal(exc)
        if refusal is None:
            raise
        raise refusal from None
    if read.size > WORKSPACE_FILE_MAX_BYTES or len(read.data) > WORKSPACE_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="too_large")
    data = read.data
    try:
        content, encoding = data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        content, encoding = base64.b64encode(data).decode("ascii"), "base64"
    return WorkspaceFileOut(
        path=read.path,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        encoding=encoding,  # type: ignore[arg-type]
        content=content,
    )


@router.post(
    "/workspace/write",
    response_model=WorkspaceWriteOut,
    responses={
        **_WORKSPACE_READ_ERRORS,
        409: {
            "model": WorkspaceChangedOut,
            "description": "`workspace_changed` (body carries the file's `sha256` and `bytes` now): "
            "`expected_sha256` no longer matches, or the file is gone. Nothing was written. Also "
            "`workspace_unavailable` as on the reads, and the lease refusals "
            "(`lease_read_only`, `lease_held`) as on `/chat/steer`.",
        },
        413: {
            "model": WorkspaceErrorOut,
            "description": "`too_large`: `content` is over 512,000 bytes encoded.",
        },
    },
)
async def chat_workspace_write(
    body: WorkspaceWriteRequest, request: Request, lease_token: LeaseToken = None
) -> Any:
    """Replace one workspace file with the operator's text, and tell the agent.

    Written to the same workspace `GET /chat/workspace/tree` lists and `GET /chat/workspace/file`
    reads. With `expected_sha256` the write is conditional: compared under the same per-path lock
    the agent's own writes take in this process, and refused with `409 workspace_changed` when the
    file is not what the caller read. The file is replaced whole and atomically, keeping its mode.

    Then the agent is told as `POST /chat/workspace/edited` would tell it (`op: write`): a run in
    flight reads the note before its next model call (`status: queued`), otherwise it is appended
    for the next run (`status: recorded`). Audited as `workspace_write`. Lease-guarded like
    `/chat/steer`.
    """
    from felix.tools.workspace_backend import WorkspaceChanged, get_workspace_backend
    from felix.workspace_notes import WorkspaceNote

    settings = request.app.state.settings
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    data = body.content.encode("utf-8")
    if len(data) > WORKSPACE_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="too_large")
    _pane_parts(body.path)
    auth, workspace = await _thread_workspace(request, body.thread_id, body.manifest)
    backend = get_workspace_backend(settings)
    expected = body.expected_sha256.lower() if body.expected_sha256 else None
    # Under a request context of its own, so the hosted backend backs up the scope it wrote as the
    # request ends, as it does for a run's writes (`async_run_with_context`).
    ctx = RequestContext(settings=settings, auth=auth, manifest_id=workspace.manifest or "", thread_id=thread)
    try:
        async with async_run_with_context(ctx):
            written = await backend.write_file_checked(workspace.scope, body.path, data, expected)
    except WorkspaceChanged as exc:
        return JSONResponse(status_code=409, content=exc.detail)
    except (ValueError, OSError) as exc:
        refusal = _workspace_refusal(exc, missing_dir_is_404=False)
        if refusal is None:
            raise
        raise refusal from None

    note = WorkspaceNote(path=written.path, op="write", bytes=written.bytes)
    status, event_id = await _deliver_workspace_note(request, auth.tenant_id, thread, note)
    _audit_workspace_change(
        request,
        auth,
        "workspace_write",
        workspace,
        {"thread_id": thread, "path": written.path, "bytes": written.bytes},
    )
    return WorkspaceWriteOut(
        status=status, path=written.path, bytes=written.bytes, sha256=written.sha256, event_id=event_id
    )


def _audit_workspace_change(
    request: Request, auth: AuthContext, kind: str, workspace: Any, payload: dict[str, Any]
) -> None:
    from felix.audit import store as audit_store

    audit_store.record_event(
        request.app.state.settings,
        auth.tenant_id,
        kind,
        principal_subj=auth.principal_sub or "",
        manifest_id=workspace.manifest or "",
        status="ok",
        payload={**payload, "manifest": workspace.manifest},
    )


_WORKSPACE_CHANGE_CONFLICTS = (
    "`workspace_changed` (body carries the file's `sha256` and `bytes` now): `expected_sha256` no "
    "longer matches. Also `workspace_unavailable` as on the reads, and the lease refusals "
    "(`lease_read_only`, `lease_held`) as on `/chat/steer`."
)


@router.post(
    "/workspace/delete",
    response_model=WorkspaceDeleteOut,
    responses={
        **_WORKSPACE_READ_ERRORS,
        409: {
            "model": WorkspaceChangedOut,
            "description": f"{_WORKSPACE_CHANGE_CONFLICTS} Nothing was deleted.",
        },
    },
)
async def chat_workspace_delete(
    body: WorkspaceDeleteRequest, request: Request, lease_token: LeaseToken = None
) -> Any:
    """Delete one workspace file, and tell the agent.

    The workspace is the one `GET /chat/workspace/tree` lists. Files only: a directory is
    `400 not_a_file`, a symlink `400 invalid_path`, and a missing file `404 not_found`. With
    `expected_sha256` the delete is conditional, compared under the same per-path lock the agent's
    own writes take in this process, and refused with `409 workspace_changed` when the file is not
    what the caller read.

    Then the agent is told as `POST /chat/workspace/edited` would tell it (`op: delete`). Audited
    as `workspace_delete`. Lease-guarded like `/chat/steer`.
    """
    from felix.tools.workspace_backend import WorkspaceChanged, get_workspace_backend
    from felix.workspace_notes import WorkspaceNote

    settings = request.app.state.settings
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    _pane_parts(body.path)
    auth, workspace = await _thread_workspace(request, body.thread_id, body.manifest)
    backend = get_workspace_backend(settings)
    expected = body.expected_sha256.lower() if body.expected_sha256 else None
    # Its own request context, as a write has, so the hosted backend backs up the scope it changed.
    ctx = RequestContext(settings=settings, auth=auth, manifest_id=workspace.manifest or "", thread_id=thread)
    try:
        async with async_run_with_context(ctx):
            deleted = await backend.delete_file(workspace.scope, body.path, expected_sha256=expected)
    except WorkspaceChanged as exc:
        return JSONResponse(status_code=409, content=exc.detail)
    except (ValueError, OSError) as exc:
        refusal = _workspace_refusal(exc)
        if refusal is None:
            raise
        raise refusal from None

    note = WorkspaceNote(path=deleted.path, op="delete")
    status, event_id = await _deliver_workspace_note(request, auth.tenant_id, thread, note)
    _audit_workspace_change(
        request, auth, "workspace_delete", workspace, {"thread_id": thread, "path": deleted.path}
    )
    return WorkspaceDeleteOut(status=status, path=deleted.path, event_id=event_id)


@router.post(
    "/workspace/rename",
    response_model=WorkspaceRenameOut,
    responses={
        **_WORKSPACE_READ_ERRORS,
        409: {
            "model": WorkspaceChangedOut | WorkspaceErrorOut,
            "description": f"`target_exists`: something is already at `to_path` (the file itself "
            f"included). {_WORKSPACE_CHANGE_CONFLICTS} Nothing was moved.",
        },
    },
)
async def chat_workspace_rename(
    body: WorkspaceRenameRequest, request: Request, lease_token: LeaseToken = None
) -> Any:
    """Move one workspace file to another path in the same workspace, and tell the agent.

    Never replaces anything: when `to_path` exists the move is refused with `409 target_exists`.
    Directories `to_path` needs are made, as a write makes them. Files only, refused as
    `POST /chat/workspace/delete` refuses them; both paths are held to the same rules as a write's
    (`invalid_path`, `reserved_path`), and a directory on the way to `to_path` that is a file is
    `400 invalid_path`. With `expected_sha256` the move is conditional, compared under both paths'
    locks.

    Then the agent is told as `POST /chat/workspace/edited` would tell it (`op: rename`, with
    `to_path`). Audited as `workspace_rename`. Lease-guarded like `/chat/steer`.
    """
    from felix.tools.workspace_backend import WorkspaceChanged, get_workspace_backend
    from felix.workspace_notes import WorkspaceNote

    settings = request.app.state.settings
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    _pane_parts(body.path)
    _pane_parts(body.to_path)
    auth, workspace = await _thread_workspace(request, body.thread_id, body.manifest)
    backend = get_workspace_backend(settings)
    expected = body.expected_sha256.lower() if body.expected_sha256 else None
    ctx = RequestContext(settings=settings, auth=auth, manifest_id=workspace.manifest or "", thread_id=thread)
    try:
        async with async_run_with_context(ctx):
            moved = await backend.rename_file(
                workspace.scope, body.path, body.to_path, expected_sha256=expected
            )
    except WorkspaceChanged as exc:
        return JSONResponse(status_code=409, content=exc.detail)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="target_exists") from None
    except (ValueError, OSError) as exc:
        refusal = _workspace_refusal(exc)
        if refusal is None:
            raise
        raise refusal from None

    note = WorkspaceNote(path=moved.path, op="rename", to_path=moved.to_path)
    status, event_id = await _deliver_workspace_note(request, auth.tenant_id, thread, note)
    _audit_workspace_change(
        request,
        auth,
        "workspace_rename",
        workspace,
        {"thread_id": thread, "path": moved.path, "to_path": moved.to_path},
    )
    return WorkspaceRenameOut(
        status=status, path=moved.path, to_path=moved.to_path, sha256=moved.sha256, event_id=event_id
    )


@router.post("/tool_result", responses=LEASE_REFUSALS)
async def chat_tool_result(
    body: ToolResultRequest, request: Request, lease_token: LeaseToken = None
) -> dict[str, Any]:
    """Complete a client-executed tool that paused the active agent run."""
    from felix.tools.client_bridge import client_tool_result_json, complete_result

    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    content = client_tool_result_json(body.content)
    signaled = await complete_result(
        thread,
        body.tool_call_id,
        content,
        error=body.error,
    )
    return {
        "ok": True,
        "signaled": signaled,
        "thread_id": thread,
        "tool_call_id": body.tool_call_id,
    }


@router.post(
    "/fork",
    responses={
        409: {
            "model": ChatRefusalOut,
            "description": "`thread_exists`: `new_thread_id` names a thread that already has events or "
            "session metadata, or is the source thread itself. Nothing was copied; fork to a fresh id.",
        }
    },
)
async def chat_fork(body: ForkRequest, request: Request) -> dict[str, Any]:
    """Copy a thread's active branch into a new thread, recording the source as its parent."""
    auth = _auth_from_request(request)
    settings = request.app.state.settings
    source_id = effective_thread_id(auth.tenant_id, body.thread_id)
    dest_id = effective_thread_id(auth.tenant_id, body.new_thread_id)
    if source_id is None or dest_id is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    # Refused before the lock even when the source is empty, which the store would not see.
    if source_id == dest_id:
        raise HTTPException(status_code=409, detail="thread_exists")
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    from felix.session.branch import fork_and_persist

    # The existence check, the parent record, the copy and the destination's stored leaf are
    # one hold of its leaf lock.
    result = await fork_and_persist(
        store.open(source_id),
        store.open(dest_id),
        settings=settings,
        tenant_id=auth.tenant_id,
        from_event_id=body.from_event_id,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error") or "thread_exists")
    return result


@router.post("/rewind", responses=LEASE_REFUSALS)
async def chat_rewind(
    body: RewindRequest, request: Request, lease_token: LeaseToken = None
) -> dict[str, Any]:
    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    session = get_session_store(settings, tenant_id=auth.tenant_id).open(thread)
    # The summariser is resolved before the rewind starts: the rewind holds the thread's leaf
    # lock from reading the leaf it abandons to storing the new one, and a manifest lookup has
    # no business inside that hold.
    summarize = body.summarize
    model = None
    if body.manifest:
        try:
            resolved = await resolve_tenant_manifest(
                settings, auth.tenant_id, body.manifest, thread_id=thread
            )
            if summarize is None:
                summarize = bool(
                    getattr(getattr(resolved.manifest.spec, "session", None), "branch_summary", True)
                )
            from felix.patterns.model import build_model

            model = build_model(settings, resolved.manifest.spec.model)
        except LookupError, ValueError:
            model = None
    if summarize is None:
        summarize = True
    from felix.session.branch import rewind_and_persist

    result = await rewind_and_persist(
        session,
        body.event_id,
        settings=settings,
        tenant_id=auth.tenant_id,
        summarize=summarize,
        model=model,
        instructions=body.instructions,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "rewind_failed"))
    return result


# The most events one `GET /chat/history` response may carry.
#
# This endpoint loaded the whole thread and returned every message, so the response grew
# without bound for the life of a thread -- a long-running session eventually returns a
# payload nothing wants to hold, and the client has no way to ask for less.
#
# The cap is deliberately far above any thread that exists today. Lowering the *default*
# would be the bigger win and is a breaking change for a shipped client, so it is left
# as a decision: this makes paging possible and makes the response bounded, without
# changing what an existing caller receives.
MAX_HISTORY_EVENTS = 5000


@router.get("/history/{thread_id}")
async def chat_history(
    thread_id: str,
    request: Request,
    limit: int | None = None,
    before_seq: int | None = None,
) -> dict[str, Any]:
    """Server-side transcript for a thread suffix (tenant-prefixed).

    `before_seq` pages backwards: it returns the events immediately preceding that
    sequence, so a client walks a long thread by handing back the `oldest_seq` it last
    received. `limit` counts *events read*, not messages returned, because the filter
    below drops some kinds -- a limit that counted messages could not be turned into a
    cursor without re-reading.
    """
    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    if limit is not None and limit < 1:
        raise HTTPException(status_code=400, detail="limit_must_be_positive")

    window = min(limit or MAX_HISTORY_EVENTS, MAX_HISTORY_EVENTS)
    session = get_session_store(settings, tenant_id=auth.tenant_id).open(thread)

    # The newest window, not the oldest: `get_events(limit=n)` takes the first n, which
    # for a transcript is the wrong end. `head` is O(1) on both arms, so this costs one
    # cheap query rather than loading the thread to find its length.
    upper = before_seq if before_seq is not None else int((await session.head()).get("seq") or 0)
    lower = max(0, upper - window)
    events = await session.get_events(GetEventsOpts(from_seq=lower, to_seq=upper))

    messages: list[dict[str, Any]] = []
    for ev in events:
        role = ev.role or ("assistant" if ev.kind == "assistant" else "user")
        if ev.kind in {"message", "user", "assistant", "system"} or ev.content:
            messages.append(
                {
                    "role": role,
                    "content": ev.content or "",
                    "seq": ev.seq,
                    "kind": ev.kind,
                }
            )
    return {
        "thread_id": thread,
        "messages": messages,
        "events": messages,
        # `lower` rather than the first message's seq: the filter above may have dropped
        # the oldest events in the window, and a cursor that skipped them would lose
        # them on the next page.
        "oldest_seq": lower,
        "has_more": lower > 0,
    }


@router.delete("/history/{thread_id}", responses=LEASE_REFUSALS)
async def chat_history_delete(
    thread_id: str, request: Request, lease_token: LeaseToken = None
) -> dict[str, str]:
    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    await store.open(thread).reset()
    return {"status": "deleted", "thread_id": thread}


@router.post(
    "/sessions/lease",
    response_model=LeaseAcquireOut,
    responses={
        409: {
            "model": ChatRefusalOut,
            "description": "`lease_held`: another holder has it exclusively, or this renewal did not "
            "present the hold's token. `lease_contended`: concurrent changes kept it from landing.",
        }
    },
)
async def acquire_session_lease(body: LeaseRequest, request: Request) -> dict[str, Any]:
    """Take or renew a hold: `exclusive` drives the thread, `shared` observes it read-only.

    `exclusive` is `409 lease_held` while another holder has it. `shared` succeeds with a
    token of its own: on a thread someone else drives, `held_by_other` is true. Renewing
    either kind needs that hold's `token` -- the holder id alone is `lease_held` -- and an
    observer's renewal extends only its own hold.
    """
    from felix.session.lease import acquire_lease

    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    result = await acquire_lease(
        thread,
        holder_id=body.holder_id,
        mode=body.mode,
        ttl_seconds=body.ttl_seconds,
        token=body.token,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error") or "lease_held")
    snapshot = await gather_thread_snapshot(
        settings=request.app.state.settings,
        tenant_id=auth.tenant_id,
        thread=thread,
    )
    return {**result, "snapshot": snapshot}


@router.post(
    "/sessions/lease/release",
    response_model=LeaseReleaseOut,
    responses={
        403: {
            "model": ChatRefusalOut,
            "description": "`token_required`, `token_mismatch` or `not_holder`: this caller does not hold "
            "what it asked to release.",
        },
        409: {
            "model": ChatRefusalOut,
            "description": "`lease_contended`: concurrent changes to the lease kept the release from "
            "landing. Nothing was released; retry.",
        },
    },
)
async def release_session_lease(body: LeaseReleaseRequest, request: Request) -> dict[str, Any]:
    """Drop the one hold `token` names; every other hold stays. The token is required.

    `holder_id`, when sent, must be that hold's holder. Releasing the exclusive hold leaves
    observers observing -- none is promoted.
    """
    from felix.session.lease import release_lease

    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    result = await release_lease(thread, holder_id=body.holder_id, token=body.token)
    if not result.get("ok"):
        error = result.get("error") or "release_failed"
        # Contention is not a refusal: this caller may well hold the lease, and a retry can land.
        raise HTTPException(status_code=409 if error == "lease_contended" else 403, detail=error)
    snapshot = await gather_thread_snapshot(
        settings=request.app.state.settings,
        tenant_id=auth.tenant_id,
        thread=thread,
    )
    return {**result, "snapshot": snapshot}


@router.post("/ui", responses=LEASE_REFUSALS)
async def chat_ui_response(
    body: UiResponseRequest, request: Request, lease_token: LeaseToken = None
) -> dict[str, Any]:
    """Resolve a pending select/confirm/input prompt from the web client."""
    from felix.ui import resolve_ui_response

    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    return await resolve_ui_response(
        thread,
        body.request_id,
        value=body.value,
        cancelled=body.cancelled,
        note=body.note,
    )


@router.get("/sessions/{thread_id}/export")
async def export_session(thread_id: str, request: Request) -> Any:
    """Export the active branch as JSONL (eval artifacts / sharing)."""
    from fastapi.responses import PlainTextResponse
    from felix.session.export import events_to_jsonl
    from felix.session.tree import active_branch_events

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    session = get_session_store(settings, tenant_id=auth.tenant_id).open(thread)
    # The stored leaf as a value: an export reads the branch and must not move this
    # process's leaf, which a turn on the thread may be appending under.
    leaf = await stored_leaf(session)
    events = await session.get_events()
    branch = active_branch_events(events, session_id=thread, leaf_id=leaf)
    body = events_to_jsonl(branch)
    return PlainTextResponse(
        body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{_safe_filename(thread_id)}.jsonl"'},
    )


@router.post("/sessions/custom", responses=LEASE_REFUSALS)
async def append_custom_entry(
    body: CustomEntryRequest, request: Request, lease_token: LeaseToken = None
) -> dict[str, Any]:
    """Persist a custom (UI/plugin) entry. Set ``in_context`` to include it in the LLM."""
    from felix.session.tree import annotate_and_append
    from felix.session.types import AppendableEvent

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    md = dict(body.metadata or {})
    md["in_context"] = bool(body.in_context)
    md["type"] = "custom"
    ids = await annotate_and_append(
        get_session_store(settings, tenant_id=auth.tenant_id).open(thread),
        [
            AppendableEvent(
                kind="custom",  # type: ignore[arg-type]
                role=body.role,
                content=body.content,
                metadata=md,
            )
        ],
        sync=True,
    )
    return {
        "ok": True,
        "thread_id": thread,
        "event_id": ids[-1] if ids else None,
        "in_context": body.in_context,
    }


@router.get("/sessions")
async def list_sessions(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> dict[str, Any]:
    """One page of the caller's tenant's threads, newest first, from their stored metadata.

    Each row is `{id, createdAt, updatedAt, parentSessionId, sessionName, preview, manifest}`.
    `preview` is the thread's first user message, masked, whitespace-collapsed and cut to 120
    characters; `manifest` is the manifest its newest turn ran under. Either is `null` for a
    thread that has none, or that was written before the harness recorded it.

    Ordered by last update, to the second, then by id. `next_cursor` is `null` on the last page;
    otherwise pass it back as `cursor` for the next. A thread updated during a walk moves ahead of
    the cursor, so that walk does not see it again; the next one lists it first.
    """
    from felix.cursors import InvalidCursor
    from felix.session.thread_state import list_thread_metadata

    auth = _auth_from_request(request)
    try:
        items, next_cursor = await list_thread_metadata(
            settings=request.app.state.settings, tenant_id=auth.tenant_id, limit=limit, cursor=cursor
        )
    except InvalidCursor as exc:
        # Same as `/audit` (routes/audit.py): a malformed cursor is the client's error, not a 500.
        raise HTTPException(
            status_code=400, detail=client_safe_message(exc, authored_for_clients=True)
        ) from exc
    return {"sessions": items, "items": items, "next_cursor": next_cursor}


@router.get("/sessions/search")
async def search_sessions_route(request: Request, q: str = "", limit: int = 20) -> dict[str, Any]:
    from felix.session.search import search_sessions

    auth = _auth_from_request(request)
    hits = await search_sessions(request.app.state.settings, auth.tenant_id, q, limit=limit)
    return {"query": q, "hits": hits}


@router.get("/sessions/{thread_id}/lease", response_model=LeaseStatusOut)
async def get_session_lease(thread_id: str, request: Request) -> dict[str, Any]:
    """Who holds the thread: the exclusive holder, if any, and every observer."""
    from felix.session.lease import lease_status

    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    return await lease_status(thread)


@router.get("/sessions/{thread_id}")
async def get_session_snapshot(thread_id: str, request: Request) -> dict[str, Any]:
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    return await gather_thread_snapshot(
        settings=request.app.state.settings,
        tenant_id=auth.tenant_id,
        thread=thread,
    )


@router.post("/sessions/name", responses=LEASE_REFUSALS)
async def set_session_name(
    body: SessionNameRequest, request: Request, lease_token: LeaseToken = None
) -> dict[str, Any]:
    from felix.session.thread_state import update_thread_meta
    from felix.session.tree import annotate_and_append
    from felix.session.types import AppendableEvent

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    meta = await update_thread_meta(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=thread,
        session_name=body.name,
    )
    await annotate_and_append(
        get_session_store(settings, tenant_id=auth.tenant_id).open(thread),
        [
            AppendableEvent(
                kind="session_info",  # type: ignore[arg-type]
                content=body.name,
                metadata={"type": "session_info", "name": body.name},
            )
        ],
        sync=True,
    )
    return {"ok": True, "thread_id": thread, "name": body.name, "meta": meta}


@router.post("/sessions/label", responses=LEASE_REFUSALS)
async def set_session_label(
    body: LabelRequest, request: Request, lease_token: LeaseToken = None
) -> dict[str, Any]:
    from felix.session.thread_state import update_thread_meta
    from felix.session.tree import annotate_and_append, set_label
    from felix.session.types import AppendableEvent

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    set_label(body.event_id, body.label)
    await update_thread_meta(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=thread,
        labels={body.event_id: body.label},
    )
    await annotate_and_append(
        get_session_store(settings, tenant_id=auth.tenant_id).open(thread),
        [
            AppendableEvent(
                kind="label",  # type: ignore[arg-type]
                content=body.label,
                metadata={
                    "type": "label",
                    "targetId": body.event_id,
                    "label": body.label,
                },
            )
        ],
        sync=True,
    )
    return {"ok": True, "thread_id": thread, "event_id": body.event_id, "label": body.label}


@router.post("/sessions/feedback")
async def set_session_feedback(body: FeedbackRequest, request: Request) -> dict[str, Any]:
    """Rate an assistant turn up or down, or clear a rating.

    Stored twice, for two readers. The thread's metadata holds the current rating per
    event, which the snapshot returns as `feedback` so a client can draw it. And every
    change is an audit event, `turn_feedback`, because the question an operator asks of
    feedback is tenant-wide -- which answers did people mark down this week -- and the
    audit log is the record that is already filterable, paged and tenant-scoped.

    Unlike a label, nothing is appended to the session log: a rating is about the
    conversation, not part of it, and an event there would move the thread's leaf.
    """
    from felix.audit import store as audit_store
    from felix.session.thread_state import get_thread_meta, update_thread_meta
    from felix.session.tree import get_event_id

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")

    store = get_session_store(settings, tenant_id=auth.tenant_id)
    events = await store.open(thread).get_events()
    target = next((e for e in events if get_event_id(e) == body.event_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="unknown_event_id")
    # A rating says how good an *answer* was. On a user message it would mean nothing,
    # and on a tool result it would grade the tool rather than the agent.
    if target.kind != "message" or target.role != "assistant":
        raise HTTPException(status_code=400, detail="not_an_assistant_message")

    entry: dict[str, Any] | None = (
        None
        if body.rating is None
        else {"rating": body.rating, "note": body.note, "at": int(time.time() * 1000)}
    )
    await update_thread_meta(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=thread,
        feedback={body.event_id: entry},
    )
    meta = await get_thread_meta(settings=settings, tenant_id=auth.tenant_id, thread_id=thread)
    audit_store.record_event(
        settings,
        auth.tenant_id,
        "turn_feedback",
        principal_subj=auth.principal_sub or "",
        status=body.rating or "cleared",
        payload={
            "thread_id": thread,
            "event_id": body.event_id,
            "rating": body.rating,
            "note": body.note,
            "model_id": meta.get("model_id") or "",
        },
    )
    return {"ok": True, "thread_id": thread, "event_id": body.event_id, "feedback": entry}


@router.post("/abort", responses=LEASE_REFUSALS)
async def chat_abort(body: AbortRequest, request: Request, lease_token: LeaseToken = None) -> dict[str, Any]:
    from felix.session.thread_state import update_thread_meta
    from felix.steer import request_abort

    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    result = await request_abort(auth.tenant_id, thread)
    await update_thread_meta(
        settings=request.app.state.settings,
        tenant_id=auth.tenant_id,
        thread_id=thread,
        phase="aborted",
    )
    snapshot = await gather_thread_snapshot(
        settings=request.app.state.settings,
        tenant_id=auth.tenant_id,
        thread=thread,
    )
    return {**result, "snapshot": snapshot}


@router.post("/continue", responses=LEASE_REFUSALS)
async def chat_continue(body: ContinueRequest, request: Request, lease_token: LeaseToken = None) -> Any:
    """Resume after abort/error without a new user message (wake-based)."""
    from felix.session.types import analyze_wake
    from felix.steer import clear_abort

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    tools = request.app.state.tools
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    await clear_abort(auth.tenant_id, thread)
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    session = store.open(thread)
    # Once. This read the whole thread, then read it again to look at one element.
    events = await session.get_events()
    wake = analyze_wake(events)
    if wake.fresh:
        raise HTTPException(status_code=400, detail="nothing_to_continue")
    # Last turn must be user or tool result for a clean continue.
    last = events[-1] if events else None
    if last and last.role == "assistant" and not last.tool_calls and not wake.pending_tool_calls:
        raise HTTPException(status_code=400, detail="already_complete")

    try:
        resolved = await resolve_tenant_manifest(settings, auth.tenant_id, body.manifest, thread_id=thread)
        await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        if isinstance(exc, (LookupError, ValueError)):
            raise HTTPException(status_code=404, detail=f"unknown_manifest:{body.manifest}") from exc
        raise
    model_id = _allowlisted_model(resolved.manifest, body.model, settings)
    # Empty incoming — session strategy rebuilds context from leaf.
    messages = [ChatMessage(role="user", content="Continue.")]
    if wake.pending_tool_calls:
        # Nudge the model with a system-style continue after pending tools resolved client-side.
        messages = [ChatMessage(role="user", content="[continue]")]

    req_ctx = RequestContext(
        settings=settings,
        auth=auth,
        manifest_id=body.manifest,
        thread_id=thread,
    )
    from felix.session.thread_state import update_thread_meta

    await update_thread_meta(settings=settings, tenant_id=auth.tenant_id, thread_id=thread, phase="retry")
    async with async_run_with_context(req_ctx):
        try:
            agent = await build_tenant_agent(
                settings,
                manifest=resolved.manifest,
                sub_agents=resolved.sub_agents,
                tools=tools,
                tenant_id=auth.tenant_id,
                skill_owner=auth.skill_owner,
            )
            result = await agent.invoke(
                InvokeInput(
                    messages=messages,
                    thread_id=thread,
                    model_id=model_id,
                    tenant_id=auth.tenant_id,
                )
            )
        except ModelGatewayError as exc:
            # `body` is an upstream response: neither trusted nor single-line.
            # CodeQL does not flag it -- its taint source is the network rather than
            # the request -- but a gateway body is shaped by prompt content, which
            # makes it as forgeable as anything the client sends directly.
            log_gateway_error(logger, exc)
            raise HTTPException(status_code=502, detail=client_safe_message(exc)) from exc
        except Exception as exc:
            http = _http_from_invoke_prep(exc)
            if http is not None:
                raise http from exc
            raise
    await update_thread_meta(settings=settings, tenant_id=auth.tenant_id, thread_id=thread, phase="idle")
    return {
        "messages": [m.model_dump() for m in result.messages],
        "final": result.final.model_dump() if hasattr(result.final, "model_dump") else result.final,
        "thread_id": thread,
        "continued": True,
    }


@router.post("/thinking", responses=LEASE_REFUSALS)
async def chat_thinking(
    body: ThinkingRequest, request: Request, lease_token: LeaseToken = None
) -> dict[str, Any]:
    from felix.session.thinking import parse_thinking_level
    from felix.session.thread_state import update_thread_meta
    from felix.session.tree import annotate_and_append
    from felix.session.types import AppendableEvent

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    try:
        level = parse_thinking_level(body.thinking_level)
    except ValueError as exc:
        # `unknown_thinking_level:<value>` -- the caller's own input, echoed back.
        raise HTTPException(
            status_code=400, detail=client_safe_message(exc, authored_for_clients=True)
        ) from exc
    await update_thread_meta(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=thread,
        thinking_level=level,
    )
    await annotate_and_append(
        get_session_store(settings, tenant_id=auth.tenant_id).open(thread),
        [
            AppendableEvent(
                kind="thinking_level_change",  # type: ignore[arg-type]
                content=level,
                metadata={"type": "thinking_level_change", "thinking_level": level},
            )
        ],
        sync=True,
    )
    return {"ok": True, "thread_id": thread, "thinking_level": level}


@router.post("/mode", responses=LEASE_REFUSALS)
async def chat_permission_mode(
    body: PermissionModeRequest, request: Request, lease_token: LeaseToken = None
) -> dict[str, Any]:
    """Set the thread's permission mode: `default`, `plan`, `accept_edits` or `bypass`.

    Takes effect from the next run. A mode the thread's manifest does not allow is stored and
    ignored -- the run falls back to the manifest's default -- because the manifest a thread runs
    can change under it. `bypass` waives every approval, so setting it needs `approvals:bypass`
    here, and the run checks the scope again for whoever drives the thread then.
    """
    from felix.governance.permission_mode import BYPASS_SCOPE, may_bypass, record_mode_change
    from felix.session.thread_state import update_thread_meta

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    if body.mode == "bypass" and not may_bypass(settings, auth.scopes):
        raise HTTPException(status_code=403, detail=f"missing_scope:{BYPASS_SCOPE}")
    await update_thread_meta(
        settings=settings, tenant_id=auth.tenant_id, thread_id=thread, permission_mode=body.mode
    )
    record_mode_change(settings, auth.tenant_id, auth.principal_sub or "", thread, body.mode, via="route")
    return {"ok": True, "thread_id": thread, "mode": body.mode}


@router.post("/ask")
async def chat_ask(body: AskRequest, request: Request) -> dict[str, Any]:
    """Answer one question from a thread's context, leaving the thread exactly as it was.

    Answered by the thread's own manifest, admitted and screened as a turn of it would be, from
    the history its next turn would read. Read-only: no session event, no steer or follow-up, no
    phase change, and no lease check — a question is most useful while a run or another tab
    holds the thread. One model call, no tools, metered under the manifest. `status` is
    `answered`, `not_in_context` when the conversation does not hold the answer, or `withheld`
    when a final-response judge refused it.
    """
    from felix.manifests.governance import GovernanceError
    from felix.session.side_question import UnknownManifestError, UnknownThreadError, answer_side_question

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    try:
        return await answer_side_question(
            settings,
            auth=auth,
            tenant_id=auth.tenant_id,
            thread_id=thread,
            question=body.question,
            tools=request.app.state.tools,
        )
    except UnknownThreadError as exc:
        raise HTTPException(status_code=404, detail="unknown_thread") from exc
    except UnknownManifestError as exc:
        # The manifest the thread last ran under no longer resolves.
        raise HTTPException(status_code=404, detail="unknown_manifest") from exc
    except ModelGatewayError as exc:
        log_gateway_error(logger, exc)
        raise HTTPException(status_code=502, detail=client_safe_message(exc)) from exc
    except GovernanceError as exc:
        raise HTTPException(status_code=422, detail=client_safe_message(exc)) from exc
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        raise


@router.post("/compact", responses=LEASE_REFUSALS)
async def chat_compact(
    body: CompactRequest, request: Request, lease_token: LeaseToken = None
) -> dict[str, Any]:
    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await _refuse_unless_driver(request, thread, lease_token)
    try:
        resolved = await resolve_tenant_manifest(settings, auth.tenant_id, body.manifest, thread_id=thread)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"unknown_manifest:{body.manifest}") from exc

    from felix.patterns.model import build_model
    from felix.session.compaction import CompactingSessionStrategy
    from felix.session.thread_state import update_thread_meta

    session = get_session_store(settings, tenant_id=auth.tenant_id).open(thread)
    # Compaction draws the branch from this process's index (`compaction._load_branch`), so it
    # is synced -- under the thread's lock, which keeps it out of a turn's append.
    await sync_leaf(session)
    strategy_spec = getattr(resolved.manifest.spec, "session", None)

    def _budget(field: str, default: int) -> int:
        """A declared zero is a value, not an absent one.

        `int(getattr(spec, field, default) or default)` treats `0` as unset, so a manifest
        setting `keep_recent_tokens: 0` -- which the schema allows, `ge=0` -- silently ran with
        20000 and compaction never had anything to cut. The declared window was not what the
        route used, and nothing said so: this repo's signature defect shape.
        """
        value = getattr(strategy_spec, field, None)
        return default if value is None else int(value)

    strategy = CompactingSessionStrategy(
        reserve_tokens=_budget("reserve_tokens", 16384),
        keep_recent_tokens=_budget("keep_recent_tokens", 20000),
        context_window_tokens=_budget("context_window_tokens", 128000),
        enabled=True,
    )
    model = build_model(settings, resolved.manifest.spec.model)
    await update_thread_meta(
        settings=settings, tenant_id=auth.tenant_id, thread_id=thread, phase="compaction"
    )
    # The middleware context carries the tenant but no manifest; the summarizer's usage
    # row should name the manifest this thread runs, not fall to an empty one.
    ctx = get_context()
    async with async_run_with_context(replace(ctx, manifest_id=body.manifest, thread_id=thread)):
        result = await strategy.compact_now(
            session,
            model=model,
            system_prompt="",
            instructions=body.instructions,
            reason="manual",
        )
    await update_thread_meta(settings=settings, tenant_id=auth.tenant_id, thread_id=thread, phase="idle")
    snapshot = await gather_thread_snapshot(settings=settings, tenant_id=auth.tenant_id, thread=thread)
    return {**result, "thread_id": thread, "snapshot": snapshot}
