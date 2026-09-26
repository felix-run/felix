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
| Redaction | Known secrets scrubbed from tool output, session events, audit payloads, fiber state, and skill bodies read over `/skills` |

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
    pin_compile: true             # refuse continue/resume if the manifest or a sub-agent drifts
    retention_days: 30
```

| Framework | What Felix enforces at compile |
|-----------|--------------------------------|
| `soc2` | No anonymous inbound outside development; trace + anomaly on; scopes/schemes; policies **or** approvals **or** limits; plaintext forbid + pin |
| `eu_ai_act` | Transparency notice; content screening or input guardrails; if `risk_tier: high`, approvals required with `allow_unattended: false` |

`retention_days` is the manifest's data-retention policy for its own audit trail: the nightly
sweep deletes this manifest's `audit_events` older than that many days. It can only shorten the
operator's `FELIX_AUDIT_RETENTION_DAYS` (30 by default), never extend it — the deployment's TTL
is the ceiling, and a manifest keeps less than the deployment, not more. The rule is read off
the manifest that *governs* the rows — resolved exactly as a request resolves it, so a bundled
manifest's value (`governed.yaml` says 30) applies to every tenant that serves the bundled copy,
and a tenant's own stored version of that name replaces it for that tenant. Usage (the billing
record, 365 days), fibers and A2A tasks (7 days, terminal rows only) and session threads (off:
the event log is the chat record) have deployment-wide TTLs, `FELIX_*_RETENTION_DAYS`, `0`
keeping forever. Two sweeps delete object-store bytes as well as rows, each through a ledger
because the store cannot be listed: uploads (`FELIX_ATTACHMENT_RETENTION_DAYS`, off by default,
caller data) and spilled tool outputs (`FELIX_ARTIFACT_RETENTION_DAYS`, 30 by default, the
harness's working copy). A manifest's `retention_days` does **not** shorten its spill: a
manifest set to 7 days still keeps raw tool output for the deployment's artifact TTL, so set that
no longer than the shortest manifest that needs it. Session retention drops whole idle threads and their metadata; it does not
reach the facts memory capture extracted from them, which are governed by memory's own rules.

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
reply, and passes through unscreened. A judge scores by, in order of preference: `spec.decider`
when the rule sets `decider: true` (the probability the text meets `criteria`, as written), the
chat model in `model`, and the heuristic — each failure falls through to the next, so a decider
outage degrades a judge rather than opening it. A decider-scored judge sends the judged text to the
decider's provider, and only judges text up to 8,000 characters — anything longer is scored by the
model judge or the heuristic, which read all of it, rather than by a prefix. Treat a decider judge
as a quality control, not a security boundary: the text it judges can argue for its own verdict
(Jev is not adversarially robust), so untrusted tool output belongs behind `content_screening`,
not behind a judge alone. Tenant
isolation is application-level `tenant_id` by default; enable Postgres RLS
with migration `0006_tenant_rls` and `FELIX_DATABASE_RLS=true`
(sets `app.tenant_id` / `app.rls_bypass` GUCs per transaction). Every table except
`memory_vector_config` (one deployment-wide row, no tenant data, RLS never enabled) carries
a `tenant_id` and the `felix_tenant_isolation` policy — `ENABLE`d, `FORCE`d, and comparing
`tenant_id` to the session GUC with a bypass arm, and the only policy on the table;
`tests/unit/test_rls_coverage.py` renders the migrations and fails when a new table does not.

The tenant id itself is validated at every *inbound* door — `assert_valid_tenant_id`, which
`auth/middleware.py`, `auth/jwt.py` and every `Principal` construction run, and which
`Settings.validate_runtime` also applies to the tenant ids an operator pins in
`FELIX_JWT_VERIFIERS`, `FELIX_ALLOWED_TENANTS` and `FELIX_AUTH_API_KEYS` — against the same
rule `storage/fs.py` applies to an
object-key segment: letters, digits, `.`, `_`, `-`, at most 128 characters, never `.` or
`..` alone, and never `:` or `#` (those would break the `{tenant}:{suffix}` thread-id
prefix). One definition rather than two, because a tenant id is a thread-id prefix *and* a
path segment in `artifacts/`, `workspace/`, `skills/` and `manifests/` keys *and* a field in
every log record. Under a claim-mode JWT verifier it arrives from a token claim, which on
Cognito is frequently user-writable, so it is checked before the `FELIX_ALLOWED_TENANTS`
allowlist rather than after.

The worker is the exception worth knowing: its sweeps build a `felix.context.AuthContext`
directly from a tenant id read out of the database, with no `Principal` and so no door. A
row written before this rule existed still flows through those paths, which is why the
sweeps escape the value where they log it rather than assuming it is clean.

Behind all of that, a log *message* is one line by construction. A newline reaching one
would end the record and start another that an attacker wrote in full — most damagingly a
forged *refusal*, since the log is what an incident is reconstructed from. Under
`FELIX_LOG_FORMAT=json` this never applied (`json.dumps` escapes the separator because the
message is a value, not a line); the text format now escapes the message before rendering
it, so the guarantee no longer depends on each call site remembering `loggable()`. Call
sites still use it, because the formatter cannot bound length — it sees a finished record,
and truncating there would cut the record rather than the value.

**Tracebacks are covered too, by indentation rather than escaping.** `logging.Formatter`
appends `exc_text` after the message, and an exception's own `str` is not indented the way
its frames are — it renders at column 0, so a newline inside an exception message used to
produce a fully record-shaped line on any `exc_info=True` / `logger.exception(...)` call
site whose exception text is built from a caller-influenced value. Escaping the block would
close that by flattening the traceback onto one line, which is unreadable. Every line of a
traceback is now pushed two columns right instead: the property a forged record needs is the
column, not the content, so the text is still all there and still readable, and only the
record itself begins at column 0. Frame lines therefore sit two columns further right than
a stock Python traceback.

Indentation alone would only be a claim about columns, and a terminal need not honour it —
`\x1b[1G` is cursor-horizontal-absolute, so an ESC surviving into a traceback redraws that
line at column 0 however far right it was written, and the bidi overrides reorder it in
place. Each line is therefore escaped as well as indented, the same treatment the message
gets. The one visible cost is that a tab inside a frame's source line renders as `\t`.

Two consequences to know rather than be surprised by. The indenter splits on every Unicode
line separator, not just `\n`, so an exotic one inside an exception message (U+2028, U+0085,
a bare `\r`) becomes an ordinary indented line break — the text survives, but *which*
separator it was does not. A log message keeps that evidence, because there the separator is
escaped and stays visible as ` `; a traceback does not. And a record that arrives
carrying `exc_text` without `exc_info` — which `logging.handlers.SocketHandler` constructs
deliberately, and a `QueueHandler.prepare` override may — has its cached block indented
rather than regenerated, so the traceback is neither dropped nor trusted unindented.

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

**Sub-agents inherit the caller's admission.** `spec.auth.inbound` is checked on the manifest
a request names — the router — and not again on each sub-agent it compiles. A child's own
`allow_anonymous`, `schemes` and `required_scopes` therefore apply when the child is called by
name, and not when a router hands it a request: the bundled `router` (anonymous) reaches `deep`
(which is not) this way on purpose. Put the admission you need on the router. What a child keeps
is everything else it declares — its tools, policies, approvals, screening and limits are
compiled into it and apply whichever way it was reached.

**`pin_compile` covers sub-agents**, recursively: the pin records a digest of every child a
router compiles, and an edited, added or removed one is drift — 409 on a turn, a failed fiber on
resume. The compile builds from the children the pin check resolved for that request, so the tree
the pin verified is the tree that runs; a child published after the check waits for the next turn,
which refuses it. One limit, stated so nobody assumes more: a thread pinned before sub-agents were
covered adopts its children as they are on its next turn, so an edit made before that upgrade is
accepted.

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

The other two retrieval refs are milder, and it is worth being precise about why rather than
grouping them. `spec.search_tools` lets the model choose the query but the operator chooses
the endpoint, so the only address reached is the one in `FELIX_SEARCH_BACKEND`.
`spec.document_tools` reaches no network at all — only rows already ingested into this
deployment's own store, in the calling tenant, with the tenant taken from the compile rather
than from the call. Neither takes a confinement field, because neither has a destination to
confine.

**None of that makes what they return trustworthy.** A search snippet is written by whoever
ranked for the query and a retrieved chunk is text somebody ingested, so both transports
(`search`, `documents`) sit outside the trusted allowlist and content screening covers them
exactly as it covers a fetched page. A manifest binding any of the four without
`content_screening.enabled` is warned at compile time and counted in
`felix_untrusted_tools_unscreened`.

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

## Shell tools

`spec.shell_tools` execs an argv on the host the API runs on, in the `FELIX_WORKSPACE_ROOT`
checkout. There is no shell interpreter: `&&`, `|` and `;` are literal arguments. The
manifest's `commands` are argv prefixes (`git status` covers `git status --short`, not
`git push` and not `git -c … status`), and every one must be covered by a prefix in
`FELIX_SHELL_ALLOWED_COMMANDS` — checked at manifest write, at compile, and per call.
Empty, the default, refuses every shell tool. The child inherits only `PATH`, `HOME`, `LANG`,
`LC_ALL`, `TZ`; `cwd` resolves under the workspace root; the run is killed at `timeout_ms`;
output is capped and marked truncated.

The run is a process group of its own: at `timeout_ms`, past `MAX_TOTAL_OUTPUT_BYTES`, or
when the calling request is cancelled, the whole group is killed — the pytest a test script
spawned dies with the script rather than holding the pipes open. Output is read in bounded
chunks and only a tail is kept, so a command that prints without end costs the API a fixed
amount of memory and then its life. A refused argv or `cwd` is a `permission_denied` tool
error counted as `felix_shell_denied`, so a model probing the allowlist is visible on the same
dashboards a policy deny is. A manifest that binds a shell tool may not allow anonymous callers
outside development — the same rule `cowork.yaml` records for its client shell — because the
approvals that would gate the tool are anonymous too when the caller is.

What the allowlist cannot bound, and a deployment that binds a shell tool must hold true on its
own:

- **A listed command runs repository code as the API's user.** `./scripts/test.sh` imports
  whatever the agent wrote; `uv run ruff` reads the workspace's `pyproject.toml`. The uid is the
  boundary — not `HOME`, which only decides which config files a tool reads silently
  (`~/.gitconfig` credential helpers, `~/.netrc`, `~/.config/gh`). The host holds no cloud
  credentials, no Docker socket, and no credentials in that user's home.
- **A relative `argv[0]` resolves against the `cwd` the model chose.** The prefix pins a
  string, not a file. `write_file` and `edit_file` can replace `scripts/test.sh` before `run` execs it.
- **Choose commands with no argument-driven code execution and no network.** `git status`
  is safe by git's grammar; `pytest -p`, `make -f`, `node -r`, `find -exec`, `ruff --config`
  take a module or file to run from their arguments. The prefix grammar cannot see past the
  prefix.
- **A shell tool voids two guarantees the rest of the harness keeps.** The egress guard
  (`security/ssrf.py`) covers every outbound client Felix builds, not a command the model runs
  — `uv` resolves an index the workspace names. And secret confinement covers what Felix
  masks, not a file in the checkout — the workspace holds no `.env` and no deployment secret,
  because an allowlisted `git diff` or a failing test can print one into the transcript.
- **One tenant per process.** `FELIX_WORKSPACE_ROOT` is process-global; every tenant's shell
  tool reads and writes the same tree. A multi-tenant deployment does not bind one.

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
| `content_screening.decider` (decision model) | The same as the model screener, and independently of it: with both set, either one unable to run leaves the text unscreened rather than cleared, and a decider whose route no longer resolves is unavailable too. |
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

A durable step that raises *outside* the invoke's own handler — its save cannot land, the
lease write fails, a store is down — is not retried forever. The fiber sleeps for a delay
that doubles per consecutive failure (1m, 2m, 4m, 8m at the default; capped at an hour from
the eighth) and after `FELIX_FIBER_MAX_ATTEMPTS` (5) it is `dead` — fifteen minutes after
the first failure at the default: never claimed again, the last error (first line, no
statement text) on `GET /chat/runs/{resume_token}`, terminal to every consumer. When the
save is what fails, the count is written on its own columns so the bound still holds; only
a store that is entirely down leaves the fiber released for the next tick, as before. A
step that completes resets the count. An `invoke` that fails is `failed` in one tick, as
before.

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

A caller on `/v1/chat/completions` may pass `max_tokens`; it only ever *lowers* the manifest's
per-turn ceiling (`spec.model.max_tokens`, or `limits.max_output_tokens` when that is tighter),
never raises it — the output budget is checked at the top of a turn, so a caller-sized turn
would otherwise run a full turn past the declared bound before it tripped.

`spec.model.cache: true` sets three breakpoints on the Anthropic wire: the system block, the
last tool definition, and the newest message. The third is the one that matters for a long run —
without it the transcript is re-billed at full input price every turn, and the transcript is where
an agentic run's tokens are. Cache reads are counted in full against `max_input_tokens`, because
they are tokens the provider processed; their lower price is `max_cost_usd`'s business.

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

**Undeclared fields fall back to `DEFAULT_LIMITS`**, so a manifest that declares no
limits is still bounded (500 tool calls, 3600s, 1M input tokens, 100k output tokens,
$1000). **`ABSOLUTE_LIMITS` is the separate, higher ceiling a manifest may declare up
to**, and the schema rejects anything above it.

The two were one constant, which made the default unraisable: `max_input_tokens` was
1,000,000 as both the fallback and the maximum, and since the counter sums the tokens
each turn actually processed — a react run re-sends its prefix every turn — an agent
carrying a 38 KiB prompt reaches it in about 26 turns. There was no way to declare a
budget for a longer run without raising the floor under every manifest that declares
nothing. A manifest may now declare up to 20M input tokens; unset still means 1M.

Cache reads count in full, and that is deliberate: they are tokens the provider
processed. On the Anthropic wire `input` excludes them and `cache_read` reports them
separately; on the OpenAI wire `prompt_tokens` already includes them and `cache_read` is
zero. Summing all three is what makes the same budget mean the same thing on both. The
*price* difference is `max_cost_usd`'s job, which prices a cache read at its own rate.

A tool invoked with no request context is **denied** rather than run unbudgeted.

## Policy semantics

`spec.policies` gates named tools on the caller's scopes. Every rule matching a tool must
pass; the first missing scope denies the call, and the denial names it.

| Field | Behaviour |
|-------|-----------|
| `tools` | The tools this rule gates, matched by glob (`fnmatch`, case-sensitive): `calculator`, `github__*`, `*__search`, `*`. Applies equally to `spec.approvals`, judge `target_tools`, `content_screening.tools` and `command_screening.target_tools`. A pattern with no `*` or `?` is a literal name, so a tool whose name contains `[...]` still matches itself. A pattern matching no bound tool is logged and counted (`felix_rule_targets_nothing`) rather than refused, since the bound set varies — an MCP server whose discovery failed binds nothing. A rule naming no tools at all gates nothing and is rejected: it would otherwise satisfy the `soc2` profile's "policies **or** approvals **or** limits" requirement while enforcing nothing. |
| `required_scopes` | Scopes the caller must hold. **Required**: a rule that lists tools but no scopes permits every caller while appearing to govern them, so it is rejected rather than accepted as a no-op. |

Four things to know before relying on it:

- **A run with no scopes is denied, not permitted.** "No scopes" must never read as "all
  scopes", so a context carrying an empty set denies every policied tool. Three do: any
  request under `auth_mode=none` (`auth/middleware.py:120` returns `ANONYMOUS`), scheduled
  jobs (`jobs/scheduler.py:71`, principal `cron`) and `felix eval` (`eval/runner.py:164`,
  principal `eval`). The last two construct an `AuthContext` with no `scopes` argument, so
  they take the field's `frozenset()` default.
- Policy scopes are matched literally. The `admin` / `*` bypass and the `x:write` implies
  `x:read` rule that `require_mgmt_scopes` applies to the management API deliberately do
  **not** apply here.
- `manifests/governed.yaml` policies `calculator` on `tools:calc`, so it will deny its own
  calculator under `make dev` (which sets `FELIX_AUTH_MODE=none`). Mint a token with the
  scope — see the `felix mint-jwt` line above — rather than removing the policy.
- **A durable run is not in that list**, though this document said it was until 2026-09-14.
  `start_durable_chat` records the caller's scopes on the fiber row and the resume rebuilds an
  `AuthContext` from them (`durability/runs.py:96`, `durability/fibers.py:288`), so
  `spec.policies` and `execution.mode: durable` work together. The resumed run's principal is
  `fiber`, not the person — `on_behalf_of` carries who it is for, which is what keeps a
  `bind_principal` approval valid across a resume without an audit row claiming a human took an
  action a worker took.

  The exception is a fiber with **no recorded caller** — one enqueued outside a request context,
  or written before fibers recorded authority at all. Those keep the old behaviour: principal
  `fiber`, no scopes, every policied tool denies. That is the fail-closed direction, and it is
  the case the stale sentence described before it outlived its scope.

  Carrying authority in durable state is bounded three ways, and the bounds are the design:

  | | |
  |---|---|
  | Never wider | Exactly the caller's scope set. A caller with none confers none. |
  | Never longer than the run | `expires_at`, checked before every step, on both the fiber scheduler and the Temporal activity. `hibernate_after_seconds` (300s) by default, `execution.resume_token_ttl_seconds` if set, capped at `ABSOLUTE_LIMITS["resume_token_ttl_seconds"]` (24h). |
  | Never longer than the token | Clamped to the token's `exp` when it has one. Felix has no revocation, so `exp` is the only bound on a compromised credential and a durable run must not outlive it. |

  Two things this does **not** bound. `expires_at` gates step *entry*, so a step that starts
  just inside the horizon runs to completion — cap it with `limits.max_wall_clock_seconds`.
  The fiber *row* outlives the run's usability by `FELIX_FIBER_RETENTION_DAYS` (7): the nightly
  sweep deletes terminal fibers older than that, and with them the record of who started the run.

  A fiber enqueued with no request context, from a different tenant than the run, or before
  this existed, records nothing and resumes with no scopes. When it *does* carry authority,
  `pin_compile` is forced: the manifest is re-resolved at resume, and running a rewritten
  manifest with the original caller's scopes is exactly what a pin is for.

  That forcing is also why the content hash ignores fields sitting at their default. The
  hash is over the manifest's *meaning*, not the schema's shape — a manifest that writes a
  field's default and one that omits it compile to the same agent — and without that, adding
  a defaulted field to `spec` moved every stored manifest's hash and failed every in-flight
  fiber at resume, for a change no operator made. Setting a field away from its default or
  back still moves the hash, in both directions; `tests/unit/test_manifest_pin_hash.py`
  pins that, because a hash that noticed only additions would let a pinned thread keep
  running after its governance was switched off.

  **Two limits of the pin an operator should know.** First, *changing a default in a release
  is a migration*: a manifest that omits the field now compiles differently and hashes the
  same, so the pin will not fire. Rewrite the rows or rotate the pins deliberately — this
  sits in the same family as removing a key or narrowing a field, which `manifests/compat.py`
  already documents. Second, a pin binds a **thread**, not a conversation: `POST /fork`
  starts a new thread with no pin, so a forked conversation continues under the current
  manifest. That is the design — the pin protects a run in progress — and it is also the
  recovery path when a deliberate manifest edit leaves a pinned thread refusing.

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

`content_screening.decider` adds `spec.decider` beside `model`: one call asks whether the text
tries to override the assistant's instructions, to jailbreak it, or to exfiltrate data, and flags
on the highest probability. It is **additive** — the markers still run first, `model` still runs
beside it, either one flagging flags — because Jev is documented as not adversarially robust: it
is a cheap extra net for paraphrased injections the markers miss, not a replacement for the model
screener. The screened text, a 4,000-character window at a time, goes to the decider's provider —
which `FELIX_DECISION_ROUTES` may point at a different vendor from the chat model. Tool output is
secret-masked before it is screened; a user turn is not, as with the model screener.

Screened tool output is read window by window across its whole length, like a user turn; output
longer than eight windows (32,000 characters) is reported unavailable, so `on_flag` quarantines or
blocks it rather than screening its first window and admitting the rest. This applies whenever
`model` or `decider` is set.

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

**What an operator sees, on either channel.** A pending row and the `approval_required` stream
frame carry the same story: `rule_id`, `reason` (the rule's `description`, or the finding for a
command-screening gate), `thread_id`, `tool_call_id`, and `expires_at`.

That symmetry is what lets a **durable** run announce a gate at all. Its agent runs in the
worker while its stream is served by the API, so the in-process side event cannot cross — and
`GET /approvals` was the whole channel, on precisely the path where a human has time to answer.
`POST /chat/stream` on a durable manifest now reads the pending rows for the run's thread and
**rebuilds** the frame from them, rather than forwarding a message across a bus. The difference
matters: a dropped pub/sub message *is* the lost prompt, and the run would block its full
`ttl_seconds` and then deny with nobody ever asked, whereas a missed poll costs only latency.
It also means a client attaching *after* the gate fired still sees it.

**The stream frames need `approvals:read`, the same scope `GET /approvals` needs.** Without it a
durable run still streams its transcript, its status and its answer, and simply says nothing
about gates. This is not belt-and-braces: `thread_id` is supplied by the caller, so the thread a
durable run names is a question the caller *chose* rather than one they own — nothing in Felix
binds a thread to a principal. Ungated, a caller holding only chat access could name any thread
in the tenant and read the tool names, full arguments and gate reasons it is blocked on, which
is precisely what the management route refuses them. `admin`/`*` bypass and `approvals:write`
implies `approvals:read`, exactly as on the route, because both ask the same function.

Each approval is announced once per stream. `GET /approvals` answers "what is pending now", so
the row returns on every poll until it is decided; re-showing a prompt someone has already
answered is worse than showing it late. A **decided or expired** approval is never announced —
both are history, not a question. (Nothing moves a timed-out gate off `pending`: the waiter
returns a denial and writes nothing back. `find_approved` filters expiry for the authorization
half and the announcement filters it for the display half, so a stale row is inert either way —
but it still occupies the table until `FELIX_APPROVAL_RETENTION_DAYS` is set, which is what
actually reclaims it.)

And the **poll remains the channel of record**: a stream that was never open, or that dropped
before the gate fired, sees nothing, which is why `felix doctor` and the operator console read
`/approvals` rather than depending on an attached stream.

`reason` and `tool_call_id` are empty on rows written before migration
`0015_approval_reason_and_call`, and on gates that genuinely have neither — a command-screening
gate has no rule description, a tool called outside a tool loop has no call id. Treat all of
them as optional when mirroring the wire.

`thread_id` and `tool_call_id` are **attribution, not ownership**: `create_pending` reuses a
pending row across threads keyed on the tuple above, so each names whichever call opened the
row. `GET /approvals?thread_id=…` therefore under-reports rather than over-reports, which is
the safe direction — a caller asking about one conversation never learns about another's. The
filter is applied in SQL before `LIMIT`, so a busy tenant cannot hide the thread you asked
about; `?thread_id=` (empty) means "approvals with no thread" and is distinct from omitting it.

**Three fields sound alike and are not.** `reason` is the *gate's* words, set when the row is
created and never changed. `decision_note` is the *decider's*, set when someone approves or
denies. Neither is the denial text the tool returns to the model, which is composed at the call
site and persisted nowhere. `reason` is truncated at 2048 characters on the way in — it comes
from a tenant-scoped manifest author, and the table is unbounded until an operator sets
`FELIX_APPROVAL_RETENTION_DAYS`.

`command_screening` rules with `decision: require_approval` go through the same flow and
wait up to `command_screening.approval_ttl_seconds` (default 300).

**Across processes.** The run that is waiting and the request that decides are usually in
different processes — a durable fiber waits on the worker, the operator approves through the
API. The wait is a Redis list (`BLPOP`), so the decision crosses. Without Redis the waiter is
a process-local future: the decision lands in the API's memory, the fiber times out and
denies, and the operator was told the approval worked. `FELIX_REDIS_URL` may therefore not
be empty outside `development` (`validate_runtime` refuses to start; `felix doctor` says
why). A URL that is set but unreachable still starts — `/ready` fails on it, which is what
takes the replica out of rotation — and is logged at warning once per subsystem per process
(waiters, steer, thread notifications, session leases), on the first failed connection and
again when a command fails on a client that had connected, rather than silently degraded.
The same channel carries UI prompts and client-tool answers.
## Browser-facing posture

Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer` and `Cache-Control: no-store`; a response that arrived over
TLS carries `Strict-Transport-Security` for `FELIX_HSTS_MAX_AGE_SECONDS`, with
`includeSubDomains` unless `FELIX_HSTS_INCLUDE_SUBDOMAINS=false` (on an apex or shared
parent hostname it pins every sibling for the whole max-age). "Arrived over TLS" is the
connection's own scheme, or the **last** `x-forwarded-proto` entry — the one the proxy
wrote — and that header is believed only when `FELIX_TRUSTED_CLIENT_IP_HEADER` declares a
proxy you operate; the same setting drives the rate-limit key, so declaring one is both.

The API reference (`/docs`, `/openapi.json`) is a map of every route including the
management ones, so under `api_key` and `jwt` it takes the same credential as the API and
counts against the rate limit like any other path. A browser cannot send that credential
(there is no cookie or query-string path, and the page's own fetch of the spec carries
none), so on an authenticated deployment the reference is read by `curl`, through an
authenticating reverse proxy or SSO in front of the origin, or by `FELIX_DOCS_PUBLIC=true`
— which republishes the route map anonymously and is warned at startup outside
development. The docs page sets a per-response nonce-based Content-Security-Policy with
`'strict-dynamic'`, so only the pinned Scalar bundle and its inline config run and no CDN
origin is allowlisted; ReDoc is not served, so there is one reference surface with one CSP.

## Request limits

Rate limiting runs **outside** authentication, so a failed credential is counted — it
previously ran inside, and a 401 returned before the limiter was reached.

```bash
FELIX_RATE_LIMIT=120
FELIX_RATE_LIMIT_WINDOW_SECONDS=60
FELIX_TRUSTED_CLIENT_IP_HEADER=      # e.g. cf-connecting-ip, behind a proxy you operate
FELIX_TRUSTED_PROXY_HOPS=1           # proxies you operate that append to that header
```

Keyed per client address. Redis-backed when `FELIX_REDIS_URL` is reachable; if it is not,
limiting **degrades to per-process** with a logged error rather than failing requests or
skipping the control. Leave `FELIX_TRUSTED_CLIENT_IP_HEADER` empty unless a proxy you
operate writes that header — otherwise a client can present as unlimited distinct
clients. A forwarding proxy *appends* the peer it saw to `X-Forwarded-For`, so the
client is read from the **right**, `FELIX_TRUSTED_PROXY_HOPS` entries deep: the last
entry with one proxy, the one before it with two. Repeated header lines (HAProxy
`option forwardfor` adds a line rather than extending the list) are joined first, so the
rule holds across them. The leftmost entry is whatever the client chose to send and is
never used. A header with fewer entries than the declared hops, or whose chosen entry is
not an IP address, is not trusted at all: the key falls back to the socket peer, the one
address a client cannot choose. A single-valued header (`cf-connecting-ip`) is the
one-entry case of the same rule.

`/metrics` requires authentication: its label values include tenant-supplied manifest ids
and remote MCP tool names.

`PUT /manifests/{name}` and `felix validate-manifest` run the same write-time validator.
Always refused: an outbound `auth` or `env` value that looks like a credential, a URL
carrying `user:password@`, a stdio MCP command outside `FELIX_MCP_STDIO_ALLOWED_COMMANDS`,
and a sandbox image outside `FELIX_SANDBOX_ALLOWED_IMAGES` — each of these stored fine
before and failed, or executed, on the next request. Under `forbid_plaintext_secrets` (forced
by any framework, and by `FELIX_ENVIRONMENT=production`) every non-ref `env` value is
refused too. On read, `GET /manifests/{name}` and the write's own echo replace any literal
`auth`, any non-ref `env` value and any URL userinfo with `[REDACTED]`, so `manifests:read`
never returns a credential a stored manifest still carries. `cowork.yaml` no longer allows
anonymous callers: it binds a shell on the developer's machine, and under
`FELIX_AUTH_MODE=none` the approvals that gate that shell are anonymous too — which means
`make dev` (auth `none`) cannot drive cowork; the Compose stack, which mints a key, can.
`POST /chat` honours `Idempotency-Key`: one turn per key per **principal** (the authenticated
subject within its tenant), replayed for `FELIX_IDEMPOTENCY_TTL_SECONDS`. A replay returns
before the manifest's inbound auth (`required_scopes`, `schemes`) runs, which is why the scope
is the principal and not the tenant — a response one caller earned is never handed to another.
Claims are `SET NX EX` in Redis when `FELIX_REDIS_URL` is set and in-process otherwise; while
Redis is unreachable the store degrades to in-process (one log line per transition, as the
rate limiter does), so a retry then dedupes only within one replica. The in-process store is
bounded (50 000 keys, oldest evicted) and a response over 256 KiB is not stored, so the
header cannot be used to grow a process; the client key is hashed into the Redis keyspace, so
it cannot pick a cluster slot.

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
- **Clock skew of sixty seconds is tolerated** on `exp`, `nbf` and `iat` (`JWT_LEEWAY_S`);
  a token past that is `expired`.
- **An unusable verifier is visible on `/ready`.** A cached `access`/`cognito` key set past
  its TTL is not served, a shared issuer with no `;aud=` is refused, a `FELIX_JWKS_PUBLIC`
  that does not import verifies nothing — in each, every token from that issuer fails while
  the database and Redis probes stay green. `/ready` carries a `jwks` row under
  `auth_mode=jwt`; it **fails only when no configured verifier is usable** (the pod cannot
  authenticate anyone and leaves rotation) and otherwise stays ready and logs which issuer
  is out, so one issuer's outage does not take the deployment off the Service for the
  issuers that still work. Remote key sets refresh every five minutes against a fifteen-minute
  TTL, and a failed refresh retries after thirty seconds, so one IdP blip cannot age a set
  past its TTL. An IdP outage longer than the TTL still 401s that issuer's tokens; the
  deployment stays up for the others.

## Tenant resolution

`tenant_id` is the isolation boundary and, in the default `claim` mode, it arrives in a
token claim. Constrain it:

```bash
FELIX_ALLOWED_TENANTS=acme,globex     # empty = accept any claimed tenant
```

`felix doctor` fails a claim-mode verifier with an empty allowlist outside development (it
says nothing for `fixed` and `issuer`, which read no claim). Prefer `;tenant=fixed:<tenant>` for a single-tenant deployment. On Cognito, `custom:*`
attributes are frequently user-writable, so a claim alone is not an authorization
decision — which is why, outside `FELIX_ENVIRONMENT=development`, a `tenant=claim`
verifier with an empty `FELIX_ALLOWED_TENANTS` is refused at startup (`validate_runtime`)
rather than accepting whatever tenant the token names. `fixed` and `issuer` verifiers never
read the claim and need no allowlist — but `issuer` takes the **first DNS label of the
issuer host** and discards the path, so two Cognito user pools or two Keycloak realms
(`…/us-east-1_A` and `…/us-east-1_B`, `…/realms/acme` and `…/realms/globex`) would
collapse into one tenant; that configuration — or an issuer-derived label that equals another
verifier's `fixed:` tenant — is refused at startup in every environment.
Pin path-scoped issuers with `;tenant=fixed:<tenant>`. The allowlist is global, not
per-verifier: with two `claim` verifiers and `FELIX_ALLOWED_TENANTS=acme,globex`, a token
from either issuer may claim either tenant. If one issuer must not be able to name the
other's tenant, give it `;tenant=fixed:` instead. A token with **no** tenant claim in `claim` mode is now rejected — it
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
| `artifacts:read` | `/artifacts` — read back a tool output too large to keep in the transcript. Its own scope rather than part of `audit:read`, because a spilled result is raw tool output and often the most sensitive data a run touches. The model's own way back, the `read_artifact` tool bound beside `spec.artifacts`, checks no scope and is held to something narrower instead: it reads only what its own conversation spilled (the thread, or the request when there is none), so a leaked id does not reach another caller's run through the model. Spill is kept for `FELIX_ARTIFACT_RETENTION_DAYS` (30; `0` keeps forever) and then swept, objects and ledger row together — so evidence meant to outlive that belongs in the audit log, not in an artifact |
| `approvals:read` / `approvals:write` | `/approvals`; `approvals:read` also gates the `approval_required` frames on a durable `POST /chat/stream` |
| `jobs:read` / `jobs:write` | `/jobs` |
| `plans:read` / `plans:write` | `/plans` |
| `eval:read` / `eval:write` | `/eval` |
| `usage:read` | `/usage` |
| `memory:read` / `memory:write` | `/memory` — inspect, search, correct and prune what an agent has remembered |
| `documents:read` / `documents:write` | `/documents` — ingest, search, inspect and remove the corpus an agent retrieves from |
| `skills:read` | `/skills` — list the Agent Skills a manifest can reach, read the body `activate_skill` hands the model (secret-redacted, as `manifests:read` redacts a manifest), and see which skill activated on which turn. Separate from `manifests:read` because a skill body is **prompt content**: it is appended to the system prompt on activation, so reading one is reading instructions the agent will follow. Read-only; activation is the model's decision mid-turn. By default a manifest's `spec.skills` *adds to* the bundled and `FELIX_SKILLS_DIR` catalogue rather than restricting it, so the model is offered every skill on the host; `spec.skills_declared_only: true` makes the declared names the whole set. The `declared` field on each item says which ones this manifest named, and `declared_only` on the response says which rule is in force. Worth setting on any manifest that has to be reviewable: a skill body is appended to the system prompt, so an ambient skill is an instruction the agent follows that `pin_compile` does not cover — the hash is over the manifest, and the drift is on the host's disk |
| `files:read` / `files:write` | `/files` — upload a file once and reference it by id on later turns. Bounded per tenant by `FELIX_ATTACHMENTS_MAX_BYTES_PER_TENANT` (256 MiB; `0` disables) on top of the 600 KiB per-upload cap, answered as 409 rather than 413 — the request is a fine size and the account is full. `FELIX_ATTACHMENT_RETENTION_DAYS` (`0`, keep forever) lets the nightly sweep collect old uploads, bytes and ledger row together; before it, `attachments/` was an object-store prefix nothing ever collected. Uploads predating migration `0016` are counted by neither, because a backfill would need the `list` the `ObjectStore` Protocol deliberately does not have. Under `auth_mode=none` every local process holds `files:write`, so the ceiling is the only thing bounding the disk there. `DELETE /files/{file_id}` is the erasure path. Separate from `artifacts:read`, which reads spill the *harness* wrote: these are caller-supplied bytes with a caller-driven lifecycle, so permission to add them is its own grant. The tenant comes from the caller's credentials and never from the path, so no spelling of a reference reaches another tenant's upload. A turn names an upload with a `file` content part, expanded to bytes immediately before the model call — after `apply_inbound_screening`, which is where it must be if the session log is to keep the reference rather than the base64. That is not a gap this opened: `_message_text` collects only `text` blocks, so **image content has never been screened on any path**, inline `data:` URLs included, and text rendered inside an image is an injection channel on both |

```bash
felix mint-jwt --sub ops --tenant default \
  --scopes audit:read,manifests:write,approvals:write,jobs:write
```

## Supply chain: what proves an image is the one Felix published

Every published image (`ghcr.io/felix-run/felix:X.Y.Z` and `:X.Y.Z-gcp`, each for
`linux/amd64` and `linux/arm64`) is signed by digest with cosign under the release
workflow's OIDC identity, carries an SPDX SBOM attestation per platform and SLSA provenance
from buildx, and was scanned for CRITICAL/HIGH findings before its version tag existed.
How that pipeline works, what it refuses, and the repository settings it depends on are in
[`docs/RELEASING.md`](../docs/RELEASING.md). An operator verifies:

```bash
cosign verify ghcr.io/felix-run/felix:X.Y.Z \
  --certificate-identity-regexp '^https://github.com/felix-run/felix/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
cosign verify-attestation --type spdxjson ghcr.io/felix-run/felix:X.Y.Z \
  --certificate-identity-regexp '^https://github.com/felix-run/felix/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The signature says the image was built by that workflow at that tag; the SLSA provenance
attached by buildx says from which Dockerfile, sources and build args; the SBOM says what is
in it. What the workflow cannot prove is who was allowed to push the tag — that is the tag
ruleset and environment protection described in `docs/RELEASING.md`, repo settings rather
than code. Dependencies are held for 48 hours after publication before CI accepts them
(`scripts/check-dependency-age.py`), and every action the workflows run is pinned by commit
SHA, every scanner and base image by digest.

## GitOps check

```bash
felix validate-manifest path/to/agent.yaml -e production
# or in CI after editing manifests/
uv run felix bundle-manifests
```
