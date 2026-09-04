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
`guardrails.providers: [pii]` targets `input` (block or redact) — on every path a turn
takes: `/chat` and `/chat/continue`, `/v1`, A2A, a cron job's prompt, an eval item, and a
durable fiber on resume; a tool call made directly over MCP has no turn, so its arguments
are screened instead and a flagged argument refuses the call whatever `on_flag` says. The
screen is a wrapper the compile puts around the agent, so there is no entrypoint to forget;
the HTTP routes screen once more before the agent exists, to answer 422 before a stream
opens or a durable run is enqueued, and tell the agent so. `guardrails.targets`
name where PII is caught: `input` is the user turn, `output` is everything leaving the
model boundary — tool output *and* the agent's reply — and `final_response` is the reply
alone. The reply-path controls (`output`/`final_response` PII, and `judges` with
`final_response: true`) wrap the agent rather than its tools, and apply on the streaming
path as well as `invoke`: reply text is held until the run ends and released screened,
while tool and approval frames stream as they happen. A denial or redaction emits a
`guardrails_reply` or `judge_deny` audit event. Two things the reply controls do not
cover, stated so nobody assumes them: the session log holds the reply as the model
produced it, so `GET /chat/stream/{thread_id}` replays and `GET /chat/threads` exports
carry the unscreened text — the controls govern the reply as it leaves the run, not the
transcript (tracked in `docs/ROADMAP.md`); and `thinking_delta` is reasoning, not the
reply, and passes through unscreened. Tenant
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

## Outbound egress

### Per-integration timeouts

Every outbound integration carries its own request ceiling, and each is capped at
`MAX_INTEGRATION_TIMEOUT_MS` — 3,600,000 ms, derived from
`ABSOLUTE_LIMITS["max_wall_clock_seconds"]`, the longest a run is ever meant to take. The
cap exists because these fields are tenant-supplied through `PUT /manifests`: unbounded,
they are a knob for holding connections, tasks and pool slots open indefinitely. A value of
zero or below is rejected rather than floored, so a manifest that can never work does not
validate.

| Field | Default |
|-------|---------|
| `spec.mcp_servers[].timeout_ms` | 30s |
| `spec.peers[].timeout_ms` | 60s — a peer call runs a whole agent turn on the far side |
| `spec.containers[].timeout_ms` | 30s |
| `spec.sandboxes[].timeout_ms` | 30s |
| `spec.browser_tools[].timeout_ms` | 15s, and it bounds `page.goto` only |
| `spec.http_tools[].timeout_ms` | 15s, and it is a **whole-call deadline** — the redirect chain and every read together, not each read separately |
| `spec.client_tools[].timeout_seconds` | 120s |

Connect is pinned separately at 10s on every outbound client and does **not** scale with
these values: reaching a host takes seconds or never, so a raised request ceiling must not
also let an unreachable host park a socket. `FELIX_MODEL_TIMEOUT_SECONDS` (default 120)
bounds each model-provider request the same way.


Every manifest-supplied or model-supplied URL is checked before it is dialled.

**The guard is enforcing, not advisory.** Outbound HTTP goes through a transport that
resolves the hostname once, validates every returned address, and then connects to one of
the addresses it validated — so the address that was checked is the address that is used.
Without that pin the check and the connection are two independent lookups, and a hostname
that resolves differently the second time (DNS rebinding, TTL 0) or a nameserver that
answers the client while starving the checker gets through. TLS is unaffected: the
certificate is still verified against the hostname the caller asked for.

A lookup that fails or times out refuses the dial. That is safe precisely because this is
the connection: there is no second lookup left to fail. A proxy or unix socket is refused
rather than ignored, because both choose a destination the guard never validates — and
because an explicit transport disables httpx's environment proxies, `HTTP_PROXY` and
`HTTPS_PROXY` do **not** apply to these calls. A deployment whose egress containment is a
proxy allowlist needs to know that.

**Two outbound paths take the URL from the *model* rather than from a manifest** —
`spec.browser_tools` and `spec.http_tools` — which makes them the highest-value rebinding
targets in the harness. They are pinned differently because they dial differently.

`spec.http_tools` goes through `safe_async_client` like every other outbound call, so it
inherits the pin for free, on the first request and on each redirect hop: the fetch tool
drives redirects by hand rather than letting httpx follow them, so every hop re-enters the
guarded transport and is re-checked against the tool's `path_prefix` as well. httpx's own
`follow_redirects` would have re-validated egress but not the prefix, and one `302` from an
allowed page is enough to leave it.

**The browser pins its navigation host too.** Chromium resolves independently, so the
guard's lookup and Chromium's would otherwise be two lookups. The browser is launched with
`--host-resolver-rules=MAP <host> <validated address>`, so the name it navigates to can only
reach the address the guard approved. A hostname is matched against a strict pattern before
it reaches that flag: the flag takes a comma-separated list, so a host containing a comma
could otherwise append rules of its own.

**A fetch tool must declare a boundary.** `spec.http_tools` is the one ref where the model
chooses the destination, so a manifest that says nothing must not get the whole public
internet: an entry needs either a `path_prefix` confining it, or an explicit
`allow_any_host: true`, and validation fails otherwise. `allow_any_host` is logged at bind
time — a review question, not a silent default. What to check when reviewing one:

- **Is `path_prefix` set, and to a whole origin you meant?** It is validated as an absolute
  http(s) URL and normalised to end in `/`. That slash is load-bearing: without it
  `https://docs.felix.run` also matches `https://docs.felix.run.evil.com/`. Matching is on
  parsed scheme, host and port, not on text, and it is re-applied to every redirect hop.
- **Is `content_screening` on?** A fetched page is attacker-controlled input, so the tool's
  transport is untrusted and screening applies — but only if enabled. Compiling a manifest
  that binds one without it logs `untrusted tool(s) … unscreened`.
- **Is `max_bytes` sized for your context window?** The far end chooses the length; the body
  is streamed and truncated, counted **after** decompression, so a gzip bomb is capped at
  what the model would actually see.

A fetch tool is **not** replay-safe. A resumed run will not re-issue it, because the model
names the endpoint and a GET that mutates is ordinary on the open web.

**What is still advisory:** cross-host subresources and redirects *in the browser tool*.
Those keep resolving normally and are checked per request but not pinned, because denying
them outright breaks
any page that loads assets from a CDN. A page that loads a script from a host which answers
the check and the load differently can still reach an address the guard would have refused.
Closing that means launching with `MAP * ~NOTFOUND` as well — verified to work, and to block
every cross-host subresource — or running the browser behind an egress allowlist.

**Where the check runs matters.** The syntactic half — scheme, `http` outside development,
internal names and suffixes, and IP literals including the decimal form (`http://2130706433/`
is 127.0.0.1) — runs when a manifest is parsed. The half that **resolves the hostname** runs
at dial time, off the event loop. Resolving at parse time was both a liveness problem and a
security gap: it put a blocking `getaddrinfo` inside a pydantic validator on the API event
loop, once per ref on every manifest read and write, and a name validated at write time can
resolve somewhere else by the time it is dialled. `felix validate-manifest` performs the
resolving check too, so an author still learns about a blocked host without a request
waiting on it (`--no-resolve-egress` for an air-gapped runner).

That resolving check rejects the request if *any* returned address is loopback,
link-local (cloud metadata), private, carrier-grade NAT, reserved, multicast, or
unspecified — including IPv4-mapped IPv6 forms and decimal-integer hosts. Internal names
and suffixes (`.svc`, `.cluster.local`, `.internal`, `metadata.google.internal`,
`kubernetes.default`, …) are refused outright.

A DNS failure does **not** hard-fail the call: the connection will fail on its own, and
refusing every lookup error makes the harness brittle offline.

Browser tools additionally register a Playwright request interceptor, so redirect hops
and subresources are re-checked — `page.goto()` follows both, and the URL is
model-supplied. Every other outbound client sets `follow_redirects=False`.

## Sandbox confinement

`spec.sandboxes[].binding` names a container image and reaches `docker run`, so images
are allowlisted: only `python:3.14-slim` unless `FELIX_SANDBOX_ALLOWED_IMAGES` names
more. Containers run non-root with `cap_drop: ALL`, `no-new-privileges`, a read-only root
filesystem plus a small `noexec` tmpfs, a PID limit, a CPU quota, a memory cap, and
networking disabled.

The Docker call runs on a worker thread, so the declared `timeout_ms` is enforceable and
a runaway container cannot stall the API event loop.

## When a control cannot run

Screening and PII degrade **loudly**, and "unavailable" is not treated as "clean":

| Control | Unavailable behaviour |
|---------|----------------------|
| `content_screening.model` (LLM screener) | Honours `on_flag`: `block` denies with 503 / `[screening unavailable]`; otherwise the turn or tool output is quarantined. Emits `felix_control_unavailable{control="content_screening"}`. |
| `guardrails.providers: [pii]` | Falls back to three regexes (email, US SSN, card-like digits) with a `WARNING` and `felix_control_degraded{control="pii"}`. A *transient* engine failure is retried rather than latched for the process lifetime. |

The lean image ships neither Presidio nor a spaCy model, so `providers: [pii]` there is
the regex fallback — check the startup warning before relying on it.

`guardrails.providers` is a closed set (`pii`), so a typo is a manifest validation error
rather than a silently absent wrapper.

Command screening inspects every execution-bearing argument, not just `command`/`cmd` —
including `code`, `script`, `stdin`, and `argv`, and *every* string argument for
`sandbox` / `container` transports, where the payload is the program.

Screening can only judge the arguments it is handed, so a turn the model did not finish
writing is refused before it reaches any wrapper. A response that stops on `max_tokens`
can still carry a syntactically complete tool call whose arguments were cut off
mid-write — `{"path": "/srv/app/tmp"}` truncated to `{"path": "/srv"}` is valid JSON
naming a different target, and it screens clean because the shortened value is all there
is to screen. Every tool call on such a message is failed with `[error/truncated]` and
the run is recorded with status `truncated`; none of them execute, including calls that
look complete, because the batch cannot be split into trustworthy and untrustworthy
halves after the fact.

## Resuming an interrupted run

A run killed mid-tool leaves an assistant turn holding a tool call with no result. Whether
the effect happened is not knowable afterwards, so on resume each unanswered call is closed
out with an `[error/interrupted]` result rather than being re-issued. What the model is told
depends on how the tool was declared:

| `Tool.replay_safe` | Told to the model |
|---|---|
| `True` | The call did not finish and is safe to make again. |
| `False` (default) | The call did not finish; it may already have taken effect, so do not assume either way and do not repeat it unchecked. |

The default is `False`, so a tool that has never considered the question is never presented
as repeatable. Read-only built-ins (`list_dir`, `read_file`, `search_files`, `calculator`,
`list_skills`) declare `replay_safe=True`; skill activation, writes and every outbound
integration do not.

Closing the call out is also what makes the thread resumable at all: the provider rejects a
transcript containing a tool call with no answer, so before this an interrupted run could
not be continued.

## Run budgets

`spec.limits` bounds a single run. Every field is enforced at two points — before each
tool call and at the top of each agent turn — so a run can exceed a budget by at most one
step.

Size that step honestly: a step includes the outbound call it makes, and no deadline is
propagated into the executor, so a budget is never enforced *during* a call. A run's real
ceiling is `max_wall_clock_seconds` plus the longest single call it can still start —
bounded, since every per-integration `timeout_ms` is capped (below), but not equal to the
budget alone.

| Field | Bounds |
|-------|--------|
| `max_tool_calls` | Tool invocations in the run. |
| `max_peer_hops` | A2A `peer__*` calls, to stop two peered instances ping-ponging. |
| `max_wall_clock_seconds` | Elapsed time since the run started. |
| `max_input_tokens` / `max_output_tokens` | Accumulated tokens, including cache reads and writes. |
| `max_cost_usd` | Accumulated spend, priced from the model catalog. |

Side requests are metered but deliberately uncached. Compaction, memory capture, inbound
screening and branch summarisation each issue a model call in the middle of a turn, and
each carries a different prefix from the conversation around it — so they opt out of the
prompt cache rather than displacing what the turn had cached. Their tokens still count
against the run's budget.

Budgets only bound what they can see. A streaming turn used to run the inference twice —
once to stream for display, once to get the authoritative answer — while metering only
the second, so `max_cost_usd`, `max_input_tokens` and `max_output_tokens` counted roughly
half of what a streaming run actually spent and admitted about twice the intended budget.
A streaming turn is now a single metered call.

Because `max_cost_usd` fails closed, the price table behind it is a control input rather
than reporting. Rates live on the model catalog (`felix/model_catalog.py`) alongside
context window and request quirks, so a model is priced and described in one place.
Bundled prices are flat per model. A provider that bills long context at
a higher rate across the *whole* request needs that expressed as pricing tiers on a
manifest price override — `tiers: [{input_tokens_above: N, input: …, output: …}]`, where
the highest matching threshold replaces the base rates entirely. No bundled entry sets
tiers: the thresholds and rates move, and a stale number here both mis-charges the tenant
and lets the budget cap admit more spend than it should.

**Undeclared fields fall back to `ABSOLUTE_LIMITS`**, so a manifest that declares no
limits is still bounded (500 tool calls, 3600s, 1M input tokens, 100k output tokens,
$1000). Declared values may only tighten those; the schema rejects anything larger.

A tool invoked with no request context is **denied** rather than run unbudgeted.

## Policy semantics

`spec.policies` gates named tools on the caller's scopes. Every rule matching a tool must
pass; the first missing scope denies the call, and the denial names it.

| Field | Behaviour |
|-------|-----------|
| `tools` | The tools this rule gates, matched by glob (`fnmatch`, case-sensitive): `calculator`, `github__*`, `*__search`, `*`. Applies equally to `spec.approvals`, judge `target_tools`, `content_screening.tools` and `command_screening.target_tools`. A pattern with no `*` or `?` is a literal name, so a tool whose name contains `[...]` still matches itself. A pattern matching no bound tool is logged and counted (`felix_rule_targets_nothing`) rather than refused, since the bound set varies — an MCP server whose discovery failed binds nothing. A rule naming no tools at all gates nothing and is rejected: it would otherwise satisfy the `soc2` profile's "policies **or** approvals **or** limits" requirement while enforcing nothing. |
| `required_scopes` | Scopes the caller must hold. **Required**: a rule that lists tools but no scopes permits every caller while appearing to govern them, so it is rejected rather than accepted as a no-op. |

Two things to know before relying on it:

- **A run with no scopes is denied, not permitted.** That includes any request under
  `auth_mode=none`, and it includes durable fibers, scheduled jobs and `felix eval`, whose
  contexts carry an empty scope set. `spec.policies` and `execution.mode: durable` are
  therefore not usable together today — every policied tool denies.
- Policy scopes are matched literally. The `admin` / `*` bypass and the `x:write` implies
  `x:read` rule that `require_mgmt_scopes` applies to the management API deliberately do
  **not** apply here.
- `manifests/governed.yaml` policies `calculator` on `tools:calc`, so it will deny its own
  calculator under `make dev` (which sets `FELIX_AUTH_MODE=none`). Mint a token with the
  scope — see the `felix mint-jwt` line above — rather than removing the policy.
- **Durable runs are the exception.** A fiber records the caller's scopes and resumes with
  them, so `spec.policies` and `execution.mode: durable` work together. The resumed run's
  principal is `fiber`, not the person — `on_behalf_of` carries who it is for, which is what
  keeps a `bind_principal` approval valid across a resume without an audit row claiming a human
  took an action a worker took.

  Carrying authority in durable state is bounded three ways, and the bounds are the design:

  | | |
  |---|---|
  | Never wider | Exactly the caller's scope set. A caller with none confers none. |
  | Never longer than the run | `expires_at`, checked before every step, on both the fiber scheduler and the Temporal activity. `hibernate_after_seconds` (300s) by default, `execution.resume_token_ttl_seconds` if set, capped at `ABSOLUTE_LIMITS["resume_token_ttl_seconds"]` (24h). |
  | Never longer than the token | Clamped to the token's `exp` when it has one. Felix has no revocation, so `exp` is the only bound on a compromised credential and a durable run must not outlive it. |

  Two things this does **not** bound. `expires_at` gates step *entry*, so a step that starts
  just inside the horizon runs to completion — cap it with `limits.max_wall_clock_seconds`.
  And the fiber *row* is not swept by `jobs/retention.py`, so the record of who started a run
  outlives the run's usability; only its usability expires.

  A fiber enqueued with no request context, from a different tenant than the run, or before
  this existed, records nothing and resumes with no scopes. When it *does* carry authority,
  `pin_compile` is forced: the manifest is re-resolved at resume, and running a rewritten
  manifest with the original caller's scopes is exactly what a pin is for.

## Content screening targets

`content_screening.tools` is **additive**. Screening covers every untrusted tool — anything
whose transport is not `local`, plus anything whose `source` starts with `mcp`, `peer`, `a2a`,
`queue`, `browser`, `client`, `sandbox` or `container` — and, in addition, whatever `tools`
names. Naming a trusted local tool extends screening to it;
it does not narrow screening away from anything.

There is deliberately no way to turn screening off for an untrusted tool while leaving it on
elsewhere. The two used to be alternatives, so a non-empty `tools` list *replaced* the
untrusted-tool default: naming one local tool silently unscreened every MCP, peer, browser,
sandbox, container and queue tool while the manifest still read as a working control. Turning
screening off for untrusted output is the thing screening exists to prevent, so the narrowing
was removed rather than renamed. On cost: neither `content_screening.model` nor `on_flag` is a per-tool lever, so this removes
the only one there was. It is free in the default configuration — both bundled manifests that
enable screening leave `model` empty, and the marker path is a substring scan — and it costs a
model call per untrusted tool per turn where `model` *is* set. If that bites, the shape to add
is a knob orthogonal to trust (which tools get the *expensive* screener, with marker screening
unconditional), not a way to exempt an untrusted tool from screening altogether.

Two things to re-measure if you set `model` and previously narrowed `tools`:

- **Availability.** `on_flag: block` plus a screener outage now denies output from every
  untrusted tool rather than the named subset. Right direction, wider radius — watch
  `felix_content_screening{action=unavailable}`.
- **False positives.** The marker scan is a substring match, `"system prompt"` included, so a
  docs server, a code-search tool or an issue tracker quoting a jailbreak can now be
  quarantined where it was exempt. Watch `{action=quarantine}`.

### Screening is opt-in, and says so when it is off

`content_screening.enabled` defaults to `false`, and of the governance frameworks only
`eu_ai_act` requires it — `soc2` does not, and its data-governance check is satisfiable by
guardrails instead. A manifest that binds an MCP server, an A2A peer, a browser,
sandbox, container, queue or client tool without enabling it is valid, and its untrusted output
reaches the model with the whole governed toolset behind it. That case is now named at compile
with a WARNING and `felix_untrusted_tools_unscreened`.

A warning rather than a changed default: turning screening on for every existing deployment
binding an MCP server changes cost and behaviour, which is not a thing to do silently. Enabling it without a `model` is the cheap option — an anchored regex scan, no model call — and
is what `manifests/cowork.yaml` does for its client tools.

The warning reports what **compiled**, not what was declared: every outbound binder catches its
own failure, so an unreachable MCP server binds zero tools and produces no warning. In staging,
CI and `felix validate-manifest` that means a manifest declaring five MCP servers can be silent.

## Approval semantics

**Precedence.** Approvals is the only control that selects *one* rule — policies and judges
apply every match, so for them more matches only tighten. When several approval rules match a
tool, a rule naming it **literally** wins over one matching by pattern, and among equals the
last declared wins. That makes globbing non-weakening: a pattern can only gate a tool nothing
gated before, and never displaces a stricter literal rule.


| Field | Behaviour |
|-------|-----------|
| `ttl_seconds` | How long the run waits for a decision before failing closed. |
| `one_shot` | The grant is marked consumed on use; a replay of the same call needs a new approval. |
| `bind_principal` | Only the principal who was approved may use the grant. Without it, any principal in the tenant can reuse it. |
| `allow_unattended` | EU AI Act high-risk manifests must set this to `false`. |

`spec.policies` and `spec.approvals` are capped at 64 rules each: matching is O(rules × tools)
and a manifest is compiled per request.

Approvals are matched on `(tenant, manifest, tool, sha256(args))` and stored in Postgres
— never in model-visible state, so the model cannot forge one. Every failure path
(no request context, store error, waiter timeout) denies.

`command_screening` rules with `decision: require_approval` go through the same flow and
wait up to `command_screening.approval_ttl_seconds` (default 300).

## Request limits

Rate limiting runs **outside** authentication, so a failed credential is counted — it
previously ran inside, and a 401 returned before the limiter was reached.

```bash
FELIX_RATE_LIMIT=120
FELIX_RATE_LIMIT_WINDOW_SECONDS=60
FELIX_TRUSTED_CLIENT_IP_HEADER=      # e.g. cf-connecting-ip, behind a proxy you operate
```

Keyed per client address. Redis-backed when `FELIX_REDIS_URL` is reachable; if it is not,
limiting **degrades to per-process** with a logged error rather than failing requests or
skipping the control. Leave `FELIX_TRUSTED_CLIENT_IP_HEADER` empty unless a proxy you
operate overwrites that header — otherwise a client can present as unlimited distinct
clients.

`/metrics` requires authentication: its label values include tenant-supplied manifest ids
and remote MCP tool names.

`/health`, `/live` and `/ready` are public and unthrottled, because kubelet presents no
credential and treats a 429 as a failed probe (`PROBE_PATHS` in `felix/security/rate_limit.py`
feeds both allowlists). `/ready` therefore tells an anonymous caller which dependency is
down, and nothing more: the exception text goes to the log, its report is cached for two
seconds, and concurrent callers share one probe, so the route can be hammered and the
database cannot. If even up/down per dependency is too much for your perimeter, restrict
those paths at the ingress — the chart's default rule forwards `/`.

## JWT verification

`FELIX_JWT_VERIFIERS` is `scheme:issuer[;aud=…][;tenant=claim|issuer|fixed:<tenant>]`,
comma-separated. What is enforced:

- **`exp` is required.** joserfc validates expiry only when the claim is present, so a
  token minted without one was previously accepted forever.
- **`aud` is required for shared issuers** (`access`, `cognito`). Those issuers sign for
  every application under them, so without an audience check a token minted for a
  different app at the same issuer is accepted. A verifier for those schemes with no
  `;aud=` is refused.
- **Remote keys come from the issuer.** `access` and `cognito` key sets are fetched and
  cached (15 min TTL), refreshed by the API on a timer. `FELIX_JWKS_PUBLIC` is used for
  the `self` scheme only — it must never verify a token that claims a remote issuer.
- **Algorithms are asymmetric-only**; there is no HS256 or `none` path.

## Tenant resolution

`tenant_id` is the isolation boundary and, in the default `claim` mode, it arrives in a
token claim. Constrain it:

```bash
FELIX_ALLOWED_TENANTS=acme,globex     # empty = accept any claimed tenant
```

Prefer `;tenant=fixed:<tenant>` for a single-tenant deployment. On Cognito, `custom:*`
attributes are frequently user-writable, so a claim alone is not an authorization
decision. A token with **no** tenant claim in `claim` mode is now rejected — it
previously fell back to the issuer host's first DNS label, silently putting every such
user in the same tenant.

### Periodic controls are per-tenant

The worker's scans sweep every tenant, not just `default`. That is worth checking after
an upgrade, because the failure mode is silent: a detection control that runs for one
tenant looks identical, in logs and metrics, to one that finds nothing.

| Control | Sweep | Enumerated from |
|---|---|---|
| Scheduled jobs | `run_due_jobs_all_tenants` | tenants with a job |
| Anomaly scan | `run_anomaly_scan_all_tenants` | tenants with audit events |
| Continuous eval | `run_continuous_eval_all_tenants` | tenants with an active manifest |

Each sweep isolates a tenant's failure so one tenant's bad data cannot stop detection
for the rest, and each takes an RLS bypass for the enumeration only — the per-tenant
work that follows runs scoped. `felix-scheduler` must be running alongside
`felix-worker` or none of them fire at all.

## Management API scopes

When `FELIX_AUTH_MODE` is `jwt` or `api_key`, management routes require scopes
(skipped for `auth_mode=none`). `admin` or `*` bypasses checks; `*:write`
implies the matching `*:read`.

| Scope | Routes |
|-------|--------|
| `manifests:read` / `manifests:write` | `/manifests` |
| `audit:read` | `/audit` |
| `artifacts:read` | `/artifacts` — read back a tool output too large to keep in the transcript. Its own scope rather than part of `audit:read`, because a spilled result is raw tool output and often the most sensitive data a run touches |
| `approvals:read` / `approvals:write` | `/approvals` |
| `jobs:read` / `jobs:write` | `/jobs` |
| `plans:read` / `plans:write` | `/plans` |
| `eval:read` / `eval:write` | `/eval` |
| `usage:read` | `/usage` |
| `memory:read` / `memory:write` | `/memory` — inspect, search, correct and prune what an agent has remembered |
| `documents:read` / `documents:write` | `/documents` — ingest, search, inspect and remove the corpus an agent retrieves from |

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
