# Manifest secrets and governance

Felix treats agents as declarative YAML (`apiVersion: felix/v1`). This note
covers **secret refs**, **compile pins**, and **opt-in framework mapping**.
It is **not** a SOC2 Type II or EU AI Act conformity assessment — those stay
with the operator’s compliance program. Felix only fails closed when a
manifest opts into `spec.governance.frameworks`.

## Example agent

Bundled reference: [`manifests/governed.yaml`](../manifests/governed.yaml).

```bash
# Schema + framework rules (assume production)
felix validate-manifest manifests/governed.yaml -e production

# Local chat requires a non-anonymous principal with scopes:
#   chat:write  (inbound)
#   tools:calc  (calculator policy)
felix mint-jwt --sub ops --tenant default --scopes chat:write,tools:calc
```

`quick` / `support` stay anonymous for local DX. Prefer `governed` (or a
fork) for JWT / API-key production. Outbound MCP in `governed` is commented
out until `FELIX_MCP_AUTH_TOKEN` exists — scoped chat works without it.

## Secret injection

| Layer | Mechanism |
|-------|-----------|
| Platform (model keys, consumer secret) | `FELIX_SECRETS_BACKEND=env\|file\|aws\|gcp` + `hydrate_secrets()` at API/worker startup |
| Manifest outbound (`mcp_servers.auth`, `env`, peer/container `auth`) | `secret:NAME` or `{secret: NAME}` resolved at compile; **never** store resolved values in `manifest_json` |
| Redaction | Known secrets scrubbed from tool output, session events, audit payloads, fiber state |

Production (`FELIX_ENVIRONMENT=production`) or `governance.forbid_plaintext_secrets: true`
rejects Bearer/long-token auth and non-ref MCP `env` values.

PII: `spec.guardrails.providers: [pii]` uses **Presidio** when
`felix-harness[pii]` is installed, otherwise a regex fallback. Eval LLM judges
are opt-in via rubric `llm_judge` / `judge_criteria` or `felix eval --llm-judge`
(CI stays on `--mock`).

### AWS

```bash
FELIX_SECRETS_BACKEND=aws
FELIX_AWS_REGION=us-east-1
# Leave FELIX_ANTHROPIC_API_KEY empty; create SM secret felix-anthropic-api-key
# Manifest refs use the same backend, e.g. secret:FELIX_MCP_AUTH_TOKEN
```

See [aws/README.md](aws/README.md). Prefer IRSA over static keys.

### GCP

```bash
FELIX_SECRETS_BACKEND=gcp
FELIX_GCP_PROJECT=your-project
# Secret Manager ids: felix-anthropic-api-key, FELIX_MCP_AUTH_TOKEN, …
```

See [gcp/README.md](gcp/README.md). Prefer Workload Identity.

### Helm

Prefer `secrets.existingSecret` or **External Secrets Operator**
(`externalSecrets.enabled` in the chart) instead of baking tokens into
Helm values. Platform keys still hydrate via env; manifest `secret:NAME` looks
up the same `FELIX_SECRETS_BACKEND`. See [helm/README.md](helm/README.md).

## MCP stdio is off by default

`spec.mcp_servers` with `transport: stdio` spawns a subprocess from manifest-supplied
argv, at compile time. That is arbitrary code execution as the API process, reachable by
anyone holding `manifests:write`, so it is disabled unless the operator opts in:

```bash
FELIX_MCP_STDIO_ALLOWED_COMMANDS=/usr/local/bin/uvx,/usr/bin/npx
```

Matching is exact on the string the manifest supplies or its resolved absolute path —
allowlisting `/usr/bin/npx` does not allow a bare `npx` resolved through `PATH`. The
child process does **not** inherit the API environment; it receives
`PATH`/`HOME`/`LANG`/`LC_ALL`/`TZ` plus the keys declared in `mcp_servers[].env` (with
`secret:NAME` refs resolved), and loader variables such as `LD_PRELOAD` and `PYTHONPATH`
are rejected. Prefer `transport: http`/`sse` where you can.

`FELIX_AUTH_MODE=none` is refused on any non-loopback bind, in every environment.

## Governance frameworks

```yaml
spec:
  governance:
    frameworks: [soc2, eu_ai_act]  # empty = no extra compile rules
    risk_tier: limited            # limited | high
    transparency_notice: true     # EU AI Act Art. 50 notice in prompt + agent card
    forbid_plaintext_secrets: true
    pin_compile: true             # refuse continue/resume if manifest hash drifts
    retention_days: 30
```

| Framework | What Felix enforces at compile |
|-----------|--------------------------------|
| `soc2` | No anonymous inbound outside development; trace + anomaly on; scopes/schemes; policies **or** approvals **or** limits; plaintext forbid + pin |
| `eu_ai_act` | Transparency notice; content screening or input guardrails; if `risk_tier: high`, approvals required with `allow_unattended: false` |

Runtime also enforces `spec.auth.inbound`, routes inbound MCP through the
compiled agent, emits audit events from the agent loop, and redacts durable
state. User turns are screened when `content_screening.enabled` and/or
`guardrails.providers: [pii]` targets `input` (block or redact). Tenant
isolation is application-level `tenant_id` by default; enable Postgres RLS
with migration `0006_tenant_rls` and `FELIX_DATABASE_RLS=true`
(sets `app.tenant_id` / `app.rls_bypass` GUCs per transaction).

## Inbound and outbound constraints

```yaml
spec:
  auth:
    inbound:
      allow_anonymous: false
      schemes: [jwt, api_key]     # how the caller may authenticate
      required_scopes: [chat:write]
    outbound:
      providers: [anthropic]      # model providers this agent may route to
```

`schemes` is enforced against the authenticated principal — `api_key`, or a JWT verifier
scheme (`access`, `cognito`, `self`); `jwt` is an umbrella for all three. An empty list
allows any scheme. Anonymous access is governed by `allow_anonymous`, not by this list.

`providers` is checked at **compile**, against the resolved route for the primary model
and every entry in `model.fallbacks`, so a violation fails the build rather than
surfacing at the first model call.

## Run budgets

`spec.limits` bounds a single run. Every field is enforced at two points — before each
tool call and at the top of each agent turn — so a run can exceed a budget by at most one
step:

| Field | Bounds |
|-------|--------|
| `max_tool_calls` | Tool invocations in the run. |
| `max_peer_hops` | A2A `peer__*` calls, to stop two peered instances ping-ponging. |
| `max_wall_clock_seconds` | Elapsed time since the run started. |
| `max_input_tokens` / `max_output_tokens` | Accumulated tokens, including cache reads and writes. |
| `max_cost_usd` | Accumulated spend, priced from the model catalog. |

**Undeclared fields fall back to `ABSOLUTE_LIMITS`**, so a manifest that declares no
limits is still bounded (500 tool calls, 3600s, 1M input tokens, 100k output tokens,
$1000). Declared values may only tighten those; the schema rejects anything larger.

A tool invoked with no request context is **denied** rather than run unbudgeted.

## Approval semantics

| Field | Behaviour |
|-------|-----------|
| `ttl_seconds` | How long the run waits for a decision before failing closed. |
| `one_shot` | The grant is marked consumed on use; a replay of the same call needs a new approval. |
| `bind_principal` | Only the principal who was approved may use the grant. Without it, any principal in the tenant can reuse it. |
| `allow_unattended` | EU AI Act high-risk manifests must set this to `false`. |

Approvals are matched on `(tenant, manifest, tool, sha256(args))` and stored in Postgres
— never in model-visible state, so the model cannot forge one. Every failure path
(no request context, store error, waiter timeout) denies.

`command_screening` rules with `decision: require_approval` go through the same flow and
wait up to `command_screening.approval_ttl_seconds` (default 300).

## Management API scopes

When `FELIX_AUTH_MODE` is `jwt` or `api_key`, management routes require scopes
(skipped for `auth_mode=none`). `admin` or `*` bypasses checks; `*:write`
implies the matching `*:read`.

| Scope | Routes |
|-------|--------|
| `manifests:read` / `manifests:write` | `/manifests` |
| `audit:read` | `/audit` |
| `approvals:read` / `approvals:write` | `/approvals` |
| `jobs:read` / `jobs:write` | `/jobs` |
| `plans:read` / `plans:write` | `/plans` |
| `eval:read` / `eval:write` | `/eval` |
| `usage:read` | `/usage` |

```bash
felix mint-jwt --sub ops --tenant default \
  --scopes audit:read,manifests:write,approvals:write,jobs:write
```

## GitOps check

```bash
felix validate-manifest path/to/agent.yaml -e production
# or in CI after editing manifests/
uv run felix bundle-manifests
```
