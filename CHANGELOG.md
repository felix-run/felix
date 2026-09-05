# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **An end-to-end test layer: `tests/e2e/`.** Every HTTP test in the repo cut the chain at one
  of two places — the ones that reached a route replaced `build_tenant_agent` with a stub, and
  the ones that ran the real compiler and loop never opened a socket. So the path a request
  actually takes was covered in pieces and never end to end, which is the defect shape the repo
  names: the branch production takes is the branch nothing covers. The new layer boots the
  zero-argument `create_application()` that Granian is handed in production, sends real HTTP
  through it, and maps every logical model id to `felix_ai.providers.scripted` — a real
  provider, deliberately absent from the production registry — so the middleware stack,
  manifest resolution, the compile, the governance wrappers, the pattern and the reply controls
  are all the production ones. Nine tests pin a tool call travelling the whole stack, the route
  actually taken, a scoped policy denying a tool for a caller without the scope, PII scrubbed
  from the reply on both `/chat` and `/chat/stream` (the streaming path being the one whose
  failure mode is tokens already sent), the turn metered and priced, the audit trail from
  prompt to answer, the SSE frame contract including the tool's output, and `api_key` mode
  refusing an unauthenticated request. Every one is proved by mutation: nulling
  `apply_policies`, `apply_reply_controls`, `record_usage`, the tool-call audit emit and its
  payload, the `tool_start` frame, the model resolution, the calculator handler and the
  missing-credential check each turns its test red.

### Fixed

- **The test suite could spend real money.** `scripts/test.sh` redirected the database and the
  object store to in-memory twins but left the vendor credentials as the repo `.env` set them,
  so any test that reached a model called the vendor for real. Not hypothetical: the first run
  of the new e2e suite billed live Anthropic calls, because `.env` also overrides
  `default_model_id` and the route the fixture believed was scripted was not. The runner now
  blanks every credential-bearing setting — including `FELIX_MODEL_PROVIDER_OPTIONS`, whose
  per-provider `api_key` `resolve_provider_config` prefers *over* the named vendor fields, so a
  key there re-armed a vendor while the obvious two looked safe — and a new invariant
  enumerates them from `Settings` so a credential field added later fails the suite instead of
  leaking into it. Blank keys stop the billing but not the egress (an unauthenticated request
  is still sent), so `tests/e2e` additionally maps every logical model id to the scripted
  provider and refuses to boot if any route still resolves to a real one.

- **Final-response judges and PII guardrails now govern the reply on the streaming path.**
  `wrap_final_response_judges` passed events straight through on `stream_events`, so the
  only outbound model-call control was inert on the primary chat surface; and
  `apply_guardrails` wrapped tools only, so `guardrails.targets: [input, output]` scrubbed
  user input and tool output and let the reply through untouched — `governed.yaml`
  declared a reply scrub that did not exist. Both now wrap the agent: on `invoke` the
  reply is screened before it returns; on a stream the reply's text is held until the run
  ends and released screened (or replaced by the denial), while tool, approval and session
  frames stream as they happen — including the `session_progress` envelope the react
  loop carries each delta in a second time, and every assistant turn, not only the final
  one. `output` means everything leaving the model boundary — tool output and the reply —
  and `final_response` the reply alone, which no longer wraps tools. A redaction or denial
  emits a `guardrails_reply` / `judge_deny` audit event. The mechanics live in
  `felix/governance/reply.py`; the builder slot is `apply_reply_controls`. Not covered, and
  said so in `deploy/GOVERNANCE.md`: the session log keeps the reply as produced, so a
  resume replay carries the unscreened text.

- **`/ready` and `/live` no longer require a credential.** The Helm chart probes both, and
  kubelet sends no `Authorization` header, but only `/health` was on the public allowlist —
  so under `api_key` or `jwt` every pod's readiness probe got 401, the pod never became
  Ready, and the liveness probe restarted it. Compose probes `/health`, which is why local
  runs never showed it. Both paths are now public and exempt from rate limiting (kubelet
  treats a 429 as a failure), and because `/ready` probes the database, cache and object
  store, its report is cached for two seconds and concurrent callers share one probe, so
  an anonymous caller can hammer the route and not the dependencies. The public body now
  says which probe failed and not why — the detail was the exception text, naming internal
  hosts, ports and database users — and the detail is logged instead. One `PROBE_PATHS`
  set feeds both allowlists, and `tests/unit/test_operability.py` reads the probe paths
  out of the chart, the Dockerfile and the Compose files and asserts each is declared there.
- **Compose migrates before it serves.** A one-shot `migrate` service runs `felix migrate
  head` against Postgres, and api, worker and scheduler wait for it to complete. Before
  this the schema was a separate `make migrate` from the host — which `make up-lite` and
  `make up-gcp` cannot even reach, since they publish no database port — and a first
  `make up` served a schemaless database that `/ready` (a `SELECT 1`) reported Ready. The
  gcp overlay pulls the same published image for the migration, the Temporal overlay's
  worker waits on it too, and the pgbouncer overlay leaves it on Postgres rather than the
  pooler. The Helm chart had a pre-install migrate Job all along.
  `tests/unit/test_compose_migrate.py` pins the wiring, and the CI docker job now runs
  `scripts/check-compose-render.py` over the rendered config, because one property is
  invisible to a parse: `build: !reset null` applied through a YAML merge key does not
  reset, and the published-image overlay would quietly build on the host again.
- **The Helm chart runs each process in its own Deployment.** api, worker and scheduler
  were three containers in one pod, so the HPA scaled the scheduler with the api and every
  replica enqueued every cron sweep — N replicas, N audit flushes, N retention passes, N
  fiber resumes per tick. Now `<release>-api` is what the Service, the PDB and the HPA
  select; `<release>-worker` has its own `worker.replicaCount`; `<release>-scheduler` is
  always one with a `Recreate` strategy. Three more chart defects went with it: the worker
  had no model or object-store credentials although it runs the agent loop (fiber resume,
  continuous eval) — it has them now, and deliberately not the JWT signing key or the
  `/internal` shared secret, which nothing on its path reads and which a tool primitive
  reached through model output must not find; the PDB's `minAvailable: 1` against the
  one-replica default denied every node drain, so it is `maxUnavailable: 1` now; and
  `image.repository: felix` resolved to docker.io/library/felix, which does not exist, so
  a fresh `helm install` went straight to ImagePullBackOff — it is `ghcr.io/felix-run/felix`.
  The api gets a 120s termination grace and a preStop sleep so an SSE turn in flight
  finishes; the worker gets a liveness probe on its metrics port (`worker.metricsPort`,
  9464), because a wedged worker was invisible and never restarted.
  `tests/unit/test_helm_topology.py` asserts all of it on what `helm template` renders,
  and CI runs it with `FELIX_REQUIRE_HELM=1` so a missing binary fails rather than skips.
  **Breaking for values files:** `replicaCount`, `resources`, `autoscaling` and
  `podDisruptionBudget` moved under `api.`, and the old top-level keys fail the render
  with a message naming the new one rather than being silently ignored. Upgrading
  replaces the old Deployment (immutable selector), so the api is briefly absent — see
  `deploy/helm/README.md`.
### Added

- **Usage rows carry their cost, and `GET /usage/summary` adds it up.** `record_tokens`
  wrote token counts and the *logical* route name and no cost, so `GET /usage` returned
  raw rows and nothing could answer "what did tenant X spend last month" — and anyone
  recomputing later fed the logical name to the price table and got `$0` for every custom
  route. Migration `0011_usage_cost` adds `cost_usd` and `wire_model_id`; the cost is fixed
  at write time, by the wire id and any `spec.model.price` override, so a later rate change
  does not rewrite history. `GET /usage/summary?since_ms&until_ms&manifest_id` groups by
  manifest, model and UTC day with totals (default: the last thirty days). The contract runs
  against both backends in `tests/conformance/test_usage_store.py`.
- **`spec.model.price` now prices usage.** It was documented as overriding cost
  attribution and only decorated the `/v1/models` listing. `build_model` carries it on the
  client, every metering site passes it, and the budget and the row use it.
- **`felix_model_unpriced`** counter: a turn reported usage but Felix has no rate for the
  wire model and the manifest sets none, so it was metered at `$0` — the signal that
  `limits.max_cost_usd` is failing *open* (the roadmap had this the wrong way round).
- **Inbound screening reaches every entrypoint.** `content_screening.enabled` ran on
  `/chat`, `/v1` and A2A. A cron job's prompt (writable with `jobs:write`), an eval item's
  `user_input` (writable with `eval:write`), `/chat/continue` and a resumed durable fiber
  reached the agent unscreened, and a tool call made directly over MCP executed a governed
  tool on unscreened arguments. All screen now; MCP screens the argument tree, and a flagged
  argument refuses the call whatever `on_flag` says, because there is no turn to quarantine.
  The screen is a wrapper the compile puts around the agent, so there is no entrypoint to
  forget; the HTTP routes still screen before the agent exists (to answer 422 before a
  stream opens or a durable run is enqueued) and mark the request so it is not paid twice.
  A screening refusal no longer reports the screener's score, and emits a
  `felix_inbound_screening` counter and an `inbound_screening` audit event (surface and
  action, no content). The model screener reads a turn or an argument set in windows, so a
  long benign prefix cannot push a payload past its window; an argument set over eight
  windows or 256 strings is refused as `oversize` rather than screened. Input PII applies
  to arguments too — values are redacted in place, PII in a key refuses. A screener
  *outage* under `on_flag: block` puts a durable fiber to sleep for a minute and retries
  the step, up to ten times, instead of failing every in-flight run for a provider blip.
- **A manifest cannot store a credential, and a read cannot return one.**
  `assert_no_plaintext_secrets` ran at compile and only under `forbid_plaintext_secrets` or
  in production, so `PUT /manifests` stored `auth: Bearer sk-...` and `GET /manifests/{name}`
  handed the token to the lower `manifests:read` scope. `PUT /manifests` and
  `felix validate-manifest` now run one write-time validator (`validate_for_write`) — they
  had drifted, the CLI saying `ok` to what the route refused. Always refused: a
  credential-shaped `auth` or `env` value (the heuristic now knows vendor-prefixed keys,
  JWTs, `user:password`), a URL carrying `user:password@`, a stdio command off the
  allowlist, and a sandbox image outside `FELIX_SANDBOX_ALLOWED_IMAGES` (which stored fine
  and raised inside every build); under `forbid_plaintext_secrets` or production, every
  non-ref `env` value, as before. Every read — resolved, by version, and the write's own
  echo — replaces any literal `auth`, non-ref `env` value and URL userinfo with `[REDACTED]`;
  `secret:NAME` refs are untouched. `cowork.yaml` no longer allows anonymous callers: it
  binds a shell on the developer's machine, and under `FELIX_AUTH_MODE=none` the approvals
  that gate it are anonymous too — so `make dev` cannot drive cowork; the Compose stack can.
- **Retention now covers every table the harness appends to.** The nightly sweep pruned
  `audit_events`, `plans` and `memory_vectors`; `fibers` (which carry the caller's principal and
  scopes), `usage_events`, `a2a_tasks` and `session_events` grew forever. Each TTL is a setting:
  `FELIX_AUDIT_RETENTION_DAYS` (30), `FELIX_USAGE_RETENTION_DAYS` (365),
  `FELIX_FIBER_RETENTION_DAYS` (7, terminal fibers and finished A2A tasks) and
  `FELIX_SESSION_RETENTION_DAYS` (0 = keep; when set, whole threads whose events *and*
  metadata are older than the TTL, so a fresh fork of an old conversation survives). A manifest's
  `governance.retention_days` — previously read by nothing — now shortens the audit TTL for its
  own rows, capped by the operator's. On `memory://` the sweep never pruned audit rows (it
  filtered the flush buffer, not the store) and swept plans for tenant `default` only; both fixed.
- **A durable step that keeps failing is buried, not retried forever.** A failure outside the
  invoke's own handler (a save that could not land, a store that was down) released the fiber
  and the next tick claimed it again, once a minute until `expires_at`. Fibers now carry
  `attempts` (migration `0012_fiber_attempts`); a failed step sleeps for a doubling backoff
  (1m, 2m, 4m, 8m at the default, capped at 1h) and at `FELIX_FIBER_MAX_ATTEMPTS` (5) the
  fiber is `dead`, with the error on the run view. `dead` is terminal to the SDK poller, the
  resume stream, the Temporal loop and the retention sweep (which reads the fiber store's
  terminal set rather than its own copy), under an invariant.

### Changed
- **The session summarizer is billed to the tenant that triggered it, and counts against
  the run's budget.** Compaction wrote its own usage row against the literal tenant
  `"default"`, priced it by the logical route name (which the price table does not know,
  so custom routes priced at $0), and never touched the run's `limit_state` — on a long
  thread the single largest input-token call escaped `limits.max_cost_usd` and the token
  caps, and landed on another tenant's bill. The `summarizing:N` strategy recorded nothing
  at all. Both now go through `record_model_usage` like the turn itself: attributed to the
  request's tenant and manifest, priced by the wire model id, and tagged
  `meta.kind: compaction | summarization` so the row can be told apart from the turn.
  `POST /chat/compact` runs under a context naming the thread's manifest for the same
  reason. A metering failure is logged at warning rather than swallowed.
- **The turn's reported cost was priced by the logical route name.** The react loop
  metered by the wire id but built the `usage` block it reports with `model.model_id`,
  the operator's route name — so every custom route reported `$0` on the turn while
  being budgeted correctly. `record_usage` now returns the priced block and the loop
  reports that. One `record_model_usage(result, model, ...)` replaces nine hand-copied
  call sites across the pattern loops, and `record_usage` grew a `meta` argument.

- **The fallback and escalation composites moved to `patterns/model_composites.py`.** They
  are private, so nothing outside the repo is affected; tests that reached for
  `_FallbackClient` or `_is_provider_error` import from the new module.

  Failing over to another model and escalating to a stronger one are a policy about what to
  do when an answer is unavailable or not good enough — a different subject from resolving a
  route and metering a turn. `patterns/model.py` is **522 lines, under the 600 budget for the
  first time**, and holds route resolution, `record_usage`, the traced wrappers and the
  provider factories.


- **`stream_turn` is a separate Protocol, not a required member of `ModelProvider`.** This
  reverses a published contract, so it matters to anyone implementing a provider:
  `felix_ai.types.ModelProvider` no longer requires `stream_turn`, and
  `StreamingModelProvider` plus the `supports_stream_turn` predicate are new public exports.

  Requiring it claimed every provider must stream, while the scripted provider, the wire
  clients, four call sites and the traced wrapper all treated it as optional. It also had a
  consequence nobody had noticed: `ModelProvider` is `runtime_checkable`, so a provider
  implementing the whole documented contract **failed `isinstance` against its own
  Protocol**. Nothing to change in a provider that already worked — this only stops the
  type system contradicting the docs.

  Probe with `supports_stream_turn(client)` rather than
  `getattr(client, "stream_turn", None)`. It is a `TypeIs`, so it narrows in both
  directions, and it tests `callable` — a wrapper that forwards attributes can answer to
  the name without implementing it.

### Fixed

- **An escalating turn was billed twice and metered once.** `confidence_escalation` calls the
  primary, judges the answer, then calls the stronger model — two billed turns for one reply —
  and returned only the second. Callers meter what they are handed (`record_usage(result, …)`
  sees the returned result and nothing else), so the discarded turn reached neither
  `ctx.limit_state` nor the usage table and `limits.max_cost_usd` under-counted the run by
  whatever it cost. Measured on a plausible pair: **2,100 tokens billed, 100 metered**.

  The composite folds the discarded usage into the result it returns, because it is the only
  thing that can see both turns. Both turns are summed under the returned model's id, so a
  two-model escalation prices the primary's tokens at the target's rate — wrong in the third
  decimal, right about the budget counting every token spent.

  `confidence_escalation` is a published manifest field that had no behavioural test at all;
  `tests/unit/test_confidence_escalation.py` is its first.


- **A fallback chain that could not stream produced an empty answer and made no model
  call.** `_FallbackClient` skips members without `stream_turn`, so a chain of chat-only
  providers streamed from none of them and fell out of the loop having yielded nothing —
  and it could not warn the caller off, because the composite defines `stream_turn`
  unconditionally and so answers `supports_stream_turn` with True whatever its members do.

  `react` happened to recover (`if result is None: result = await model.chat(...)`).
  `delegating` did not: its return sits inside the streaming branch, so a `plan_execute` or
  `parallel` run with `spec.model.fallbacks` and a chat-only provider synthesised an empty
  answer — unlogged, and unmetered because no inference occurred. The composite settles it
  now the way `_EscalationClient` already did: resolve with `chat()`, chunk for display. One
  inference, correctly metered, and the contract is true of the composite rather than of one
  of its two callers.


### Added

- **A model call is a span, and spans are one trace.** Felix emitted exactly two spans —
  `tool.call` and `manifest` — and both were orphan roots, because `tracer.start_span` does
  not touch the active context and nothing established a request-level one. A single chat
  therefore produced two or three unrelated single-span traces. Worse, the one operation
  carrying token usage, model name, provider and cost — the model call — had no span at all,
  so the thing a tracing backend most wants was the thing it could not see.

  Spans now attach to the OTel context, so they nest; `opentelemetry-instrumentation-fastapi`
  (declared in the `otel` extra since it was added, and never imported) opens the request root
  and honours an inbound `traceparent`; and every model client built through `build_one_model`
  is wrapped so each provider call emits a `chat {model}` span carrying the OTel **GenAI
  semantic conventions** (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.*`) plus
  Felix's cache and cost attributes. Borrowed rather than invented names, so any OTLP consumer
  renders these as generations rather than anonymous timing bars.

  The wrapper sits on the leaf client, not the composite, so a fallback chain that tries two
  providers shows two spans instead of hiding the retry inside one. It also does **not**
  define `stream_turn` unconditionally: that capability is detected with
  `supports_stream_turn`, and a wrapper that always had it would push every
  non-streaming provider into the streamed path — where `record_usage` is never reached and
  the turn escapes every token and cost limit.

- **The worker reports something.** It never called `configure_logging` or
  `setup_observability`, and it has no HTTP server, so every fiber resume, audit/usage flush,
  retention sweep, anomaly scan and continuous-eval run was invisible: one log line each, no
  counter, no span, nothing to scrape. A sweep that had stopped firing looked exactly like a
  sweep that ran and found nothing — the confusion `deploy/GOVERNANCE.md` warns about for the
  per-tenant detection controls.

  All eight scheduled tasks now emit `felix_worker_task{task,status}` and
  `felix_worker_task_seconds`, the worker configures logging and tracing at startup and flushes
  spans at shutdown, and `FELIX_METRICS_PORT` (default `0`, off) exposes its counters.
  That endpoint is unauthenticated — the worker has no auth middleware — and carries the same
  tenant-supplied label values as the API's `/metrics`, so it must stay on an internal network.

- **`docs/OBSERVABILITY.md`** — the metric catalog and span/attribute schema, including which
  counters to watch and why `/metrics` is authenticated and rate-limited.
  `tests/unit/test_metric_catalog.py` re-derives both tables from the source, so a metric
  added without a row fails, and so does a row describing a metric nothing emits.

- **Latency exists.** `record_histogram` had been exported, documented and unit-tested with
  zero call sites, so there was no latency data anywhere. `felix_model_call_seconds`,
  `felix_tool_call_seconds` and `felix_worker_task_seconds` are its first three. Tool latency
  is labelled by transport and manifest and deliberately *not* by tool name: MCP tool names
  arrive from a remote server and are unbounded label values.

- **`felix_buffer_dropped`** — `DurableBuffer` counted and logged dropped audit/usage rows but
  exported nothing, so silent data loss could only be found by reading logs afterwards.

- **OTLP can reach an authenticated collector.** The exporter was hardcoded to gRPC with
  `insecure=True` and no header support, which meant no hosted or TLS-terminated OTLP endpoint
  was reachable at all. `FELIX_OTEL_PROTOCOL` (`grpc`/`http`), `FELIX_OTEL_HEADERS`,
  `FELIX_OTEL_INSECURE`, `FELIX_OTEL_SERVICE_NAME` and `FELIX_OTEL_SAMPLE_RATIO` are new;
  header parsing splits once so base64 credentials survive their `=` padding.

- **`FELIX_OTEL_CAPTURE_CONTENT`** (default `false`) — prompts and completions stay off spans.
  A tracing backend is an egress destination, and span attributes are not covered by the
  governance content screening that guards tool output.

- **`make up-observability`** — `deploy/docker/compose.observability.yml` brings up an OTel
  Collector, Prometheus, Grafana, Jaeger, Loki and Postgres/Valkey exporters against the base
  stack, with datasources and one dashboard provisioned. The dashboard is built from counters
  that actually exist, and its governance row surfaces the controls `deploy/GOVERNANCE.md`
  tells operators to watch — `felix_control_unavailable`, `felix_untrusted_tools_unscreened`,
  `felix_policy_unsatisfiable`, `felix_rule_targets_nothing` — which nothing previously showed.

  Three decisions the files explain in place, each found by running the stack rather than
  reading it. The overlay rebuilds the image with the `otel` extra, because the lean default
  omits it and OTLP export then degrades to a single warning line while everything else looks
  healthy. Logs arrive over OTLP rather than from a `filelog` receiver over
  `/var/lib/docker/containers`, which needs the collector to run as root, hands it every
  container's logs, and opens zero files without saying so. And optional-overlay scrape targets
  are file-discovered, so a service that is not running is absent rather than permanently down.

- **`scripts/metrics-token.sh`** — mints a *second* API key with an empty `scopes` array and
  merges it into `FELIX_AUTH_API_KEYS` beside the operator key. `/metrics` has no scope gate,
  so the result reads metrics and is refused everywhere else; leaking it does not leak write
  access to `PUT /manifests`. Prometheus has no env expansion in scrape configs, so the bare
  token is written to a gitignored `deploy/docker/.metrics-token` (mode 600).

- **`serviceMonitor` Helm template** (gated, off by default) — the Kubernetes analogue, reading
  the same kind of unscoped key from a Secret.

- **`FELIX_OTEL_LOGS`** — ships the log stream over OTLP with `trace_id`/`span_id` stamped from
  the active context, so a log line joins to the span that produced it and Felix's `request_id`
  rides along. Off by default; log volume is the operator's cost to choose.

- **`make up-temporal`** — `deploy/docker/compose.temporal.yml` runs the Temporal server, its
  UI on :8233, and `felix-temporal-worker` on task queue `felix-fibers`. `auto-setup` creates
  its two databases inside Felix's existing Postgres rather than standing up a second one, so
  the overlay stays within reach of a small VM. `FELIX_DURABILITY=temporal` is set for api,
  worker and temporal-worker together — they must agree, or a durable chat is enqueued for one
  driver and executed by the other.

- **`make up-memoturn`** — runs [Memoturn](https://memoturn.com) locally and points Felix's
  OTLP export at it. Felix gains **no dependency**: Memoturn ingests OTLP natively and maps
  spans carrying `gen_ai.*` attributes to generations, which is what `patterns/model.py`
  already emits, so the whole integration is an endpoint plus a Basic auth header expressed
  as `FELIX_OTEL_PROTOCOL=http` and `FELIX_OTEL_HEADERS`. Nothing in Felix knows the vendor
  exists — "Protocols, not vendors" applied to telemetry.

  It borrows Felix's Postgres (own database), Valkey (own index) and MinIO (own bucket)
  instead of the Postgres + Valkey + MinIO + Caddy + Apache Doris its own compose stands up,
  and `TELEMETRY_ENGINE=postgres` drops Doris — a 4 GB floor, more than the whole Felix
  stack. Verified end to end: a chat arrives as 6 `GENERATION` rows carrying model and real
  token counts, 3 `TOOL` rows, and the rest `SPAN` — the classification that proves the
  `gen_ai.*` attribute names are right.

- **Session and caller identity on spans.** `session.id` / `gen_ai.conversation.id` carry the
  thread (namespaced `{tenant}:{thread_id}`, so threads cannot collide across tenants), which
  is what makes a multi-turn conversation one session in a backend instead of N unrelated
  traces. `FELIX_OTEL_CAPTURE_IDENTITY` (default true) adds `user.id` / `gen_ai.user.id` from
  the auth principal and `felix.tenant.id`; set it false when principal subjects are personal
  identifiers. The anonymous principal is never recorded — it is a default, not a person.

### Fixed

- **`FELIX_OTEL_CAPTURE_CONTENT` did nothing.** It was declared in `config.py` and documented
  in `.env.example`, the Helm values, `docs/OBSERVABILITY.md` and this changelog, and read by
  no code — a setting that reads as a control and was not one, found only by looking for its
  effect in a backend and not finding it. It now writes `gen_ai.input.messages` and
  `gen_ai.output.messages` (32k cap) through `redact_json`, the audit path's masking.

  That masking replaces values Felix *knows* are secrets, by substring match; it is not
  pattern detection, so a credential a user types into a chat is exported verbatim. The docs
  now say so, because the previous wording implied more than the code does.

  `tests/unit/test_span_enrichment.py` also adds the guard that would have caught this: every
  `otel_*` / `metrics_*` setting must be read somewhere outside `config.py`.

- **Cache tokens never reached any backend.** They went out as `felix.usage.*` only, while
  backends read `gen_ai.usage.cache_creation_input_tokens` / `..._cache_read_input_tokens`.
  Not cosmetic: `usage_with_cost` counts them, so a backend computing cost from input/output
  alone silently disagrees with Felix's own number as soon as prompt caching is on.

- **Probe endpoints and ASGI plumbing were traced.** `/health`, `/live`, `/ready` and
  `/metrics` are excluded now, as are the per-request `http send` / `http receive` child
  spans. Docker healthchecks and a Prometheus scrape run every 10-15s forever: in the first
  real export, 270 of 372 traces were probes against 3 actual chats, and 552 of 936
  observations were ASGI plumbing. Agent activity was under 1% of what was being exported.

- **The Memoturn console rendered "Something went wrong".** Its image serves static files
  only — `/api/*` and `/auth/*` routing lives in Memoturn's production front proxy, which
  this overlay omits because TLS is the only other thing that proxy does. The bundle is
  built with `VITE_API_BASE=/api`, so every call the SPA made 404'd on the static server
  while the API itself stayed healthy and answered ingest. The console's own Caddy now
  forwards both prefixes, same-origin, using the routing from Memoturn's `infra/Caddyfile`.

- **OTLP/HTTP export sent every span to a 404.** The Python OTLP/HTTP exporters treat
  `endpoint` as the complete URL for their signal and append nothing, but
  `FELIX_OTEL_ENDPOINT` is a single base shared by traces and logs — so it was POSTed
  verbatim and rejected by every collector and backend alike. The per-signal path is now
  derived (`/v1/traces`, `/v1/logs`), and an endpoint that already carries one is left alone.
  Only the receiving end ever logged the 404, which is why `grpc` looked fine and `http` was
  quietly inert.

- **`FELIX_DURABILITY=temporal` completed durable chats and persisted none of them.** The
  workflow ran, `tctl workflow show` reported `"status":"completed"`, and
  `GET /chat/runs/{resume_token}` stayed `pending` forever. Two defects compounding, both
  invisible to the Postgres backend because it re-reads every row it claims:

  `create_fiber` returned a row dict with no `version` key, so `_save_fiber`'s compare-and-set
  read `int(row.get("version") or 0)` — zero — for a row the database had already written. And
  `start_durable_chat` started the workflow *before* the save that stamps `backend: temporal`,
  so that save bumped the stored version behind the running workflow's back and its snapshot
  could never match again. Every write the activity made was discarded with
  `fiber version conflict ... expected=0; discarding stale write`, which only the worker's own
  log ever saw.

  The row now carries the version it was stored with, and the workflow is started only after
  the row is final. A durable chat that finishes invisibly is worse than one that fails.

- **FastAPI was instrumented from the lifespan, where it does nothing.** `instrument_app`
  installs ASGI middleware and Starlette finalises its stack before the lifespan runs, so the
  call was accepted, reported success, and added no request span. The symptom looked like
  tracing *working*: every span still exported, each as its own single-span trace. It now runs
  at construction time, and a test pins the call out of the lifespan — a unit test of the span
  helpers cannot see this, only where the call sits can.

- **Every span claimed `service.version` `0.1.0`**, hardcoded, while the packages shipped
  0.2.2. It now reports the real harness version.

### Security

- **A provider credential was masked only if its option was *named* like one.**
  `_provider_option_secrets` decided which `FELIX_MODEL_PROVIDER_OPTIONS` values to redact
  by matching option names containing key/token/secret/password, so a provider whose
  credential option is called `credential`, `authorization` or `bearer` had its value
  published verbatim in tool output. That is a denylist, and it failed open for exactly the
  third-party providers the options map exists to serve — the reasoning
  `_TRUSTED_TRANSPORTS` already records for tool transports.

  Secrecy is now decided by allowlisting the option names a provider consumes as
  *addressing* — `base_url`, every `{placeholder}` its endpoint templates, and its header
  option keys — asked of that provider's own descriptor, minus the names the harness itself
  reads as the credential. That last part matters: exemptions are derived from placeholders
  in the operator-supplied `base_url`, so without it a URL containing `{api_key}` would have
  made the credential look like addressing and exempted it. Per provider, not a union:
  `account_id` is addressing for Cloudflare and meaningless to Groq, and exempting it
  everywhere would repeat the over-reach being removed. A plugin registers a bare factory
  rather than a descriptor, so its exemption is derived from its own configured URL.

  Erring toward masking is deliberate, and it is not free: `redact_text` is an
  unconditional string replacement over session events, audit payloads and fiber state, so
  a long low-entropy value wrongly treated as secret rewrites unrelated text wherever it
  appears. That is the cost being traded against leaking a credential.

- **`felix temporal-worker` never hydrated secrets, so three of its four redaction sinks
  were inert.** That process registers the `fiber_step` activity, which runs a full agent
  turn — and fiber state, audit payloads and session events all redact through
  `collected_secret_values()` with no settings, seeing only the process-global list that
  hydration populates. A credential echoed into a tool result was persisted verbatim there
  and served back through session export and fiber resume, and this was true of *every*
  secret, not only provider options. `deploy/GOVERNANCE.md` promised all four sinks. An
  entrypoint-wiring test now asserts every process that runs turns hydrates.

- **Options-blob credentials were masked in tool output and nowhere else.** Audit rows,
  session events and fiber state redact through `collected_secret_values()` with no
  settings, so they see only the process-global list — and hydration registered a value
  only when it arrived as a `secret:NAME` ref. A literal `{"groq": {"api_key": "gsk_..."}}`,
  the form `.env.example` documents, never reached three of the four sinks
  `deploy/GOVERNANCE.md` promises. Startup now registers every credential in the blob.

- **A non-string option value was a live credential the masker could not see.**
  `parse_provider_options` coerces every value with `str()`, so an integer or a nested dict
  is sent as a bearer token, while the masker skipped anything that was not already a
  string. Both now agree on the coerced form.

### Fixed

- **An unresolvable `secret:NAME` provider option was left in place and sent upstream**,
  shipping the internal secret *name* to the third-party endpoint and into any log of that
  request. It is dropped now, so the settings-field fallback applies or the
  missing-credential path fires.

- **A nested option value was registered for redaction only as its Python repr**, so the
  same data re-rendered as JSON, or its leaf pulled out, no longer matched. Leaves are
  registered too. And a `null` option coerced to the string `"None"` — truthy — which
  suppressed both the settings fallback and the missing-credential warning and went out as
  `Bearer None`; `None` and booleans are dropped rather than stringified.

- **A provider with no credential sent `Authorization: Bearer ` rather than no header** — a
  malformed credential that proxies and gateways treat inconsistently and that diagnoses
  nothing. The Anthropic path was already correct, since it sends the key unwrapped and
  `_headers` drops empty values. Because omitting the header turns a 401 into a request a
  permissive upstream may accept anonymously, an empty credential now logs a warning naming
  the provider and the setting to fix — once per provider, since `resolve_provider_config`
  runs on the per-request path (including the inbound injection screener, on every turn) and
  an unconditional warning there is unbounded log volume for a legitimate keyless local
  gateway.

- **An unresolved `secret:NAME` provider option was added to the redaction list**, redacting
  the one diagnostic that names what failed to resolve out of the logs someone is reading to
  find out why. The `{"secret": "NAME"}` object form — valid everywhere else — is now
  resolved too, rather than stringified into `"{'secret': 'NAME'}"` and sent as a token.

### Added

- **`/documents` and `felix/documents/` — an operator-owned corpus an agent can retrieve from.**
  The harness had no document retrieval at all: `felix/memory/` stores *facts an agent wrote*,
  and `tools/retrieval.py` is tool selection. Neither is a corpus.

  Ingest chunks the text, embeds it when an embedder is configured, and stores chunks; search
  is hybrid, fusing a lexical channel and a vector one with the same Reciprocal Rank Fusion
  long-term memory uses — `rrf_fuse` is reused rather than reimplemented. A channel that cannot
  run is skipped and *said so*: `FELIX_MEMORY_EMBEDDER` is `none` by default, so the out-of-the-
  box path is full-text only, and every hit carries the channels that surfaced it. Reuses the
  memory embedder setting rather than adding a second one — one embedder per deployment, one
  vector dimension.

  Migration `0010_documents` adds `document_chunks` with a generated `content_tsv` (GIN) and a
  **nullable** pgvector column (HNSW). Nullable is the load-bearing half: `memory_vectors`
  shipped `NOT NULL` with no default and every insert failed silently for months, so the
  conformance arm here asserts a document stores without an embedder. The table joins the RLS
  policy set in the same migration rather than a follow-up — a tenant table added outside it is
  one the isolation policy silently does not cover.

  Routes are scoped `documents:read` / `documents:write`, and the tenant always comes from the
  authenticated principal, never the request body. Re-ingesting the same `(source, title)`
  replaces rather than duplicates, so re-syncing a corpus is idempotent; replacement is
  delete-then-insert in one transaction, so a shortened document does not keep its old tail.

  Conformance runs the one contract against both backends — the two arms are *different
  rankers*, Python cosine against pgvector's `<=>`, so they can disagree about which chunk is
  best while each looks right alone. Verified against a real pgvector Postgres, not only the
  skip path.

  Two defects the tests caught while writing them:
  - The runt-tail fold used a flat 64-character floor, so at `max_chars=45` it folded every
    time and returned an 80-character chunk — a budget repealed by the tidying rule meant to
    serve it. The floor now scales with the budget and the merge is bounded.
  - Chunk overlap was taken literally, so an overlapped chunk began mid-word — `"ss guard
    resolves"`. That costs twice: the lexical channel tokenises `"ss"` as a term matching
    nothing, and a model reads a truncated first word as if it were the text.

  The agent-facing `search_documents` tool is deliberately a follow-up rather than part of this
  change.

  Found by review before this shipped, and worth recording because the first round of tests
  caught none of it:
  - **Ingest reported success and stored nothing.** A dimension mismatch made
    `_write_embedding` raise; the `except` swallowed it but left the *transaction* aborted, so
    the commit discarded the chunk inserts too — `put_document` returned `(doc_id, 1)` against
    an empty table. The `memory_vectors` failure one level down, reachable by pointing
    `FELIX_MEMORY_EMBEDDER` at a model of another size. The write is inside a savepoint now,
    and the contract asserts the stored count matches the reported one.
  - **Metadata was a 1,700× storage amplifier**: unbounded in the request model *and* copied
    onto every chunk, so one request inside the 1 MiB body limit stored 1.7 GB. Bounded at
    16 KB and written to chunk 0 only — either fix alone leaves the other shape available.
  - **The lexical channel was unguarded** while the vector one was, so a pod running ahead of
    the migration lost the request instead of a channel — and took the vector channel with it.
  - **A NUL byte was an uncaught 500** (psycopg raises `DataError`, not `ValueError`), accepted
    on the in-memory arm, so the contract could not see the divergence.
  - **`count_documents` was dead** — exported, tested, called by nothing. It now enforces
    `FELIX_DOCUMENTS_MAX_PER_TENANT`, which is what it was for.
  - Embedding is batched, so a large document no longer loses its whole vector channel to one
    oversized provider request; `SELECT *` is gone from both channels; the Postgres arm of
    `list_documents` returns metadata and orders with a tiebreaker, both of which diverged from
    the twin.
  - The conformance Postgres arm had **no teardown**, so it never reset while the memory arm
    reset around every test — the two were not running the same contract from the same state.
  - The route tests reached a **real Redis** through the repo `.env`, making the rate limiter a
    shared cross-process window: six runs inside a minute and the seventh failed on 429.


- **`spec.search_tools` and a `SearchBackend` registry — `deep` can research.** It declared
  `pattern: deep` and shipped with `tools: [calculator, list_skills]`, which made a research
  agent that could not retrieve. It now has `search` to find sources and `fetch` to read them;
  the two are useless apart, so they landed together.

  The backend is a Protocol behind an open registry (`felix.search.register_search_backend`),
  selected by `FELIX_SEARCH_BACKEND` and validated against the registry at boot — a list that
  picks a swappable implementation is never a closed `Literal` here. One backend is bundled:
  **SearXNG**, because it is the only mainstream option an operator can run themselves without
  an account, the same reason `FELIX_OBJECT_STORE=fs` is the default. It needs nothing beyond
  httpx, so unlike the embeddings backends it sits behind no extra. Brave, Tavily or an
  internal index are a plugin, not a core change.

  Off by default: search reaches an endpoint someone runs or pays for, so a deployment must not
  acquire it by upgrading.

  Deliberately *not* shaped like `spec.http_tools`, and the difference is the point. There the
  model chooses a destination, so the tool needs `path_prefix`, per-hop address validation and
  redirect walking. Here it supplies only a query and the endpoint is the operator's, so the
  risk profile is MCP's — the egress target is fixed, and it is the **results** that are
  untrusted, because a title and snippet are written by whoever ranked for the query. Transport
  `search` is absent from `_TRUSTED_TRANSPORTS` and `search` joins
  `_UNTRUSTED_SOURCE_PREFIXES`, so content screening covers them; `manifests/deep.yaml` enables
  it, and a hostile snippet comes back quarantined.

  Unlike a fetch, a search **is** replay-safe — the endpoint is the operator's own and a query
  has no side effect, so what made `http_fetch` unsafe to replay does not apply.

  Two things the tests caught. Declaring a search tool with no backend configured binds it
  anyway, returning a distinct "not configured" message and warning at bind time: skipping the
  binding would have made `spec.search_tools` silently do nothing, and "no backend" has to be
  distinguishable from "nothing matched" or a model rephrases and retries forever. And the
  bundled backend needed the same development `allow_http` exemption every other integration
  gets, or a self-hosted SearXNG on `http://localhost:8888` — the ordinary way to try this —
  was unreachable from the machine most likely to run it.

  `tests/loopback_http.py` extracts the real-HTTP-server helper the fetch suite introduced, now
  shared by both: `safe_async_client` refuses `mounts`/`proxy`, so a `MockTransport` cannot be
  installed without routing around the component under test.

  Found by review before this shipped, and worth recording because the first round of tests
  missed all of it:
  - **The rendered result block is a grammar, and its fields were interpolated raw.** `\n`
    separates fields, `\n\n` separates records, and title/url/snippet are all
    attacker-influenced — so one result could forge as many more as it liked, with URLs, in
    text the model reads as harness output, or emit a line shaped like `search_error:`. Every
    field is now flattened to one line and capped; previously only `snippet` was capped, so an
    unbounded title crowded out the results that cap protected. The repo's own rule: validating
    a value for one grammar does not validate it for the next.
  - **No whole-call deadline and no response cap** — the same defect `http_fetch` had fixed and
    this did not inherit. A backend dribbling a byte per read held a worker open indefinitely,
    and `resp.json()` parsed whatever arrived. Both bounded now.
  - **`FELIX_SEARCH_API_KEY` sat outside `_HYDRATE_MAP`**, so it was the one credential an
    operator on a secrets backend had to supply as plaintext env, and it reached none of the
    redaction sinks.
  - **`deep` is no longer anonymous.** Content screening covers what comes *back*; the URL the
    model hands to `fetch` passes through no screener, so "unconfined fetch is safe because
    screening is on" was wrong in the direction that matters. Anonymous access used to buy a
    calculator; with `fetch` bound unconfined it buys an egress primitive.
  - `FELIX_SEARCH_URL` is validated at boot — scheme, host, no query string (the backend
    appends `/search` and httpx replaces the query, so either half silently vanished) — instead
    of surfacing as `search_error: EgressBlocked` on every call.
  - Twelve mutations survived the first suite, including the SSRF blocklist disabled entirely:
    the egress test used `http://169.254.169.254`, so the *scheme* check refused it before the
    IP check ran, and `http://example.com` failed identically. All twelve now fail.


- **`spec.http_tools` — an agent can read a URL.** Until now it could not, except through
  `spec.browser_tools`, which launches a headless Chromium per call. The built-in registry was
  `calculator`, four workspace file tools, and three skill tools, so `manifests/support.yaml`
  shipped as a support agent that could only do arithmetic and `deep.yaml` as a research agent
  that could not retrieve. Roughly 620 lines of governance enforcement were wrapping that.

  Deliberately *not* the existing `HttpExecutor`, which is the other direction: that posts a
  tool's arguments to a URL the manifest fixed, so the operator picks the destination. Here the
  model picks it, which is the higher-risk shape, and it is why every knob is bounded by the
  manifest — `path_prefix` confines the URL, `max_bytes` caps the response, `timeout_ms` the
  call, all with schema ceilings.

  Egress is not re-implemented: `safe_async_client` already resolves once, validates every
  answer, and dials one of the approved addresses, and each redirect hop re-enters the same
  guarded transport. So the roadmap's stated prerequisite — pin the connection to the validated
  address — was already met by `#128`–`#130`, and this needed no separate resolving pre-check.

  The transport is `http`, absent from `_TRUSTED_TRANSPORTS`, and `http` was added to
  `_UNTRUSTED_SOURCE_PREFIXES` alongside it: a fetched page is attacker-controlled input and
  must reach content screening. Both layers are pinned by tests, separately — asserting only
  their combination let either regress in silence, which mutation testing showed.

  The body is streamed to the cap rather than read whole, because the far end chooses the
  length; a non-textual response is described rather than decoded; a non-2xx keeps its body,
  since a 404's message is usually the useful part and a model told only "it failed" retries the
  same URL. HTML becomes readable text through `html.parser` — no new dependency, and script and
  style bodies are dropped, `script` being the likeliest place for a page to address the model.

  `path_prefix` is enforced on **every redirect hop**, not just the first. Redirects are driven
  by hand rather than by httpx for two reasons: the egress guard re-checks each hop but knows
  nothing about `path_prefix`, so one `302` from an allowed page walked the agent out of its only
  confinement — the exfiltration shape the prefix exists to prevent; and httpx `aread()`s each
  interim body before building the next request, so a 40 MB redirect body cost 80 MB resident on
  a fetch capped at one kilobyte. `timeout_ms` is now a whole-call deadline (`asyncio.timeout`)
  rather than four per-operation ones, so a server dribbling a byte just inside the read timeout
  can no longer hold a worker open indefinitely — nothing upstream catches that, since
  `check_budgets` never runs *during* a call.

  A fetch tool must declare a boundary: `path_prefix`, or an explicit `allow_any_host: true`
  that is logged at bind time. Every other outbound ref names an operator-fixed destination, so
  defaulting this one to the whole public internet would have made the harness's first
  general-purpose exfiltration primitive the path of least resistance. `path_prefix` is validated
  as an absolute http(s) URL and normalised to end in `/` — without the slash
  `https://docs.felix.run` matched `https://docs.felix.run.evil.com/` — and matched by parsed
  origin rather than by `str.startswith`.

  Defects caught by review and mutation testing before this shipped, recorded because none were
  found by the tests written alongside the code:
  - The refusal path echoed `assert_safe_outbound_url`'s detailed message to the model, turning
    a block into a one-bit oracle for internal addressing. Every refusal now returns one fixed
    line, with the detail logged.
  - The per-hop guard raises a *detailed* `ValueError`, not `EgressBlocked`. Uncaught it left the
    executor entirely — past secret masking, content screening, guardrails and artifact spill,
    none of which wrap a raise — and `fatal: true` would have ended the run on a bad redirect.
  - `html_to_text` fell back to returning the **raw document** when a page had no visible text,
    handing back exactly the script bodies it had just suppressed. An SPA shell is the ordinary
    case. It returns `(no readable text)` now, and `<script/>` no longer re-opens as
    start-then-end — a browser treats the slash as an open tag and hides what follows, so the
    difference let one page read one way to a human and another to the model.
  - `replay_safe` was `True` on the reasoning that a GET is read-only. That holds for the
    workspace read tools because the operator sets the root; here the model names the endpoint,
    so it is `False`.
  - Thirteen mutations survived the first test suite, including the entire SSRF section — with
    `_is_blocked_ip` stubbed to permit everything, those tests passed in 30s instead of 0.5s,
    having really dialled private space. `http_fetch_error:` prefixes every error return, so the
    substring they asserted also matched a connect timeout. Refusals are asserted by equality
    now, and every manifest knob is exercised through the binder rather than on a hand-built
    executor.

  `manifests/support.yaml` binds it as `fetch_docs`, confined to `https://docs.felix.run/`, and
  enables marker-based content screening. Compiling it without that screening logs
  `untrusted tool(s) ... unscreened`, which is how the gap was noticed — the warning added in the
  Sep 2026 audit wave earning its place on the first capability that needed it. The support agent
  can now read the documentation it supports.

- **`FELIX_MANIFEST_SOURCE=store|bundled`.** Nearly every finding in the recent security
  work traced to a manifest field reaching the harness at runtime — unbounded timeouts and
  approval TTLs, uncapped ref lists, stdio commands. Those are bounded now, and they had to
  be: an operator's own bundled manifest can hold the same values, so the bounds were never
  really about who wrote the file.

  But a deployment that never authors a manifest at runtime should not have to guard that
  path. Under `bundled` the write routes are **not registered** — absent from the app and
  from `/openapi.json`, with Starlette answering a `PUT` as `405 Allow: GET` — and no
  manifest store is constructed at all. The posture is expressed by withholding the store at
  the runtime seam rather than by a branch in the resolver, because `_read_tenant_postgres`
  already collapses to the bundled file when no store is supplied. The read routes follow
  the posture too: `GET /manifests` lists the bundled set rather than Postgres rows that
  will never be served, and a `?version=` read 404s.

  The default stays `store`. Runtime manifests, versioning, canary and rollback are the
  product for a multi-tenant deployment; removing them would dictate a workflow rather than
  offer one. Flipping an existing deployment has two consequences worth reading first: every
  tenant collapses onto the image's file, dropping any per-tenant `auth.inbound` tightening,
  and `pin_compile` threads see one 409 as the resolved version becomes `null`. Both are
  documented in the README, and `felix doctor` reports the active posture.


- **`spec.mcp_servers[].timeout_ms` and `spec.peers[].timeout_ms`.** `ContainerRef` and
  `SandboxRef` already carried a per-integration timeout; the MCP and A2A refs did not, so
  30s and 60s respectively were unraisable and a slow-but-working server produced a tool
  result that read like a refusal. A peer call runs an entire agent turn on the far side,
  so it had the tightest ceiling on the longest operation. Both are floored at one second
  and reach discovery and the call alike, over HTTP and stdio.

- **`manifests/contributor.yaml` — Felix working on the Felix codebase.** Every piece
  this needs already existed; nothing wired them together. The manifest points the
  workspace file tools at a Felix checkout, binds the Docker sandbox for snippet
  checks, declares four developer skills (`felix-architecture`, `felix-conventions`,
  `felix-testing`, `felix-contributing`, all new under `skills/`), and reaches GitHub
  over MCP so the agent can open pull requests against the repository it runs on.

  Three limits are structural, not oversights, and the system prompt tells the agent
  about each so it cannot claim otherwise. The sandbox has no network and no volume
  mount, so it verifies snippets and cannot run the suite — `make check` and CI do
  that. `write_file` replaces whole files rather than patching. The local checkout and
  GitHub are separate worlds: editing one does not touch the other.

  Controls, since this agent can write to its own source: `write_file` and every
  mutating `github__` tool *in the recorded catalog* require approval, under
  `eu_ai_act` at `risk_tier: high` so that `allow_unattended: false` is enforced at
  compile time rather than being an inert field. Content screening is on because MCP
  output carries an untrusted transport and GitHub issue bodies are written by
  strangers. No client tool, container, queue, peer, sub-agent, or stdio MCP server is
  declared, so the isolated container is the only code execution the agent gets.

  One gap is worth naming rather than burying: approval rules match tool names exactly
  — there are no globs in the governance stack — and `McpServerRef` has no per-server
  tool allowlist, so the entire remote catalog binds as `github__*`. A write tool that
  GitHub adds or renames binds ungated, and no unit test can catch it, because the test
  can only compare the manifest to itself. Closing that needs a tool allowlist on
  `McpServerRef` or a toolset-scoped MCP URL.



- **`spec.memory.checkpointer` now selects where session state lives**, having
  shipped as a closed `Literal` that no code read. Every value silently meant
  "whatever `FELIX_DATABASE_URL` points at". It is now resolved through an open
  registry: `postgres` (default, unchanged) and `none` (no session state — every
  turn starts from the messages it was given), plus anything a plugin adds with
  `register_checkpointer`.

  There is deliberately no in-process built-in. A thread is not manifest-scoped —
  fifteen `/chat` routes address one by id with no manifest in hand — so a manifest
  choosing a different *backend* would split-brain, the agent reading one log while
  `/history`, `/continue` and `/compact` read another. `none` is exempt because it
  is a claim about the agent, enforced where the agent reads.

  `agentcore`, `sqlite` and `do` are no longer accepted. They never did anything,
  and `do` named Cloudflare Durable Objects — compute this stack deliberately does
  not run. A manifest setting one now fails validation instead of quietly getting
  Postgres. No bundled manifest used them.

  `checkpointer: none` is refused alongside anything the loop would silently drop
  for want of a store: a `session.strategy` other than `full_replay`,
  `session.compact_after_turn`, and `memory.capture.enabled` — the last because
  `_turn_seq` stamps `origin_seq` from the session head, so with no store every
  fact lands at genesis and supersession ordering collapses rather than erroring.

  A bad name is now refused at manifest *write* time (`PUT /manifests/{name}`) as
  well as by the CLI. Opening the field from `Literal` to `str` moved typo-catching
  out of pydantic, and a stored typo would otherwise have raised inside every
  build — a 500 per request until someone read a traceback.

- **`FELIX_DB_PREPARED_STATEMENTS`** — set it `false` behind a pooler that does not
  track prepared statements. psycopg3 prepares after five executions and the sixth
  lands on a different server connection, so this fails on the sixth query rather
  than the first. RDS Proxy forces the choice: it pins the session when it sees a
  prepared statement, defeating the pooling it was deployed for.
- **`make up-pooled`** — PgBouncer in transaction mode in front of Postgres, for when
  `WORKERS x (POOL_SIZE + MAX_OVERFLOW)` outgrows your `max_connections`.
- **`make up-replicas` and `scripts/smoke-replicas.sh`** — two API replicas behind one
  origin, and a smoke that proves a resume stream on one sees an append made on the
  other.
- **Compose passes the resume-pacing settings** (`FELIX_STREAM_RESUME_IDLE_SECONDS`,
  `..._POLL_SECONDS`, `..._POLL_MAX_SECONDS`). They were documented in `.env.example`
  and unreachable from Compose.
- **Open registries for the remaining swappable backends.**
  `register_object_store`, `register_secrets_backend`, and
  `register_warehouse_backend` join the pattern, model-provider, and embedder
  registries. `ObjectStore`, `SecretsProvider`, and `Warehouse` were already
  Protocols, but each was selected by a hardcoded if/elif, so a third party could
  implement the interface and had no way to have it chosen.
- **`FELIX_OBJECT_STORE`, `FELIX_SECRETS_BACKEND`, `FELIX_WAREHOUSE`,
  `FELIX_MEMORY_EMBEDDER`, and `FELIX_AUTH_MODE` accept registered names.** They were
  closed `Literal`s, which made a registered backend unreachable — most visibly for
  the embedder, whose registry had been open all along. An unknown value now fails at
  startup with the registered names rather than being rejected by the schema.
- **`register_session_strategy`** — `spec.session.strategy` was an open string parsed
  by a closed parser.
- **`spec.extensions`** — the one field exempt from the manifest schema's
  `extra="forbid"`, namespaced by plugin name and delivered to a pattern builder as
  `PatternBuildContext["extensions"]`. A plugin previously had no way to carry any
  manifest configuration at all.
- **`FELIX_SKILLS_DIR`** — an extra `SKILL.md` directory searched alongside the
  bundled one. Bundled skills resolved only from `__file__`-relative repo paths, so a
  pip-installed Felix had none and no way to point at its own.
- **`examples/felix-plugin-example/`** — a working out-of-tree plugin exercising every
  seam, including the `[project.entry-points."felix.plugins"]` declaration, for which
  the repo previously held no example. `felix doctor` now lists discovered plugins and
  registered patterns, and `felix validate-manifest` rejects an unknown pattern name
  (nothing validated the pattern before, so a bad name passed CI and failed at build).


- **A conformance contract for model providers.** `tests/conformance/test_model_provider.py`
  runs one contract against three arms — `scripted`, `openai`, `anthropic` — mirroring how
  the store suite runs one contract across backends. None needs infrastructure, so every arm
  runs on every CI run and a skip there would be a bug rather than a missing database.

  Twelve test files each built their own model double, and every one re-decided what a
  provider owes its caller — which is how `stream_turn` stayed off the published Protocol
  for so long: a double that implemented it and one that did not both looked correct alone.
  Nothing exercised the chain a third-party provider actually travels, either. The contract
  pins the *de facto* shape, not just the declared one: `opts` accepted as the **third
  positional argument** (six side-request call sites pass it that way, so a keyword-only
  `opts` is a `TypeError`), `model_id` and `route` as bare attributes, usage reported from
  both `chat` and `stream_turn`, stop reasons in the neutral vocabulary, tool arguments
  parsed to a dict whatever the wire sent, and a `ModelGatewayError` whose `.status`
  `_is_provider_error` can read while the upstream body stays out of `str(exc)`.

  The fake transport models the endpoint rather than replaying a tape: OpenAI omits usage
  from a streamed response unless `stream_options.include_usage` asked for it, so the fake
  does too. Without that the contract could not see a provider that forgets to ask — the
  highest-value provider bug there is, because it leaves every streamed run unmetered and
  the budgets fail open.

- **`felix_ai.providers.scripted`** — the model double, written once. Opt-in rather than
  registered by default: a fake in the production registry would let a typo in
  `FELIX_MODEL_ROUTES` succeed silently and answer every prompt with canned text.


- **Ten more providers, as table rows.** `workers_ai` (Cloudflare Workers AI), `groq`,
  `together`, `deepseek`, `cerebras`, `fireworks`, `openrouter`, `xai`, `mistral`, and
  `google` via its OpenAI-compatible endpoint. All speak OpenAI chat-completions, so each is
  a `ProviderSpec` row rather than a module, and each is configured through
  `FELIX_MODEL_PROVIDER_OPTIONS` — no settings field per vendor, which is the pattern that
  does not scale.

  Two things are not uniform, and both became properties of the row rather than special
  cases in the factory. `base_url_default` may carry `{option}` placeholders, because
  Cloudflare puts the account id in the URL path. And `header_options` sends a header only
  when its option is set, which is how one provider covers both "direct" and "routed through
  AI Gateway" (`cf-aig-gateway-id`) instead of being two providers. `HttpModelClient` gained
  `extra_headers`, where an override to the empty string *removes* a header the wire format
  would otherwise send.

  Every OpenAI-compatible provider is now also selectable as `FELIX_MEMORY_EMBEDDER`, since
  `/embeddings` is the same wire format — and it resolves through the same descriptor, so the
  embedder and the model client cannot disagree about the endpoint or the credential.

  **Calling a hosted Cloudflare API is not Cloudflare compute.** The no-Workers/DO/Hyperdrive
  invariant is unchanged and now says so explicitly across README, CLAUDE.md, CONTRIBUTING,
  the invariants rule and the reviewer agents: the line is where Felix *runs*, not whose API
  it calls — the same distinction `storage/s3.py` already relied on for R2.


- **A provider is a descriptor, and a plugin can finally be given a credential.** Adding a
  provider meant a hand-written factory plus a `Settings` field plus a `_HYDRATE_MAP` entry
  plus a `.env.example` block plus a README row — and the easy one to forget, `_HYDRATE_MAP`,
  is also what feeds `collected_secret_values()`. Forgetting it did not merely skip secrets
  hydration; it meant the key was **never masked out of tool output**. `ProviderSpec` in
  `felix_ai.providers` collapses that to one row and the harness *derives* the secret
  handling, so adding a provider cannot silently open that hole.

  `FELIX_MODEL_PROVIDER_OPTIONS` is a new JSON setting carrying a per-provider endpoint and
  credential. The built-in providers have named `Settings` fields; a plugin's provider cannot,
  because `Settings` is `extra="ignore"` — so `FELIX_MYPROVIDER_API_KEY` never lands anywhere
  and a registered third-party provider had **no way to be given a key at all**. An entry also
  overrides the named field, which is how a built-in gets pointed at a gateway. Values under a
  key/token/secret name are added to the redaction list.

  `PluginRegistry.register_model_provider` now exists, and the reference plugin demonstrates
  it. It was the one open registry with no seam on the registry object and no example, so the
  documented way to add a provider was to read core's source.

- **`scripts/prove-fails.sh`, and a boot gate for every entrypoint.** Four defects shipped or
  nearly shipped in one session, and three were the same shape: *the branch production takes is
  the branch nothing covers.* `create_app()` read its optional `settings` parameter instead of the
  resolved `cfg` and died at boot with a green suite, because every test passes `settings=` and
  production is the only caller that does not.

  `tests/unit/test_entrypoint_wiring.py` closes that class. It calls each entrypoint the way its
  console script does — `create_application()` with no arguments — and resolves every `module:attr`
  string production depends on but no import statement mentions: `[project.scripts]` targets, the
  string Granian is handed, the Taskiq broker and scheduler paths, and the `felix-*` binary each
  Dockerfile `CMD` and Compose `command:` names. Those are invisible to ruff and to `ty`, and a
  rename breaks only the container.

  `scripts/prove-fails.sh <target>` runs a test against pre-change source — a detached worktree on
  `PYTHONPATH`, working tree untouched — and reports **PROVEN** (failed: evidence), **VACUOUS**
  (passed: pins nothing), or **BROKEN** (*errored*, which is neither, and means the test itself is
  wrong). `--base <ref>` picks the comparison point; `--only <dists>` takes a comma-separated list of
  distributions to shadow, for when a distant base makes `tests/conftest.py` error in fixture setup.
  It shadows `PYTHONPATH` and changes nothing on disk, so a test that *reads* the tree is proven by
  mutation instead — the script says so rather than printing a verdict it has no basis for. Two invariants here have
  been vacuous — one matched `timeout=<Constant>` while every literal it hunted lived inside
  `httpx.Timeout(...)` — and neither announced itself.

  In `.claude/`: a `structural-test-proof.sh` hook names the command when a tree-scanning test
  gains a case, `pr-quality-gate.sh` asks for `felix-security-reviewer` when the diff touches a
  control path, and the `security-review` checklist gains a grammar-crossing section — a hostname
  validated against a DNS pattern went into `--host-resolver-rules`, whose grammar is a
  comma-separated list, so `evil.com,MAP * 169.254.169.254` would have reached every name past it.
  Both new guards are mutation-tested rather than trusted.

### Fixed

- **`replay_safe` had never worked in any release.** Five builtin tools declare
  `replay_safe=True` — `calculator`, `list_skills`, `list_dir`, `read_file`, `search_files` — and
  `patterns/react.py` reads it to decide what to tell the model about a tool call that was
  interrupted: replay-safe tools are "safe to call again", everything else is "do not assume it
  succeeded or failed". The flag was `False` on every tool in every manifest, so that branch had
  never once been taken.

  Seven wrappers in `manifests/builder.py` and `wrap_tool` in `tools/executor.py` each built the
  wrapped tool with `Tool(name=..., description=..., ...)` from eight of its ten fields. `peer` is
  restored by `__post_init__` from `is_peer`; `replay_safe` is restored by nothing. `apply_limits`
  wraps every tool unconditionally, so the loss was universal rather than conditional on a manifest
  declaring policies or screening.

  `_clone_tool` — which uses `dataclasses.replace` and was already correct — carries the docstring
  that predicted this: *"a field that the rebuild forgot would be silently reset to its default on
  every wrapped tool ... `replay_safe` was added and very nearly lost exactly that way."* It was
  lost, in the seven wrappers that did not call it. All eight sites now clone, and
  `test_no_governance_wrapper_rebuilds_a_tool_by_hand` fails if a ninth is written by hand.

  Found by disabling each governance control in turn and re-running the suite, which is also how
  the two gaps below surfaced. No security impact: the failure was conservative, telling the model
  not to retry something it safely could.

- **`apply_secret_masking` and `apply_policies` had no behavioural coverage.** Either could be
  reduced to `return tools` with the full suite green — the innermost control, which redacts
  resolved `secret:` values from tool output before the transcript or the audit log sees them, and
  the control enforcing `spec.policies` scope requirements. The suite's only mention of either was
  the `EXPECTED_WRAPPER_ORDER` list, which asserts the stack's order and calls nothing.
  `tests/unit/test_governance_controls_enforce.py` covers both, including the fail-closed case
  where a run has no request context and "no scopes" must not read as "all scopes".

- **A `spec.policies` rule with `tools` but no `required_scopes` is now rejected.** `BREAKING`
  for a manifest that has one. `required_scopes` is the only enforcement `apply_policies` has,
  and an empty list makes its check vacuously true — so
  `policies: [{id: finance-only, tools: [wire_transfer]}]` validated, compiled, and *wrapped the
  tool*, which is what made it hard to see: the compiled stack looked correct, `felix
  validate-manifest` blessed it, and every anonymous caller reached the tool.

  Rejected at parse rather than accepted as a no-op, because a control that appears in the
  manifest while enforcing nothing is worse than a missing one. A rule naming no tools is
  rejected for the same reason, one step earlier: it gates nothing at all. Note that neither
  shape is expressible in JSON Schema, so the editor integration will not flag it — the error
  arrives from `felix validate-manifest` or at request time. A stored manifest carrying either
  shape will fail to resolve until it is fixed; that is deliberate, since the alternative is a
  security control that silently does nothing.

  A rule naming no tools is rejected too. That one is not merely inert: `governance.py:43`
  counts a non-empty `spec.policies` toward the `soc2` profile's "policies **or** approvals
  **or** limits" requirement, so a policy with scopes and no tools satisfied the compliance
  posture while gating nothing.

  `apply_policies` fails closed on a scopeless rule reaching it, for a `Policy` built in code
  rather than parsed. Two related holes closed with it: `required_scopes` entries that are
  blank or whitespace are rejected, because the list branch of `_scopes_from_payload` does not
  filter and a token carrying an empty `scopes` entry would satisfy them; and the wrapper now
  coerces the caller's scopes to a `frozenset` before testing membership, since
  `AuthContext.scopes` is an unvalidated dataclass field and a plugin authenticator returning
  a `str` turned `s not in scopes` into a substring test — under which `tools:calc` is
  satisfied by `tools:calculator`, and `admin` by `no-admin`.

- **A manifest refused for a stated reason now says so.** `parse_manifest` raised pydantic's
  `ValidationError`, which is not relayable and was raised outside both `try` blocks in
  `PUT /manifests/{name}` — so a refusal answered `500 Internal Server Error` with the reason
  only in the server log, and a stored manifest carrying a since-rejected shape answered 500 on
  every read. It now raises `ManifestParseError`: `400` with the reason at write, `422` at read
  (the same trade `memory.checkpointer` already makes one line above), and the message is
  rendered from the error locations rather than `str(exc)`, which embeds `input_value=` and
  would have carried an inline credential into HTTP bodies, job rows and fiber state.

  Found by `felix-security-reviewer` during the governance mutation audit.

- **Governance rules target tools by glob, as the docs have always said they did.**
  `internals/governance.mdx` promised *"Tool targeting for policies, approvals, and judges
  matches by glob so MCP tools named `server__*` stay gated even if the remote renames
  suffixes"*, and every one of them matched literally. So `{tools: ["github__*"],
  required_scopes: [repo:write]}` — the shape MCP's `server__tool` prefixing makes natural, and
  the one the docs told operators to write — matched no bound tool and gated nothing.

  `manifests/tool_match.py` now backs policies, approvals, judge `target_tools` and
  `content_screening.tools`. Screening is not in the docs' list but is included anyway: leaving
  one of the four literal is the surprise, since `server__*` would then gate under a policy and
  not under screening. Matching is `fnmatchcase` and works in any position — `github__*`,
  `*__search`, `mcp__*__write`, `*`.

  **Approvals is the only control that selects one rule**, so with globs "which rule matched"
  became the whole gate. A rule naming a tool literally now wins over one matching by pattern,
  and among equals the last declared wins — which is what the previous name-keyed dict did,
  since a glob contributed nothing to it. Globbing is therefore non-weakening: a pattern can
  only gate a tool nothing gated before. Plain last-match-wins was not that, and a strict
  literal rule followed by a broad `github__*` audit rule carrying `when_args` lost its gate
  entirely.

  A pattern matching no bound tool is logged at WARNING and counted as
  `felix_rule_targets_nothing` at compile time. Not refused: an MCP server whose discovery
  failed binds no tools, and refusing would let a remote outage take the agent down with it.
  Globs make an inert rule easier to write by hand, so the counter is the thing to watch after
  adding one.

- **`spec.policies` and `spec.approvals` are capped at 64 rules**, like every integration list.
  Matching is O(rules × tools) and `build_agent` compiles per request; unbounded, 8000 policies
  measured 0.24s of synchronous CPU on the event loop, which stalls every other tenant sharing
  that worker and is reachable by any principal holding `manifests:write` for one tenant.

- **One model tool call could execute a tool twice.** Both dispatch sites decided how to call a
  function by *calling it* with the wider signature and catching `TypeError`:

      try:    return await execute(args, ctx, inner)
      except TypeError: return await execute(args, ctx)

  That cannot distinguish "wrong arity" from "a `TypeError` raised inside a body that already
  ran". Any tool whose body raises `TypeError` on attacker-shaped input — an MCP server
  returning a list where a dict was expected, a JSON field that is null — ran twice, past every
  governance wrapper, with no interrupted-call marker, while the model saw one call.

  `wrap_executor` is benign for the eight wrappers in `manifests/builder.py`, whose `execute`
  takes two parameters so the three-argument call always raises before the body runs. It is not
  benign for `apply_artifact_spill`, whose `execute` is `(args, ctx=None, _inner=inner)` and
  dispatches on the first call. `define_tool` had the same shape in `handler(parsed, ctx)`
  falling back to `handler(parsed)`. Both measured at 2 executions per call.

  Arity is now decided by `tools/types.py:accepts_positional` — introspection, once, before
  anything runs. An unintrospectable callable selects the narrower call, because a genuine
  mismatch raising once and loudly beats running a side effect a second time.

  `apply_artifact_spill` no longer takes `inner` as a defaulted third parameter — it closes
  over it like the eight wrappers do. That parameter existed only to satisfy the old probe and
  was the sole reason `wrap_executor` ever took the wide branch, so removing it deletes the
  shape rather than only the symptom.

  Found by `felix-security-reviewer` during the governance mutation audit. The one remaining
  `except TypeError` probe, in `security/rate_limit.py`, is left alone deliberately — but not
  for the reason first given here. Its `try` spans `pipe.execute()` and `int(results[0])` as
  well as the queueing calls, so a nil pipeline element would fire the handler *after* the
  INCR landed and issue a second one. It stays because the direction is safe: double-counting
  makes the limiter stricter, never more permissive.

- **A failure in post-call bookkeeping told the model a successful tool call had failed, and
  ran the after-tool hook twice.** `ToolRunner.dispatch` executed the tool and then did its
  metering, audit and `run_after_tool` inside the *same* `try`, so a failure in any of that
  fell into the handler written for "the tool call failed" — which invokes `run_after_tool`
  again with `result=None, is_error=True` and returns `[error/...]` to the model. A model told
  a side-effecting tool failed may run it again.

  Not reachable by the obvious route: `run_after_tool` isolates each hook and
  `emit_agent_audit` swallows its own failures, so neither can raise. What can is the handling
  of a hook's *return* — an after-tool hook replacing `content` with an object whose `__str__`
  raises produced two hook invocations, `[False, True]`, and an `[error/internal]` message for
  a tool that had succeeded. Measured before and after.

  The `try` now covers only the call. Everything after it moved to
  `_record_and_postprocess`, which cannot undo the call and so degrades — logging and
  `felix_control_degraded{control=after_tool}` — instead of rewriting the outcome.

- **`content_screening.tools` is additive, not substitutive.** `BREAKING` in the safe
  direction: a manifest that sets it will now screen more than it did.

  The list and the untrusted-tool default used to be alternatives, so a non-empty `tools`
  *replaced* the default rather than adding to it. Naming one trusted local tool — the natural
  way to *extend* screening — silently turned it off for every `mcp__*`, `peer__*`, browser,
  sandbox, container and queue tool, while the manifest still read as a working control and
  `felix validate-manifest` blessed it. Injected content on a fetched page then reached the
  model with the whole governed toolset behind it.

  Screening now covers every untrusted tool plus whatever `tools` names. A manifest that never
  sets `tools` is unaffected — `matches_any([], name)` is False, so that path is unchanged.

  There is deliberately no replacement escape hatch. Narrowing screening away from untrusted
  output is the thing screening exists to prevent, so it was removed rather than renamed.

  Being straight about the cost, because "use `model` and `on_flag` instead" would not be:
  neither is per-tool, so this removes the only per-tool cost lever there was. It is free in
  the default configuration — both bundled manifests that enable screening leave `model` empty,
  and the marker path is a substring scan — and it costs one model call per untrusted tool per
  turn where `model` *is* set. A manifest binding twenty MCP tools and naming three went from
  three screener calls to twenty. If that bites, the shape to add is a knob orthogonal to trust
  — which tools get the *expensive* screener, with marker screening unconditional — rather than
  a way to exempt an untrusted tool from screening at all.

  Two things to re-measure if you set `model` and previously narrowed `tools`: `on_flag: block`
  plus a screener outage now denies output from every untrusted tool rather than the named
  subset, and the marker scan's substring match (`"system prompt"` included) can quarantine a
  docs server or an issue tracker that was previously exempt.

  Found by `felix-security-reviewer` during the governance mutation audit.
- **A manifest whose policies nothing can satisfy now says so at compile.**
  `felix_policy_unsatisfiable`, plus a WARNING naming the reason. `apply_policies` denies when
  a required scope is absent — "no scopes" must not read as "all scopes" — but durable fibers
  (principal `fiber`), scheduled jobs (`cron`), `felix eval`, and `FELIX_AUTH_MODE=none` all
  carry an empty scope set by construction. So `spec.policies` plus any of them denies *every*
  policied tool. Safe, and baffling: `manifests/governed.yaml` policies `calculator`, and
  `make dev` sets `FELIX_AUTH_MODE=none`, so the bundled reference manifest denies its own
  calculator under the documented dev command.

  A warning, not a refusal. The combination is legitimate — the same manifest can be served
  over HTTP to a scoped caller *and* resumed as a fiber — so refusing would break a working
  deployment to prevent a surprise.

  Deliberately **not** done: persisting the originating caller's scopes on the fiber row so a
  resumed run inherits them. That puts a credential-shaped thing into durable state and means
  a fiber resumed weeks later still carries the original caller's authority. It is a change to
  the security model, not a bug fix, and it wants a decision rather than a commit.

- **A manifest that binds untrusted tools without content screening now says so at compile.**
  `felix_untrusted_tools_unscreened`, plus a WARNING naming the tools.
  `content_screening.enabled` defaults to `false` and `validate_governance` requires it only under
  `eu_ai_act` — `soc2` does not, so this was a normal, valid manifest in which
  attacker-controlled text reached the model with the whole governed toolset behind it — the
  last remaining path of that shape after screening became additive.

  A warning rather than a changed default, because turning screening on for every deployment
  binding an MCP server changes cost and behaviour, and that is not a thing to do silently in a
  patch.

  **It found one on its first run.** `manifests/cowork.yaml` binds `local_shell` and
  `local_open` — tools that execute on the user's own machine through the client bridge — and
  their output reached the model unscreened. Approval gates whether the command runs; screening
  is what looks at what comes back. It now enables marker-only screening (`on_flag: quarantine`,
  no `model`). A YAML grep for the untrusted *binder* blocks had missed it, because
  `client_tools` is one and does not look like `mcp_servers`.

  Screening cowork exposed that its marker list was unusable there. `apply_content_screening`
  carried its own `_INJECTION_MARKERS` — a second, blunter copy of the patterns in
  `governance/content_screening.py`, including a bare `"system prompt"` substring. Since
  `_replace_content` swaps the whole output rather than redacting the match, `cat CLAUDE.md` on
  this repository returned `[quarantined]`; 23 of its files contain the phrase. A control that
  eats a developer's `git log -p` is a control someone switches off, and switching it off would
  have removed screening from `local_shell` too. The builder now uses the anchored patterns the
  other module already had (`system\s+prompt\s*:`, `<\s*/?\s*system\s*>`), which still flag
  "ignore previous instructions ..." and "System prompt: you are now ...".

  cowork also names `read_file`, `search_files`, `list_dir`, `recall` and `list_memories` in
  `content_screening.tools`. They are `transport: local`, so the untrusted default does not
  cover them, and they read the same filesystem the shell does — with recall being the re-entry
  path, since capture runs over turns containing client-tool output. A comment in that manifest
  claimed recalled memories were already screened; they were not, and now they are.

- **A durable run parked on an approval was re-claimed and re-executed.** `FIBER_LEASE_MS` was
  `5 * 60 * 1000` and `approvals/interrupt.py:DEFAULT_TIMEOUT_SECONDS` is `300.0` — the same
  number, arrived at independently. An approval rule may set `ttl_seconds` up to 3600, so a run
  waiting on a decision outlived its own claim and the next sweep re-claimed it, re-running an
  invoke whose tool side effects had already happened and then losing the write to the `version`
  CAS. Up to twelve times for one model tool call.

  The lease is now renewed while a step is in flight, at a third of its length. That makes it
  bound the right thing: how long after a worker dies its fiber stays stranded, not how long a
  step may take. A renewal is refused if the lease has already been taken by another replica, so
  a worker cannot steal its claim back mid-step.

- **Under `FELIX_DATABASE_RLS=true`, a durable resume could run the wrong manifest.**
  `resolve_tenant_manifest`, `assert_pin_matches` and `prepare_tenant_invoke` ran *above* the
  `async_run_with_context` block that sets the `app.tenant_id` GUC, and the worker installs no
  ambient context. So the FORCE'd policy filtered every row, `get_active` returned `None`, and
  `_read_tenant_postgres` fell through to the **bundled** manifest of the same name — and
  operators are told to fork `governed`. `ensure_thread_pin` was equally blind, so the drift
  check that exists to catch exactly this could not see the stored pin either. All three now run
  inside the context.

- `felix.durability.fibers.reset_memory_fibers()`, called by `tests/conftest.py`. The in-memory
  fiber store was the one `memory://` twin with no reset, so fibers leaked between tests and any
  assertion on how many came back passed alone and failed in the suite.

- **A durable run resumes as the caller who started it.** `spec.policies` and
  `execution.mode: durable` were mutually exclusive and nothing said so: `_step_fiber` built
  `AuthContext(principal_sub="fiber")` with the default empty scope set, so every policied tool
  denied on resume and `auth.inbound.required_scopes` refused the resume outright. A manifest
  that worked over HTTP stopped working the moment it was made durable.

  The fiber now records the caller's `principal_sub`, `scopes`, `anonymous` and `scheme` at
  enqueue, and resumes with them.

  This is authority living in durable state, so the bounds are the point: never wider than the
  caller's own scope set, never longer than the run, and never longer than the token. The last
  two needed work that the first version of this entry claimed was already done —
  `resume_token_ttl_seconds` had no ceiling, so "it dies with the run" was a promise a manifest
  author could set to ten years, and the token's `exp` was never consulted at all. It is now
  capped at `ABSOLUTE_LIMITS["resume_token_ttl_seconds"]` (24h, clamped at the read site too so
  a row stored before the cap cannot exceed it) and `expires_at` is clamped to the token's
  `exp`. Felix has no revocation anywhere in `felix/auth/`, so `exp` is the sole bound on a
  compromised credential, and this would otherwise have been the first path where authority
  survived it.

  The resumed principal is `fiber`, not the caller. Every other machine actor here does the
  same — `cron`, `eval`, `a2a` — and impersonating the human would put `principal_subj=alice,
  scheme=jwt` in an audit row for work a worker did minutes later. A new `AuthContext
  .on_behalf_of` carries who the run is for, and `bind_principal` reads it, so an approval
  granted interactively still matches its own resumed run.

  `pin_compile` is forced when a fiber carries recorded authority. The manifest is re-resolved
  at resume, and `pin_compile` defaults to false — so a holder of `manifests:write` could
  publish a new active version between the 202 and the scheduler tick and have it run with the
  original caller's scopes. Carrying authority and re-resolving the code that authority runs
  are not separable decisions.

  Recording is refused when the ambient caller's tenant is not the run's. Both callers derive
  both from the same request today; the guard is for the admin route or per-tenant fan-out that
  would otherwise write tenant A's scopes into tenant B's fiber.

  Not recorded, deliberately: no JWT, no raw claims. Only the decisions the auth layer already
  made, plus `exp` as a single integer because it is a bound rather than a credential.

  Two things this does not bound, both documented in `deploy/GOVERNANCE.md`: `expires_at` gates
  step *entry*, so a step starting just inside the horizon runs to completion; and the fiber
  row is not swept by retention, so the record outlives the run's usability.

### Changed

- **Removed `args_schema` from `spec.sandboxes`, `spec.containers` and
  `spec.browser_tools`.** All three binders hardcode their argument model — `SandboxArgs`,
  `ContainerArgs`, `BrowserUrlArgs` — because the executor reads fixed keys. A
  manifest-supplied schema could only advertise arguments the executor would then ignore,
  which is worse than having none: the model is told a tool takes parameters it does not.
  `QueueRef` and `ClientToolRef` genuinely read theirs and keep it.

  Migration: a stored manifest setting one of the three now fails validation. Nothing could
  have depended on its behaviour, because it had none.

- **`test_inert_manifest_fields.py` now guards the class, not just the instances.** Twelve
  fields are declared in `schema.py` and read nowhere — `precount`, `retention_days`,
  `min_rate`, the `PlanExecuteSpec` block, and others. Each validates, completes in an
  editor, and does nothing. They are pinned in a ratchet: adding an unread field fails the
  build, and fixing one also fails until it is removed from the set, so both edits are
  deliberate.


- **`/docs` is the Scalar API reference, not Swagger UI.** The harness served FastAPI's
  bundled Swagger UI while the docs site already described `/docs` as Scalar. It now renders
  the same `/openapi.json` through a pinned Scalar bundle: tag sidebar, curl as the default
  client, and a `servers` entry taken from the page's own origin — without it the spec has no
  `servers` block, so every snippet rendered as a bare `curl /health` that could not be
  pasted anywhere. No new dependency (Swagger UI was a CDN script too, and this is one HTML
  route). The bundle is pinned and carries an SRI hash, so a public, always-unauthenticated
  origin cannot be handed a different script than the one this commit reviewed. Swagger UI's
  `/docs/oauth2-redirect` went with it — the only route ever served under `/docs/` — so the
  rate limiter's orphaned `/docs/` prefix exemption goes too. The spec path and the curl
  snippets' base URL both resolve per request against `root_path`, as Swagger UI's did —
  precomputing them would have left `/redoc` working and `/docs` blank behind a proxy
  prefix. `/openapi.json` and `/redoc` are unchanged, as is `/docs` being public in every
  auth mode.


- **`tenant_id` no longer defaults on the session-layer accessors** —
  `get_session_store`, `build_checkpointer`, `PostgresSessionStore` and
  `InMemorySessionStore`. Omitting it silently meant tenant `"default"`, which is
  how the `remember` bug above went unnoticed. Source-incompatible for an
  out-of-repo caller that omitted it; every in-repo call site already passed one.
  A repo invariant now fails if the default comes back. Note the residual: an
  explicit `tenant_id="default"` still compiles, so the regression test asserting on
  the resulting ordinal is the real guard, not the signature.


- **A plugin can no longer silently shadow a built-in.** Auth modes already refused
  it; session strategies did not, so an installed package could replace
  `compacting` for every manifest using it. `register_session_strategy` now rejects
  a built-in prefix, and longest-prefix-wins makes resolution independent of
  registration order.
- **An audit sink is constructed once, not per event**, and a sink failure logs at
  `warning` rather than `debug` — it is the compliance-export path, so a broken
  export was invisible at default log level.


- A resume stream that is genuinely being notified now relaxes its poll to 60 seconds
  rather than 10, since the poll is a safety net once wake-ups are being delivered.
  It tightens again on its own when they stop.


- **Streaming is implemented once per wire format, not twice.** `_stream` is a text-only
  view of `_stream_turn` in the base class, and both overrides are gone — 149 lines,
  including a 32-line block byte-identical to its `stream_turn` sibling. The copies had
  already drifted (`_body` attaches the `cache_control` breakpoint to the last tool; the
  hand-built body carried no `tools` key at all), and neither was reachable for a shipped
  client. SSE framing is one shared `iter_sse_json` rather than four copies.

- **`entry_for` is defined in terms of `known_entry_for`**, so the catalog's "one rule for
  finding it" is written once. The four transitional aliases in `felix.patterns.model` are
  deleted — three had no readers and the fourth had one test import that now uses the public
  name. `parse_model_routes` is cached on the routes string; it went from three call sites
  to seven in this branch and re-parsed the JSON on each.


- **One provider registry, not two.** `felix.patterns.model_registry` re-exports
  `felix_ai.registry`, so a provider written against `felix_ai` alone — the point of the
  package boundary — and one written against the harness land in the same dict. Two
  registries would have meant `build_one_model` finding only half the providers.


- **`spec.model.region` is removed.** Declared, never read by anything, and a leftover from
  a Bedrock-shaped past. Same disposition as the `checkpointer` aliases: `_Strict` forbids
  extras, so a manifest that sets it now fails validation instead of carrying decorative
  surface that looks like it configures something.

- **`memory.consolidate.model` and the eval judge default now point at `claude-haiku`.**
  Both defaulted to `llama-3-fast`, which routes to Ollama — the exact bug already fixed and
  explained on `memory.capture.model`, which these were the missed siblings of. A deployment
  holding only an Anthropic key had consolidation fail on every run, and the LLM judge
  silently degrade to the heuristic, each saying so only in a log.


- **The model layer is its own workspace package, and it cannot import the harness.**
  `packages/ai` (`felix_ai`) now holds the two wire formats, the model catalog, the neutral
  turn types, and the HTTP transport. `felix.patterns.model` keeps only what genuinely needs
  the harness: resolving `FELIX_MODEL_ROUTES` against `Settings`, metering a turn through
  `record_usage`, and the fallback/escalation composites. Every public name it used to export
  is re-exported, so no call site outside the wire tests changed.

  The point is not tidiness. Felix's provider *seam* was already open — `register_model_provider`
  is a plain dict and nothing in core enumerates providers — but everything a provider needs in
  order to be **correct** was private and Anthropic-shaped. `_HttpModelClient`, `_post_with_retry`,
  the SSE parsers, tool-argument repair and stop-reason mapping were all underscore-prefixed and
  excluded from `__all__`, so a third-party provider had to re-derive retry-on-429, `Retry-After`
  handling and usage accounting — and what it got wrong in usage fails *open* on
  `limits.max_cost_usd`. Those are now public: `HttpModelClient`, `post_with_retry`, `map_stop`,
  `parse_tool_arguments`, `tool_json_schema`, `OpenAICompletionsClient`, `AnthropicMessagesClient`.

  `felix_ai` may not import `felix`, and `tests/unit/test_invariants.py` walks every import node
  — a lazy in-function import is not an escape hatch. That is what makes "model-agnostic" a
  property of the build rather than a claim in a README. The two things the harness genuinely has
  to inject arrive through Protocols the harness types already satisfy structurally (`ToolSchema`
  for a tool's name/description/schema, `ModelConfig` for the request timeout) and two explicit
  sinks (`felix_ai.observability` for counters, `felix_ai.context` for the prompt-cache key),
  installed once by `patterns/model_sinks.py`.

  Behaviour is unchanged. `felix.model_catalog` and the message types re-export from their new
  home, so `usage/`, `react.py` and `runtime.py` are untouched. The tool parameter on
  `ModelProvider` widened from `list[Tool]` to `Sequence[ToolSchema]`, which is what the wire
  layer always actually needed — it only iterates — and fixes a variance mismatch that made the
  composites fail to satisfy the Protocol they implement.

### Fixed

- **The internal landing path screened four payload keys while the event carried more.**
  `POST /internal/sessions/{id}/events` screened `content`, `payload.content` and
  `payload.{text,message,output}`, but `_payload_to_appendable` also lifts `tool_calls` and
  `metadata` onto the event — and `event_to_chat_message` replays both into model context,
  `metadata.attachments` as image attachments and `metadata.thinking` as thinking blocks. So
  an injection placed in either field was stored and replayed unscreened.

  Screening is now derived from the lift rather than from a list of key names, because a
  list of key names was the defect. `screenable_text` lives next to
  `_payload_to_appendable`, so a field added there is screened the day it is added rather
  than the day someone remembers this endpoint.

- **The session export interpolated a thread id into a quoted header.**
  `Content-Disposition: attachment; filename="{thread_id}.jsonl"`. `effective_thread_id`
  rejects `:` and `#`, which made header splitting look unreachable — but it permits `"`,
  and one quote ends the parameter early and starts attacker-controlled header text. The
  filename is now reduced to an allowlist, since a filename needs nothing outside it.

  These are the last two of the four findings deferred from the August tenant-isolation
  review.


- **The tenant id was validated at the far end, not where it enters.** `effective_thread_id`
  refused a tenant containing `:` or `#`, so such a tenant failed closed on the first write —
  but it authenticated fine, because the id is accepted verbatim from an api-key `tenant_id`
  field or a JWT claim, and on Cognito `custom:*` claims are frequently user-writable. A late
  refusal is not a partition: `acme` and `acme:sub` would both "own" the thread `acme:sub:x`.

  The rule now lives in one place, is enforced at both doors as a 401, and runs in
  `Principal.__post_init__` — so an unusable tenant is unrepresentable rather than merely
  rejected on the paths someone remembered. `_usable_tenant` in the HTTP layer delegates to
  it, so the two cannot drift apart.

- **A lease could be taken on a thread id with no tenant prefix.** `session/lease.py` keys a
  lease as `felix:lease:{thread_id}`, so the tenant segment of `{tenant}:{suffix}` is the
  only thing separating one tenant's lease from another's. Every id reaching it came from
  `effective_thread_id`, which prefixes — but that was convention, and a route, job or plugin
  building an id without the prefix would have shared one namespace across every tenant with
  nothing failing. All three entry points now refuse an unscoped id.

  These are two of the four findings deferred from the August tenant-isolation review, taken
  together because they are the same fact: the prefix is load-bearing and was enforced only
  where someone happened to look.


- **The hosted providers' `secret_names` were inert.** `_compat` passed
  `api_key_config_key=config_key and None`, which is the constant `None`, while declaring
  `secret_names` anyway — and `_HYDRATE_MAP` is derived with
  `if spec.api_key_config_key and spec.secret_names`, so all thirty names were unreachable.
  The descriptor promised a hydration path it did not have, in the same commit as a comment
  claiming derivation made that impossible. The hosted rows now declare neither (they have
  no `Settings` field to hydrate *into*), and their credential can be a `secret:NAME` value
  inside `FELIX_MODEL_PROVIDER_OPTIONS`, resolved at startup through the same backend as
  MCP and peer refs. The guard test was equally vacuous — it reused the derivation's own
  predicate — and now asserts that no descriptor declares names it cannot reach.

- **`isolate_cache` was still dropped on the primary streaming path.** The previous fix
  threaded it into `_stream` — which callers reach only when a provider has no
  `stream_turn` — while `stream_turn` itself resolved the options and discarded them. The
  same defect the fix was written to close, one method over. The conformance contract now
  asserts the flag is *honoured* on both paths, not merely accepted.

- **Embedders were registered for providers that may not serve `/embeddings`.** The whole
  OpenAI-compatible table was registered, so `FELIX_MEMORY_EMBEDDER=groq` passed startup
  validation and failed at the first embed. It is a declared capability on the row now.
  Relatedly, `FELIX_MEMORY_EMBEDDING_MODEL` defaults to `bge-base-en-v1.5`, a
  sentence-transformers name, and was read unconditionally — so that string went out as a
  model id to OpenAI, which the old `_build_openai` did too. Only an explicitly set value
  wins now; otherwise the provider's own default applies.


- **A catalog entry that stated no rates was billed as Claude Sonnet.** `ModelPricing()`
  carries Sonnet's list price in its own field defaults, and it was the `pricing` field
  default — so `gpt-4.1`, `gpt-4`, `o1`, `o3`, `o4` and `llama`, each of which says in a
  comment that it has *no bundled rate*, were all priced at $3/$15 per Mtok anyway. An entry
  that does not state rates now has none.

- **"Free" was nearly made a property of the model's name.** An earlier pass in this cycle
  priced the `llama` catalog entry at zero to represent a local runtime. `entry_for` matches
  by substring, and Llama is sold by Workers AI, Groq, Together and Fireworks — so that would
  have made every hosted Llama free to `limits.max_cost_usd`. Whether tokens cost anything is
  a property of the provider (`ProviderSpec.bills_per_token`), and only `ollama` sets it
  false; a local route can still declare a spend cap because zero spend satisfies any cap.


- **Cross-provider handoff decided the provider by sniffing the model id.**
  `provider_family` matched `claude`, `gpt`, `llama`, `mistral` as substrings — the last
  vendor sniff in the harness — and it gates whether a thread's tool calls and images are
  flattened to plain text before switching models. It got two things wrong: an operator who
  routed `claude-flavoured` to OpenAI got the wrong answer, and two *different* unrecognised
  providers both answered `unknown`, so a genuine cross-provider switch looked like a no-op
  and replayed content the next model could not read.

  The function already took a `routes` argument and **no caller passed it**, so the clean
  path existed and was simply unwired. The route table is now the only source; an id missing
  from it is distinctly unknown, keyed on the id, so two unrecognised models still count as
  a family change.


- **The OpenAI path shaped nothing, and carried an Anthropic field to every endpoint.**
  `ModelQuirks` had exactly one reader — the Anthropic wire format — and its docstring said
  so. The OpenAI-compatible path, which is also Ollama and every LiteLLM/vLLM gateway, had
  no `max_output_tokens` clamp and no sampling suppression, which is why the `o1`/`o3`/`o4`
  catalog entries could never have worked: those reject `temperature` and require
  `max_completion_tokens`, and both were sent regardless. Meanwhile it emitted an Anthropic
  `thinking` block into **every** request whenever `spec.thinking_budget` was set, on the
  grounds that a LiteLLM proxy to Anthropic honours it — but the same body goes to
  api.openai.com, to Ollama and to any self-written gateway, and a server that validates its
  request schema rejects the unknown key outright. `reasoning_effort` went out just as
  widely, to models that have never accepted it.

  Shaping is now capability-driven on both paths. Crucially it keys on `known_entry_for`,
  not `entry_for`: an unmatched id yields `_DEFAULT`, whose quirks describe the current
  Claude generation, so shaping on that would strip `temperature` from an unknown OpenAI
  endpoint that accepts it. Unknown means shape nothing — on this path omitting an optional
  parameter is survivable and sending a rejected one is a hard 400. The Anthropic block now
  keys on a new `ModelCatalogEntry.native_wire`, because `ModelQuirks.budget_tokens` defaults
  to `True` and so cannot tell an OpenAI entry from a pre-4.6 Claude one — an Anthropic model
  behind an OpenAI shim still gets its block, and nothing else does.

- **`ModelChatOptions.isolate_cache` was ignored on the text-stream path.** `stream()`
  resolved the options and then dropped them, so `_stream` could not see the flag: a
  summariser or screening call still wrote the conversation's `prompt_cache_key`, churning
  the cached prefix the next real turn would have hit — the exact thing the option exists to
  prevent. It is threaded through to both wire formats now.


- **`limits.max_cost_usd` was enforced against a number nobody chose.** Three faults
  compounded. The catalog's `_DEFAULT` carried `ModelPricing()`, whose field defaults are
  Claude Sonnet's list price, so **every model Felix did not recognise billed at $3/$15 per
  Mtok** — including anything an operator added through `FELIX_MODEL_ROUTES`, and every
  local Ollama model, which is free. `entry_for` was then fed the *logical* route id rather
  than the wire model, so a route named `fast` matched nothing in the catalog and fell to
  that default even when it pointed at a model Felix knows perfectly well. And a turn that
  reported no usage accumulated nothing and said nothing, leaving the run uncapped.

  Unknown is now unknown: `ModelCatalogEntry.pricing` may be `None`, an unpriced model
  contributes zero rather than a guess, and `is_priced()` distinguishes "free" from "no
  idea" — a local model is priced at an explicit zero, which is a fact about the deployment
  rather than an absence of information. Pricing keys on the wire model
  (`record_usage(..., wire_model_id=...)`) while reporting keeps the logical name, because
  that is what an operator configured and recognises.

  A cap that cannot be counted now fails at *compile*: a manifest that explicitly declares
  `limits.max_cost_usd` on a model with no known rates is refused, with `spec.model.price`
  named as the way to supply them. Only a declared cap is refused — `effective_limits` fills
  an unset one from `ABSOLUTE_LIMITS`, and refusing on that would break every local
  deployment over a ceiling the author never asked for. At runtime, a turn reporting no
  usage logs a warning and increments `felix_model_unmetered` instead of passing silently;
  the usual cause is a provider whose streamed response omits usage, which makes the whole
  run free as far as the budgets are concerned.

- **Compaction sized every manifest against 128K.** `runtime.py` resolved the context window
  from `spec.model.id`, which is a logical route name, so it matched only the loose
  `claude-sonnet` family key. A manifest on the default route compacted against 128K instead
  of the 1M window it pays for — summarising away seven eighths of the context and spending
  a model call to do it. It now resolves the route first.


- **`FELIX_MODEL_ROUTES` provider names are validated at startup.** It was the one
  registry-backed setting `_validate_registry_backed_settings` skipped, so a typo surfaced
  mid-request and only on the route actually taken — a bad *fallback* stayed invisible until
  the primary was already failing, which is the worst moment to find a second misconfiguration.

- **The OpenAI embedder could never be pointed at a gateway.** `_build_openai` read
  `settings.openai_base_url`, which is not a field on `Settings` and never has been, so the
  read always fell through to `api.openai.com` — silently, on the inline memory-recall path,
  while the model client honoured `FELIX_LITELLM_BASE_URL`. Both now resolve through the same
  provider descriptor and agree by construction.

- **An Ollama base URL ending in `/v1` produced `/v1/v1`.** The factory appended
  unconditionally; the descriptor appends only when absent.

- **`build_model` no longer lazily re-registers providers.** `if not list_model_providers():
  register_builtin_providers()` was a sentinel that could only be right while nothing else
  registered first: after a `reset_model_provider_registry()` it restored the three builtins
  and dropped every plugin provider, because `load_optional_plugins` had already run and would
  not run again. `felix.patterns` registers the builtins at import, before anything can call
  `build_model`.

- **The in-memory manifest store outlived the test that wrote to it.** The `memory://` twins
  are module-level dicts by design, but nothing reset them between tests — so a manifest
  stored as `quick` shadowed the bundled file for the rest of the session, and a minimal one
  has no `auth.inbound` block, which 401'd everything downstream. That surfaced once as
  eleven unrelated tests failing together, and the response at the time was to avoid the
  assertion rather than fix the isolation.

  `tests/conftest.py` now resets the store and the resolver caches around every test, and
  the end-to-end write test that caused it is restored. `clear_resolver_cache()` also clears
  the bundled cache, which it never did — under `manifest_source=bundled` that is the only
  manifest cache in the system, so "clear the resolver cache" was not clearing it.


- **`create_app()` crashed on boot when it was not handed settings.** The manifest-source
  posture added `if not settings.bundled_only:`, but `settings` is the *optional* parameter
  — the resolved config is `cfg = settings or get_settings()`. Production calls
  `create_app()` with no arguments, so the shipped image raised `AttributeError: 'NoneType'
  object has no attribute 'bundled_only'` before serving a request.

  The whole suite stayed green because every test passes `settings=` explicitly. There is now
  a test that does not, which is the only kind that could have caught it.


- **A canary looked benchmarked when nothing had benchmarked it.** `run_continuous_eval`
  reads `canary_version` from each active canary and passes it to `start_run`, which writes
  it onto the eval run row — but resolution never used it. So the score belonged to whatever
  version was active while the row said "canary". The number existed and was wrong, which is
  worse than not measuring: a rollout gate reported a result it had not earned.

  `resolve_tenant_manifest` now takes `pin_version` and the eval runner passes the version it
  recorded. A pinned version that cannot be resolved fails the run — `fail_count` equal to
  the item count, with the error in `scores` — rather than falling back to the active
  manifest, because falling back is precisely what made the original bug invisible.


- **The browser handed a model-supplied hostname to Chromium, which resolved it again.**
  Pinning outbound HTTP to a validated address left the browser as the only advisory guard,
  and the worst-placed one: its URL comes from the model rather than a manifest, so a name
  answering the guard's lookup and Chromium's differently could reach an address the guard
  had just refused — cloud metadata being the obvious target, with `op: content` returning
  the body.

  The browser now launches with `--host-resolver-rules=MAP <host> <validated address>`, so
  the navigation host can only reach the address the guard approved. Verified end to end
  against a real Chromium, not only by constructing the flag.

  The hostname is matched against a strict pattern before it reaches that flag. It is a
  comma-separated list, so a host containing a comma — which `urlparse` will hand back
  quite happily — could append rules of its own, and `evil.com,MAP * 169.254.169.254` would
  point every other name at the metadata service.

  Cross-host subresources and redirects remain advisory: they keep resolving normally and
  are checked per request but not pinned, because denying them breaks any page that loads
  assets from a CDN. That residual is documented in `deploy/GOVERNANCE.md` rather than left
  to be discovered, along with the launch rule that would close it.


- **The SSRF guard was advisory: it validated one lookup and the connection used another.**
  `assert_safe_outbound_url` resolved a hostname, checked the answers, discarded them, and
  let httpx resolve again independently at connect. Two ways through. A name that resolves
  differently the second time wins — a TTL of zero is free to publish. And a nameserver that
  simply drops the guard's query while answering the client's wins outright, because an empty
  `resolve_host` was treated as "defer to the connection": no race, no timing, just two
  different answers to two different askers.

  Outbound HTTP now goes through a transport that resolves once, validates every returned
  address, and connects to an address it validated. There is no second lookup to disagree
  with the first. One bad answer refuses the whole name, so a round-robin containing a
  private address is not reachable by retrying, and a lookup that fails or times out refuses
  the dial — safe here precisely because this *is* the connection.

  TLS is unaffected. httpcore passes the origin hostname to `start_tls` regardless of what
  `connect_tcp` was given, so SNI and certificate verification still run against the name the
  caller asked for while the socket goes to the pinned address. Verified against a real
  HTTPS host.

  The syntactic half — scheme, `http` outside development, internal names and suffixes, IP
  literals — now runs on the transport itself rather than only at each call site, so a new
  caller cannot get half a guard by forgetting a line, and a redirect target is checked on
  the same terms as the original URL. `proxy`, `mounts` and `uds` are refused rather than
  ignored: each chooses a destination the guard never sees. Note that supplying a transport
  also disables httpx's environment proxies, so `HTTP_PROXY` no longer applies to these
  calls.

  Every approved address is kept, not just the first. Pinning one would discard the fallback
  Happy Eyeballs provides, which is what lets a host with an AAAA record work from a
  container with no IPv6 egress; each address in the list has been validated, so they are
  tried in turn.

  **The browser is the exception**, and it is the model-facing one: Chromium resolves for
  itself, so there the check stays advisory. Documented in `deploy/GOVERNANCE.md` rather than
  quietly covered by the claim above.



- **A blocking DNS lookup ran inside a pydantic validator, on the API event loop.**
  `assert_safe_outbound_url` resolves hostnames, and three schema validators called it while
  parsing — so every MCP server, peer and container ref cost a synchronous `getaddrinfo` on
  every manifest read *and* write, freezing every other request on the worker for the
  duration. Measured here at 37.9 ms/ref on a cold cache against a resolver that answers —
  which is the case that matters, since distinct hostnames are exactly what an attacker
  supplies — and seconds each against a nameserver that drops queries rather than
  answering. The same 64 refs now parse in 0.4 ms.

  The validators keep the checks that never needed a lookup: scheme, `http` outside
  development, internal names and suffixes, and IP literals including the decimal form.
  Resolution moves to dial time — `mcp_rpc` already did it there; the HTTP tool, container
  and peer clients were doing it at construction — and runs through
  `assert_safe_outbound_url_async`, which is `asyncio.to_thread` around the same function, so
  the fix does not simply relocate the stall to the tool-call path.

  Dial time is also the better placement: a hostname validated when a manifest is written can
  resolve somewhere else by the time it is dialled. The honest framing is that parse-time was
  a *second, independent* observation of the record, so this trades two chances to observe for
  one — a weak defence, since an attacker can publish a benign record until the manifest is
  stored, hours before the dial.

  Two hardening changes came out of reviewing it. The lookup now runs on a 3s budget and a
  timeout **blocks** rather than falling through: `to_thread` uses the loop's shared default
  executor, a running thread cannot be cancelled, and the guard is advisory — httpx resolves
  again at connect — so letting a slow resolver pass would hand it the exact bypass the guard
  exists to close. And a refused dial no longer reports the address it resolved to: that
  string lands in a tool message the model reads, which turned any peer, container or MCP ref
  into an internal-DNS oracle. The detail is logged instead.

  The browser was the last resolving check still on the event loop, and the worst placed —
  once per subresource, on a model-supplied URL. Its route interceptor is `async` and already
  awaits, so it now awaits the check too.

  Authoring feedback moved with it. `felix validate-manifest` now resolves every outbound
  host and rejects blocked addresses, which is where a DNS lookup belongs: no request is
  waiting on it. `--no-resolve-egress` for an air-gapped CI runner.



- **The last two hardcoded outbound timeouts, and no bound on the configurable ones.**
  Making the model and MCP ceilings raisable left `memory/embedder.py` on a hardcoded 60s
  for an OpenAI-compatible `/embeddings` call — a model-provider request that ignored the
  model-provider setting — and `HttpExecutor` on a hardcoded 30s. The embedder now reads
  `Settings.model_timeout_seconds`, since both its factories already receive settings;
  **its effective default therefore moves from 60s to 120s**, and it runs inline on the
  memory-recall path. `HttpExecutor` is constructed nowhere in core — it is a plugin-facing
  export — so it now *accepts* a caller-supplied ceiling rather than gaining a configurable
  one.

  The container executor was the outbound client that missed the connect pin in the
  previous change, and the one carrying a tenant-supplied `timeout_ms`: a gateway that
  blackholes SYN could park a socket for the whole request ceiling, per tool call. Connect
  is now pinned at 10s across every outbound client, from one shared constant rather than
  five module-private copies of the same policy.

  `timeout_ms` was also unbounded on all five refs that carry it, so a tenant-supplied
  manifest could ask for a 24-hour outbound call and hold a connection open for it. It had
  no floor either, so a negative value reached `httpx` as a deadline in the past through the
  two conversion sites that did not floor. Both ends are now enforced in the schema, and
  `ClientToolRef.timeout_seconds` — the sixth timeout in that file, which spelled the same
  ceiling as a bare `3600` — shares the derived constant.

  A security review of the bound then found the larger hold-open knob on the same
  tenant-supplied surface: `ApprovalRule.ttl_seconds` was unbounded, and an unanswered
  approval holds the request, an asyncio task and a Redis connection for its whole TTL while
  the waiter polls once a second for the duration. A thousand-day TTL validated. Both it and
  `command_screening.approval_ttl_seconds` now share the derived ceiling.

  The outbound ref lists are capped at 64. Validating one ref resolves its hostname through
  a synchronous `getaddrinfo` inside a pydantic validator, on the API event loop, so list
  length multiplies a blocking call — an uncapped list in one `PUT /manifests` could stall
  every other request on the worker for minutes. The cap contains the amplification; moving
  resolution off the validator is the actual fix and is not attempted here.

  Memory recall now degrades on time as well as on error. It runs inline in a turn and is
  best-effort, so inheriting a model timeout sized for a long generation was the wrong
  budget; a hung embedder is abandoned after five seconds.

  Treat the ceiling as a bound rather than a guarantee: `max_wall_clock_seconds` is checked
  before dispatch and between turns, never during a call, so a run's real ceiling is its
  budget plus the longest call it can still start.

  The invariant added with the previous change was vacuous: it matched only
  `timeout=<constant>`, and the same commit moved every literal inside `httpx.Timeout(...)`
  — an `ast.Call` — so nothing it named could trigger it. It now resolves the value (bare
  constant, module-level constant by name, all-constant `httpx.Timeout(...)`, or a missing
  `timeout=` where httpx applies its own silent 5s) and discovers its own file set instead
  of reading a hand-maintained list that a new client silently escapes. Exemptions are
  explicit and carry a reason.

- **A model request that legitimately took longer than two minutes failed the run, three
  times over.** Six `httpx.AsyncClient` sites in `patterns/model.py` shared a hardcoded
  `timeout=120.0` with no setting, and `_post_with_retry` caught `httpx.HTTPError` — which
  includes `ReadTimeout` — and retried. So a generation that needed more than the ceiling
  re-sent identical input and waited out the identical ceiling `max_retries + 1` times
  before surfacing as a 500.

  This is not hypothetical: it is what stopped an agent from pushing three files through a
  single MCP tool call, because emitting roughly 40 KB of file content as tool arguments
  takes longer than 120s. The work had to be split into one call per file.

  `FELIX_MODEL_TIMEOUT_SECONDS` (default `120`) now bounds each HTTP request to a model
  provider, read from the `Settings` the client was built with rather than the process
  globals — `build_model` exists so a caller can pass its own. On a streaming call it bounds
  the gap between chunks rather than the whole turn, which is why the reported failure only
  ever hit the non-streaming `chat()` path.

  Read and write timeouts are no longer retried; connect timeouts and retryable status codes
  are unchanged, because nothing was accepted and the next attempt is a genuinely different
  bet. Connect is pinned at 10s separately, so raising the request ceiling for a long
  generation does not also let an unreachable endpoint hang for that long.


- **The anomaly scan and continuous eval only ever ran for tenant `default`.** Both
  take `tenant_id: str = "default"` and the worker cron passed nothing, so on a
  multi-tenant deployment anomaly detection covered one tenant and a canary in any
  other was never benchmarked — the rollout looked clean because nothing looked.
  `run_due_jobs` had the identical bug and was fixed once already; these two were not
  swept up with it.

  Both now sweep every tenant, enumerated from the data that gives them work (audit
  events, active manifest pointers), isolating each tenant's failure so one tenant's
  bad data cannot stop detection for the rest. The enumeration takes an RLS bypass —
  the worker has no request context, so without one the policy filters everything and
  the scan sees nothing for anybody.

  This is a detection-coverage gap rather than an exploit: nothing was exposed, but
  on a multi-tenant deployment the control reported healthy while watching a single
  tenant.

- **`SkillRef.name` and `version` shaped an object key without validation.** Both are
  unvalidated manifest strings interpolated straight into
  `skills/{tenant}/{name}/SKILL.md`. No shipped backend could be walked with them —
  the filesystem store rejects `..` segments and S3/GCS treat keys as literal text —
  but `artifacts.py` deliberately validates its own key parts rather than trusting
  whichever store an operator configured, and this loader did not. They are now
  checked as single segments before any key is built, so the guarantee does not
  depend on the backend.

  The lookup order was also interleaved, tenant-then-shared *per version*, so a
  shared **versioned** skill was tried before the tenant's own unversioned one and
  the tenant's was never read — the same shadowing shape as the `AGENTS.md` layer
  fixed alongside the context-file scoping. Every tenant-scoped key is now tried
  before any shared one. The shared `skills/{name}/` namespace remains an operator
  layer: no route lets a tenant write a bare object key.

- **`POST /internal/sessions/{id}/events` wrote into whatever tenant the consumer
  credential named, whatever thread the path asked for.** The session id went
  straight from the URL into `append_event` with no tenant prefix, no delimiter
  rejection, and no check that the thread belonged to the caller — the one rule
  `felix_api/threads.py` exists to keep in a single place. A consumer credential
  carrying no tenant resolves to `default`, so on that configuration every tenant's
  queue write-backs were filed into `default`'s session log. It was also the only
  primitive that could plant a thread id under a tenant that does not own it, which
  is what the memory-provenance cross-tenant read needed to be more than a wrong
  number.

  The id now has to belong to the caller's tenant or the write is refused with
  `403 thread_not_in_tenant`. Ownership is a prefix check rather than a
  delimiter-free suffix, because fibers legitimately mint `{tenant}:fiber:{id}`;
  ids are compared whole, so `acme:default:x` is a different thread from
  `default:x` rather than a route to it.

  **Operator action:** a queue consumer must authenticate with a credential scoped
  to the tenant whose work it processes. A tenantless service key can now only write
  to `default:` threads — previously it wrote to any thread, into `default`.

- **`/v1/chat/completions` composed a thread id by hand instead of using the shared
  rule.** `f"{tenant}:{body.user}"` applied the tenant prefix but never screened
  `body.user` for delimiters, so a client could send `user: "fiber:abc123"` or
  `"job:nightly"` and have its turns appended to a durable fiber's or a scheduled
  job's session log — which that unattended run then replays as history. Within a
  tenant, so not a cross-tenant read, but a prompt-injection channel into runs
  nobody is watching. It also minted ids the chat routes could never address, so
  those threads could not be listed, exported or deleted. It now goes through
  `effective_thread_id` and answers `400 invalid_user`.

- **The two thread-id helpers disagreed.** `effective_thread_id` rejected `#` and
  applied no tenant check; `thread_belongs_to_tenant` did the reverse. Both now
  refuse a tenant id carrying `:` or `#` — the tenant prefix is the whole ownership
  boundary, so `acme` and `acme:sub` would otherwise both "own" `acme:sub:x`, and
  `session/lease.py` keys a lease by thread id alone — and both cap the id at 512
  characters, which was previously unbounded into a primary key, an index, an
  advisory-lock key and a Redis channel name.

- **A manifest could name another tenant's object-store key and have the contents
  read into its system prompt.** `spec.system_prompt.files`, `system_md` and
  `append_system_md` are unvalidated strings, and the loaders tried each key *as-is*
  before scoping it to `workspace/{tenant}/` — so the unscoped attempt hit first and
  won. Anyone with `manifests:write` could author
  `files: ["workspace/other-tenant/notes.md"]` and read it back through the model.
  Keys are now rewritten into the caller's own prefix rather than offered the chance
  to escape it.

  The local fallback had the matching hole: it joined the key onto
  `FELIX_WORKSPACE_ROOT` with no containment, so an absolute key resolved outside the
  root entirely (`Path("/srv/ws") / "/etc/passwd"` is `/etc/passwd`) and `../` was
  never normalised. It now goes through `resolve_under_root`, the same gate the
  workspace tools use.

  `load_agents_md_layer` keeps its unprefixed lookup deliberately: its filenames are
  fixed, not manifest-supplied, so it is an operator-placed layer rather than a key
  an agent can choose. It now searches the tenant's own copy first, so that layer
  cannot shadow a tenant's file.

  `load_agents_md_layer` keeps a shared operator layer, but now checks **all three**
  tenant-scoped filenames before any shared one. The loop was tenant-then-shared per
  *name*, so a shared `AGENTS.override.md` beat a tenant's own `AGENTS.md` and that
  file was never consulted. The shared layer is object-store only: the workspace root
  is one shared directory with no tenant component, so any manifest binding
  `write_file` could otherwise drop an `AGENTS.override.md` into another tenant's
  system prompt.

  An existing test asserted the old behaviour — it stored `AGENTS.md` at the bare key
  and expected a manifest to reach it.

  **Operator action required.** Context files now resolve only under
  `workspace/{tenant}/`, in the object store *and* under `FELIX_WORKSPACE_ROOT`.
  Anything previously placed at a bare key or at the root of the workspace directory
  stops being found, silently — the prompt just loses that section. Move them:
  `workspace/<tenant>/AGENTS.md`, and `<FELIX_WORKSPACE_ROOT>/workspace/<tenant>/…`
  on disk. A bare `AGENTS.md` / `AGENTS.override.md` / `CLAUDE.md` **object** still
  works as a shared operator layer; the same file on disk no longer does.

- **The `remember` tool read the wrong tenant's session log.** `_provenance`
  resolved the session store without a `tenant_id`, so it always got tenant
  `"default"`. For every other tenant the thread was not there, `head()` returned
  seq 0, and each remembered fact was stamped `origin_seq = 0` — so `as_of` read
  the whole store as genesis and supersession ordering collapsed. Where a thread id
  also existed under `"default"`, the ordinal was read from *that* tenant's log
  instead, which is a cross-tenant read that only tenant RLS (opt-in, off by
  default) would have stopped.

  An existing test asserted the buggy behaviour: it seeded the log under
  `"default"` while running the request as another tenant, and passed only because
  the production code read `"default"` too.

- **Approvals were silently denied on any deployment with Redis configured.** Present
  in 0.2.0, 0.2.1 and 0.2.2 — verified against each tag — and the Compose default
  configures Redis. A run
  pausing for approval waits on a Redis `BLPOP`; the shared client sets a 2-second
  socket timeout; BLPOP blocks server-side while the client sits in a socket read.
  So every wait longer than two seconds — that is, every approval a human answers —
  raised a socket timeout on a perfectly healthy connection, was read as "Redis is
  unavailable", and fell back to an in-process future. The decision then went to
  Redis while the run waited on a future nobody would resolve, and the run was told
  `denied / timeout`. **The approver saw success and the agent denied the call**,
  with nothing logged, because from the code's point of view nothing failed. Not a
  multi-replica problem: the two halves miss each other inside a single process. If
  you run 0.2.x with approvals, assume decisions were lost and re-check anything
  that depended on one.
- **A steer sent after a Redis blip stopped reaching the run.** `_redis_failed`
  latched on the first failed connect and was never cleared, so the process fell
  back to a queue local to itself for its whole life. `enqueue` kept returning
  `{"queued": "steer"}` while the replica running the turn saw nothing — a user
  typing "stop" got a 200 and the agent kept going. The same latch degraded thread
  notifications to polling permanently. Connection handling is now one helper with
  a bounded cooldown, shared by notifications, steer and waiters.
- **Thread notifications, on several counts.** A subscription was scoped to a single
  wait rather than to the reader, so a stream subscribed and unsubscribed once per
  poll interval; the reference count was taken after the SUBSCRIBE round trip, so a
  departing reader could unsubscribe a channel an arriving one still needed while it
  believed it was being notified; and a connect abandoned by a closing event loop
  left a guard that disabled notifications for the life of the process.
- **`memory://` announced every append on tenant `"default"`.** The in-memory session
  store took a tenant and ignored it, so a real tenant's reader was never woken and a
  `"default"` reader was woken by other tenants' writes. Development and test path
  only — Postgres was unaffected — but the two stores no longer disagree.
- **The PgBouncer overlay named an image tag that does not exist.**
  `edoburu/pgbouncer:1.25.2` is the version PgBouncer prints in its own log, not a
  published tag, so `make up-pooled` failed at the pull.
- **Content screening decided trust from a denylist, so it failed open.**
  `Tool.executor.transport` is an open string — a plugin may mint its own — but
  `apply_content_screening` skipped any tool whose transport was not on a hardcoded
  untrusted list. Two in-tree transports were already falling through it: `http`,
  whose executor returns arbitrary remote response text, and `client`, whose content
  originates in the user's browser. Neither was screened by default, and nor was any
  transport a third party introduced. Trust is now an allowlist — only in-process
  (`local`) tools skip screening.
- **Plugin tools did not exist outside the API process.** `compose()` was the only
  path that registered them, so a manifest naming a plugin tool resolved over HTTP
  and raised `Unknown tool` when the same manifest ran as a durable fiber, a
  scheduled job, or an eval. `default_tool_provider()` now performs the same plugin
  pass, and a test asserts both paths resolve the same tool set.
- **Three plugin registration methods were never read.**
  `register_authenticator`, `register_router`, and `register_audit_sink` accepted
  registrations that core never consulted — a plugin following the documented
  Protocol got no error and no effect, and `register_authenticator` was advertised in
  the module docstring. Authenticators are now resolved by the auth middleware, audit
  sinks receive events beside the usage sink, and the redundant router seam is gone
  (`plugin.routes()` already covered it). A repo invariant now fails if any
  `register_*` method loses its consumer.
- **An unrecognised session strategy silently became `full_replay`.** A typo
  (`windowed-20`, `compact`) bought unbounded context with nothing in the logs. It
  now warns and names the known strategies.
- **A JWT verifier with an unknown scheme was dropped without a word**, leaving an
  operator with a verifier that simply never matched.
- **`uv sync --extra oidc|a2a|granian` failed from the repo root.** All three were
  declared on `felix-harness` but never forwarded from the workspace root.
- **A plugin whose `register()` raised took down whichever process loaded it.**
  `load_optional_plugins` guarded `ep.load()` but not `register(registry)`, leaving
  the front door of the seam as its one undefended call site — including
  `Settings.validate_runtime`, whose job is to produce a legible startup error.
- **`felix doctor` red-FAILed a plugin-registered auth mode**, having just loaded
  the plugin that registered it. The built-in mode set was written out in three
  places; it now has one home in `felix.auth.context.BUILTIN_AUTH_MODES`.
- **Backend names were validated only in the API process.** The worker learned
  about `FELIX_SECRETS_BACKEND=vualt` from a traceback in the middle of a task;
  it now validates at startup, and `felix doctor` reports the same check.

## [0.2.2] — 2026-08-25

### Fixed

- **The Helm chart no longer pins the previous release's image.** `Chart.yaml`'s
  `version` and `appVersion` and `values.yaml`'s `image.tag` track the release,
  but `RELEASING.md` listed only the nine Python version fields and both greps it
  offered matched Python files alone — so `v0.2.1` shipped a chart still pinned to
  `0.2.0`. `helm install` from that tree deployed the image `v0.2.1` existed to
  replace: the one where a migrated database returns no rows to a deployment that
  has not opted into RLS. Anyone who installed `v0.2.1` by chart got `0.2.0`;
  reinstall or `--set image.tag=0.2.2`. The procedure now counts twelve places and
  greps for all of them.


## [0.2.1] — 2026-08-25

### Fixed

- **A migrated database no longer returns nothing to a deployment that has not
  opted into RLS.** `0006_tenant_rls` applies `ENABLE` *and* `FORCE ROW LEVEL
  SECURITY` unconditionally, while its header described the migration as
  optional — "enable with `FELIX_DATABASE_RLS=true`" — which is the runtime half.
  With that flag false (the default) the `after_begin` listener set no GUC at
  all, so the policy's `tenant_id = current_setting('app.tenant_id', true)`
  evaluated to `NULL`, and every one of the 16 tenant tables returned zero rows.
  Silently: no error, just empty results. Only a superuser or `BYPASSRLS` role
  escaped it — which is what the bundled compose stack uses, and why this never
  appeared in local development while being a total outage on managed Postgres,
  where you are not superuser. The listener now declares `app.rls_bypass`
  explicitly when RLS is off, which is what `database_rls=false` means; tenant
  scoping in the query layer is unchanged and remains the primary isolation.
  `FELIX_DATABASE_RLS` is now a genuine runtime toggle — flip it and restart, no
  migration needed. The migration stays unconditional on purpose: one that
  produced a different schema depending on the environment it ran in would not be
  reproducible, and there would be no way to enable RLS later without re-running
  DDL.

- **An RLS transaction that cannot name its tenant says so.** With
  `FELIX_DATABASE_RLS=true`, a transaction whose tenant did not resolve also set
  no GUC and saw nothing. Filtering is the correct answer there — a bypass would
  be a hole — but it was indistinguishable from an empty table. It now logs at
  WARNING naming `rls_bypass()` and `rls_tenant()`.

- **`felix doctor` reports RLS coherence.** The schema half and the runtime half
  can disagree in either direction and neither shows up in a request: policies
  without the flag means the app bypasses them, the flag without policies means
  nothing enforces it, and policies plus the flag plus a superuser connection
  means the policies are skipped anyway.

- **Streamed `parallel` and `plan_execute` runs are metered.** `_yield_model_stream`
  drove the model through `model.stream()`, which yields text and nothing else, and
  never called `record_usage` — the sole feed for `limit_state.tokens_input`,
  `tokens_output` and `cost_usd`. Their synthesis and planning inferences were
  therefore invisible to `limits.max_input_tokens`, `max_output_tokens` and
  `max_cost_usd`, and produced no usage row, metric, or plugin sink record, while the
  non-streaming twins of the same methods metered correctly. A declared spend ceiling
  that only holds when you do not stream is not a ceiling. `reflect`'s verifier call
  was unmetered on both paths and is now recorded too. No bundled manifest uses these
  patterns, so this reached manifests that declare `spec.pattern: parallel` or
  `plan_execute`.

- **`reflect` no longer passes an answer it could not score.** `_score` returned
  `0.8 if len(answer) > 40 else 0.4` on any exception — above the 0.7 default
  `ReflectSpec.threshold` — so an unreachable verifier, a rate-limited one, or a reply
  of `"Score: 0.9"` that `float()` rejects all silently *passed* the gate that exists
  to catch bad answers, with nothing logged. It degrades to the same
  `_heuristic_judge_score` fallback `_judge_score` uses, says so at WARNING, and reads
  the first number out of a reply rather than assuming a bare one.

- **`ABSOLUTE_LIMITS` is indexed, not `.get()`.** A missing key resolved to `None`,
  which means "no cap at all" — the posture `effective_limits` exists to prevent. Its
  values are also coerced per field, since the dict mixes `int` and `float` and the
  `int` budgets were being filled from a `float`.

- **The built-in command-screening deny rules are type-checked.**
  `_DEFAULT_COMMAND_RULES` typed its decision as `str`, so they were never checked
  against the `Literal` that `CommandRule.decision` requires.

### Changed

- **`stream_turn` is declared on the `ModelProvider` Protocol.** It was reached by
  `getattr` and left off the published contract, so a third-party provider could
  implement that contract in full and still land in the unmetered `stream()` path with
  nothing to tell its author why.

- **Each composite pattern is implemented once.** `_DelegatingAgent` carried an `_x`
  and a `_stream_x` per pattern; the copies drifted, which is what produced the
  metering defect above. They now share one `_run_*(input, *, emit_events)`, the shape
  `patterns/react.py:_run` already uses. The agent moved to `patterns/delegating.py`
  and the deep pattern's plan tools to `patterns/plan_tools.py`, taking
  `patterns/__init__.py` from 920 lines to 152 and removing both of its `noqa: E402`
  imports.

- **The HTTP model client is one class per wire format.** `_OpenAIClient` and
  `_AnthropicClient` replace a 593-line class that branched on a
  `style: Literal["openai", "anthropic"]` flag in three places — the seam the
  `ModelProvider` Protocol and the provider registry above it already described.

- **The governance wrappers take their schema types instead of `Any`.**
  `apply_command_screening`, `apply_content_screening`, `apply_limits`,
  `apply_guardrails`, `apply_judges` and `wrap_final_response_judges` read typed
  attributes rather than `getattr(config, "field", default)`, which wrote every default
  twice and made a renamed field fail *open* — `getattr(screening, "enabled", False)`
  disables screening silently. `_EffectiveLimits` is now `EffectiveLimits`.

- **A test that needs an optional extra fails in CI instead of vanishing from it.**
  `tests/unit/test_temporal_backend.py` gated six tests on `temporalio` while the CI
  test job installed `--dev` only, so they never ran — and a module-level
  `importorskip` collapses to one collect-time skip, so they never appeared in the skip
  count either. Tests now gate through `require_optional(module, extra)`, which skips
  locally and fails under `FELIX_REQUIRE_OPTIONAL_EXTRAS=1`; CI sets it and installs
  the extras that gate tests. The coverage floor moves 60 to 70, matching the measured
  number.

- **`make check-ci` and `make conformance`.** Six gates CI runs had no make target, so
  `make check` could pass while CI failed.

## [0.2.0] — 2026-08-24

### Added

- **`ApprovalRule.when_args` gates a rule on the arguments a call carries.** Approval
  rules matched on tool *name*, which is the wrong granularity when a tool is harmless
  in one shape and a privileged operation in another. `remember` is ordinary capture
  until it carries a `topic_key`, at which point it retires whatever else holds that
  key — the same outcome `forget` is gated for, reached without touching `forget`, and
  `recall` prints every stored key so they are enumerable. Gating the whole tool would
  put an approval in front of every memory write. Names are not validated against the
  gated tool's schema, so a typo yields a rule that never fires and still passes
  `validate-manifest` and the attestation checks; a bind-time warning is the fix and is
  recorded in the roadmap.

- **`POST /chat/stream` honours `spec.execution.mode: durable`.** `POST /chat` enqueued
  a fiber and returned 202 with a `resume_token`; the streaming route did not mention
  the field at all, so a manifest asking for durable execution got it on one route and
  was silently ignored on the other. It now streams the run's progress: the first frame
  carries the token, status changes are reported as they happen, and a completed run
  emits its final message. A disconnect tears down the *poll* rather than the run,
  which is what durable is for — the opposite of the transient path, where a hung-up
  client deliberately kills the run so it stops burning tokens.

- **`FelixClient.prompt` waits for a durable run.** It returned the 202 receipt as
  though it were the answer, so a caller switching a manifest to durable got
  `{"status": "accepted", …}` where the content used to be — no error, just the wrong
  shape. It now polls to a terminal status and emits progress. `wait_s` bounds the
  wait: `0` returns the receipt without polling, and exhausting a budget returns
  `status: "waiting"` rather than a failure, because the run is still going and the
  token still resolves.

- **`GET /chat/history` can be paged, and is bounded.** It returned every message a
  thread had ever had, so the response grew for the life of a thread with no way to ask
  for less. `limit` takes the *newest* events — `get_events(limit=n)` takes the first
  n, which for a transcript is the wrong end — and `before_seq` pages backwards from
  the `oldest_seq` a response hands back. The default is unchanged; lowering it is a
  breaking change for a shipped client and belongs to a product decision.

- **Store conformance covers sequence allocation.** `append_batch` returns the sequence
  numbers it allocated, asserted against both backends, because the in-memory twin
  counts a list where Postgres reads and locks — a value that is right for one arm
  proves nothing about the other.

### Changed

- **All four HTTP middleware layers are pure ASGI.** Starlette implements
  `BaseHTTPMiddleware` with a task group, an `anyio.Event` and a zero-buffer memory
  object stream per request, and every response chunk crossed four of them. `/health`
  went from 651.6 µs to 125.3 µs and an SSE chunk from 77.6 µs to 1.5 µs. An invariant
  now bans `BaseHTTPMiddleware` at the source level so the tax cannot be reintroduced
  by an `@app.middleware("http")` decorator.

- **The database pool and worker count are configuration.** `pool_size=5,
  max_overflow=10` was written literally into two engine constructors, so fifteen
  connections per worker was a ceiling nobody could raise without editing the source.
  Now `FELIX_DB_POOL_SIZE` (10), `FELIX_DB_MAX_OVERFLOW` (20),
  `FELIX_DB_POOL_TIMEOUT_SECONDS`, `FELIX_DB_POOL_PRE_PING`, and `FELIX_WORKERS` —
  the last of which was a bare `os.environ` read, invisible to `felix doctor` and
  absent from `.env.example`.

- **The resume stream backs off while a thread is quiet.** A fixed 1 Hz poll per client
  until 300 seconds of silence is 100 queries/second across a hundred reattached tabs.
  The poll now decays toward `FELIX_STREAM_RESUME_POLL_MAX_SECONDS` (10) — but only
  after thirty seconds of silence, because backing off costs first-event latency and
  the moment a user is most likely to act is right after they reattach.

- **Recall surfaces every kind of memory, as reference material.** Recall filtered on
  `kind="fact"`, so `instruction` and `task` rows were stored and never seen. All kinds
  now reach the prompt in one `<known_facts>` block, explicitly reference material:
  nothing recalled is an instruction to follow. An earlier version of this change gave
  user-stated rules their own honoured block; that was withdrawn when the provenance
  behind it did not survive review.

### Fixed

- **The streaming body-size cap silently did nothing.** `body_limit_middleware` wrapped
  the request and handed it to `call_next`, which ignores its `request` argument
  entirely — so the capped receive channel was never read, and a chunked upload with no
  `Content-Length` had no limit at all. That is the case the wrapper was written for.

- **`FELIX_DURABILITY=temporal` had never worked.** `@workflow.run` rejects a class
  declared inside a function — the worker re-imports it by name inside its sandbox —
  and the definitions were built inside `_defs()`, so every call raised. The two entry
  points failed differently, which is why nobody noticed: `start_fiber_workflow` failed
  into its caller's `except Exception`, logged a warning and let the Postgres scheduler
  run the chat, while `felix temporal-worker` failed outright. A failed start now
  records `backend: fibers` and `backend_fallback: temporal_start_failed` on the row,
  because degrading quietly is defensible and degrading invisibly is what let this sit.

- **Retirement of a memory is a decision by whoever retired it.** Resurrection was
  gated on the row's *writer*, so an operator deleting an agent-written row — nearly
  every row, and exactly the population the memory route exists to clean up — left
  something any rank-1 writer could bring back by re-storing the same text, with no
  tool call at all. Every retirement route now stamps `retired_by`, and both arms are
  asserted against one table of rules rather than against each other.

- **The rate limiter's eviction sweep stalled the worker.** It ran `max(v)` across every
  tracked key inside a request, and keys are per-IP — so the defensive component's cost
  grew with the attack it exists to absorb: 1412.7 µs at 50,000 keys, now 6.7 µs. The
  steady-state hit also went from 13.92 µs to 0.37 µs, because the old code rebuilt the
  whole timestamp list on every request.

- **The bundled skills catalog was re-read on every chat request** — `rglob` plus
  `read_text`, synchronously on the event loop, so it stalled every other request on
  the worker rather than only the one that asked. 56.6 µs to 1.4 µs, and off the loop.

- **Five independent store reads on the reattach path ran in series.** 2.66 ms to
  1.38 ms against a real Postgres, and the gap widens with network latency rather than
  narrowing. `GET /v1/models` resolved eight manifests one at a time on a cold cache.

- **Credentials were re-parsed on every authenticated request** — 15.6 µs for fifty API
  keys, and the same for JWT verifiers. Now parsed once per configuration, keyed on the
  raw settings string so a rotation invalidates it without anything having to remember.

- **Four resolver caches were unbounded, three of them keyed by tenant**, so every
  tenant that resolved a manifest left an entry for the life of the process. Now
  least-recently-used with a bound; the active-pointer cache in particular carried a
  30-second TTL and was never *removed* when it lapsed, only ignored.

- **The continue endpoint read the whole thread twice** — once for `analyze_wake`, then
  again to look at its last element.

### Security

- **A client-supplied `thread_id` could forge log entries.** It arrives as
  `Field(min_length=1)` with no charset constraint and reached the log without being
  looked up first, so one request produced two lines:

  ```
  stream cursor unavailable for t-1
  ERROR:felix_api.routes.chat:tenant acme authenticated as admin
  ```

  The second is fabricated. An attacker who can write "tenant X authenticated as admin"
  into the trail makes it argue for something that never happened, and the trail is what
  an incident is reconstructed from. Every untrusted value now goes through `loggable`,
  which escapes rather than strips — an injection attempt shows as a literal `\n`
  instead of vanishing.

- **Arbitrary exception text no longer reaches API clients.** The SSE handlers relayed
  `str(exc)` from a bare `except Exception`, so a driver error, a serializer failure or
  an assertion reached an external caller verbatim — connection strings and schema
  names included. Relaying is now opt-in per exception type through one funnel; anything
  else gets `internal error (request <id>)`, which keeps a report joinable to its
  traceback without the traceback travelling.

- **A rejected secret path was logged unescaped.** `FileSecrets.get` logs the requested
  path when it rejects it — a value chosen by whoever asked and, on that branch, one the
  loader has just refused. A newline in it forged an entry among genuine rejection
  records, and the forged one could claim an *accept*. An AST test now enforces that the
  module logs key names and never values.

### Added

- **Reconnect to a chat stream after it drops.** `GET /chat/stream/{thread_id}`
  replays what a client missed and then tails the thread; frames on both streams now
  carry an `id:` a client hands back as `Last-Event-ID`. A cold reconnect opens with a
  `snapshot` frame, a warm one replays only the events after the cursor. The cursor is
  the session log's own sequence rather than a per-connection counter — a counter
  restarts at 1 on every reconnect and so means nothing to the next connection. Only
  structural frames carry an `id:`; deltas arrive per token and stamping them would
  cost a query each, and SSE leaves `lastEventId` untouched on frames without one.

  This does **not** change what happens to the run: a disconnect still tears it down,
  deliberately, so a hung-up client stops burning tokens. What a reconnect recovers is
  the thread as it stands, not the abandoned turn. Tailing polls the shared session
  log, so it works whichever replica served the original turn and needs no Redis.

- **Memory is on in `governed` and `cowork`.** Everything above shipped inert:
  `capture` was disabled in all eight bundled manifests and `recall.tools` defaulted
  off, so no agent wrote or read a memory while the published docs described the
  feature as working. Both manifests now capture durable facts (3 per turn, 200-char
  floor) and expose the governed recall tools. Verified end to end against a real
  model and a real pgvector Postgres: the agent chose sensible topic keys
  (`ops.deploy_runbook_location`), both facts from one turn shared an ordinal, and
  recall found them through the full-text and topic channels.

### Fixed

- **`spec.memory.capture.model` was declared and never read**, so fact extraction ran
  on the turn's model — a small mechanical job billed to a frontier model on every
  turn, doubling the cost of having memory at all. Now honoured, falling back to the
  turn's model when the configured one cannot be built. Its default also moved from
  `llama-3-fast` to `claude-haiku`: the old default routes to Ollama, which a
  deployment with only an Anthropic key does not run, so capture would have failed
  every turn and said so only in a log.
- **Memories written through the `remember` tool carried no provenance.** The agent
  is compiled before the request's thread is known, so the bind-time `thread_id` was
  always empty and `origin_seq` never set — leaving half the store looking like
  genesis to an as-of query. Both are now resolved from the request context at call
  time. Found by running the feature, not by a test.

- **Memory management API** — `GET /memory`, `GET /memory/search` (the same hybrid
  ranking the agent sees, with the contributing channels reported so a surprising
  result is explainable), `GET /memory/as-of/{turn_seq}`, `POST /memory` and
  `DELETE /memory/{id}`, under new `memory:read` / `memory:write` scopes. An agent
  that remembers across sessions otherwise accumulates a store nobody can inspect —
  and when it starts answering from a fact that is stale, wrong, or was extracted
  from a hostile tool result, finding and removing that fact needed a database
  console. The time-travel surface is read-only: rewinding memory is a data-loss
  primitive on a shared table, and session rewind is deliberately non-destructive.

- **Agent-facing memory tools** — `remember` / `recall` / `forget` / `list_memories`,
  behind `spec.memory.recall.tools` (off by default). They are bound *before* the
  governance block, so recalled text passes through secret masking, policies, content
  screening, limits, guardrails, judges and approvals like any other tool output. The
  automatic fact prelude bypasses all of it, which matters because recalled text was
  extracted by a model from earlier turns and those turns can contain whatever a tool
  returned. A test asserts the wrapping behaviourally and fails if the binding ever
  moves below the wrapper block.

- **Memory recall is hybrid, and no longer just the newest rows.** Recall was
  `ORDER BY created_at` — the most recent facts, related to the question or not. It
  now runs three independent channels and fuses their *rankings* with Reciprocal Rank
  Fusion: full text over `content_tsv`, topic key over `topic_tsv` (so "what timezone"
  finds `user.timezone`, which shares no words with the stored value), and vector
  similarity over the pgvector column (so a paraphrase with no shared tokens is found
  at all). Fusing ranks rather than scores is what makes the channels combinable —
  `ts_rank_cd` and cosine distance are not on comparable scales, so any weighted sum
  of them would be meaningless.

  Semantic recall is optional and off by default: `FELIX_MEMORY_EMBEDDER=none` needs
  nothing installed, and recall simply skips its vector channel. `sentence_transformers`
  uses the existing `embeddings` extra; `openai` and `ollama` speak an
  OpenAI-compatible `/embeddings` endpoint over httpx, which is already a core
  dependency. A failing embedder costs the vector channel, never the turn.

- **Long-term memory rows carry supersession and provenance.** `topic_key` makes a
  newer value supersede an older one atomically; ids are content hashes scoped by
  manifest, so storing a fact twice collapses instead of accumulating duplicates
  recall would return twice; `status` distinguishes superseded from forgotten; and
  `origin_seq` / `thread_id` are now actually populated — the turn-versioning columns
  existed but nothing wrote or queried them, because `capture_from_turn` had no way
  to pass an ordinal. The session log's own `seq` is the turn clock, so there is no
  second counter to keep in step. Adds `as_of()`, which reconstructs what was
  believed at a past turn including facts since superseded, plus `forget` and
  `get_many`. Migration `0009_memory_recall`; recall itself is still to come.

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

- **`memory_vectors` rejected every insert on Postgres.** `embedding vector(768)
  NOT NULL` has existed since `0001_baseline`, added by raw SQL and so invisible
  from `db/models.py`, and `put_memory` never supplied a vector. Every insert raised
  NotNullViolation; the only caller wraps it in `except: logger.debug(...)`, so
  long-term memory had never stored a row outside the in-memory twin and nothing
  said so. The constraint is dropped — a memory without an embedding has to be
  storable, since the design degrades to full-text when no embedder is configured.

- **Embeddings ran on the event loop, stalling every concurrent request.**
  `encode_texts` calls `SentenceTransformer.encode` synchronously, and all three
  consumers reach it while a turn is being served: tool retrieval (up to four times
  per loop step), procedural recall, and the `semantic:N` session strategy. With the
  `embeddings` extra installed, one encode blocked the whole worker — every other
  request on that process, not just the one that asked for it — and the first call for
  a model also loaded it from disk inside that stall. The work now runs in a thread.
  Tool retrieval keeps the cheap keyword path inline, since it is on the hot path and
  `tools_retrieval` is off by default; the guard that decides is pinned to the code it
  mirrors by a test that runs both. Also takes a lock around the model load, which the
  move to a thread pool is what made reachable — two concurrent misses would otherwise
  each construct the same multi-hundred-MB encoder.

- **Recalled memory facts never reached the model on a threaded chat.**
  `_assemble_messages` built the message list with the per-run prelude, then handed
  the thread to the session strategy — which builds a fresh list from the session log
  and returns *that*, discarding the prelude. So `<known_facts>` reached the model
  only on threadless invokes, which is not how anyone uses the API. The prelude is now
  applied after the render, directly following the system prompt. Position inside
  `messages` is cache-neutral (the ephemeral breakpoints sit on the tool list and the
  system block, never on a message), so the property the prelude exists to protect is
  unchanged. Every existing test checked that the block was *built* correctly; none
  checked that it survived assembly, which is how this went unnoticed.

- **One-off requests made during a turn shared the conversation's prompt cache.**
  Compaction summarising, memory extracting facts, inbound screening scoring, and branch
  summarisation each issue a model call in the middle of somebody's turn while carrying a
  completely different prefix — and each inherited the thread's cache identity. On an
  OpenAI-style endpoint `prompt_cache_key` defaults to `felix:<thread_id>`, so the side
  request churned the prefix the conversation had cached and the next real turn missed; on
  Anthropic the `cache_control` marker wrote a fresh cache entry, billed above base input,
  for a prompt never read again. `ModelChatOptions.isolate_cache` opts a request out of
  both, and every side request now sets it. Thinking is unaffected.

### Added

- **Interrupted tool calls are closed out, so a crashed run can be resumed.** A run killed
  mid-tool leaves an assistant turn holding a call with no result, and the provider rejects
  any transcript containing an unanswered tool call — so the one situation `/chat/continue`
  exists for was the one it could not handle. Each outstanding call now gets an
  `[error/interrupted]` result before the thread resumes.
- **`Tool.replay_safe` declares whether a tool may be re-run after a crash.** Whether the
  effect happened is not knowable after the fact; re-running a search costs latency, while
  re-running a payment charges twice. Defaults to `False`, so a tool that has not considered
  the question is never presented to the model as repeatable. Read-only built-ins declare
  it `True`.


- **Governance wrappers rebuilt each tool field by field**, so a field they did not know
  about was silently reset to its default on every wrapped tool — and every tool passes
  through the wrapper stack. `replay_safe` was very nearly lost this way on the same commit
  that added it. Cloning is now structural, and an invariant test asserts the round trip so
  the next field cannot be dropped quietly.

- **Store conformance suite.** `memory://` is the CI test path, and
  `tests/unit/test_invariants.py` asserted only that every Postgres-touching module *has*
  an in-memory twin — not that the twin behaves like the store it stands in for. Every
  green run was therefore evidence about the twin rather than about production.
  `tests/conformance/` runs one contract against both backends: append ordering and seq
  density, batch atomicity under concurrency, the full event round trip, query windows and
  filters, head/reset/wake, and secret masking. A new `conformance` CI job runs it against
  a real Postgres, and fails rather than skips when the database is missing, because a
  silently skipped arm looks exactly like a pass. Both backends pass the contract today;
  the point is that this is now checked rather than assumed.

### Fixed

- **Streaming runs produced no audit record for the turn.** `invoke` and `stream_events`
  were near-copies of one loop, and they had drifted: the non-streaming path emitted
  `user_input` and `final_response` audit events, the streaming path emitted neither, and
  nothing outside the pattern emitted them either. `deploy/GOVERNANCE.md` presents the
  audit log as the compliance evidence trail, and streaming is the default path for any
  chat UI, so the primary path was the one with no record. Tool-level audit was
  unaffected — it lives in the shared dispatch.
- **An aborted streaming run was not recorded to the session.** The non-streaming path
  appended the partial answer with status `aborted`; the streaming path only emitted an
  event. Both now do both.
- **A fatal tool error still drained follow-ups on the streaming path.** The
  non-streaming path returned immediately. A run that ended on a fatal tool error is not
  in a state a follow-up can continue from, so neither path drains them now, and the
  `final_response` audit records status `error`.

### Added

- **Compaction recovers from a context overflow.** The trigger is a token estimate —
  characters over four, anchored on the last reported usage — so it runs slightly behind
  the truth, and a manifest can declare a window larger than the model really has. When
  the estimate was optimistic the provider rejected the request and the run failed, even
  though the rejection was the most accurate signal yet that the conversation needed
  compacting. An overflow now forces a compaction pass and retries once; a second failure
  propagates. Throttling is deliberately excluded — it mentions tokens and limits too,
  and compacting over backpressure discards history to fix a problem a retry solves.
- **Two providers overflow without saying so, and are now caught.** One accepts the
  request and reports more input tokens than the window holds; another truncates the
  input and returns a length-stop having produced no output at all. Neither raises, so
  both were previously recorded as ordinary turns. In the streaming path recovery only
  happens before any delta has shipped, since a client that has already rendered text
  cannot un-render it.

### Changed

- **A streaming turn is one model call instead of two.** `stream_events` streamed a turn
  for display and then called `chat()` for the real answer, so every streaming turn ran
  the whole inference twice. The input was billed twice; only the second call was
  metered, so `limits.max_cost_usd` and the token budgets counted roughly half of what a
  streaming run spent and admitted about twice the intended budget; and the answer was
  sampled twice, so the text a user watched arrive could differ from the text that was
  saved. The streamed request also carried no tools, which is why the second call existed
  at all. `stream_turn` now yields display deltas and finishes by yielding the
  authoritative result — same message, tool calls, stop reason and usage. Providers that
  implement only `stream()` keep the previous two-call behaviour.
- **Streamed tool-call arguments are parsed and repaired.** Arguments arrive as JSON
  fragments concatenated across events, and models routinely emit raw control characters
  and invalid backslash escapes inside string literals. Both are repaired before parsing;
  a fragment that is still unparseable yields empty arguments — rejected by schema
  validation downstream — rather than raising and losing the turn.
- **Model metadata is one record instead of three tables.** Context window, max output,
  price, accepted request parameters, thinking support, and modalities lived in
  `patterns/capabilities.py` (longest **prefix** wins), `usage/catalog.py` (**substring**,
  and **first key in dict order** wins) and `usage/pricing.py` (substring, longest wins).
  Three rules for one question, and they disagreed: the request builder treated
  `claude-opus-4-5` as 200K while `/v1/models` published 1M for it, because `claude-opus`
  matched as a substring first. `felix/model_catalog.py` now holds one entry per family
  and one lookup; the rest are views over it. `patterns/capabilities.py` is removed —
  its `context_window` field was dead, which is how the two numbers drifted unnoticed.

### Fixed

- **Compaction ignored the model's real context window.**
  `spec.session.context_window_tokens` has a schema default of 128000 that pydantic fills
  in whether or not the operator wrote it, so a manifest on a 1M-context model compacted
  at 128K minus reserve — summarising away seven eighths of the window it was paying for,
  and spending a summarisation call to do it. An undeclared window now follows the model;
  an explicitly declared one still wins, including when it equals the default.
- **A turn cut off at `max_tokens` executed the tool calls it was still writing.** The
  ReAct loop only inspected `stop_reason` on the branch where the assistant produced no
  tool calls, so a truncated `tool_use` went straight to execution. Truncated arguments
  can still parse — `{"path": "/srv/app/tmp"}` shortened to `{"path": "/srv"}` is valid
  JSON naming a different target — and command screening judges the arguments it is
  handed, so the shortened value screened clean. Every tool call on an unfinished message
  is now failed with `[error/truncated]` and the run is recorded as `truncated`; the
  whole batch is refused, because it cannot be split into trustworthy and untrustworthy
  halves after the fact. Applies to both `/chat` and `/chat/stream`.
- **Extended thinking was write-only, so tool-using turns lost their reasoning.** Felix
  sent `thinking` on the request but discarded it from the response, and the session log
  dropped the field entirely. The provider signs each thinking block and requires it
  replayed alongside the tool call it produced, so a thinking-enabled manifest lost its
  reasoning at the first tool call and every later turn was answered without it. Thinking
  blocks are now captured, persisted on the session event, and replayed ahead of the
  `tool_use` blocks. Unsigned blocks are dropped rather than sent, since an unverifiable
  signature rejects the whole turn.
- **Long-context requests were under-priced.** Cost estimation had one flat rate per
  model, but providers that bill long context do so across the *whole* request once total
  input crosses a threshold. `max_cost_usd` is a fail-closed control reading that number,
  so a budget cap admitted more spend than it should. Price entries now accept `tiers`,
  where the highest matching threshold replaces the base rates. No bundled entry sets
  tiers — thresholds and rates move, and a stale number here mis-charges tenants — so
  they are supplied per deployment through a manifest price override.
- **A spent quota was retried like transient overload.** Both return 429, but an
  exhausted quota or a billing failure will not clear inside the request, so the full
  backoff ladder was added to a failure the caller was going to see anyway.
- **Parallel tool calls could interleave on one file.** `spec.tool_execution: parallel`
  runs a batch under `asyncio.gather` and two calls could name the same file, directly or
  through a symlink. Workspace writes now serialize on the resolved path.

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

[0.2.2]: https://github.com/felix-run/felix/releases/tag/v0.2.2
[0.2.1]: https://github.com/felix-run/felix/releases/tag/v0.2.1
[0.2.0]: https://github.com/felix-run/felix/releases/tag/v0.2.0
[0.1.0]: https://github.com/felix-run/felix/releases/tag/v0.1.0
