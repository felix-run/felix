# Observability

What Felix emits, what the names mean, and what an operator should watch. This page is the
"telemetry vocabulary" `docs/ROADMAP.md` asks for: without it the only way to know what to
graph is to grep the call sites.

`tests/unit/test_metric_catalog.py` re-derives the tables below from the source at test
time, so a metric added without a row here fails CI.

## Getting the data out

| Signal | Where | Needs |
| --- | --- | --- |
| Metrics (API) | `GET /metrics` on the API, Prometheus text format | nothing — `prometheus-client` is a core dependency |
| Metrics (worker) | `FELIX_METRICS_PORT` on the worker process | nothing |
| Traces | OTLP export | `FELIX_OTEL_ENABLED=true` + `felix-harness[otel]` |
| Logs | OTLP export, trace-correlated | `FELIX_OTEL_ENABLED=true` + `FELIX_OTEL_LOGS=true` + the extra |
| Audit / usage rows | Postgres, `GET /audit`, `GET /usage` | nothing |

Two properties of `/metrics` are deliberate and easy to undo by accident:

- **It requires authentication.** Label values include tenant-supplied manifest ids and
  remote MCP tool names, so an anonymous scrape discloses every tenant's manifest and tool
  names. It is not in `_PUBLIC_EXACT` (`felix/auth/middleware.py`).
- **It is rate-limited.** It is a scrape target with unbounded label cardinality, so it is
  not in the rate limiter's `SKIP_EXACT` either.

`FELIX_METRICS_PORT` on the worker carries the *same* label values and has **no**
authentication, because the worker has no auth middleware. Bind it to an internal network;
never publish it.

## Metrics

### Model and cost

| Metric | Labels | Meaning |
| --- | --- | --- |
| `felix_tokens` | `manifest_id`, `model`, `kind` | Tokens billed this turn; `kind` is `input` or `output`. |
| `felix_model_unmetered` | `manifest_id`, `model` | **Watch this.** A turn reported no usage, so it counted against no budget — `limits.max_cost_usd` and the token limits fail *open* for it. Usually a streamed response missing `stream_options.include_usage`. |
| `felix_model_call_seconds` | `model`, `status` | Provider call latency, one observation per attempt. |
| `felix_model_switch` | `from`, `to`, `reason` | A fallback or escalation changed model mid-run. |
| `felix_model_retry` | `provider`, `status` | An upstream call was retried. |
| `felix_model_retry_skipped` | `provider`, `reason` | A retry was declined (`reason=quota`). |
| `felix_model_timeout` | `provider` | `FELIX_MODEL_TIMEOUT_SECONDS` elapsed. |

### Tools

| Metric | Labels | Meaning |
| --- | --- | --- |
| `felix_tool_calls` | `transport`, `status`, `manifest_id`, `error_code` | One per tool invocation; `status` is `ok`, `error` or `denied`. |
| `felix_tool_call_seconds` | `transport`, `manifest_id` | Tool latency. Labelled like `felix_tool_calls` on purpose — the tool *name* is not a label, because MCP tool names come from a remote server and are unbounded. |
| `felix_interrupted_tool_calls` | `manifest_id` | Calls abandoned when a run was steered or cancelled. |

### Governance

`deploy/GOVERNANCE.md` tells operators to watch these. They are the reason a dashboard is
worth having at all — each one means a control did not do what the manifest implies.

| Metric | Labels | Meaning |
| --- | --- | --- |
| `felix_policy_deny` | `manifest_id`, `policy`, `tool` | A policy blocked a call. |
| `felix_policy_unsatisfiable` | `manifest_id` | A policy can never pass — it reads as a control and is one only in the sense that it denies everything. |
| `felix_rule_targets_nothing` | `manifest_id`, `rule`, `kind` | A rule matches no tool. Looks like a control; is not one. |
| `felix_untrusted_tools_unscreened` | `manifest_id` | Untrusted tool output reached the model without content screening. |
| `felix_content_screening` | `manifest_id`, `tool`, `action` | Screening ran; `action` says what it did. |
| `felix_secret_masking` | `manifest_id`, `tool` | A secret was masked out of tool output. |
| `felix_approval_required` | `manifest_id`, `tool`, `rule` | A call paused for human approval. |
| `felix_control_unavailable` | `control` | **Watch this.** A control could not run at all. |
| `felix_control_degraded` | `control`, `manifest_id`, `reason` | A control ran in a reduced mode (e.g. PII without Presidio). |
| `felix_egress_blocked` | `reason` | An outbound request was refused by SSRF/egress policy. |
| `felix_browser_egress_blocked` | `reason` | The same, from the browser tool. |

> **Caveat that will bite you.** A manifest's `spec.observability.metrics` allowlist
> suppresses counters not named in it, including every counter in this table. A zero on a
> governance panel can therefore mean "nothing happened" *or* "this tenant opted out of
> telling you". `docs/ROADMAP.md` carries this as an open decision.

### Run and worker health

| Metric | Labels | Meaning |
| --- | --- | --- |
| `felix_run_stop_reason` | `manifest_id`, `reason` | How a run ended. |
| `felix_context_overflow_recovered` | `manifest_id`, `reason` | The context window overflowed and the loop recovered. |
| `felix_worker_task` | `task`, `status` | One per periodic sweep. A `task` whose rate drops to zero has stopped firing — which otherwise looks identical to one that runs and finds nothing. |
| `felix_worker_task_seconds` | `task` | Sweep duration. |
| `felix_buffer_dropped` | `buffer` | **Watch this.** Audit or usage rows were dropped because a buffer hit `DEFAULT_MAX_PENDING`. Silent data loss otherwise. |

## Spans

Span attributes follow the OpenTelemetry **GenAI semantic conventions** where one exists,
so a backend that understands them (Jaeger, Grafana, Memoturn, any OTLP consumer) renders
model calls as generations with token usage attached rather than as anonymous timing bars.

| Span | When | Key attributes |
| --- | --- | --- |
| HTTP request | per request, via `opentelemetry-instrumentation-fastapi` | standard HTTP semconv; the trace root |
| `manifest` | one per compile | `manifest_name`, `manifest_version` |
| `chat {model}` | one per **provider call** | `gen_ai.operation.name`, `gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.finish_reasons`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `felix.usage.cache_creation`, `felix.usage.cache_read`, `felix.cost_usd`, `felix.model.route`, `felix.manifest.id` |
| `tool {name}` | one per tool call | `gen_ai.tool.name`, `gen_ai.operation.name=execute_tool`, `felix.tool.transport`, `status` |
| `worker {task}` | one per periodic sweep | `felix.worker.task` |

A fallback chain that tries two providers emits **two** `chat` spans, because the span
wraps the leaf client rather than the composite. That is deliberate: collapsing them would
hide the retry, which is the thing worth seeing.

`felix.usage.unmetered=true` on a `chat` span is the span-level twin of
`felix_model_unmetered` — that turn counted against no budget.

### Content is not captured

Prompts and completions do not appear on spans. `FELIX_OTEL_CAPTURE_CONTENT=true` opts in.
Leave it off unless the tracing backend is inside your trust boundary: a tracing backend is
an egress destination, and span attributes are not covered by the governance content
screening that guards tool output.

## Logs

`FELIX_OTEL_LOGS=true` ships the standard-library log stream over OTLP alongside traces.
The SDK stamps `trace_id` and `span_id` onto each record from the active context, so a log
line joins to the exact span that produced it — and Felix's own `request_id` rides along,
so an inbound `x-request-id` remains the thing you search by.

It is off by default because log volume is a cost the operator should choose.

**Why not tail the container logs instead.** Pointing a collector's `filelog` receiver at
`/var/lib/docker/containers` was tried and rejected: it needs the collector to run as root
to read files that are `root:root 0640`, hands it every container's logs on the host, fails
*silently* when it cannot (the receiver opens zero files and reports nothing), and is
Docker-shaped in a way that survives neither Kubernetes nor a process started by hand.
Emitting from the process needs no mount, no root, and is the only version that carries
trace context.

## Known limitations

Real, documented rather than hidden:

- **No multiprocess metrics.** `prometheus_client` is used in single-process mode, so a
  multi-worker Granian reports per-worker fragments of every counter. One worker per
  replica, or aggregate across the replica label.
- **A reused metric name degrades silently.** `observability/metrics.py` catches the
  `ValueError` from registering a name under a second label set and writes the sample as a
  `logger.info` line instead. The series simply never appears in `/metrics`.
- **`GET /audit/metrics` reports `avg_latency_ms: 0`.** It reads `payload.latency_ms` /
  `payload.duration_ms`, which the `tool_call` audit payload does not write. Use
  `felix_tool_call_seconds` instead.
- **No sampling below the trace root.** `FELIX_OTEL_SAMPLE_RATIO` is head-based and
  parent-respecting: a sampled request keeps all of its child spans.
