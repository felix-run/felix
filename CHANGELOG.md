# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`GET /ready` and `GET /live`.** `/health` returned a static `{"status":"ok"}` while
  the Helm chart wired **both** the readiness and the liveness probe to it — so a pod
  with a dead database reported Ready and received traffic, and a genuine dependency
  outage never restarted anything. `/ready` probes the database, cache, and object store
  (bounded, so a hung dependency fails rather than hangs) and returns 503 when any is
  down; `/live` does no I/O, because a dependency blip must not restart an otherwise
  healthy process. `/health` stays as a liveness alias so existing deploys and smoke
  tests keep working. Helm probes now point at the right one each, with a higher
  `failureThreshold` on liveness than readiness.
- **Request correlation.** Every response carries `x-request-id`, honouring an inbound
  one when supplied, and the id is attached to every log record — a single chat request
  fans out across tool calls, model calls, session writes, and audit events, and nothing
  tied them together before.

### Fixed

- **`FELIX_LOG_LEVEL` was never applied**, and `structlog` was a hard dependency that
  nothing imported. Logging is now configured at startup, with JSON output in production
  and readable text elsewhere.
- **SSE streams had no error path, heartbeat, or anti-buffering headers.** A mid-stream
  failure truncated the body under an already-sent `200 OK` with no error event and no
  `[DONE]`, so a client could not distinguish success from failure; a long tool call
  emitted nothing and hit proxy idle timeouts; and nginx buffered the response by
  default, defeating streaming entirely. Streams now emit an `error` event and always
  terminate with `[DONE]`, send a keep-alive comment during quiet periods, set
  `x-accel-buffering: no`, and let client disconnects cancel the run instead of
  continuing to burn model tokens.

### Fixed

- **Every chat request leaked an S3 client.** `build_object_store` was called inside
  `build_tenant_agent` — once per request — and `S3ObjectStore` called `__aenter__` with
  no matching `__aexit__`, no `close()`, and no shutdown hook, so an aiobotocore client
  and its connection pool leaked each time until the process hit `EMFILE`. Object stores
  are now cached per backend configuration and closed on shutdown.
- `S3ObjectStore._get_client` had no lock, so two concurrent first-requests each created
  a client and orphaned one with no reference left to close it.
- `S3ObjectStore.get` swallowed any exception whose message merely contained `"404"` — a
  request id or a byte count would do it. It now matches the error type and the response
  status.
- **`dispose_engine()` was `cache_clear()` plus a comment plus `pass`**, so connections
  were never returned and lingered across Granian worker recycles. It now disposes every
  engine the module created.
- SQLAlchemy pools had no `pool_recycle` or `pool_timeout`, so PgBouncer / RDS Proxy /
  Cloud SQL dropped idle connections the pool still believed were live and
  `pool_pre_ping` paid a round trip to discover it. `create_engine_from_settings` also
  had no pool sizing at all, giving two differently tuned pools against one database.
- A failing object store silently stripped the system prompt: the bare `except` left
  `store = None`, so `SYSTEM.md`, `AGENTS.md`, instruction files, and object-store skills
  all vanished and the agent fell back to `f"You are {name}."`. It now logs an error.

### Security

- **Failed authentication was never rate limited.** Starlette's `add_middleware` inserts
  at index 0, so the auth middleware — registered last — ran *first*, and a 401 returned
  before the limiter was consulted. Credential guessing was completely unthrottled.
  Middleware is now registered auth → rate limit → body limit, giving the runtime order
  body limit → rate limit → auth.
- **`/metrics` was public and rate-limit exempt.** Its counters carry tenant-supplied
  manifest ids and remote MCP tool names as label values, so an anonymous scrape
  disclosed every tenant's manifest and tool names. It now requires auth and is counted.
- **Rate limiting was per-process and keyed per tenant.** `RedisRateLimiter` existed but
  was never wired, so the effective ceiling was `limit x replicas`; and under
  `auth_mode=none` every caller shared one `tenant:default` bucket, so a single client
  could 429 the whole deployment. Limits are now settings-driven (`FELIX_RATE_LIMIT`,
  `FELIX_RATE_LIMIT_WINDOW_SECONDS`), Redis-backed when one is reachable, and keyed per
  client. `FELIX_TRUSTED_CLIENT_IP_HEADER` opts into a proxy header — off by default,
  since the header is attacker-controlled unless a proxy you operate overwrites it.
- **The body limit trusted `Content-Length`.** A chunked request carries no such header,
  so its body was read unbounded. The stream is now capped as it is consumed.
- **Compute ceilings.** The `calculator` allowed `9**9**9**9` (`ast.Pow` with no operand
  bound); `search_files` compiled a model-supplied regex with no length or complexity
  bound and ran it over every file. Exponentiation is now bounded, the query is capped,
  lines are truncated before matching, patterns nesting a quantifier inside a quantified
  group are refused, and the scan runs off the event loop under a deadline.

### Fixed

- `InMemoryRateLimiter._windows` was a `defaultdict` that never evicted, so per-IP keys
  grew forever — a memory-exhaustion DoS in the component meant to prevent DoS.
- `RedisRateLimiter` did `INCR` then `EXPIRE` non-atomically; a crash between them left a
  key with no TTL, rate-limiting that principal permanently. Both now go in one pipeline.
- 429 responses now carry `Retry-After`.

### Security

- **A JWT with no `exp` was accepted forever.** Only `iss` (and `aud` when configured)
  were marked essential, and joserfc validates expiry only when the claim is present.
  `exp` is now required.
- **Shared issuers were accepted without an audience check.** A verifier configured as
  `access:example.cloudflareaccess.com` accepted tokens minted for *any* application
  under that issuer. `access` and `cognito` verifiers now require `;aud=`.
- **Remote JWKS was never fetched, and the fallback used the local signing key.**
  `_load_key_set` carried a literal "Lazy remote fetch via httpx would go here" comment,
  so the `access` and `cognito` schemes had no key source — and its fallback returned
  `FELIX_JWKS_PUBLIC` regardless of URL, meaning the local self-signing key would verify
  tokens claiming to come from Cloudflare Access or Cognito (safe only because `iss`
  still had to match). Key sets are now fetched from the issuer and cached with a 15
  minute TTL, refreshed by the API on a timer; `FELIX_JWKS_PUBLIC` is used for the
  `self` scheme only.
- **The tenant came from an unvalidated claim, and a missing claim collapsed users
  together.** `tenant_id` is the isolation boundary; in the default `claim` mode it is
  whatever the token says, and on Cognito `custom:*` attributes are frequently
  user-writable. Added `FELIX_ALLOWED_TENANTS`. A token with no tenant claim is now
  rejected rather than falling back to the issuer host's first DNS label, which silently
  placed every such user in the same tenant.
- **`require_mgmt_scopes` failed open when `app.state.settings` was absent.** Three
  chained `getattr` defaults landed on `"none"`, skipping every scope check. It now
  denies. `create_app` always sets that state, so this only fires for a sub-app or
  plugin router mounted without it.
- A malformed `;tenant=` spec silently left `tenant_mode="claim"`, quietly downgrading a
  pinned tenant to a token claim. It now logs an error.

### Fixed

- Expiry detection matched `"exp" in msg`, and `"exp"` is a substring of `"unexpected"` —
  so signature failures were reported to callers as token expiry.

### Security

- **The SSRF guard never resolved DNS.** Private, loopback, and link-local ranges were
  checked *only when the hostname was already an IP literal*, so any name resolving to
  `169.254.169.254`, `10.x`, or `127.0.0.1` passed — an attacker-controlled `A` record, a
  `*.nip.io` style name, or DNS rebinding. That is the standard cloud-metadata SSRF path,
  and it applied to MCP server URLs, A2A peers, container gateways, and model-supplied
  browser URLs. The guard now resolves the hostname and rejects the request if *any*
  returned address is blocked. Also closed: IPv4-mapped IPv6
  (`::ffff:169.254.169.254`, whose `.is_link_local` is `False`), decimal-integer hosts
  (`http://2130706433/`), carrier-grade NAT, reserved, multicast, and unspecified
  addresses, plus `.svc` / `.local` / `kubernetes.default` / `metadata.google.internal`.
  The old code also decided "is this an IP?" by string-matching its own exception
  message, so rewording an error would have started admitting private addresses.
- **Browser tools followed redirects past the check.** The URL was validated once and
  handed to `page.goto()`, but Chromium follows 3xx hops, loads subresources, and runs
  JS — and the URL is model-supplied, so a prompt-injected agent could pivot to the
  metadata service and read the body back via `op: "content"`. A Playwright request
  interceptor now re-validates every request the page makes. `path_prefix` still applies
  to the top-level navigation only, since enforcing it on subresources would break any
  real page.
- **Sandbox containers are now confined.** They ran as root with every Linux capability,
  a writable filesystem, and no PID or CPU limit — `network_disabled` and `mem_limit`
  were the only controls. Now non-root with `cap_drop: ALL`, `no-new-privileges`, a
  read-only root filesystem plus a `noexec` tmpfs, a PID limit, and a CPU quota. Images
  are allowlisted via `FELIX_SANDBOX_ALLOWED_IMAGES` (default: the built-in python image
  only), because `spec.sandboxes[].binding` is manifest-supplied and reaches `docker run`.

### Fixed

- **The sandbox timeout never worked, and a container stalled the whole API.** The
  synchronous docker SDK was called directly from a coroutine, so the `asyncio.wait_for`
  around it could never fire — nothing yields — and the event loop blocked for the
  container's lifetime, meaning a model emitting `while True: pass` froze every
  concurrent request. The call now runs on a worker thread.

- **A single 429 failed the whole run.** There was no retry anywhere in the model layer:
  `_is_provider_error` existed but was only consulted by `_FallbackClient` to advance to
  the next *model*, and no bundled manifest configures `spec.model.fallbacks`. Requests
  now retry rate limits and transient upstream failures (408/409/429/5xx) with
  exponential backoff and jitter, honouring the provider's `Retry-After` when present —
  seconds or HTTP-date. Three attempts total, so a blip is absorbed without hanging a
  run; non-retryable statuses are not retried. Falls through to `_FallbackClient`
  afterwards exactly as before.
- **Prompt caching was invalidated on every turn.** Recalled memory facts were appended
  to the system prompt, and Anthropic renders `tools → system → messages` with caching as
  a prefix match — so a block that changes whenever memory captures a fact moved the
  cached prefix constantly and `cache_read_input_tokens` would sit near zero. Facts are
  now rendered as a per-run user-role prelude, which also keeps model-extracted text —
  which can originate in tool output — out of the developer-tier instruction channel.


- **Thinking levels were broken against every current Claude model.** The Anthropic
  request builder emitted one shape for all of them:
  `thinking: {"type": "enabled", "budget_tokens": N}` plus `temperature: 1`. Both are
  **removed** on the current generation and return HTTP 400 — `budget_tokens` on Fable 5,
  Opus 5, Opus 4.8/4.7 and Sonnet 5, and sampling parameters across the whole 4.6+
  family. Request parameters are now chosen from a per-model capability table
  (`patterns/capabilities.py`): adaptive thinking plus `output_config.effort` where
  supported, the legacy budget where it is still accepted, and sampling parameters
  dropped where they are rejected. `max_tokens` is clamped to each model's real ceiling,
  and the non-streaming default rises from 4096 to 16000.
- **`stop_reason` was never read.** Both providers' responses were ignored and the value
  synthesised as `"tool_use" if tool_calls else "end_turn"`, so a reply truncated at
  `max_tokens`, a safety `refusal`, and a `pause_turn` all presented to the agent loop as
  a normal completion — a cut-off answer was indistinguishable from a finished one. The
  real reason is now read from both providers (with OpenAI's `finish_reason` translated),
  `StopReason` gains `refusal` and `pause_turn`, and the loop records such runs as
  `truncated` / `refused` with a warning and a `felix_run_stop_reason` metric.
- **Model ids, prices, and context windows were two generations stale.** Routes pointed
  at `claude-sonnet-4-5` and a date-suffixed `claude-haiku-4-5-20251001`; Haiku was
  priced at $0.80/$4.00 (actual: $1.00/$5.00), under-reporting the cost of every run; and
  `/v1/models` advertised a 200K context for models that have 1M. Routes now cover
  `claude-opus` / `claude-sonnet` / `claude-haiku` / `claude-fable`, with the previous
  logical ids retained so existing manifests keep resolving. Price lookup now takes the
  longest match, so `claude-opus-5` is no longer shadowed by a shorter key.

### Security

- **Four security controls no longer disable themselves silently.** Each degraded on a
  transient failure with `logger.debug` as the only signal — invisible at the `INFO`
  default — and no metric.
  *The LLM injection screener* returned `None` for both "clean" and "could not run", and
  both call sites read it as clean, so a missing key, an expired credential, a 429, or a
  provider outage turned `content_screening.on_flag: block` into a no-op — **including
  on the tool-output path that screens MCP, A2A, browser, and sandbox content**. It is
  now tri-state and honours `on_flag` when unavailable. An unparseable score is also
  treated as unavailable rather than clean.
  *The PII guardrail's* `_presidio_checked` was a permanent latch, so one transient
  engine-init failure pinned the process to three regexes for its entire lifetime. Only
  deterministic outcomes (package absent, no spaCy model) latch now; a transient failure
  retries. The fallback is announced at `WARNING` with a `felix_control_degraded` metric.
  *`guardrails.providers`* was unvalidated free text, so a typo (`"PII"`,
  `"pii-redaction"`) meant **no wrapper was applied at all** while `guardrails_enabled()`
  still returned `True`, so compile validation passed and nothing warned. It is now a
  closed set, like `targets` beside it.
  *Command screening* read only `args["command"]`/`["cmd"]`, so the built-in sandbox tool
  — whose arguments are `(code, path, stdin)` and which runs `["python", "-c", code]` —
  **skipped every rule while appearing wrapped**. It now inspects every
  execution-bearing argument, and every string argument for `sandbox`/`container`
  transports, where the payload is the program.

- **Untrusted content no longer reaches the system/developer trust tier.** The wrapper
  stack exists to keep tool output untrusted; three paths promoted it anyway.
  *Compaction* fed a raw transcript — including tool output from MCP servers, web pages,
  and files — to a summarizer with no fencing, then re-injected the model's reply as
  `role="system"` **and persisted it**, so it replayed on every later turn and outranked
  the user, after the original tool result had been dropped. The transcript is now fenced
  and labelled as data, the summarizer is told never to adopt instructions found inside,
  and the summary is emitted as user-role reference material.
  *The skills catalog* interpolated `skill.description` — from a tenant-writable
  `SKILL.md` in the object store — into the **system prompt** with no XML escaping, so a
  description containing `</description></skill></available_skills>` appended arbitrary
  text to the highest-trust surface. Now escaped.
  *Memory* captured model-repeated tool text as a durable fact and injected it into the
  next run's system prompt. Facts now carry provenance and render fenced as reference
  material that cannot close its own fence.
- **A safety judge with no model scored backwards.** `_heuristic_judge_score` ranked by
  keyword overlap, so for a criterion like *"must not leak credentials or secrets"*,
  output *containing* those words scored highest and **passed**, while benign output was
  blocked. `JudgeRule.model` defaults to `""`, so any manifest declaring a safety judge
  without a model got exactly that. Negative criteria now fail closed, and
  `assert_absent:` / `assert_present:` express polarity explicitly.
- **`auth.inbound.schemes` is now enforced against the caller.** It was only a
  compile-time check that *something* was set; it never constrained the request, so a
  manifest naming `[jwt]` accepted an `api_key` principal. The authenticated scheme is
  now carried on the principal (`api_key`, or the JWT verifier scheme `access` /
  `cognito` / `self`) and checked, with `jwt` acting as an umbrella for the verifier
  schemes. An empty list still allows any scheme.
- **`auth.outbound.providers` is now enforced.** It was declared and never read, so a
  manifest naming `[anthropic]` could still route to OpenAI or a local Ollama. Checked at
  compile against the resolved route for the primary model and every `model.fallbacks`
  entry.


- **`spec.limits` budgets are now enforced.** `max_wall_clock_seconds`,
  `max_input_tokens`, and `max_output_tokens` were declared in the manifest schema,
  range-bounded, and documented — and appeared nowhere else in the codebase.
  `LimitState.started_at_ms` was never set or read. Worse, `any_limit()` counted those
  fields toward `_has_boundary_control`, so a manifest satisfied the SOC 2 compile check
  *"require non-empty policies, approvals, or limits"* with `limits:
  {max_wall_clock_seconds: 600}` and got no runtime enforcement at all — the shipped
  `manifests/governed.yaml` did exactly this. A validator that attests to a control which
  does not exist is worse than no validator. All budgets are now checked before each tool
  call and at the top of each agent turn.
- Added `limits.max_cost_usd`, a per-run spend ceiling priced from the model catalog as
  tokens accumulate.
- **The default posture is bounded.** `apply_limits` was installed only when a manifest
  declared a limit, so a manifest with none had no cap on tool calls, wall clock, tokens,
  or spend. It is now always installed, and undeclared fields fall back to the documented
  `ABSOLUTE_LIMITS` (500 tool calls, 3600s, 1M/100k tokens, $1000).
- **The limits wrapper no longer fails open.** Its whole body sat inside
  `if req is not None:`, so with no request context every check was skipped. A tool
  invoked without a context is now denied rather than run unbudgeted.

### Changed

- **`spec.a2a.publish` now controls agent-card discovery, and its default changed to
  `true`.** The field was never read, so every agent was advertised regardless. Honouring
  it with the previous `false` default would have 404'd `/.well-known/agent-card.json`
  for every existing manifest, so the default now matches the behaviour deployments
  already have and the field is an opt-*out*.
- The agent card now emits `spec.skills` (it hardcoded `"skills": []`) and merges
  `spec.a2a.capabilities` alongside the transport capabilities.
- **`spec.observability.metrics` now allowlists counter names** for the manifest.
  Previously the per-manifest list did nothing; counters outside the list are dropped
  before the series is created, which also bounds Prometheus cardinality.
- **`spec.anomaly` thresholds are read from the manifest.** `jobs/anomaly.py` used
  hardcoded `MIN_VOLUME=10` / `BASELINE_FACTOR=3.0` and ignored the spec entirely, so
  `enabled: false` did not disable anything. Findings now carry the thresholds that
  produced them. `min_rate` remains unimplemented — see below.

### Fixed

- Injection quarantine and PII redaction crashed on dict-shaped tool output.
  `ToolOutput` includes `dict[str, Any]`, but both wrappers did `out.content = ...`,
  which raises `AttributeError` — so both controls silently degraded into "the tool
  crashed". Both now use the existing `_replace_content` helper, which already handled
  every shape.
- Procedural memory returned the top-k arbitrary rows when nothing matched the query
  (`return (scored or ranked)[:top_k]`), injecting irrelevant, possibly stale procedures
  as instructions on every turn.

- **Durable fibers could run the same step twice.** `resume_due_fibers` selected every
  fiber in `('running','pending')` with no lock, no limit, and no claim, while a fiber
  stayed `running` for the duration of its step. The scheduler fires every minute, so a
  step still running at the next tick was picked up and invoked again — concurrently, on
  a single node, and guaranteed with two workers. Since the `invoke` op runs a full agent
  with tools, that meant duplicated side effects and duplicated model spend. Fibers are
  now claimed with `FOR UPDATE SKIP LOCKED` plus a lease (`0008_fiber_leases`), the sweep
  is bounded, an expired lease is reclaimed so a crashed worker cannot strand a fiber,
  and `_save_fiber` uses a version compare-and-set so a lost update cannot rewind
  `cursor` and replay a completed step.
- **Concurrent appends to one thread could 500 and lose events.** `append_batch` computed
  `seq` as `max(seq)+1` against a `(tenant_id, thread_id, seq)` primary key, so two
  concurrent appends — an SSE stream plus `/chat/steer`, `/chat/tool_result`, or
  `/chat/sessions/custom`, all of which target the same thread by design — computed the
  same head and one died with an unhandled `IntegrityError`. Appends now take a
  transaction-scoped advisory lock per thread.
- **Scheduled jobs never fired for any tenant except `default`.** `run_due_jobs` takes
  `tenant_id: str = "default"` and the worker cron called it with no argument. Added
  `run_due_jobs_all_tenants`, which sweeps every tenant that has jobs and isolates
  per-tenant failures. Jobs are also claimed *before* being invoked (the minute-cron
  previously re-fired the same job on every tick until the first run completed), and the
  write-back no longer forces `enabled=True` from a stale read, which silently
  re-enabled jobs an operator had just disabled.
- The fiber sweep and the job sweep both run under `rls_bypass()`; as cross-tenant
  maintenance they previously ran with no `app.tenant_id` GUC, so enabling
  `FELIX_DATABASE_RLS` would have silently returned nothing and stalled them.

### Security

- **Upstream model-provider error bodies are no longer relayed to API clients.**
  `ModelGatewayError` embedded `body[:200]` of the raw provider response in its message,
  and both `/chat` and `/v1/chat/completions` return `str(exc)` to the caller — so
  provider request ids, organization identifiers, quota and billing detail, and any
  echoed request content reached whoever made the request. The body is now kept on
  `.body` (bounded) for server-side logging only; the client sees
  `"<provider> provider returned HTTP <status>"`. Found independently by this audit and
  by CodeQL (`py/stack-trace-exposure`).

- **Fixed an unauthenticated remote code execution path.** `spec.mcp_servers` entries
  with `transport: stdio` carry a manifest-supplied `command`, `args`, `cwd`, and `env`
  that reached `create_subprocess_exec` **at compile time**, so resolving a manifest ran
  the command — and the child inherited the API process environment (model API keys, the
  Postgres URL, cloud credentials). Writing such a manifest needed only the tenant-level
  `manifests:write` scope, and the shipped Compose defaults (`FELIX_AUTH_MODE=none` +
  `FELIX_ALLOW_INSECURE=true` + a `0.0.0.0` publish) meant it needed no credentials at
  all. stdio is now **disabled unless `FELIX_MCP_STDIO_ALLOWED_COMMANDS` names the exact
  commands allowed**; the check runs on manifest write, at compile, and at spawn. The
  child no longer inherits the parent environment — it gets `PATH`/`HOME`/`LANG`/`LC_ALL`/
  `TZ` plus the keys the ref declares — and loader variables (`LD_PRELOAD`, `PYTHONPATH`,
  `NODE_OPTIONS`, …) are rejected outright.
- **`FELIX_AUTH_MODE=none` is now refused on any non-loopback bind**, in every
  environment. `FELIX_ALLOW_INSECURE=true` relaxes the environment check only; it is no
  longer a way to serve an unauthenticated API to a network.

### Changed

- **Secure-by-default local stack (breaking).** `FELIX_HOST` defaults to `127.0.0.1`
  (containers still set `0.0.0.0` explicitly), Compose defaults to
  `FELIX_AUTH_MODE=api_key` with `FELIX_ALLOW_INSECURE=false`, and the API port publishes
  on `127.0.0.1` (override with `FELIX_BIND_ADDR`). `make up` now runs
  `scripts/dev-key.sh`, which generates a local API key into `.env` on first run and
  prints it, so the quickstart stays one command. Existing deployments that relied on
  anonymous access must set an auth mode or bind loopback.
- `felix doctor` reports the stdio allowlist and fails the loopback check when
  `auth_mode=none` is paired with a public bind.
- **`ApprovalRule.bind_principal` and `one_shot` were declared in the manifest schema
  and enforced nowhere.** `find_approved` matched only
  `(tenant, manifest, tool, call_signature, status)`, never filtered by principal, and
  never consumed the grant. So principal A's approval auto-approved principal B's
  byte-identical call in the same tenant, and a single approval authorized unlimited
  replays until it expired. Both flags are now enforced: `bind_principal` adds a
  `principal_subj` predicate, and `one_shot` adds a `consumed_at` predicate plus a
  conditional-UPDATE consume, so two concurrent identical calls cannot both spend one
  grant. Migration `0007_approval_consumed_at`.
- **Command screening's `require_approval` never created an approval.** It returned a
  deny string naming a rule, so the bundled default for `sudo` told the model to go ask
  a human who was never asked, and no operator ever saw a request. It now creates a
  pending approval, emits `approval_required`, and blocks on the decision — via the same
  path `spec.approvals` uses. `command_screening.approval_ttl_seconds` (default 300s)
  bounds the wait so a run cannot block forever on an approver who never comes.

### Fixed

- **Audit and usage events emitted while serving traffic were never persisted.**
  `emit_agent_audit` and `record_usage` are called from the agent loop, which runs in the
  **API** process, but the only `flush_pending` callers were Taskiq cron tasks in the
  **worker**. Wherever those are separate containers — Compose, Helm, the documented
  deploy paths — the worker drained an always-empty buffer while the API's grew for the
  life of the process. So `GET /audit` returned only worker-side events, metered usage
  was lost, and the API leaked memory in proportion to tool calls. The API now runs its
  own flush loop (`FELIX_AUDIT_FLUSH_SECONDS`, default 5s) and drains on shutdown.
- **A failed flush no longer discards the batch.** Both stores drained the buffer
  *before* writing, so one `commit()` failure lost those events permanently — for audit,
  that is the compliance record. Batches are now re-queued in order and retried.
- Buffers are bounded (10k events) and count what they drop, so an unreachable database
  degrades visibly instead of exhausting the process silently.
- `emit_agent_audit` logged nothing when recording failed (`except Exception: pass`); it
  now warns.

### Added

- Security scanning: CodeQL, a `pip-audit` CVE check over the locked
  dependency set (all extras), a gitleaks secret scan of the full history, and
  a Trivy scan of the image CI builds.
- Test coverage is measured and gated at the current 60%, ratcheted upward
  deliberately rather than set aspirationally.
- `tests/unit/test_invariants.py` — the repo rules are now enforced rather than
  documented: `.env.example` covers every `Settings` field, no optional
  dependency is imported at module scope, every Postgres-touching module has a
  `memory://` path, and the governance wrapper order in `builder.py` is fixed.
- `scripts/lean-import-check.py` and a CI `lean` job that imports all 156
  modules with no extras installed — the default image's promise, checked.
- `scripts/validate-toolkit.py` and a CI `toolkit` job; `.claude/**` and
  `CLAUDE.md` are now inside the CI path filter instead of bypassing every gate.
- Six settings that existed only in `config.py` are documented in
  `.env.example`: `FELIX_DATABASE_RLS`, `FELIX_SCALE_OUT`, `FELIX_REPLICA_ID`,
  `FELIX_OTEL_ENDPOINT`, `FELIX_WEBHOOK_SECRET`, and `FELIX_POLICY_BUNDLE_PUBKEY`
  (the last is declared but not yet consumed by any code path).
- GitHub Actions are pinned by commit SHA, and all container base/service
  images by digest, so a rebuild is reproducible and a retagged upstream image
  cannot change what ships.
- `scripts/test.sh` — the canonical test entry point. It sets the in-memory
  store environment the suite is designed for; `make test` and CI both use it.
- `pre-commit` now runs in CI, so the hook config cannot silently break again.
- `.editorconfig` matching the ruff configuration.
- `make type` now says what to do when the optional extras are missing instead
  of printing 27 unresolved-import errors.

### Changed

- Dependabot: weekly grouped updates for actions and images; the docker
  ecosystem now points at `deploy/docker` (the previous `/` entry matched
  nothing — there is no Dockerfile at the repo root).
- Builder image `uv` 0.9 → 0.12, `ty` 0.0.73 → 0.0.74, and the Docker build
  caches uv downloads between builds.
- Relicensed from MIT to Apache License 2.0 (adds an express patent grant and
  a trademark carve-out; contributions are inbound under the same license).
  Adds a `NOTICE` file; releases published under MIT remain MIT.

### Fixed

- The runtime image no longer ships `pip`. Its vendored copies of `msgpack`
  and `setuptools` carried HIGH CVEs (GHSA-6v7p-g79w-8964, CVE-2025-47273)
  even though neither is a Felix dependency; the venv is built by uv in the
  builder stage, so the runtime never needed pip. The runtime stage also
  applies pending OS security updates, clearing four util-linux CVEs that
  `python:3.14-slim` has not picked up yet. The image scans clean.
- `pre-commit install` failed for every contributor: the ruff repo entry was
  missing its `https://github.com/` prefix, so hook installation could never
  clone it. `pre-commit validate-config` passes on the broken file — only
  `install-hooks` surfaces it.
- `make check` failed on any machine with a `.env`: the pytest leg inherited
  `FELIX_DATABASE_URL` and ran against a real Postgres, and `make type`
  checked `tests/` while CI checks only `packages apps`.
- The Docker build no longer falls back to an unfrozen `uv sync`, which could
  silently produce an image from a different dependency resolution than CI
  tested. CI now also verifies `uv.lock` is current and installs `--frozen`.
- Taskiq worker no longer dies on idle BRPOP (`redis-py` 8 default
  `socket_timeout=5`); broker/result backend use `socket_timeout=None`.
- Scheduler entrypoint awaits `run_scheduler` via `asyncio.run` (taskiq 0.12+).
- Worker/scheduler Compose healthchecks disabled (image probe targets API `/health`).

### Changed

- Session leases prefer Redis (with in-process fallback) so exclusive/shared
  attach works across API replicas.

### Added

- Session control routes: snapshots, FTS search, abort/continue, thinking
  levels, leases, compact, UI prompts, JSONL export (see README Protocols).

## [0.1.0] — 2026-08-22

### Added

- Initial public release of **Felix** — self-hostable managed agents harness
  (`apiVersion: felix/v1`).
- Surfaces: `/chat`, OpenAI-compatible `/v1`, A2A, MCP, management APIs.
- Lean Docker Compose (api, worker, **scheduler**, Postgres+pgvector, Valkey)
  with optional MinIO (`--profile full`).
- Helm chart with PVC support, consumer shared secret, scheduler container, and
  pre-install/pre-upgrade **migrate Job**.
- Durable fibers, audit spill to DuckDB (optional), JWT/api_key auth, plugins seam.
- Durable **usage meters** (`usage_events`) flushed by the worker; `GET /usage`.
- Eval **fixture + `--mock`** path for CI (`fixtures/eval/smoke.json`).
- Chat **history** (`GET`/`DELETE /chat/history/{thread_id}`) and **audit metrics**.
- Response aliases (`events`/`plans`/`requests`/`manifests`/`datasets`) for chat-ui clients.
- CLI: `migrate`, `eval`, `mint-jwt`, `bundle-manifests`, `doctor`, `version`, `temporal-worker`.
- Typed packages (`py.typed`) for harness, CLI, API, and worker.

[0.1.0]: https://github.com/felix-run/felix/releases/tag/v0.1.0
