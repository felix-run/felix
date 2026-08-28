"""Felix settings — pydantic-settings with FELIX_ env prefix."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_loopback_host(host: str) -> bool:
    """True when binding ``host`` cannot be reached from off-host.

    ``0.0.0.0`` / ``::`` bind every interface, and an empty host means the same to most
    servers, so all three are treated as public.
    """
    h = (host or "").strip().strip("[]").lower()
    if not h or h in {"0.0.0.0", "::", "*"}:
        return False
    if h in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        # A hostname we cannot classify without DNS — treat as public.
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FELIX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- runtime ---
    environment: Literal["development", "staging", "production"] = "development"
    # Loopback by default: an unauthenticated API must not be reachable off-host.
    # Containers set FELIX_HOST=0.0.0.0 explicitly (see deploy/docker/Dockerfile).
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"
    allow_insecure: bool = False  # required if auth_mode=none and host binds public

    # --- auth ---
    # Open on purpose: a plugin may register an authenticator for its own mode via
    # `felix.plugins`. Built-ins are none|api_key|jwt; an unrecognised value is
    # resolved against the plugin registry at request time and 401s if absent.
    # `validate_runtime` rejects an unknown mode with no plugin behind it.
    auth_mode: str = "none"
    auth_api_keys: str = ""  # JSON map token -> {tenant_id, sub, scopes[]}
    jwt_verifiers: str = ""  # comma-separated scheme:issuer (access|cognito|self)
    jwks_public: str = ""  # PEM or JWKS JSON for self-issued
    jwks_private: str = ""  # PEM for minting (CLI)
    # Comma-separated tenants a JWT may claim. Empty = any claimed tenant is
    # accepted, which is only safe when the IdP is the sole writer of that claim.
    allowed_tenants: str = ""

    # --- request limits ---
    rate_limit: int = 120
    rate_limit_window_seconds: int = 60
    # Header carrying the real client IP behind a trusted proxy (e.g.
    # "cf-connecting-ip", "x-forwarded-for"). EMPTY by default: the header is
    # attacker-controlled unless a proxy you operate overwrites it, and trusting
    # it blindly lets one client masquerade as unlimited distinct clients.
    trusted_client_ip_header: str = ""
    oauth_cache_key: str = ""  # base64 32-byte AES key
    # Comma-separated commands MCP stdio servers may spawn. Empty (default) disables
    # stdio entirely — manifest-supplied argv would otherwise be arbitrary code execution.
    mcp_stdio_allowed_commands: str = ""
    # Container images sandbox tools may run (comma-separated). Empty (default)
    # allows only the built-in python image — `spec.sandboxes[].binding` is
    # manifest-supplied, so an unrestricted value is arbitrary image pull-and-run.
    sandbox_allowed_images: str = ""

    # --- data plane (cloud-agnostic; AWS + GCP first) ---
    database_url: str = "postgresql+psycopg://felix:felix@localhost:5432/felix"
    # Opt-in Postgres RLS (requires migration 0006). Sets app.tenant_id per txn.
    database_rls: bool = False
    redis_url: str = "redis://localhost:6379/0"
    # Object store: s3 (AWS/MinIO) | gcs (GCP) | fs (local dir, small VMs) | memory
    # Lean default is fs — matches Docker image without aws/gcp extras.
    # Registrable: felix.storage.register_object_store adds a backend.
    object_store: str = "fs"
    object_store_path: str = ""  # FELIX_OBJECT_STORE=fs → under data_dir/objects if empty
    s3_endpoint: str = "http://localhost:9000"  # empty = AWS default endpoint
    s3_access_key: str = "felix"
    s3_secret_key: str = "felixsecret"
    s3_bucket: str = "felix-bundles"
    s3_region: str = "us-east-1"
    gcs_bucket: str = ""
    # Secrets: env | file | aws | gcp
    # Registrable: felix.secrets.register_secrets_backend adds a backend.
    secrets_backend: str = "env"
    secrets_dir: str = "./secrets"
    # Extra secret names to resolve via backend for output masking (comma-separated)
    secret_names: str = ""
    aws_region: str = "us-east-1"
    gcp_project: str = ""
    # Deploy target hint (docs/helm only — runtime stays agnostic)
    cloud_provider: Literal["local", "aws", "gcp", "azure", "other"] = "local"

    # --- models ---
    # Sonnet tier by default, matching the prior posture. `claude-opus` and
    # `claude-fable` are available routes; changing this changes every run's cost.
    default_model_id: str = "claude-sonnet"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    litellm_base_url: str = ""
    model_routes: str = ""  # JSON override of logical id -> {provider, model}

    # --- durability ---
    durability: Literal["fibers", "temporal"] = "fibers"
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"

    # --- scale-out ---
    scale_out: bool = False
    replica_id: str = "local"

    # --- observability ---
    otel_enabled: bool = False
    otel_endpoint: str = "http://localhost:4317"

    # --- analytics warehouse (append-only spill; Postgres is SoR) ---
    # Lean default: none. Recommended when enabling spill: duckdb
    # (felix-harness[warehouse]). Scale-out: clickhouse first; doris if
    # you already operate Apache Doris / want MySQL-protocol BI.
    # Registrable: felix.warehouse.register_warehouse_backend adds a backend.
    warehouse: str = "none"
    warehouse_path: str = ""  # duckdb file; default $FELIX_DATA_DIR/warehouse/felix.duckdb
    warehouse_url: str = ""  # clickhouse http(s)://… or doris mysql://…
    warehouse_database: str = "felix"

    # --- long-term memory ---
    # The `memory_vectors.embedding` column is `vector(768)`, created by 0001_baseline
    # and indexed by HNSW, so the dimension is fixed at deploy time and this only
    # declares what it is. 768 is also `bge-base-en-v1.5`, the model the rest of the
    # repo defaults to. Changing it is a re-embed of every row, not a config flip.
    memory_embedding_dim: int = 768
    # Semantic recall is optional. The default costs nothing and needs nothing
    # installed; recall runs its full-text and topic-key channels and skips the
    # vector one. `sentence_transformers` needs felix-harness[embeddings]; `openai`
    # and `ollama` speak an OpenAI-compatible /embeddings endpoint over httpx.
    # Registrable: felix.memory.embedder.register_embedder_backend adds a backend.
    memory_embedder: str = "none"

    # Extra directory of SKILL.md packages, appended to the bundled `skills/` dir.
    # Without this the only paths were derived from __file__ (a repo checkout), so a
    # pip-installed Felix had no bundled skills and no way to point at its own.
    skills_dir: str = ""
    memory_embedding_model: str = "bge-base-en-v1.5"
    memory_recall_limit: int = 8

    # --- SSE reconnect ---
    # How long `GET /chat/stream/{thread_id}` holds an idle connection before closing
    # it. The client reconnects with its `Last-Event-ID` and loses nothing, so this
    # trades a reconnect for not pinning a worker to a silent thread forever.
    # Bounded, not merely defaulted: a negative poll makes `asyncio.sleep` return
    # immediately and the idle counter run backwards, so the loop would query the
    # session log as fast as Postgres answers and never close the connection.
    stream_resume_idle_seconds: float = Field(default=300.0, gt=0)
    stream_resume_poll_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    # The ceiling the poll decays to once a stream has been quiet for a while. One
    # hundred reattached clients at a fixed 1 Hz is a sustained 100 queries/second of
    # pure polling, each checking out a pooled connection to learn nothing.
    stream_resume_poll_max_seconds: float = Field(default=10.0, ge=0.1, le=300.0)

    # --- database pool ---
    #
    # Was hardcoded at 5 + 10 in two places, so fifteen connections per worker was a
    # hard concurrency ceiling nobody could raise without editing the source: past it,
    # requests queue for `db_pool_timeout_seconds` and then fail. That ceiling is
    # reached sooner than it looks, because the session-event append path and the
    # resume poll are both connection-hungry.
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0)
    # A round trip on every checkout, to discover a connection a pooler closed
    # server-side. It earns that behind PgBouncer / RDS Proxy / Cloud SQL and wastes it
    # against a direct Postgres, which is why it is a setting rather than a constant.
    db_pool_pre_ping: bool = True
    # Whether the driver may prepare statements server-side.
    #
    # Must be `false` behind a transaction-mode pooler that does not itself track
    # prepared statements. psycopg3 auto-prepares after five executions, and under
    # transaction pooling the sixth lands on a different server connection where that
    # statement was never created -- measured against PgBouncer 1.25, Felix failed on
    # exactly the sixth append with `InFailedSqlTransaction`.
    #
    # Two ways out, and which applies depends on whether you control the pooler.
    # PgBouncer >= 1.21 with `max_prepared_statements > 0` tracks them for you and
    # Felix works unchanged. RDS Proxy instead *pins* the session when it sees one,
    # which defeats the multiplexing you deployed it for -- there, turn this off.
    db_prepared_statements: bool = True

    # Granian/uvicorn worker processes. Read from a bare `os.environ` in main.py until
    # now, which is the one thing the conventions say not to do with configuration:
    # invisible to `felix doctor`, absent from .env.example, and unvalidated.
    workers: int = Field(default=1, ge=1)

    # --- misc ---
    default_manifest: str = "quick"
    hibernate_after_seconds: int = 300
    # How often each emitting process drains its audit/usage buffers. The agent
    # loop runs in the API, so the API must flush too — the worker cron alone only
    # ever drained the worker's (always-empty) buffer. 0 disables the loop.
    audit_flush_seconds: float = 5.0
    consumer_shared_secret: str = ""
    webhook_secret: str = ""
    policy_bundle_pubkey: str = ""

    data_dir: str = Field(default="./data")
    # Optional workspace root for AGENTS.md / SYSTEM.md / instruction file loading.
    workspace_root: str = ""
    # When true, auto-discover AGENTS.md from workspace_root / object store.
    load_agents_md: bool = False

    @field_validator("auth_api_keys", mode="before")
    @classmethod
    def _strip_keys(cls, v: Any) -> Any:
        return v if v is not None else ""

    def _validate_registry_backed_settings(self) -> None:
        """Fail fast on a backend name nothing registered.

        These settings are open strings so a ``felix.plugins`` package can add a
        backend, which means a typo is no longer caught by pydantic. Resolve each
        against its registry at startup instead of failing later and elsewhere.
        """
        from felix.plugins import get_registry, load_optional_plugins

        load_optional_plugins()

        from felix.auth.context import BUILTIN_AUTH_MODES

        if self.auth_mode not in BUILTIN_AUTH_MODES:
            if get_registry().authenticator_builder(self.auth_mode) is None:
                raise RuntimeError(
                    f"FELIX_AUTH_MODE={self.auth_mode!r} is not a built-in mode "
                    "(none|api_key|jwt) and no installed plugin registered it."
                )

        from felix.memory.embedder import list_embedder_backends
        from felix.secrets import list_secrets_backends
        from felix.storage import list_object_stores
        from felix.warehouse import list_warehouse_backends

        for env_name, value, known in (
            ("FELIX_OBJECT_STORE", self.object_store, list_object_stores()),
            ("FELIX_SECRETS_BACKEND", self.secrets_backend, list_secrets_backends()),
            ("FELIX_WAREHOUSE", (self.warehouse or "none").lower(), list_warehouse_backends()),
            ("FELIX_MEMORY_EMBEDDER", self.memory_embedder, list_embedder_backends()),
        ):
            if value not in known:
                names = ", ".join(known)
                raise RuntimeError(f"Unknown {env_name}={value!r} (registered: {names})")

    def validate_runtime(self) -> None:
        """Fail fast on unsafe or incomplete configuration."""
        self._validate_registry_backed_settings()
        if self.auth_mode == "none":
            if not self.allow_insecure and self.environment != "development":
                raise RuntimeError(
                    "FELIX_AUTH_MODE=none requires FELIX_ALLOW_INSECURE=true (development only)."
                )
            # allow_insecure is not a licence to serve an unauthenticated API to a
            # network. A non-loopback bind is refused regardless of environment.
            if not _is_loopback_host(self.host):
                raise RuntimeError(
                    f"FELIX_AUTH_MODE=none may only bind loopback; FELIX_HOST={self.host!r} "
                    "is reachable off-host. Set FELIX_AUTH_MODE=api_key|jwt, or bind 127.0.0.1."
                )
        if self.scale_out:
            if "sqlite" in self.database_url:
                raise RuntimeError("Scale-out requires Postgres (FELIX_DATABASE_URL).")
            if self.object_store == "memory":
                raise RuntimeError("Scale-out requires a shared object store (s3|gcs|fs).")
            if self.object_store == "s3" and not self.s3_bucket:
                raise RuntimeError("Scale-out S3 requires FELIX_S3_BUCKET.")
            if self.object_store == "gcs" and not self.gcs_bucket:
                raise RuntimeError("Scale-out GCS requires FELIX_GCS_BUCKET.")


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Logical model routes — Workers AI dropped; Ollama / LiteLLM fill the OSS slot.
DEFAULT_MODEL_ROUTES: dict[str, dict[str, str]] = {
    # Current generation. Wire ids are complete as written — no date suffixes.
    "claude-opus": {"provider": "anthropic", "model": "claude-opus-5"},
    "claude-sonnet": {"provider": "anthropic", "model": "claude-sonnet-5"},
    "claude-haiku": {"provider": "anthropic", "model": "claude-haiku-4-5"},
    "claude-fable": {"provider": "anthropic", "model": "claude-fable-5"},
    # Legacy logical ids kept so existing manifests keep resolving, now pointing at the
    # current model in the same tier rather than a two-generation-old snapshot.
    "claude-sonnet-4": {"provider": "anthropic", "model": "claude-sonnet-5"},
    "claude-haiku-4": {"provider": "anthropic", "model": "claude-haiku-4-5"},
    "gpt-4.1": {"provider": "openai", "model": "gpt-4.1"},
    "gpt-4.1-mini": {"provider": "openai", "model": "gpt-4.1-mini"},
    "llama-3-pro": {"provider": "ollama", "model": "llama3.3:70b"},
    "llama-3-fast": {"provider": "ollama", "model": "llama3.2"},
}
