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
out until `DOCS_MCP_AUTH_TOKEN` exists and `FELIX_MANIFEST_SECRETS` lists it — scoped chat
works without it.

**The public demo stays anonymous, by decision (2026-10-02).** Visitors reach it through the
chat-ui Worker, which holds the one Felix credential, so nobody has an identity or scopes of
their own there. That means it cannot show `governed`'s per-caller controls: a scope check would
pass or fail the same way for every visitor. Those controls are shown by running `governed`
yourself, with the token minted above, not on the demo. Issuing visitors scoped chat keys would
turn the demo into an account system, and that is not what it is for.

## Secret injection

| Layer | Mechanism |
|-------|-----------|
| Platform (model keys, consumer secret) | `FELIX_SECRETS_BACKEND=env\|file\|aws\|gcp` + `hydrate_secrets()` at API/worker startup |
| Manifest outbound (`mcp_servers.auth`, `env`, peer/container `auth`, `github_publish.auth`) | `secret:NAME` or `{secret: NAME}` resolved at compile, only for a name `FELIX_MANIFEST_SECRETS` lists for the tenant (below); **never** store resolved values in `manifest_json` |
| Redaction | Known secrets scrubbed from tool output, session events, audit payloads, fiber state, skill bodies read over `/skills`, and every `/skill-library` read and write response — file text, and the reason, description, decision note, review-check and security-issue messages a saver or the scan wrote, and feedback bodies, suggested patches, evaluation scenarios, judge reasons and job errors. `FELIX_SKILL_IMPORT_GITHUB_TOKEN` is hydrated from the secrets backend and masked like any credential; it is sent only as a header to `api.github.com`, never in a URL, a log line or a response |

Production (`FELIX_ENVIRONMENT=production`) or `governance.forbid_plaintext_secrets: true`
rejects Bearer/long-token auth and non-ref MCP `env` values.

### Which secrets a manifest may name

Every secrets backend resolves any name the process can see, and a manifest chooses both the name
and where the value goes — an MCP server's URL, a peer's, a container gateway's, GitHub. Unchecked,
anyone holding `manifests:write` could publish `auth: secret:FELIX_AUTH_API_KEYS` against a server
they run and receive every tenant's API keys as a bearer token. So a manifest's refs are an
allowlist, per tenant:

```bash
FELIX_MANIFEST_SECRETS='{"GITHUB_MCP_TOKEN": ["acme"], "DOCS_MCP_AUTH_TOKEN": "*"}'
```

Each name maps to the tenant ids whose manifests may reference it, or `"*"` for every tenant.
Empty (the default) allows none. Matching is exact on the name the manifest writes, so
`github-mcp-token` is not allowed by `GITHUB_MCP_TOKEN`. Felix's own secrets are refused whatever
the map says, and listing one fails the boot: any name that is `FELIX_*` (or `felix/…`, the cloud
backends' spelling) after the upper-case and `-`→`_` normalisation the env backend applies, every
name in the hydrate map (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN_KEY`, `GOOGLE_CLIENT_SECRET`, …), and
the infrastructure credentials `DATABASE_URL`, `REDIS_URL`, `POSTGRES_PASSWORD`, the MinIO root
pair and the AWS/GCP process credentials. A malformed value fails `validate_runtime` at boot.

It is checked twice. `PUT /manifests/{name}` — and a canary or rollback onto a stored version —
answers `422` naming the secret and the setting before anything is stored. Compiling checks again
in front of the backend's `get` (`felix.secret_policy.TenantScopedSecrets`), so a row stored before the
check existed fails its request with the same message (`422` on `/chat` and `/v1`) and the backend
is never asked for the value. A `github_publish.auth` ref is refused the same way, not dropped.

A secret an operator registry already binds cannot also be opened to manifests: listing a name that
appears as a `secret:` ref in any other setting (`FELIX_EGRESS_CREDENTIALS`, `FELIX_WEBHOOK_ENDPOINTS`,
`FELIX_TRIGGERS`, `FELIX_CHANNELS`, `FELIX_MODEL_PROVIDER_OPTIONS`, the VAPID key, … — every
setting is scanned, bare or as JSON, so a new registry is covered without being listed), or in
`FELIX_SECRET_NAMES`, fails the boot ("already bound by FELIX_…; give manifests a separate secret").
Those registries scope their secret by tenant, host or endpoint; a manifest ref sends it wherever
the manifest says, so sharing one would undo that scoping. Give a manifest its own credential. The
registries themselves are not checked against this map, since their refs are the operator's.

A plugin reading `spec.extensions` is reading tenant input. It resolves a `secret:NAME` found there
through `PatternBuildContext["manifest_secrets"]`, the same tenant-scoped backend, never
`build_secrets`. `felix doctor` notes each bundled manifest (including `FELIX_MANIFESTS_DIR`) that
names a secret not permitted for a tenant the configuration pins (or `default` when none is pinned).

PII: `spec.guardrails.providers: [pii]` uses **Presidio** when
`felix-harness[pii]` is installed, otherwise a regex fallback. Eval LLM judges
are opt-in via rubric `llm_judge` / `judge_criteria` or `felix eval --llm-judge`
(CI stays on `--mock`). A judge that cannot run scores the item with the heuristic instead —
a weaker test that still reports a result — so the score row carries `judge_fallback: true`
and `judge_error`, the run's `stats.judge_fallbacks` counts them, `felix eval` warns on
stderr, and `felix eval --strict-judge` exits 1 rather than pass on the weaker test.

### AWS

```bash
FELIX_SECRETS_BACKEND=aws
FELIX_AWS_REGION=us-east-1
# Leave FELIX_ANTHROPIC_API_KEY empty; create SM secret felix-anthropic-api-key
# Manifest refs use the same backend, e.g. secret:DOCS_MCP_AUTH_TOKEN (listed in FELIX_MANIFEST_SECRETS)
```

See [aws/README.md](aws/README.md). Prefer IRSA over static keys.

### GCP

```bash
FELIX_SECRETS_BACKEND=gcp
FELIX_GCP_PROJECT=your-project
# Secret Manager ids: felix-anthropic-api-key, DOCS_MCP_AUTH_TOKEN, …
```

See [gcp/README.md](gcp/README.md). Prefer Workload Identity.

**Rotation under `aws` and `gcp`.** Manifest `secret:NAME` refs are resolved on every compile, and a
compile happens per request, so each process remembers a resolved value for five minutes
(`felix.secrets.CLOUD_SECRET_TTL_S`) rather than calling the secret manager per ref per request. A
rotated value reaches new requests within that window; revoke the old credential after it, not
before. A name that does not exist is never remembered, so a newly created secret is seen at once.
Deleting or disabling a secret is not a kill switch for the same reason: a process that resolved
it keeps the value for up to that window. To cut one off at once, revoke the credential at its
issuer, or restart the API and worker. Platform keys hydrated at startup are unaffected: they
change only on restart, as before.

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
allowlisting `/usr/bin/npx` does not allow a bare `npx` resolved through `PATH`. What is spawned
is decided by the operator's environment, never the manifest's:

- A bare name (`uvx`) is resolved on the **API's own** `PATH` and spawned by that absolute path.
  A manifest path that matched only through its realpath spawns the realpath, the file the
  operator listed, not the manifest's link to it.
- The child's `PATH` is the API's, with relative entries (`.`, an empty entry,
  `node_modules/.bin`) dropped, since those resolve against the `cwd` a manifest sets. A
  manifest may not set `PATH`, so an interpreter a server starts through `#!/usr/bin/env` comes
  from the operator's directories too.

The child process does **not** inherit the API environment; it receives
`PATH`/`HOME`/`LANG`/`LC_ALL`/`TZ` (plus the SDK's `LOGNAME`/`SHELL`/`TERM`/`USER`) and the keys
declared in `mcp_servers[].env` (with `secret:NAME` refs resolved). A key that makes the server
run or load code the manifest chose is refused (`stdio_policy.env_key_refused`, case-insensitive):

| Family | Refused |
|---|---|
| Loaders | `LD_*`, `DYLD_*`, `GCONV_PATH` |
| Shells | `BASH_FUNC_*`, `BASH_ENV`, `ENV`, `SHELLOPTS`, `BASHOPTS`, `PS4`, `PROMPT_COMMAND`, `IFS` |
| Interpreters | `JAVA_TOOL_OPTIONS`, `JDK_JAVA_OPTIONS`, `_JAVA_OPTIONS`, `NODE_OPTIONS`, `NODE_PATH`, `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `PYTHONBREAKPOINT`, `PYTHONWARNINGS`, `PYTHONINSPECT`, `PYTHONUSERBASE`, `BROWSER`, `PERL5OPT`, `PERL5LIB`, `PERLLIB`, `PERL5DB`, `RUBYOPT`, `RUBYLIB`, `HGRCPATH` |
| git | `GIT_CONFIG_*`, `GIT_TRACE*`, `GIT_CONFIG`, `GIT_ALLOW_PROTOCOL`, `GIT_SSH`, `GIT_SSH_COMMAND`, `GIT_ASKPASS`, `SSH_ASKPASS`, `GIT_EXTERNAL_DIFF`, `GIT_EXEC_PATH`, `GIT_PROXY_COMMAND`, `GIT_EDITOR`, `GIT_SEQUENCE_EDITOR`, `GIT_PAGER`, `GIT_TEMPLATE_DIR` |
| Lookup | `PATH` |

A manifest saved with one gets a `400` naming it. One already stored is not failed at compile,
which would take its whole agent down; that stdio server binds no tools at spawn instead. Every
other key (`GIT_AUTHOR_NAME`, a server's own token) passes. The child's git is also disarmed
exactly as the shell tool's is (`stdio_policy.GIT_DISARMED`: hooks, `core.fsmonitor`, pager,
editor, `core.sshCommand`, `core.askPass`, credential helper, signing programs and
`protocol.ext.allow=never` pinned through `GIT_CONFIG_*`, with `GIT_TERMINAL_PROMPT=0`). Those
pins need git 2.31 or later; an older git ignores `GIT_CONFIG_COUNT` without error. A git server
that relied on a credential helper must be given its token another way: through a `secret:` env
key the server reads, never in a URL, which lands in `.git/config`, `ps` and git's error text.

The list is the known keys that need no file of the attacker's on the host, not a proof that no
other exists. Not covered:

- **A repository's own config, for programs git names by driver or remote:**
  `diff.<driver>.textconv`, `merge.<driver>.driver`, `filter.<driver>.clean`/`smudge` (named
  in `.gitattributes`), `remote.<name>.uploadpack`, `submodule.<name>.update=!cmd`,
  `core.gitProxy`. These need a repository the attacker can write.
- **Keys that point at a file:** `HOME`, `XDG_*`, `GIT_DIR`, `OPENSSL_CONF`, CA bundles. On Linux
  a value may contain newlines, so `/proc/self/environ` can serve as that file for a pointer to a
  command-running format; the ones known to matter (`BASH_ENV`, `ENV`, `GIT_CONFIG_*`, `HGRCPATH`)
  are refused.
- **Trust and egress, not code:** `NODE_TLS_REJECT_UNAUTHORIZED`, `GIT_SSL_NO_VERIFY`,
  `PYTHONHTTPSVERIFY`, `HTTP(S)_PROXY`/`ALL_PROXY`. The SSRF guard does not cover a stdio child's
  own connections.

**Allowlisting an interpreter or a package runner is code execution for `manifests:write`.**
`uvx --from git+…`, `npx -y <package>`, `node -e`, `python -c` and an index URL in the arguments
all run whatever the manifest names; the allowlist decides *which binary* runs, not what it runs.
Allowlist fixed server binaries where you can: the environment rules above only mean something
for those. Prefer `transport: http`/`sse` where you can.

`FELIX_AUTH_MODE=none` is refused on any non-loopback bind, in every environment. Where it is
accepted, every boot logs a warning that the API is unauthenticated. The loopback check reads
`FELIX_HOST`, not the address the server actually binds — Granian binds what its own command line
names — so keep the two in agreement; nothing in the process can see a mismatch.

Under `FELIX_AUTH_MODE=api_key`, a malformed `FELIX_AUTH_API_KEYS` fails the boot: JSON that does
not parse, a top level that is not an object, an entry that is not an object, a `tenant_id` or `sub`
that is not a string, or a `scopes` that is not a list of strings (`"scopes": "admin"` once granted
the scopes `a`, `d`, `m`, `i`, `n`). Outside `FELIX_ENVIRONMENT=development` — production,
staging, anything else — every key must be at least 32 characters. The refusals name an entry by position, never by its key.
Boot and the request path read one parser (`felix/auth/api_keys.py`), and the request path fails
closed — no key authenticates — on a list boot would have refused. An entry without `tenant_id` is
the `default` tenant.

A plugin's `self_authenticating_mounts` skip Felix's authentication for every path under them, so
each is checked at boot: a path under `/`, not the root, with no `..`, `//`, whitespace or trailing
`/`, and a first segment no core route and no anonymous path uses (`/chat`, `/v1`, `/auth`,
`/.well-known`, …). Anything else refuses to start, naming the plugin. The anonymous paths are one
list (`anonymous_paths` in `felix/auth/middleware.py`) that both the door and this check read. Every
path rule — mounts, probes, login, triggers — compares the path the router routes: with an ASGI
`root_path` (a proxy prefix) taken off, so a request is judged by the route it reaches.

`felix mint-jwt` grants no scope unless `--scopes` names one, mints for at most a day
(`--ttl` 1 to 86400), and with `--allow-long-ttl` for at most a year (31,536,000 seconds) — never
longer. `--aud` sets `aud`. A minted token cannot be revoked, so its expiry is the whole of its
lifetime.

A failed A2A task records only that it failed (`tasks/get` returns it to the tenant). The reason is
logged as the exception's type and its message with every registered secret masked, without a
traceback, since no log formatter masks one.

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
keeping forever. Three sweeps delete object-store bytes as well as rows, each through a ledger
because the store cannot be listed: uploads (`FELIX_ATTACHMENT_RETENTION_DAYS`, off by default,
caller data), spilled tool outputs (`FELIX_ARTIFACT_RETENTION_DAYS`, 30 by default, the
harness's working copy) and file checkpoints (`FELIX_FILE_CHECKPOINT_RETENTION_DAYS`, 7 by default:
the earlier content of every file the model's file tools changed in a thread's own workspace, kept
for `/chat/rewind` with `restore_files`; `FELIX_FILE_CHECKPOINTS=false` keeps none). Checkpoint
content is workspace content and needs the same care: a file the agent deleted, a credentials file
it was told to remove included, stays under `checkpoints/{tenant}/` until the retention period has
passed since that file's last change in the thread, or a rewind forgets it; deleting the thread
does not purge it sooner. Retention counts per file, so a file stops being kept on time however
busy its thread stays, while a file the agent keeps editing keeps its history. Content is kept once
per tenant by digest and capped by `FELIX_FILE_CHECKPOINT_MAX_BYTES_PER_TENANT` (256 MiB; concurrent
captures can overshoot it by the few in flight). A
restore writes and deletes files in the thread's own workspace only, never a scope threads share,
and each one leaves a `workspace_restore` audit row with the caller and the paths, as the file
pane's writes do. A manifest's `retention_days` does **not** shorten its spill: a
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
opens or a durable run is enqueued, and tell the agent so -- naming the manifest they screened
for, so only that manifest's screen skips the turn: under a composite with no inbound controls of
its own, a router's pick or a `parallel` specialist still screens what it is handed. `guardrails.targets`
name where PII is caught: `input` is the user turn, `output` is everything leaving the
model boundary — tool output *and* the agent's reply — and `final_response` is the reply
alone. The reply-path controls (`output`/`final_response` PII, and `judges` with
`final_response: true`) wrap the agent rather than its tools, and apply on the streaming
path as well as `invoke`: reply text is held until the run ends and released screened,
while tool and approval frames stream as they happen. A denial or redaction emits a
`guardrails_reply` or `judge_deny` audit event. The session log is screened at the
write with the same verdicts: every assistant message is redacted before it is appended,
and every one without tool calls is judged, so a denied reply is stored as its denial —
replays, exports, `/chat/history` and a durable run's live tail carry what the client got.
The store is handed to sub-agents too, since a router's child writes the caller's thread,
and reflect's critique quotes its draft redacted. What a composite pattern hands from one model
call to the next is fenced as data in a tag made new for each call (`<subtask_notes-<12 hex>>`, so
no spelling of a close, a fullwidth lookalike included, can be forged), under a line saying
so: plan_execute's notes to the next
subtask, a replan and the synthesis; parallel's specialist answers to the synthesis; reflect's
draft to its verifier and back to the drafter. Each carries whatever the child's tools read. The
log is never less screened than the
wire, and sometimes more: a preamble written before tool calls is redacted but not judged,
so on a denial the reply withholds it and the log keeps it; and reflect's intermediate
drafts are each judged in the log, one judge call per draft. `spec.memory.capture`
extracts from the reply as the controls ship it — redacted, and not at all from a reply a
judge denied — and a router's controls reach a child's capture as well as its log. What the
reply controls do
not cover, stated so nobody assumes them: reasoning is not the reply, so
`thinking_delta` passes through and the signed reasoning a thread stores for replay is kept
as written, since redacting it would break its signature; and compaction and branch
summaries are model output over the whole thread, user turns and tool results included,
and are stored as written. A judge scores by, in order of preference: `spec.decider`
when the rule sets `decider: true` (the probability the text meets `criteria`, as written), the
chat model in `model`, and the heuristic — each failure falls through to the next, so a decider
outage degrades a judge rather than opening it. A model judge whose reply carries no score counts
as a failure too and is scored by the heuristic: the eval judge's own stand-in for that case is
"non-empty", which would pass any text. On tool output, the judges that will score with
the heuristic run first, one at a time — they are free, and one that denies spares every model
call — and the decider and model judges then run together; a tool result passes only if every
judge passes it, as before, and the denial named is the first heuristic one, else the first of the
rest in the manifest's order. A decider-scored judge sends the judged text to the
decider's provider, and only judges text up to 8,000 characters — anything longer is scored by the
model judge or the heuristic, which read all of it, rather than by a prefix. The model judge reads
the judged text (and the request, when there is one) fenced in a region the text cannot close,
under a system prompt that calls it untrusted data and says to ignore any instruction or score
inside it; `criteria` stays outside the fence as the instruction. The `llm` decision provider tells
its model the same of the state it is given, and the model injection screener reads the screened
text fenced the same way. Treat a model or decider judge as a quality control, not a security
boundary all the same: a fence is a convention the model is told to honour, and the text it judges
can still argue for its own verdict (Jev is not adversarially robust), so untrusted tool output
belongs behind `content_screening`, not behind a judge alone. Tenant
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

`providers` covers every model route the manifest names, not just `spec.model.id` and its
fallbacks. That includes `vision_model` (or, when the primary is text-only and none is named,
`FELIX_DEFAULT_VISION_MODEL_ID`), `confidence_escalation.escalate_to` when enabled,
`plan_execute.planner_model`/`executor_model`, `heartbeat.model`, `reflect.verifier_model`, each
`guardrails.judges[].model`, and `http_tools[].extract_model`. When their feature is enabled, it also
covers `content_screening.model`/`image_model` and `memory.capture`/`consolidate.model`. Each of
these is sent content from the run, so one left out would be a way around the pin. The memory
consolidation sweep never compiles the manifest, so it checks the pin itself and skips a pool the
pin refuses.

The same list feeds the `limits.max_cost_usd` pricing check. There, `spec.model.price` covers only
the routes built from `spec.model`: the model, fallbacks, vision, escalation, plan/execute,
heartbeat and capture. The verifier, judges, screening, consolidation and extraction build their own
client and are billed at catalog rates, so they must route to a priced model.

A request's `model` override (`POST /chat`, `/chat/stream`, `/chat/continue`) is held to the same
rules for the one turn it replaces `spec.model.id`, and so are the rules of every sub-agent it
is handed to, since each child turn runs on it under the child's own manifest. Its provider must be
in `providers`. Under a declared `max_cost_usd` it must be priced, and with `spec.model.price` set it
must be one of the routes built from `spec.model` that the manifest names, since the override is
metered at those rates. A refusal is `400 model_provider_not_allowed:<id>`, `model_unpriced:<id>`
or `model_not_priced_by_manifest:<id>`. A durable run checks again when it resumes, so a run that
outlives a route-table or manifest change fails rather than running on a route now refused.

Three calls build `spec.model` without compiling the manifest: a side question, `/chat/compact`,
and the summary a rewind writes of the branch it leaves. Each makes the build's checks first
(`validate_for_run`), so a manifest whose turns are refused for its pin or an unmeasurable cost cap
does not reach a model through them either. They also honour the thread's `pin_compile` record, so
on a thread a pinned manifest owns, naming another manifest does not send its history to that
manifest's model. A side question and `/chat/compact` answer with the reason, `422` for a check and
`409` for the pin. A rewind is not refused, since it calls no model itself: its summary is written
without one, as it is when the named manifest no longer resolves.

Not yet covered, so nobody assumes more:

- `spec.decider` resolves through `FELIX_DECISION_ROUTES`, a separate table, and is not pinned.
- The memory consolidation sweep checks the provider pin but not `max_cost_usd` measurability.
- An eval's LLM judge (`rubric.judge_model`) is sent the agent's answer whatever its manifest's
  `providers` says.

**Sub-agents inherit the caller's admission.** `spec.auth.inbound` is checked on the manifest
a request names — the router — and not again on each sub-agent it compiles. A child's own
`allow_anonymous`, `schemes` and `required_scopes` therefore apply when the child is called by
name, and not when a router hands it a request. Put the admission you need on the router. One
case is refused at compile rather than left to the author: a composite an anonymous caller may
enter, naming a child that caller could not, fails with `admits anonymous callers but its
sub_agents … do not`. Both doors are read with the check a request makes, so a child closed only
by `required_scopes` or `schemes` counts as closed, as does one whose door the build cannot read.
That is why the bundled `router`, which names `deep`, refuses anonymous callers. A child found
under one name whose manifest calls itself another is refused outright (`the names must match`),
for `sub_agents` as for `delegation`. For an *authenticated* caller, a child's `schemes` and
`required_scopes` are still not checked through a router. What a child keeps
is everything else it declares — its tools, policies, approvals, screening and limits are
compiled into it and apply whichever way it was reached.

**A credential can be pinned to manifests.** A token or api-key entry carrying a `felix_manifests`
claim (a list of names) may invoke those manifests and no other: refused with 403 on chat, `/v1`,
A2A, MCP, side questions, `/chat/compact`, a rewind's summary, and each child `delegation`
compiles. The routes that reach a manifest without a turn of it are held to the same check, and
to its `auth.inbound`: the file pane (`/chat/workspace/*`, against the manifest whose
`spec.workspace.scope` decides the directory — the thread's last, else the one named, else the
default), `POST /chat/style` naming a style, and A2A `tasks/get`, `tasks/cancel` and
`tasks/resubscribe` (against the task's manifest; a task whose manifest no longer resolves is
refused). An A2A `message/send` whose `taskId` names an existing task is admitted against that
task's manifest before anything is written, and one naming a different `manifest` is refused; a
refused send writes no task record at all. Threads and tasks are otherwise shared across the tenant. The pin is stored with a
durable run and enforced again when it resumes. A claim
that is not a list of strings is refused rather than ignored. Google sign-in mints one for an
account with `manifests` on `FELIX_GOOGLE_ACCOUNTS` (the bundled `kids` agent is what it is for).
Boot refuses a pinned account that carries anything but `<area>:read` scopes, since a write scope
reaches around the pin (rewrite the manifest, schedule a job, run an eval), and any scope with
whitespace in it (the token's `scope` claim is split on whitespace, so `"admin memory:read"` would
arrive as `admin`). Since threads are shared within a tenant, it also refuses a pinned account whose
tenant is shared with an unpinned account, with an account pinned to different manifests, with any
entry in `FELIX_AUTH_API_KEYS` (one without a `tenant_id` is in `default`), a
`FELIX_GITHUB_ORG_TENANTS` grant, a `FELIX_TRIGGERS` trigger or a `FELIX_JWT_VERIFIERS` issuer with
`;tenant=fixed:`, or that is
`default` itself or a GitHub signup's personal tenant. A claim- or issuer-mode verifier's tenants
cannot be known at boot and are not checked. Like
`auth.inbound`, the pin is checked on the manifest a request names, so a pinned manifest's
`spec.sub_agents` are the operator's choice and are not checked again.

**A pin limits which agents a credential runs, not what it can read in its tenant.** Threads are
shared across a tenant, so a turn of a manifest the credential may invoke, sent on another
credential's thread, replays that thread's history to the agent — whatever manifest wrote it. Two
API keys or two JWT principals in one tenant, one pinned and one not, are therefore not isolated
from each other's conversations; the pin is not a confidentiality boundary inside a tenant. The
boundary is the tenant: give a credential that must not read others' threads a tenant of its own.
Only Google sign-in enforces that at boot (above); for `FELIX_AUTH_API_KEYS` and external JWTs it
is the operator's configuration to get right.

**`pin_compile` covers sub-agents**, recursively: the pin records a digest of every child a
router compiles, and an edited, added or removed one is drift — 409 on a turn, a failed fiber on
resume. The compile builds from the children the pin check resolved for that request, so the tree
the pin verified is the tree that runs; a child published after the check waits for the next turn,
which refuses it. One limit, stated so nobody assumes more: a thread pinned before sub-agents were
covered adopts its children as they are on its next turn, so an edit made before that upgrade is
accepted.

`providers` is checked at **compile**, against the resolved route of every model field listed
above, so a violation fails the build rather than surfacing at the first model call.

### What a caller can put in the system tier: nothing

Every system-role message a model sees is written by the operator or the harness: the
manifest's prompt, skills catalogue, handoff and session notes. Text a caller, a tool or a
model produced reaches the model as a user turn at most, labelled for what it is — a
conversation summary (reference material), the summary of the earlier steps of a turn a
compaction cut through (also reference material), a recalled memory, or a
`POST /chat/sessions/custom` entry. That last one takes its `role` from the caller and is
stored as written, but a `system`-role entry with `in_context: true` is sent as a user turn marked as added by the
client, on live history and on a compaction checkpoint alike (`session/types.py`,
`_model_role_and_content`). Its `metadata` is the caller's own, less what the harness reads to place
or render an event (`RESERVED_METADATA_KEYS`): no `event_id` or `parent_id`, so it lands at the end
of the branch and cannot stand in for another event or graft itself where the model would read a
history the transcript does not show; no `attachments`, so it sends the model no image, which `/chat`
allows only on a user's own turn; no `thinking`; and no `pinned`, which kept an entry through every
window and compaction, so repeated ones grew each turn's context past what the manifest's strategy
bounds. A queue's write-back (`/internal/sessions/{id}/events`) cannot choose where its event lands
or pin it either, and writes only a message, a tool result or an entry: any other kind is stored as a
message, so it cannot write a `compaction` the strategies would read as the thread's summary. Nor can
it write a skill tool's result: one named `activate_skill` (or any of the four skill tools) is stored
as `queue:activate_skill`, because a real one is read back as the thread's activation and re-shown
after every window and compaction (`skills/activation.py`) -- the pin, by another name.

A person's `/skill-name args` and a manifest's `preload` are activated by the harness, not
written into the person's turn: before the model's first step it calls `activate_skill` through the
tool runner, so every wrapper (approvals, screening, masking) applies and the body reaches the model
as a tool result, never with the authority of the person's own words. Only a turn a chat route marks
as the person's (`InvokeInput.person_turn`: `/chat`, `/chat/stream`, `/v1`, a durable `/chat` run)
is read for `/name`; a `task` prompt, a plan step, an A2A peer's message, a scheduled job's prompt
or a workflow step is text a model or an author wrote, and `/name` in it runs nothing. What a
person's call may do that the model's may not -- reach a `disable-model-invocation` skill, pass
`arguments` into its instructions -- is granted by the call's id, minted by the harness and good
once, during the preamble round only (`skills/invocation.py:PersonCalls`); `arguments` the model
passes itself are ignored and the result says so. A `disable-model-invocation` skill is a person's
to run only when the manifest names it in `spec.skills`: an undeclared one -- a host skill, a
published library skill -- stays no one's to activate, so marking a skill that way still works as
an "off" switch for every manifest that does not declare it. Anyone who can chat with a manifest
that declares one can run it, anonymous callers of an `allow_anonymous` manifest included. `/v1` counts as a person's surface though its caller may be an agent: a deployment
that relies on `disable-model-invocation` against agent callers should not expose such skills to
manifests served there. The audit row of each activation says who asked (`by`: `person`,
`preload` or `model`). A preload of an imported skill nobody has adopted is skipped, and a preloaded
skill the conversation deactivated stays deactivated. `/name` and `preload` run where tools run: a
react agent, and what wraps one (`deep`, `reflect`, `plan_execute`). A composite that hands work to
child steps carries the person's own `/name` turn into each (`InvokeInput.person_command`): every
`plan_execute` subtask, `parallel` specialist and `groupchat` speaker starts with the skill
activated, under its own manifest's catalog, and a child stricter than its parent screens the
carried turn as it does its own (quarantined, it is no command). That is one `activate_skill` call,
and one audit row, per step: every subtask, every speaker every round. A router hands its child the
person's turn itself, and `reflect` reads a threaded run's activation back from the log (a
threadless one -- a composite's step, `/v1` with no `user` -- runs it again per draft). Only a turn
a route marked as the person's is carried, so a `task` prompt written `/name` runs no skill in a
composite child either. A child is its own manifest with its own door, and the route checked only
the root's: a person's `/name` runs in a manifest only when the caller passes that manifest's
`auth.inbound` (scopes, schemes, pin), as `task` requires per call -- so a parent open to more
callers than its child does not hand them the child's manual-only skills. A composite with no tool
loop of its own (router, `parallel`, `groupchat`) cannot run its own `preload`, and its compile says
so.

A skill whose frontmatter says `context: fork` runs in a subagent instead: `activate_skill` hands
its instructions and the model's `task` to one of the manifest's `spec.delegation.agents` (the one
`agent:` names) as a call of the manifest's *governed* `task` tool. A manifest with no
`spec.delegation` and no `sub_agents` runs a forked skill that names no `agent:` (or names the
manifest itself) in itself instead: the same manifest, compiled on the first fork as its own
subagent, in a fresh conversation, through a `task` tool that gets every wrapper and is then withheld from the model
(it was given no delegation to call). That subagent's own forks run nowhere (`fork_unavailable`),
so a fork is one level deep. It has the manifest's whole toolset, and the model's `task` text is
its user turn, screened by the manifest's inbound controls as a delegation child's is. Its skills,
prompt files and MCP tool lists are loaded when it is compiled, at the first fork, not when the
request was admitted. Either way, every control that names
`task` holds for it: an approval, a policy, the limits and their peer hops, the permission mode, the
child's own inbound auth, `subagent_stop` -- and returns only that agent's answer, as untrusted
output content screening reads (a deny is passed on as one). The instructions never enter the
conversation and nothing is retained. The `task` call goes through a tool runner as the model's
own would, so manifest `pre_tool_use`/`post_tool_use` hooks and plugin `before_tool`/`after_tool`
hooks on `task` fire for it, a hook's block stops it, and it has its own `tool_call` audit row
beside the `activate_skill` one and the skill's `fork` row. It is a call in the ledger like any
other: a refused fork writes a `policy_deny` row (and a denied `felix_tool_calls` count) for `task`
and another for `activate_skill`, and a hook with no `tools:` filter fires for both. A hook on
`task` is sent the call's arguments, so its endpoint receives the skill's instructions. A hook's
`terminate` on the inner call is not carried out; the run goes on with the fork's answer. It counts as one tool call against
`max_tool_calls` -- the `activate_skill` call it runs inside -- and as one peer hop. The child's
turn puts the model's `task` first and the skill's instructions after it, so a person approving
the `task` call reads the job before the body. The inner call's id is derived from the
activation's (`<id>:fork`), so an approval it waits on is withdrawn when the run is interrupted,
like any other call's (felix#531). A `model:` the skill asks for is used only if the deployment
defines that route and request-model governance would let a caller ask for it on this manifest and
every agent below it; it reaches the `task` handler by a harness-minted call id, never
through the model-facing schema. An imported skill nobody has adopted never forks, since its
instructions would become the subagent's own turn, and a forked skill is never preloaded.

An activated skill stays in view for the rest of its conversation, within limits. Retention re-shows
the *logged* result -- masked and screened when it was first returned -- as an ordinary user message
ahead of the history (never a transient one, which the wires send after the newest turn), with every
`<` escaped so the body cannot close its block, and labelled `untrusted="true"` for an imported skill.
Only a skill the compile's catalog still offers is re-shown: archiving, unpublishing or undeclaring
one stops it being re-shown in every conversation that activated it (its original result stays in
any turn the strategy still renders), and one that changed since is named rather than repeated.
Retained text is a user-role message: an imported skill's body, which reached the model as a tool
result, is re-shown in the user tier, labelled and fenced. Retained text is capped at 128K characters a render, newest activation first; the
rest are named, and activating one again sends it.

**Skill bodies are the one place model-written text can become instructions**, so they are held
to a review step. The catalogue names skills in the system tier and `activate_skill` returns a
body as instructions. A body from the bundled directory, `FELIX_SKILLS_DIR` or an uploaded
object-store key is operator-written. A body from the tenant skill library reaches a catalogue
only as a *published* version, and an agent's save (`create_skill`, `update_skill`) is a draft
that no catalogue loads. Publishing is done by an operator, or — with
`spec.skill_authoring.mode: publish` — by the agent itself, which the schema allows only behind an
approvals rule that gates every `create_skill` and `update_skill` call. So every library body in
a catalogue was either reviewed by an operator or approved by a person before it was saved. An
agent's edit of a version an operator wrote is never published automatically: what is checked is
the source of the `parent_version` the edit names, live or not.
Every publish also passes a gate that re-reads the bytes against their saved digests and
re-scans them, and a failing security scan blocks whoever asks. Library bytes are kept under
their own object-store prefix (`skill-library/`), never under the operator's `skills/` keys.

**Precedence, when a library skill and an operator upload share a name.** Host skills (bundled,
`FELIX_SKILLS_DIR`) always win, and the library refuses their names and the unversioned upload
keys. That holds only while those directories resolve. A `FELIX_SKILLS_DIR` that is set but
missing refuses to boot (`validate_runtime`): served empty, its names would stop being reserved, and on
the self-build stack, where it holds the agents' own skills under `manifests/self/skills/`, an agent
could draft its own `felix-self`. Otherwise the library's live version answers an unpinned ref, and **an explicit pin to an
operator upload wins**: a ref with `version: 0.1.0` is served `skills/{tenant}/{name}/0.1.0/SKILL.md`
(or the shared `skills/{name}/0.1.0/SKILL.md`) when one exists, whatever the library holds. A pin is
an author choosing reviewed bytes by their key, and a library an agent can draft into must not
answer it. A pin no upload holds still gets the library's live version. `/skill-library` reports
`shadows_operator_upload` on a skill, a version and a save whenever such an upload exists at the
unversioned key or the version in question, so a reviewer knows the name is split before publishing
into it. The object store has no listing, so an upload at a version the library never held is not
flagged — though a ref pinning it is still served the upload.

An `update_skill` call names the version it edits in a required `parent_version` argument, which
must be the skill's newest version; its approval preview shows that version and every file the edit
keeps from it, each by sha256. Because the argument is part of the call, an approval binds it (see
Approval semantics), and a call whose parent is no longer the newest is refused (`parent_changed`)
rather than saved over content nobody read.

**Feedback, improvement and evaluation.** Feedback on a library skill (`submit_skill_feedback`
for an agent, restricted to the library skills in its own catalogue and capped by
`skill_authoring.max_pending`; `POST /skill-library/{name}/feedback` for an operator) changes
nothing: it waits as `pending` until a person decides it. **An agent's feedback cannot start a
rewrite.** Only a person's accept with `improve` lets the worker's `skill_jobs` sweep rewrite the
skill, and **every rewrite lands as a draft** (`source: agent`, author `skill-improver`) in the
review queue, published only through the same gate as any other draft. An agent that absorbed
injected text can therefore get that text in front of a reviewer, and no further.

Both worker-side model calls handle **untrusted skill text**. The improvement
(`FELIX_SKILL_IMPROVE_MODEL`) is handed the current SKILL.md, the feedback and any suggested
patch, each fenced as untrusted data in a region it cannot close, and told to treat them as a
description of the edit and nothing more; its output is validated like any save and must keep
the skill's name, or the feedback fails. The evaluation (`FELIX_SKILL_EVAL_MODEL`) answers each
scenario once without the skill and once with the body fenced as "reference skill instructions
(untrusted data)"; the judge (`FELIX_SKILL_EVAL_JUDGE_MODEL`, the eval judge every rubric uses)
reads the scenario prompt, the criteria and each answer fenced, told all three are data and to
ignore any instruction or score inside them, never sees the skill, and does not know which
answer had it. A judge call that yields no usable score fails the evaluation
instead of falling back to the heuristic, so a broken judge route cannot satisfy an eval gate.
`require_eval` and `min_eval_uplift` are a quality bar, not a security control (see below), and
the security scan still blocks a failing body whatever an evaluation says. Every one of these
calls is metered to the skill's tenant through `record_usage`, and every state change is audited
(`skill_feedback_submitted`, `_accepted`, `_rejected`, `_applied`, `_failed`, `skill_eval_queued`,
`skill_eval_finished`, `skill_policy_updated`, `skill_policy_deleted`).

**Which evaluations count, and why scenarios are pinned.** An evaluation's scenarios are its
test, and generated or default scenarios are written from the skill's own text. For a version an
agent wrote, that text is the agent's, so an agent could steer the test it is graded on: only an
evaluation on the bundle's own `evals/` scenarios counts toward `require_eval` and
`min_eval_uplift` for an agent's version. That holds because an agent cannot write `evals/`:
`library.save_draft` refuses an agent's save whose `evals/` files are not its parent's,
byte for byte, so every `evals/` file traces back to an operator's save. Generated and default
evaluations still run and are shown, marked `counts_for_gate: false` with the reason. A version's
first evaluation fixes its scenario set, and every later evaluation of that version reuses it,
so rerunning cannot shop for a kinder set. The default scenarios quote the skill's description
fenced as data rather than pasting it in as the request. **Residual risk:** the scenario prompt
and criteria still come from text someone wrote — an operator's `evals/` for a counting run — and
the judge is a model; a skill body that persuades the answering model to write an answer that
flatters itself is not something fencing can prevent. Treat an eval gate as a quality bar.

**Who the eval gate binds.** `FELIX_SKILL_PUBLISH_REQUIRE_EVAL` and
`FELIX_SKILL_PUBLISH_MIN_EVAL_UPLIFT` (and a tenant's `require_eval` and `min_eval_uplift`) are a
**hard floor against agents**: an agent's version counts only evaluations on the bundle's own
`evals/` scenarios, and an agent cannot write those. They are a **soft floor against the
tenant's own operators**. An operator writes the `evals/` scenarios a counting evaluation runs,
and can queue another evaluation of a version after one that scored badly. The gate reads the
version's *latest* succeeded evaluation that counts, not its best or its first. So the floor
records that someone with `skills:write` chose to clear it. It does not stop that person. Hold
operators to a bar by reviewing their `evals/` changes, not by this setting alone.

**Publish policy per tenant: tighten only.** `PATCH /skill-library/-/policy` sets a tenant's
`min_quality`, `block_on_advisory`, `require_eval` and `min_eval_uplift`. The policy in force is
the deployment's `FELIX_SKILL_PUBLISH_*` settings (including `FELIX_SKILL_PUBLISH_REQUIRE_EVAL` and
`FELIX_SKILL_PUBLISH_MIN_EVAL_UPLIFT`) tightened by the tenant's values: the higher quality floor,
either advisory block, either eval requirement, the higher uplift floor. A tenant can never lower
the deployment's bar; a looser value is stored and outvoted, and `GET /-/policy` reports `source:
tenant+settings` with the tenant's own `tenant_values`. `DELETE /-/policy` drops the tenant's
values (audited `skill_policy_deleted`). A failing security scan blocks every publish whatever
either says.

**A rollback skips the evaluation requirement.** Rolling back returns to a version that was
already live, often under time pressure, and `require_eval` may have been set after it went
live. So a rollback is judged without `require_eval` and `min_eval_uplift`; the security scan,
bundle validation and the quality floor still apply.

**A publish can name the live version it expects.** `POST .../publish` and `/rollback` take an
optional `{expected_live_version}` — the live version the reviewer was shown, or null for none.
It is checked under the skill row's lock, and a mismatch is 409 `live_changed`, so two reviewers
acting on the same page cannot silently overwrite each other's decision. Without the body the
move behaves as before.

**An agent never builds on a rejected draft.** `update_skill` and the improvement job inherit every
non-SKILL.md file from their parent, which must be the newest version that is *not* a rejected
draft (archived without ever having gone live); `list_skills` and `activate_skill` report that
version as `newest_version`. Naming a rejected draft is refused `parent_rejected`. So a rejected
draft's files — a bad `scripts/` file, say — cannot ride into the next agent draft. An operator's
save keeps building on the absolute newest version, consciously.

Rejecting a draft does **not** cascade to drafts already built on it: a draft saved on top of
one that is later rejected keeps its files and stays reviewable. The impact is low, because a
non-SKILL.md file only ever originates from an operator's save. An agent's draft can carry such a
file only unchanged from its parent, so the descendant's extra files are ones an operator wrote.
Reject the descendants too when the rejected file must not come back.

**Third-party imports.** `POST /skill-library/-/import` (`felix skills add`, `skills:write`) fetches
a skill from GitHub as a draft (`source: import`); it is never published in the same request
(`publish: true` is 422 `publish_not_allowed`), so a person reads it first. `GET
/skill-library/-/browse` needs only `skills:read`, and reaches GitHub on the same token, so the
allowlist bounds it too. The fetch is pinned:

- **Ref resolution.** A ref that is hex (7-64 characters, any case) is a commit id and only that:
  it is never looked up as a branch or tag, and since GitHub's `commits/{ref}` answers for a branch
  or tag of that name too, the commit GitHub returns must start with the hex given, or the ref is
  refused (422 `ambiguous_ref`). A commit id is then accepted only when `compare` puts it on the
  default branch -- itself resolved through `refs/heads/<default>`, so a default branch named like
  a commit is still a branch -- because GitHub serves a fork's commit under the upstream's name
  (422 `commit_not_in_repo`). A compare too large for its size cap, or one GitHub cannot answer,
  fails closed the same way. `refs/heads/<name>` and `refs/tags/<name>` are explicit and resolve
  through the repository's own refs. A bare name is looked up as a tag first, then as a branch, as
  git does; one naming both is refused (422 `ambiguous_ref`), so a branch cannot shadow a release
  tag. With no ref, the default branch is resolved as `refs/heads/<default>`.
- Every file is read by blob id and checked against its git object id, and every commit is
  reported in full, never abbreviated.

The skill's name must be its folder's; an import never takes over a name another source, an agent
or an operator holds (409 `origin_mismatch`), and it is refused where an operator upload holds the
name.

**Lineage taint.** An import, and every version built on one by anyone, carries `lineage_import`:
an agent's or operator's edit, an edit of that edit, an operator save naming no parent, and an
agent's save carrying a copy of a file of imported text anywhere in the tenant (an agent
copying imported text under another name). The copy rule (`skills/copy_rule.py`):

- A file matches by its **bytes** whenever it holds any text at all -- a one-line installer
  copied verbatim is caught however short it is -- and a binary asset whenever it is not empty.
  An empty or whitespace-only file never matches. A tiny file every skill shares (`[]`, a license
  id) therefore byte-matches too, and taints the save that copies it: that fails closed, and is
  accepted.
- A text file also matches by its **normalized text**: Unicode NFKC, format characters (category
  `Cf`: zero-width spaces and joiners, the soft hyphen, the BOM, bidi controls) removed,
  casefolded, every run of whitespace one space, stripped -- for a SKILL.md, over its body alone,
  since the frontmatter names the skill. A copy that only re-spaces, re-cases, swaps
  compatibility forms (full-width letters, ligatures, non-breaking spaces) or inserts invisible
  characters still matches, and so does a SKILL.md body copied under another name. Normalized text
  shorter than 32 characters is not compared: once case and spacing are ignored, that short a
  text matches by coincidence.
- A file the save keeps byte for byte from the version it edits is not a copy, when that version
  carries no imported text: the file was vouched for there. So the agent tools and the feedback
  improver, which carry every other file of the parent along, do not re-mark an adopted skill, or
  an operator's skill holding a file the operator copied.
- **Not caught:** a paraphrase -- the rule compares text, not meaning, and an agent rewording
  imported text launders it; a partial copy; and any change of a non-whitespace character,
  including a look-alike letter from another script. Versions saved before migration `0032`,
  imports included, have no normalized digest and never gain one (a version's rows are
  immutable), so they are matched by bytes alone.

The publish gate judges such a version
as an import whoever saved it: an advisory scan blocks it whatever the policy says (tighten only),
only the bundle's own `evals/` scenarios count toward an evaluation requirement (an import drops
its `evals/`), and an agent's edit of one always waits for a person. Rolling back to one passes the
same gate.

**Adopting an import.** One thing clears the mark, and only going forward: an operator with
`skills:write` adopting a version (`POST /skill-library/{name}/versions/{version}/adopt`, or
`felix skills adopt <name> <version> --reason ...`). It saves a new draft whose files are
byte-identical to that version's, as an operator's (`source: operator`, parent and `adopted_from`
the adopted version, `author` the adopter, `reason` theirs and required), with `lineage_import`
false: the publish gate then judges it by the tenant's own policy, and once it is live
`activate_skill`, `read_skill_file` and `list_skills` no longer mark it untrusted. Versions are
immutable, so the adopted version and everything before it keep the mark (a rollback to one is
judged as an import). Adopt never publishes -- the draft goes live through the ordinary gate --
and it is refused for a version carrying no imported text (409 `not_imported`), an agent's
draft no person has decided (409 `agent_draft`: a person rejects or publishes it first, so an
adopt never vouches for text an agent wrote and nobody read), a rejected one
(`parent_rejected`), and anything but the newest version that was not rejected
(`parent_changed`, whose message names who wrote the newer version rather than inviting an adopt
of it). `save_draft` holds any save carrying `adopted_from` to the same rules (409
`adopt_mismatch` for another parent or other files), whoever calls it. It is audited as
`skill_adopted` (principal, redacted reason, the version adopted and the one saved); under
`FELIX_AUTH_MODE=none`, a development setting, the recorded principal is whatever the request
claimed and means nothing. No agent tool reaches it. The exemption is that one save's: an
operator saving the same bytes through the ordinary save still inherits the mark. Edits built on
the adopted version do not inherit it, an agent's edit that keeps the adopted files unchanged
included (see the copy rule above), while an agent's save copying an adopted file into another
skill is still caught -- an operator vouched for that text once, in that skill. An adopted skill
no longer follows its origin: an update is `not_imported` and a re-import `origin_mismatch`.
Adopt needs `skills:write`, the scope every library write needs. A separate scope would only
matter together with closing the other path an operator already has -- saving imported bytes
into a new skill of its own, which the copy rule does not check for operators -- so both are left
as one possible future step.

**Imported text in front of the model.** When an agent works with a live skill of that lineage,
`activate_skill`, `read_skill_file` and `list_skills` mark their output as relayed from an
untrusted author, and content screening (`spec.content_screening.enabled`) screens that output
exactly as it screens an untrusted tool's: markers, the optional scoring model, quarantine or
block. Operator and agent skills are not screened this way. The wrapper order is unchanged; the
screening wrapper decides per call as well as per tool. A manifest without content screening still
gets a floor for imported text: when its catalog offers an import-lineage skill, the same wrapper,
in the same slot, runs the free injection markers (no scoring model, no decider) over what those
three tools relay of it and quarantines a match; every other tool, and operator and agent skills,
stay unscreened as the manifest says. The compile says so (`felix_imported_skills_unscreened`):
turn screening on for agents that activate imported skills, since the markers alone miss a
paraphrase. In the system prompt's skill catalog, and in `list_skills`, an imported skill is
listed as untrusted (`untrusted="true"` under a preamble in the catalog, `"untrusted": true` in the
listing), and a description carrying the injection markers is withheld from both (the name is
still listed). The skill suggester (`spec.skill_suggestion`) gives the decision model each
skill's description, and in its rerank the start of its body, to rank on. An imported skill
reaches it the way it reaches the catalog: by its listed description only -- withheld when it
carries the injection markers -- quoted and marked as third-party text, and never by its body.
The decision model returns probabilities and nothing else: it runs no tool and the agent sees only
the skill name it hints, so imported text there can bias which skill is suggested, not execute
anything, and a paraphrased injection in a description can still tilt the ranking. The skill's
name -- a validated slug, but one the third party chose -- still reaches it, in both rounds. The
agent still activates a hinted skill itself -- through the screening above. Operator, agent and
bundled skills are described to it as before.

**Allowlist, token and budget.** `FELIX_SKILL_IMPORT_SOURCES` globs the repositories a browse or an
import may name, per tenant: `acme=github:acme/*` serves tenant `acme` only, and an entry with no
tenant serves every tenant (403 `source_not_allowed` otherwise). The tenant is the principal's: an
API key with no `tenant_id`, and an anonymous caller, resolve to `default`, so a `default=` entry
grants every such key -- give keys a tenant before binding sources to one. With
`FELIX_SKILL_IMPORT_GITHUB_TOKEN` set, boot refuses -- unless `FELIX_AUTH_MODE=none` in
development, which is a single person's box (`FELIX_ENVIRONMENT` alone is not: Compose defaults it
to development) -- an empty list, any entry naming no tenant, and any entry whose owner is a glob
(`github:*`): each would let one tenant read whatever the token reads, another org's private
repositories included. Every GitHub call is charged to the tenant's hourly budget and then the
deployment's (`FELIX_SKILL_IMPORT_CALLS_PER_HOUR`, 500, and `_TOTAL`, 4000; 429 `rate_limited`),
which protects the shared token's own GitHub limit (5,000 calls an hour with a token): at the
defaults, eight tenants spending their whole budget fill the deployment's, and one tenant can
spend an eighth of it. A browse of 50 skills is 53 calls -- the repository, the branch, the tree
and 50 SKILL.md heads -- and 54 when it names a bare branch or tag, which is looked up as both; an
import is one call per file plus four (five for a bare name), and an upstream check without a diff
three (four). The buckets live in Redis when `FELIX_REDIS_URL` is set; when Redis is unreachable
each replica falls back to its own in-process buckets, so both budgets are multiplied by the
number of API replicas until it recovers -- size `_TOTAL` for that, or keep Redis up. A browse
lists at most 50 skills and says how many it found.

**The cooldown is time since this tenant first saw these exact files.**
`FELIX_SKILL_IMPORT_MIN_AGE_DAYS`, raised per tenant by `import_min_age_days`, refuses (403
`too_recent`, nothing saved) a skill whose kept files -- by their tree digest -- this tenant first
saw fewer than that many days ago, on any branch, tag or commit, in a browse or an import attempt,
with the cooldown on or off. The clock is Felix's own (`skill_import_sighting`), never a commit
date, which the pusher sets: a commit backdated years is still new to Felix. Any change to a kept
file is a new digest and starts its own clock. Sightings older than 366 days are pruned by the
retention sweep, past the longest cooldown a setting allows.

**Update checks.** An imported skill keeps its origin -- source, ref, commit and kept-file digest --
on its newest version. `GET /skill-library/{name}/-/upstream` (`felix skills diff`) re-resolves
that ref under the same rules as an import's ref (a commit id only on the default branch,
`ambiguous_ref`, the egress-pinned client; a stored ref that is the default branch's name resolves
as that branch, whatever tag shares the name) and against the tenant's allowlist *as it is now*: a
source the deployment has since unbound from the tenant is refused (403 `source_not_allowed`)
before any GitHub call. Checking the stored ref needs `skills:read`; naming another one with
`?ref=` needs `skills:write`, since it chooses what the deployment's token fetches, as an import
does. The answer carries the upstream commit and digest, whether that is an update, when the
cooldown lets it in, and a per-file diff against the live version (the newest when nothing is
live). A skill whose newest non-rejected version was not imported is 409 `not_imported`.

`POST /skill-library/{name}/-/update` (`felix skills update`, `skills:write`) is the import itself
from the stored origin -- a new draft, `unchanged`, `too_recent` or `origin_mismatch`, the stricter
gate and the lineage taint all as above -- and has no publish field: a person publishes it after
review. Its `skill_draft_saved` audit event carries the reason `updated from <source>@<commit>`.
Neither check nor update resolves anything at compile time.

**What a diff costs.** Only changed files are fetched: a stored file's git blob id is computed
from its bytes and compared with the tree's, and of the files that moved only text is read -- a
binary asset is reported by size. Two kinds of bound apply. The *answer* is capped at 16,384
characters of diff per file and 131,072 in total, cut at a line boundary and flagged
(`truncated`, `diff_truncated`); past the total a file is listed without a diff and not fetched.
The *work* is capped separately, because difflib reads both whole sides before anything is cut: a
side over 256 KiB (262,144 bytes) or 4,000 lines is never diffed -- listed by size, `truncated`,
and when it is the upstream side, never fetched -- and the diff runs off the event loop. The diff
is third-party text: it is secret-redacted as a file read is, and `felix skills diff` strips
control and invisible characters from it line by line and writes the file headers itself,
indenting every hunk line, so a line of the file cannot pass for a `---`/`+++` header.

**Checks start the clock, and read-scope checks spend.** A check stamps the sighting of what it
finds, as a browse does: asking whether an update exists starts that update's cooldown,
deliberately, so an operator who sees an update today can take it once the cooldown has run from
today. Like a browse, a check under `skills:read` spends the tenant's GitHub budget, stamps
sightings and -- for the stored ref -- records the upstream state; that is intended, and the
budget is what bounds it. A check of another ref is a what-if and records nothing, and neither
does an import or update of a ref other than the newest version's when nothing changed.

**The periodic check.** With `FELIX_SKILL_IMPORT_CHECK_HOURS` (0 = off, at most 168) the worker
checks every imported skill on that cadence (`skill_upstream_checks`, every ten minutes for what is
due, at most 50 skills a tick, one sweep at a time across workers), so the clock starts without
anyone asking, and records the upstream commit, digest and check time per skill
(`skill_upstream`). `GET /skill-library/-/upstream?refresh=false` and the library detail's
`upstream` read that record and call GitHub not at all. A refused check records its code and keeps
the last good state; a folder past the import caps is recorded as `source_too_large` rather than
offered as an update no import could take; a skill whose head is no longer an import is recorded
as `not_imported` and checked again once it is one. The sweep runs in the worker, on the worker's
environment: give it the same `FELIX_SKILL_IMPORT_SOURCES` and `FELIX_SKILL_IMPORT_GITHUB_TOKEN`
as the API, or it judges origins against another allowlist and reads GitHub with another token (or
none). `felix doctor` notes this whenever the checks are on, and says when the allowlist or the
token is missing from the process it runs in.

**Budget and fairness for checks.** Every GitHub call of a check, an update and the listing is
charged as an import's is. The listing (`GET /skill-library/-/upstream`, `felix skills outdated`)
checks at most 25 skills a page, resolves a repository and ref once for every skill that shares
them, starts no check 30 seconds after its first, and stops at a spent budget with the cursor at
the first skill it did not check (`stopped`); a budget spent before its first check is 429. The
sweep charges the same tenant and deployment buckets but stops at half of each, so it never spends
the hour people asking need. It is fair across tenants: a tenant past its half is left out of the
rest of the tick and the due rows are read again without it, so one tenant's backlog cannot fill
the batch and leave every other tenant unchecked. The deployment's half, or GitHub's own rate
limit on the shared token, ends the tick. Within a tick a tenant's skills from one repository and
ref resolve once. The sweep shares the API's buckets only through Redis (`FELIX_REDIS_URL`), which
the worker already needs.

**Update notifications.** `FELIX_SKILL_UPDATE_WEBHOOKS` binds each tenant to the completion-webhook
endpoints its update events go to: `tenant=endpoint_id`, comma-separated, a tenant named as often
as it has endpoints (`acme=ops,acme=ci,beta=beta-hook`). Each id must be a
`FELIX_WEBHOOK_ENDPOINTS` endpoint whose `tenants` include that tenant, or the boot is refused;
there is no wildcard, so one tenant's skill names and sources never reach an endpoint bound only
to another, and a tenant not listed gets nothing. Both are checked again at send time: an
endpoint unbound from the tenant, or no longer open to it, goes `dead` without a request. One
endpoint bound to several tenants (`acme=ops,beta=ops`) receives all of their events, told apart
only by `tenant_id` in the body, and run and skill events can share one receiver and one secret:
a receiver dispatches on `type`, never on the body's shape.

A recorded check -- the sweep, a check of the stored ref, the listing with `refresh` -- that finds
a kept-file digest the skill's newest version does not hold, and that is not the digest already
queued for the skill, queues one `skill.update_available` event; a check older than the one that
queued what is there replaces nothing. A `?ref=` check is a what-if, is never recorded, and never
notifies. An import records the digest it imported and never queues an event, since that digest
is the newest version's own; it can only supersede one queued earlier. Queuing is all a check
does: the worker's `skill_update_notifications` sweep (every minute; 50 a tick and at most 10 of
one tenant's, so one tenant's backlog or slow receiver cannot hold the others; one sweep at a
time on its own `skill_job_lease` row; a 120 s claim per row that a crashed sweep lets lapse; no
endpoint started after half that) sends it, so no check, listing or import waits on a receiver or
fails with one. Checks run on the API and on the worker, and only the worker sends, so both need the
same `FELIX_SKILL_UPDATE_WEBHOOKS` and `FELIX_WEBHOOK_ENDPOINTS`: an API without the binding queues
nothing from its checks, and a worker without it marks the endpoint `dead` at send time. `felix
doctor` notes this whenever the binding is set.

The event is **metadata only** -- `tenant_id`, `skill`, `source`, `ref`, `current` (`version`,
`commit`, `tree_hash`), `upstream` (`commit`, `tree_hash`, `committed_at`, `first_seen_at`),
`eligible_at`, `checked_at`, and `changed_files` when the check that found it already diffed
against the newest version -- never a file, a diff or a description, because upstream text is
untrusted and the payload leaves the deployment. It is built once when queued, so every retry
sends the same bytes, and signed and delivered as completion webhooks are (below): the same
headers, egress guard, backoff, `FELIX_WEBHOOK_MAX_ATTEMPTS` and dead letter, counted in
`felix_webhook_delivery{kind="skill_update"}`. Its `webhook-id` is derived from the tenant, skill,
digest and the row's queue generation: the same on every retry of one event and to every endpoint,
so a receiver dedupes on it, and new when a digest comes back after another was queued (A, then
B, then A again is three events). Delivery state lives on the skill's `skill_upstream` row
(migration `0030`), and an event that is no longer news is **superseded** -- marked so and never
sent: a newer digest found before it was delivered replaces it, and a delivery that finishes
after that cannot overwrite the newer one; and before each delivery, and on every check that finds
the newest version current, an event whose digest the origin has moved on from, or that the
skill's newest version already holds (someone imported it), is dropped. An event changes nothing
on its own: it does not import, publish, or start a cooldown the check had not started already.
Each queued event is audited (`skill_update_notification_queued`), and queued and superseded
events are counted in `felix_skill_update_notification`.

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

Model, decision-model and embedding calls are the opposite case. They share a connection pool,
which also takes an explicit transport, so when the environment names a proxy
(`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`) Felix skips the pool for them and builds a plain
client that routes through the proxy as before (`felix_ai.wire.transport.model_http_client`).
Provider traffic stays on the proxy route; it only loses connection reuse.

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

The `felix-connectors` plugin's tools (`connectors__*`, transport `connectors`) are the same:
outside the allowlist, screened when the manifest enables it, warned when it does not. Each
declares the hosts it reaches (`Tool.egress_hosts`), the compile refuses one an agent's
`network.allow_hosts` does not cover, and every request is held to the declared hosts. Weather
comes from institutions; **the maps tools return OpenStreetMap data, which anyone can edit** -- a
place name or a road name is user-generated text that reaches the model inside a result line.
Every value is flattened to one capped line, so it cannot forge lines of its own, but only
screening reads what it says: a manifest offering `connectors__geocode`, `_places_search` or
`_route` should enable `content_screening`. The public Nominatim and OSRM instances are paced to
one request a second per process, shared by every tenant on it; a call that would wait more than
five seconds answers `rate_limited`, so one tenant's burst can make another's map calls fail for
a few seconds. A multi-tenant install should point the backends at its own instances, which are
not paced.

The market tools need `FELIX_MARKET_API_KEY`, one operator key shared by every tenant on the
install: one tenant's agent can spend a plan's quota (Alpha Vantage's free plan is 25 requests a
day) and every tenant's calls then answer `rate_limited` until it resets. The key is a credential
like any other -- hydrated from a secrets backend (`MARKET_API_KEY`) and masked in every redaction
sink -- and Alpha Vantage takes it only in the query string, so the HTTP client libraries' request
logging (`httpx`, `httpcore`, `httpx2`), which prints full URLs at INFO, is held at WARNING for
the whole process by `configure_logging`, whatever `FELIX_LOG_LEVEL` says.

Outbound MCP (`spec.mcp_servers`) is the official SDK's client, whose HTTP stack is httpx2 rather
than httpx. It is handed `safe_httpx2_client` (`security/egress_httpx2.py`): the same pin
(`connect_pinned`) and the same per-hop checks (`guard_request`) installed on httpx2's own pool, so an
MCP server URL is resolved once, validated, and dialled at the address that was checked. A failed
MCP call comes back as `[mcp_error] …` tool output rather than an exception, which audit and eval
count as a failed call; its text is screened and masked like any output.

`spec.http_tools` goes through `safe_async_client` like every other outbound call, so it
inherits the pin for free, on the first request and on each redirect hop: the fetch tool
drives redirects by hand rather than letting httpx follow them, so every hop re-enters the
guarded transport and is re-checked against the tool's `path_prefix` as well. httpx's own
`follow_redirects` would have re-validated egress but not the prefix, and one `302` from an
allowed page is enough to leave it.

**The browser pins its navigation host too** (on Chromium). Chromium resolves independently, so the
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
- **Does it set `extract_model`?** Then the tool returns a model's answer to a `prompt`, not the
  page. That saves context and cost; it is not a security boundary. Screening sees the
  extractor's *answer*, which a hostile page can steer, so with screening enabled the marker scan
  also runs over the page as served and a marked page is refused before the extractor reads it.
  The paid `model` scorer still sees only the answer. Extraction failure returns an error, never
  the page. The route is checked against `auth.outbound.providers` and, under a declared
  `max_cost_usd`, must be priced; the call is metered as `kind: web_fetch_extract`. It has its own
  deadline, `FELIX_MODEL_TIMEOUT_SECONDS`, separate from `timeout_ms`.

A fetch tool is **not** replay-safe. A resumed run will not re-issue it, because the model
names the endpoint and a GET that mutates is ordinary on the open web.

### Per-agent allowlist: `spec.network.allow_hosts`

Everything above decides whether an address is safe for *anyone* to reach. `allow_hosts` decides
which hosts *this agent* may reach, and it is the control to reach for when an agent should read
three APIs and nothing else:

```yaml
spec:
  network:
    allow_hosts: [api.github.com, "*.githubusercontent.com", docs.example.com]
```

- **Patterns** are a host, `*.` and a host (any name under it, never the apex itself), or an IP
  literal. No scheme, port or path: those are other grammars and a pattern carrying one would
  match nothing, so validation refuses it. An internationalised name is written in punycode,
  which is what a request carries (`raw_host`, not httpx's decoded `host`).
- **Unset** leaves the agent as it was. An **empty list** allows nothing.
- **Enforced where the model picks the destination.** `http_fetch` checks the URL before it
  opens a client and the guarded transport checks again on every redirect hop, with the same
  `egress_blocked` line as any refusal (`felix_egress_blocked{reason="not in allow_hosts"}`).
  The list is checked *before* anything resolves: a lookup of `<secret>.attacker.example` is a
  leak to that name's nameserver even when the request is then refused.
- **Not the browser.** A manifest with `allow_hosts` and `browser_tools` is refused at write, and
  a child with `browser_tools` under a parent's list is refused at compile. A request interceptor
  cannot confine Chromium: review found a page reaching unlisted hosts through WebRTC over TCP,
  WebTransport in a worker, and worklet module fetches, none of which `route` sees. A browser
  held to a list would look confined and not be; one behind a local proxy enforcing the bound is
  the follow-up (`docs/ROADMAP.md`).
- **Checked against what the manifest names.** An `mcp_servers` or `peers` URL, a container
  `gateway_url`, or an `http_tools` `path_prefix` on a host the list does not allow is refused
  when the manifest is written (`Spec.declared_urls`).
- **A2A peers are pinned.** Under a list, a `peer__*` tool loses its `manifest` argument: the
  agent that answers on the far side compiles there with no bound, so the model may not choose
  which one it is.
- **Children answer to it.** A sub-agent or `task` child compiles under its parent's list as well
  as its own, and a manifest URL outside a parent's list fails that compile. A child two parents
  share is rebuilt under each one's list, never reused from a looser one. A background `task`
  child runs in the worker on a run of its own, so the lists above it are stored on the durable
  run (`egress_ceilings`), re-validated when it resumes, and bind its tools there.
- **Not covered.** The list bounds the requests Felix itself sends for the model. It does not
  reach a process that opens its own sockets, nor what a server does on the far side:
  - `shell_tools` and sandboxes: the runner's or sandbox's network is the boundary (*Shell tools*);
  - stdio MCP servers, which run on the API host with its network (`FELIX_MCP_STDIO_ALLOWED_COMMANDS`);
  - a remote MCP server or A2A peer whose own tools fetch: it is listed, so it is trusted, and
    what it reaches is its operator's control;
  - container gateways' and sandboxes' outbound traffic;
  - tools a plugin binds (`deps.extra_tools`), which are not handed the agent's bound.

  Operator traffic -- hooks, completion webhooks, push, model providers -- is not the agent's and
  is not bound by it. Web search reaches the operator's search endpoint; following a result is a
  fetch, which is bound.
- **A host is not a confinement on a shared host.** `*.github.io`, `*.vercel.app`, `*.co.uk` or
  `docs.google.com` each admit pages anyone can publish to or post through (a form endpoint is a
  GET away). Only `*.<top-level domain>` is refused. On a multi-tenant host, pair the list with an
  `http_tools` `path_prefix`, or treat the list as a reachability control rather than an
  exfiltration one. A prefix is compared with the path httpx actually sends: `..` segments are
  resolved first, and an encoded dot segment or slash (`%2e%2e`, `%2f`), a backslash, or a `..;`
  segment is refused.

**Every browser, list or not, shuts the channels its interceptor cannot see** -- each is a way
past the SSRF check that guards every request. The guard sits on the whole browser context, popups
included, and on the page's WebSockets (a `ws`/`wss` URL is checked as the `http`/`https`
handshake it is). Service workers are blocked. `RTCPeerConnection`, `WebTransport`,
`sharedStorage` and `Worklet.prototype.addModule` are removed (not writable, not configurable) from
every page, frame and popup -- a TURN server over TCP needs the API removal, and a worklet's module
fetch never reaches `route`. On Chromium (`binding: chromium`, the default) WebRTC's UDP is also
disabled and the navigation host pinned; those are Chromium launch flags, and another engine gets
the script removals and the interceptor only. A model URL containing a backslash, whitespace or a
control character is refused, since Chromium's parser and the checker's disagree on them.

**Still open: a page's workers.** An init script does not run in a dedicated worker, and
Playwright's interceptors do not see a worker's sockets. A worker's `WebTransport` sends QUIC over
UDP, and a worker's `WebSocket` opens a TCP handshake, to any address -- loopback and private ones
included -- outside the SSRF guard. Where browser agents run on shared infrastructure, give the
process that runs Chromium (the API, or the worker for durable runs) a network policy that refuses
UDP and private and loopback TCP. Removing workers would close both and break pages that use them;
a browser behind a local proxy enforcing every socket is the follow-up (`docs/ROADMAP.md`).

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

### Credentials the model never holds: `spec.network.credentials`

A model can drive `http_fetch` at an authenticated API only if something adds the key, and every
place a model could put one -- an argument, a URL, a header it writes -- is a place it could copy it
from. So the key never reaches it. The operator registers it, per tenant, against one exact host:

```bash
FELIX_EGRESS_CREDENTIALS='{"github": {"host": "api.github.com", "path_prefix": "/repos/acme/",
  "secret": "secret:GITHUB_TOKEN", "scheme": "Bearer", "tenants": ["acme"]}}'
```

and a manifest names the ids it may send:

```yaml
spec:
  network:
    allow_hosts: [api.github.com]
    credentials: [github]
```

- **Attached in transit.** `GuardedAsyncTransport` adds the header (`Authorization` unless
  `header` says otherwise, prefixed by `scheme`) *after* the host check, to a request whose origin
  is the credential's -- its host exactly, its port (`port`, else the scheme's default) -- whose
  path is inside `path_prefix` as httpx sends it, and whose scheme is https (plain http only in
  development with `allow_insecure`). Each redirect hop arrives as a request of its own and is
  judged again, so a hop to another host or out of the prefix goes without it.
- **Bounded.** A manifest with `credentials` must set `allow_hosts`, naming each credential's host
  exactly, so what the agent reads with the key can only go where its list allows. An id the
  operator has not registered for the tenant, or a host the list does not name, fails the compile.
  A wildcard host and the transport's own headers (`Host`, `Content-Length`, `Cookie`, …) are
  refused at boot. Children do not inherit a parent's credentials.
- **Never shown.** The value is resolved when the agent compiles, before the masking wrappers take
  their list, and registered for masking, so a server that echoes the header back reaches the
  model as `[REDACTED]` on the first request a process makes as on every later one. It is in no
  prompt, argument, tool result or log line; the log records which credential id was attached to
  which host. A value that masking would miss or that the wire would refuse in an error quoting it
  -- under 8 characters, surrounding whitespace (a trailing newline from `echo key | …`), a
  control character -- fails the compile, as does a secret the backend cannot resolve, both with
  the credential's id and not its value (`422` on `/chat`).
- **Stripped where it does not belong.** The transport removes a bound credential's header from
  any request it does not cover, so a client that copies headers onto a redirect hop cannot carry
  the key past its host or prefix; a client carrying credentials must follow redirects by hand
  (`safe_async_client` refuses `follow_redirects=True` with them). A doubly-encoded path (`%25`)
  gets no key.
- **Who may use one.** Any manifest in a tenant the credential is registered for may name it: a
  tenant user who can write manifests can bind it, and only its host, port and prefix bound where
  it goes. A credential open to `"*"`, or to `default` -- the tenant anonymous callers and
  `FELIX_AUTH_MODE=none` run as -- is usable by anyone who may call a manifest naming it. Plain
  http follows `FELIX_ALLOW_INSECURE` in development: the key goes in clear to an `http://` URL on
  its host there, never in production.
- **Not yet covered:** MCP HTTP servers still resolve their `Authorization` into the compiled tool
  (`docs/ROADMAP.md`), and stdio children hold env secrets, where only exact-string masking stands
  between a key and the model.

### Completion webhooks

A durable run with `spec.execution.webhooks` has its outcome POSTed by the worker when it reaches
a terminal status. The request carries run output, so the endpoint list is the operator's, not the
manifest author's:

- **Ids, never URLs.** `FELIX_WEBHOOK_ENDPOINTS` maps an id to `{url, secret, tenants, private?}`
  and a manifest names ids. A tenant-supplied URL on a path carrying run output would be an
  exfiltration channel an SSRF check does not address — the destination is public and allowed.
- **Tenant allowlist.** `tenants` is required: a list of tenant ids, or `"*"` written out for an
  endpoint every tenant may name. An open endpoint lets any tenant's run arrive signed with its
  secret, so a receiver behind `"*"` must check the payload's `tenant_id`. Unregistered
  and not-yours both answer `unknown webhook endpoint: <id>` (`422` at enqueue), so the message is
  not a registry oracle. An endpoint removed or narrowed after a run started goes `dead` for it.
- **Signed.** Standard Webhooks: `webhook-id` (`<run id>:<endpoint id>`, stable across retries
  for dedupe), `webhook-timestamp`, and `webhook-signature: v1,<base64 HMAC-SHA256>` over
  `id.timestamp.body`. A `whsec_` secret is base64-decoded; any other value is used as bytes.
  A malformed `whsec_` value fails the boot. `secret` accepts a secrets-backend ref; every
  endpoint secret, literal or resolved, is registered for masking at boot in each process.
- **Egress.** Delivery goes through the SSRF guard unless the endpoint says `private: true`, the
  operator's explicit opt-out for an internal receiver. `https` is required outside development
  with `FELIX_ALLOW_INSECURE`. Redirects are not followed: a `3xx` is a failed attempt.
- **Retry and dead letter.** Non-2xx or a transport error backs off (1m doubling to 1h) and is
  `dead` at `FELIX_WEBHOOK_MAX_ATTEMPTS` (8), recorded on the fiber row itself; each attempt is
  bounded end to end by `FELIX_WEBHOOK_TIMEOUT_SECONDS` (10, at most 60), the response body is
  never read, and every attempt is counted in `felix_webhook_delivery{kind="run"}`. A sweep stops
  starting deliveries, and endpoints within one, after half the 120 s claim, so a slow receiver
  delays the rest rather than doubling them; the timeout's bound keeps one attempt inside that.

### Inbound triggers: `POST /triggers/{id}`

A signed event from elsewhere starts a durable run (`felix/triggers.py`,
`apps/api/src/felix_api/routes/triggers.py`). The operator registers each trigger in
`FELIX_TRIGGERS`; nothing a tenant controls creates one.

- **Authentication is the sender's signature, not a bearer.** The path is exempt from bearer auth
  only while `FELIX_TRIGGERS` is set, and only under `/triggers/`. The route reads the body (it must,
  to check an HMAC over it; the 1 MiB body limit bounds the read), verifies it, and parses nothing in
  it before that. Schemes: Standard Webhooks (`webhook-id.timestamp.body`, HMAC-SHA256, a 5-minute
  window either way; the same code Felix signs its own completion webhooks with,
  `security/standard_webhooks.py`) or GitHub (`X-Hub-Signature-256` over the body). Comparisons are
  constant-time. An unknown trigger id and a bad signature get the same `401 invalid_signature`, so
  ids cannot be probed; the reason goes to the operator's log.
- **Keys.** `secret` must be `secret:NAME` (resolved through the secrets backend and registered for
  masking) except on a development install -- `FELIX_ENVIRONMENT=development` *and*
  `FELIX_AUTH_MODE=none`, since the environment alone defaults to development; a literal fails the
  boot anywhere else. A key shorter
  than 16 bytes -- as written, after `whsec_` decoding, or as resolved -- is refused: an empty key
  (an unset variable behind `secret:NAME`, an empty file, a bare `whsec_`) is a signature anyone
  can compute, and is answered `503 trigger_unavailable` before any signature is checked. `secret`
  may list several, so a secret is rotated by adding the new one and later dropping the old.
  `whsec_` values are base64-decoded for every scheme, whitespace dropped (a value read back from
  a secrets manager often ends in a newline). A trigger whose secret does not resolve answers `503`
  where an unknown id answers `401`, so a broken trigger's id is visible to a caller: kept so the
  operator sees the failure on the sender's delivery log.
- **Replays start nothing -- for `FELIX_IDEMPOTENCY_TTL_SECONDS`.** A delivery is claimed in a store
  of the triggers' own (chat's `Idempotency-Key` traffic cannot evict it), and a repeat gets the
  first answer back. The key is something the signature covers: the signed `webhook-id`, or for
  GitHub the body's digest, since GitHub signs neither `X-GitHub-Delivery` nor `X-GitHub-Event`.
  Standard Webhooks replays are also bounded by the timestamp window; GitHub signs no timestamp, so
  a captured GitHub delivery replayed after the claim expires (default 24 hours) starts a new run.
  The window is 300 seconds either side of now, so a delivery verifies for 600: with
  `FELIX_TRIGGERS` set, boot refuses a `FELIX_IDEMPOTENCY_TTL_SECONDS` below that, which would let a
  claim expire while its delivery still verified.
  Without Redis the claim is per process, so behind several replicas a replay is caught only by the
  replica that saw it, and the in-process store keeps at most 50,000 claims, oldest dropped first
  (a Redis eviction policy that drops keys does the same): a busy hook can age a claim out before
  the TTL. Ignored deliveries are claimed too.
- **The payload is untrusted and never the instruction.** The run's user turn is the operator's
  `prompt`. `{trigger}` is the trigger's id; `{event}` is filled only with an event type the
  trigger lists in `events` (text the operator wrote), else `unknown`; `{delivery}` only with a
  short `[A-Za-z0-9_-]` token, else a pointer to the tool. The body reaches the model only as the
  output of the `trigger_event` tool, which the manifest must list (a trigger whose manifest does
  not is refused with `503`). That output is stamped untrusted, so a manifest with content screening
  on screens it as it screens MCP or browser output. The free marker scan runs at intake whatever
  the manifest says, and a match withholds the whole payload (counted as
  `felix_content_screening{tool="trigger_event",action="quarantine"}`).
- **`events` is a filter, not a guarantee, for GitHub.** The event header is unsigned: a sender
  holding a signed body could label it with another event type the first time it is delivered.
  The payload, not the label, is what the model reads. GitHub's `ping` starts no run unless `events`
  names it.
- **The run is the trigger's, not a person's.** It runs as `trigger:<id>` with the trigger's
  `scopes` (none by default), on a fresh thread. The manifest is resolved, admitted (inbound auth)
  and pinned inside the trigger tenant's context, so row-level security answers for that tenant:
  a manifest that requires scopes refuses a trigger not granted them, and one that limits
  `auth.inbound.schemes` must name `trigger`. Inbound auth is applied again when the worker resumes
  the run. Its tools are governed exactly as a `/chat` run's.
- **Bounded.** A body is at most 256 KiB (inside the core body limit, which a plugin may raise).
  Each trigger starts at most `rate_limit` runs a minute (default 60) per process without Redis, so
  `rate_limit` x replicas there, and with Redis per fixed window, so up to twice that across a
  window's edge; only a new delivery that would start a run is counted -- not an unsigned request, a
  replay or an ignored event -- so one captured delivery cannot spend a sender's budget. The
  middleware's per-address limit still applies in front.
- **Retention.** The payload is kept in the run's durable state until the fiber is swept
  (`FELIX_FIBER_RETENTION_DAYS`), and, once the model has read it, in the thread's session log as
  a tool result, kept as long as the thread's sessions are (`FELIX_SESSION_RETENTION_DAYS`). Anyone
  who may read the tenant's sessions may read it there.
- **Results go out the usual way:** the manifest's `spec.execution.webhooks` announces the run's
  end, so an event in can become a result out.
- **A trigger may start a workflow run instead** (`workflow` in place of `manifest` and `prompt`).
  `inputs` maps each workflow input to a JSON Pointer into the payload (`""` is the whole of it),
  `$event` (a type `events` lists — mapping it without `events` fails the boot) or `$delivery`
  (the short token). Each value must be what the workflow declares for that input — `type`,
  `enum` (compared by JSON type, so `true` is not `1`), an object's `required` and `properties`,
  an array's `items` (`ports.value_mismatch`, deliberately partial) — and nest no deeper than 32
  levels, or the delivery is `422 invalid_payload` and starts nothing; so is a quarantined payload.
  These are checked before the rate limit, so a delivery that starts nothing spends none of it,
  and the 422 is kept as that delivery's answer. A workflow that is missing, does not parse,
  declares inputs the trigger does not map, or is no longer at a pinned `revision` is the
  operator's problem: `503 trigger_unavailable`, not kept, so a redelivery after the fix runs.
  **A string, object or array from the payload, and `$delivery`, are the sender's text** and are
  listed on the run as `untrusted_inputs`. **Every agent step of a trigger-started run fences what
  it is given** — a later step's output included, since an agent that read the sender's text can
  repeat it, and a run whose mapped inputs are all numbers included, since a tool step can fetch
  the sender's text: each value is wrapped in a tag made new for that prompt
  (`<data-<12 hex> name="…">…</data-<12 hex>>`), so a value cannot forge its close, and the prompt
  ends by naming the tag and saying what is inside is data, not instructions. On every other run
  too, a value an agent step is given from another step's output is fenced the same way: a step's
  output is only as trustworthy as what that step read. Only the inputs a person started the run
  with reach an agent's prompt as written. A fence is a
  convention a model is told to honour, not a boundary it cannot cross: keep `spec.inbound`
  screening on manifests a triggered workflow reaches, and give the trigger only the `scopes` its
  steps need. A tool step receives mapped values as arguments, screened as every tool step's are.
  The run is `trigger:<id>`'s with the trigger's `scopes`, its manifests checked and pinned at the
  start; it is audited as `workflow_run_started` with trigger `trigger:<id>`. **A workflow trigger
  must pin the `revision` it runs** (boot fails without one): anyone with `workflows:write`, or an
  agent saving for its person (`spec.workflow_authoring`), can save the workflow again, and an
  unpinned trigger would hand an outside sender whatever was saved last, under the trigger's
  scopes. After each save the trigger refuses until the operator reads the new revision and pins it.

### Chat channels: Slack (`felix-channels` plugin)

An optional plugin (`packages/channels`, `uv sync --extra channels`, never imported by core) lets
people talk to an agent from Slack. The operator binds each Slack app to one tenant and manifest
in `FELIX_CHANNELS`; without the plugin installed the setting is inert.

- **Authentication is Slack's signature.** `POST /channels/slack/{id}/events` takes no bearer
  (the plugin's self-authenticating mount, `/channels/...` and nothing beside it). Each request is
  verified against the app's signing secret (`v0=` HMAC-SHA256 over `v0:{timestamp}:{body}`, a
  5-minute window either way, constant-time) before the body is parsed; an unknown id answers as a
  bad signature does. A signing secret that does not resolve, or resolves under 16 bytes, answers
  `503` and verifies nothing. Secrets are `secret:NAME` except on a development install
  (`FELIX_ENVIRONMENT=development` and `FELIX_AUTH_MODE=none`).
- **Who is talking.** A message runs as `slack:<team>:<user>` with the channel's `scopes` (none by
  default), through the manifest's inbound auth (a manifest limiting `auth.inbound.schemes` must
  name `slack`). Every sender carries the same scheme and scopes, so a manifest that refuses one
  refuses all: that is the channel's misconfiguration, logged as `channel_misconfigured`,
  acknowledged to Slack (not retried) and said in the conversation at most once a minute. Anyone in
  the bound workspace may talk to the agent unless `channels` or `users` narrow it. Nobody else
  may: a request from another workspace is ignored, and so is a person from another organisation
  in a Slack Connect channel (their `user_team`/`source_team` is not the bound workspace, or a
  shared channel does not say) -- a sibling workspace of the same Enterprise Grid org included.
  There is no per-user account linking: everyone in the workspace has the same scopes.
- **What it answers.** An `app_mention`, and a message in the app's DM. Edits, deletions and any
  bot's messages -- the app's own replies among them -- are ignored, so it never answers itself.
  The message text is the user turn, screened by the manifest's inbound screening as `/chat` input
  is, when the worker runs it.
- **Retries and limits.** Slack retries a request it was slow to get an answer to; each event is
  claimed once by its signed `event_id`, in a store of the plugin's own, so a retry starts nothing
  -- across replicas only with Redis (`FELIX_REDIS_URL`); without it the claim is per process. Each
  sender may make at most `user_rate_limit` attempts a minute (default 10, or `rate_limit` when
  that is lower), and the channel as a
  whole `rate_limit` (default 60), so one person cannot spend everyone's budget; a sender refused
  at their own limit spends nothing of the channel's (per process without Redis). One run per Slack conversation at a time: a message while one runs
  is answered "still working", at most once a minute per conversation.
- **Threads.** A Slack thread is one Felix thread (`{tenant}:channel:{id}:{conversation}:{thread}`,
  every part validated), so the agent sees the thread's earlier turns; a DM's main line is one
  running conversation.
- **The answer goes back from the worker**, through the core `register_run_finished` plugin hook,
  when the run ends: the final answer, or a fixed note for a failed or expired run -- never the
  run's error text. The answer is escaped as Slack asks (`&`, `<`, `>`), so it cannot ping
  `<!channel>` or dress a link as something else, cut at 39,000 characters (never mid-entity), and
  posted as plain text (`parse: none`, `link_names: false`, so no `@here` becomes a ping) with
  unfurling off, so Slack fetches no URL the model wrote. Slack is called through the egress guard,
  each post bounded to 8 seconds within the hook's 10. At most once: a worker that dies between
  saving the run's end and posting does not post, and a Slack outage loses that reply.
- **The hook sees its own runs only.** Core hands a `register_run_finished` hook only the runs its
  plugin tagged in `start_durable_chat(origin=...)`; no plugin sees another's runs or an untagged
  run's answer.
- **Masking.** The API resolves every channel's secrets at startup and registers them for masking,
  development literals included. The worker -- where channel runs execute -- runs no plugin startup
  hooks: there the bot token is registered from the first reply it posts, and the signing secret
  never. Name both in `FELIX_SECRET_NAMES` (`SLACK_SIGNING_SECRET,SLACK_BOT_TOKEN`) and every
  process resolves and masks them at boot.
- **Not yet:** approvals and `ask_user` as Slack buttons (a run waiting on an approval waits for
  the web client or the API); streaming partial answers; durable, retried delivery.

## Shell tools

`spec.shell_tools` execs an argv in the `FELIX_WORKSPACE_ROOT` checkout, in a separate
`felix-shell-runner` when `FELIX_SHELL_RUNNER_URL` is set (below), or in the scope's sandbox under
`FELIX_WORKSPACE_BACKEND=hosted`. **Anywhere but a development box it must be one of the two**:
a deployment with `FELIX_SHELL_ALLOWED_COMMANDS` set and neither refuses to boot, a manifest
binding a shell tool there is refused at write and at compile, and the call itself refuses rather
than exec in the API. The hosted backend serves `workspace.scope: deployment` from this host, so
for that scope only the runner counts — at write, at compile and per call. A development box is
`FELIX_ENVIRONMENT=development` *with* `FELIX_AUTH_MODE=none` (`make dev`), for the reason skill
import's token gives below: Compose defaults `FELIX_ENVIRONMENT` to development, so it alone says
nothing. Only there does a command run as a child of the API process, where the code it runs can
read the API's environment — a laptop's trade, never a deployment's. There is no shell interpreter: `&&`, `|` and `;` are literal arguments. The
manifest's `commands` are argv prefixes (`git status` covers `git status --short`, not
`git push` and not `git -c … status`), and every one must be covered by a prefix in
`FELIX_SHELL_ALLOWED_COMMANDS` — checked at manifest write, at compile, and per call.
Empty, the default, refuses every shell tool. The child inherits only `PATH`, `HOME`, `LANG`,
`LC_ALL`, `TZ`; `cwd` resolves under the workspace root with no symlink at any component; the
run is killed at `timeout_ms`; output is capped and marked truncated.

The run is a process group of its own: at `timeout_ms`, past `MAX_TOTAL_OUTPUT_BYTES`, or
when the calling request is cancelled, the whole group is killed — the pytest a test script
spawned dies with the script rather than holding the pipes open. Output is read in bounded
chunks and only a tail is kept, so a command that prints without end costs the API a fixed
amount of memory and then its life. A refused argv or `cwd` is a `permission_denied` tool
error counted as `felix_shell_denied`, so a model probing the allowlist is visible on the same
dashboards a policy deny is. A manifest that binds a shell tool may not allow anonymous callers
outside development — the same rule `cowork.yaml` records for its client shell — because the
approvals that would gate the tool are anonymous too when the caller is.

**Background commands** (`background: true`, `tools/shell_background.py`) pass every check above
before they start — both allowlists, `cwd`, command screening, and the refusal to exec in the API
off a development box — and run in the same place a foreground call would: the API on a
development box, the runner, or the scope's sandbox (`tools/shell.py:shell_place`, the one choice
every shell call makes, so an id is looked up where it was made). `shell_output` and `shell_kill`
are refused in the API off a development box too. Each command runs under a supervisor (the
module run as a script, detached from the call) that holds the foreground's limits: the group is
killed at `timeout_ms`, past `MAX_TOTAL_OUTPUT_BYTES` (written to disk no further than that), and
on `shell_kill`, which leaves a marker the supervisor acts on so nothing is signalled from outside.
A command counts as running while its supervisor holds an `flock` on the entry's `alive` file, not
by pid or by `status.json`. At most 4 run per workspace root, 8 per tenant across its roots, and 32
on the host, counted and started under one lock; 16 are kept per root, each for an hour after it
ends, and every root's are swept on each start. State files are read to 64 KB at most, and a lock a
command holds is waited on for 10 seconds, not forever. Their state is in a directory under the
system temp directory, one per workspace root, refused unless it is this user's and closed to
others; an id is looked up only under the root of the call's scope. **A runner must run under an
init** that reaps orphans: each supervisor is reparented to PID 1 when it ends, and a runner that
is PID 1 itself keeps every one as a zombie, counted against its process limit. The builder stack
sets `init: true`; a runner deployed any other way must do the same (`docker run --init`, or tini
as the entrypoint). The hosted sandbox image runs tini. What this does not hold, shared with
foreground commands: the supervisor, the state and the command all belong to one user, so a
command can defeat its own accounting. It can kill its supervisor (the entry reads `lost`, no
longer counts against any cap, and the command is no longer bounded by `timeout_ms` — as a
foreground command can leave its group with `setsid`), replace its `alive` file and write its own
outcome (it then reads as ended and frees its slot, though its supervisor still kills it at
`timeout_ms`), or hold a lock on `alive` itself after its supervisor ends (it then reads `running`
until its container restarts, holding its slot), or hold the host's start lock, so every start on
that host is refused as "being started" until it lets go (a start itself holds that lock while its
supervisor detaches, normally milliseconds and at most 10 seconds). And wherever scopes share a host (the `local`
backend, and the shell runner, where every tenant's commands run as one user) one scope's command
can read and write another's state — its output, its status, its kill marker — as it can that
scope's files. The caps bound commands that keep to the rules; the runner or sandbox is the
boundary for the rest, and a deployment that does not trust its tenants binds shell tools only
under `hosted`.

What the allowlist cannot bound, and a deployment that binds a shell tool must hold true on its
own:

- **A listed command runs repository code.** `./scripts/test.sh` imports whatever the agent
  wrote; `uv run ruff` reads the workspace's `pyproject.toml`. Wherever the command execs is the
  boundary — not `HOME`, which only decides which config files a tool reads silently
  (`~/.gitconfig` credential helpers, `~/.netrc`, `~/.config/gh`). That place holds no cloud
  credentials, no Docker socket, and no credentials in its user's home.
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
- **The model cannot write what decides what git runs.** `write_file`, `edit_file` and
  `multi_edit` refuse any path inside `.git` and any `.gitattributes` (`reserved path`), as
  `delete_file` and `rename_file` already refuse `.git`. Hooks, `.git/config` and the attributes
  that name filter, diff and merge drivers are the repository's own, not the agent's.
- **And the git it runs is disarmed too.** For a `.git` that arrived some other way -- a clone,
  or a command that wrote it -- the child's environment overrides the settings that make git run
  a program (hooks, fsmonitor, editors, pager, signing and the signing programs, `core.sshCommand`,
  `credential.helper`, `diff.external`; `tools/shell.py:_GIT_DISARMED`) for every git it starts,
  named in argv or run by a script, through `GIT_CONFIG_COUNT` (git 2.31+) and `GIT_EDITOR`,
  `GIT_SEQUENCE_EDITOR` and `GIT_PAGER`.
- **A git allowlist is not confinement.** The arguments after an allowlisted prefix are the
  model's, and git has options that run a program or read and write paths outside the workspace;
  drivers named in `.gitattributes` cannot be switched off by a fixed key. The runner (below) is the
  boundary for every shell command, git included -- as it already is for `make test`, which runs
  any code the agent writes. List only the git commands an agent needs, and treat each as running
  code with the runner's access.
- **Run locally, the child can read the API's own environment.** With `FELIX_SHELL_RUNNER_URL`
  unset — `make dev`, a plain `make up`, any deployment that does not set it — the child inherits
  only the five variables above, but it runs as the API's user in the API's PID namespace, so it
  may read `/proc/<api pid>/environ`: every secret the `env` backend resolves from there, the
  GitHub token included. Withholding a variable from the child is not withholding it from code
  the child runs. The shell runner below is what closes this; nothing else does.
- **Scoped, not separate.** `cwd` resolves under the call's workspace scope
  (`spec.workspace.scope`: the thread's directory by default, the tenant's, or — for
  FELIX_WORKSPACE_DEPLOYMENT_TENANTS only — the whole root), and the runner is told the same scope.
  On the `local` workspace backend that keeps one tenant's *paths* out of another's; it does not
  separate *processes*. Every command still runs as the same user on the same host, so code one
  tenant's shell runs can reach another's files outside the path checks. **With
  `FELIX_WORKSPACE_BACKEND=hosted` it does:** a scope's commands run in that scope's own sandbox (a
  Firecracker microVM with the internet off, through `deploy/cloudflare/workspace-gateway`), after
  the same allowlist and screening here, and never fall back to this host. A deployment that admits
  tenants it does not trust binds `shell_tools` only under `hosted`.

### The shell runner

Set `FELIX_SHELL_RUNNER_URL` (with `FELIX_SHELL_RUNNER_TOKEN`, 32+ characters; `validate_runtime`
refuses the URL without it) and the API stops execing. It still makes every check above — the
manifest prefix, the operator prefix, `cwd` confinement, argument bounds, and the governance
stack's command screening, which wraps the tool and so runs before the executor is reached —
and then sends `{argv, cwd, timeout_ms, stdin}` to `POST /run` on `felix-shell-runner` with the
token as a bearer. The runner compares the token in constant time before reading the body,
checks the argv against **its own** `FELIX_SHELL_ALLOWED_COMMANDS` and the `cwd` against **its
own** `FELIX_WORKSPACE_ROOT`, and execs through the same code the local path uses (scrubbed
environment, process-group kill at the deadline, past the output budget and when the API's
request goes away, bounded tail). The result reaches the model in the same shape as a local run.

**It fails closed.** A runner that is unreachable, answers an error or a malformed result,
redirects, or has not finished answering within `timeout_ms` plus 30 seconds fails the call with
a `transport_unavailable` tool error that says the command did not run. That deadline bounds the
whole exchange — connect, headers and body — so a runner that trickles a byte at a time cannot
hold the call open past it. A result whose `exit_code` is outside -255..255 or whose
`duration_ms` is negative or longer than any timeout allows is malformed. Nothing falls back to a
local exec. The error names neither the URL nor the token.

The URL is the one internal destination the egress guard does not cover — it would refuse a
private Compose hostname, which is its job. It is exempt because it comes from `Settings` alone:
no manifest field and no model argument can name it. The API's client follows no redirects and
reads no proxy from the environment, and the API reads at most 2 MiB of a runner's answer and
validates its fields, so a runner the workspace code has replaced can lie about a command's
output — which that code could already do — and nothing more.

**On the Compose builder stack** (`compose.self.yml`) the runner is the `shell` service: the
builder image running `felix-shell-runner`, mounting only the workspace volume, with an
environment of the allowlist, the workspace root, the token and the repository to clone — no
`GITHUB_MCP_TOKEN`, no model key, no database or Valkey URL, no API keys. It is on a network
of its own that the api and worker join and Postgres, Valkey and MinIO do not, so code in the
workspace cannot reach the unauthenticated Valkey a Taskiq message would be enqueued on. It
drops every capability, sets `no-new-privileges`, and may run at most 512 processes
(`pids_limit`), so a fork bomb in the workspace stops in that container. The overlay also
resets the host ports the base file publishes for Postgres, Valkey and MinIO: on Docker Desktop
`host.docker.internal` reaches the host's loopback, which would otherwise hand `shell` the Valkey
its own network keeps it from. It owns the workspace too: the clone, the
fetch and the venv sync run in that container, and api and worker skip them
(`FELIX_SELF_PREPARE_WORKSPACE=0`), because `git fetch` runs hooks the agent can write.

What is true there now, and what is not:

- Code the agent runs through an allowlisted command cannot read the API's or the worker's
  environment. Their processes are not in its PID namespace, and the container it runs in was
  started with nothing in its environment worth taking.
- **It runs as the same uid** (`felix`, 10001) as api and worker. Separation comes from the
  container — PID namespace, environment, mounts, network — not the uid. Both sides must write
  the shared workspace (the API's `write_file` and `edit_file`; the runner's `git commit` and
  venv), and a second uid would need group-writable files under a shared umask plus git's
  `safe.directory` on both sides, where `publish_commits` deliberately ignores system and global
  git config. A second uid would protect nothing the container does not: the only surface both
  share is the workspace, which both may write by design.
- The token is readable by the code the runner execs (`/proc/<runner pid>/environ` is in its
  namespace). It stops other containers calling `/run`; it grants that code nothing new.
- The workspace is still shared, and code in `shell` can change it while the API is using it —
  from a process that outlives the tool call. Anything the API does *with* the checkout must
  treat it as agent-written. `read_file`, `write_file`, `edit_file`, `multi_edit`, `delete_file`,
  `rename_file`, `list_dir`, `search_files`, `glob`,
  context-file loading (`AGENTS.md`, `system_prompt.files`) and the shell tool's `cwd` check
  never open a workspace path by name: they walk it from a descriptor of the root, one
  component at a time with `O_NOFOLLOW`, and refuse a symlink at any component, pointing out of
  the workspace or back into it. A directory swapped for a link to `/proc/self` or `/data`
  between a check and an open is therefore refused, not followed into the API's own
  `environ`. `list_dir` reports a link as `symlink`; `search_files` does not descend into or read
  through one. A hard link cannot stand in for one: the workspace is its own volume, and a hard
  link cannot cross filesystems. `publish_commits` reads the checkout with hooks, fsmonitor,
  external diff and textconv switched off; the git it runs still opens paths under `.git` by name
  and follows symlinks there, and reads only what parses as a git object. Code the agent writes
  is never executed in the API's container.
- The runner has internet egress (`git fetch`, `uv sync`), as the API's container did. That
  includes the cloud metadata address (`169.254.169.254`): **a builder host must carry no cloud
  instance role, service account or metadata credentials**, or the workspace's code can fetch
  them. An egress proxy that allowlists the git and package hosts would close this; it is not
  built. On Docker Desktop, any other service listening on the host's loopback is reachable from
  `shell` through `host.docker.internal` — only the stack's own stores are unpublished.

With `FELIX_SHELL_RUNNER_URL` unset — `make dev`, the base `make up`, the Helm chart, any
deployment that does not run a runner — the local-exec caveat above still applies in full. The
Helm chart deploys no shell tools and no builder: it sets no `FELIX_SHELL_ALLOWED_COMMANDS` and
mounts no workspace, so it has no runner to add.

### Skill scripts

`spec.skill_scripts` binds `run_skill_script(name, script, args)`, which runs a script from a skill's
`scripts/` (agentskills.io). A script is code, so it runs where a shell command would: the shell
runner, or a development box. The hosted backend's sandbox is refused at write and at compile for
now. The boot, write, compile and per-call isolation refusals of `spec.shell_tools` all apply, as
does the refusal of anonymous callers off a development box. What is different, and why:

- **Whose skills, decided by the operator.** Writing a skill is a grant to write *instructions*. A
  script is code on a runner whose volume holds every tenant's workspace, and whose network is the
  script's egress, with no SSRF guard of Felix's in between. So `FELIX_SKILL_SCRIPT_SOURCES` names
  which authors that grant reaches:
  - `host`, the default: the bundled skills and `FELIX_SKILLS_DIR`, which the operator ships.
  - `library`: the tenant libraries. Whoever holds `skills:write`, or adopts an import, can then
    have code run.
  - `personal`: each person's own library. Every caller with `skills:personal` becomes a code
    author on the runner.

  An imported skill nobody has adopted never runs scripts, whatever the setting. An object-store
  skill cannot: the store has no listing to write a bundle from.
- **Interpreters, not prefixes.**
  - The manifest maps an extension to an interpreter, `{.py: python3, .sh: bash}` by default.
  - Each interpreter must be in `FELIX_SKILL_SCRIPT_INTERPRETERS`, which is empty by default. This
    is checked at write, at compile and per call.
  - An interpreter is one word: no whitespace, `,`, `=` or NUL, no leading `-`, and either a bare
    name or an absolute path. A relative path would resolve against the model-written workspace
    root. The boot refuses a malformed list.
  - Putting `python3` in `FELIX_SHELL_ALLOWED_COMMANDS` instead would hand every shell tool
    `python3 -c`.
  - The script is `argv[1]` and the model's arguments follow it, so none of them is an option *to
    an interpreter that stops parsing options at the script*: python, bash, sh, node, perl and ruby
    do. A tool that reorders options after positionals, such as `make`, `find`, `npx` or `ts-node`,
    does not belong in the list.
  - `.js: node` is not a default, for the same reason. Node resolves `require()` by walking
    `node_modules` up every parent directory, the workspace root included, and the model's file
    tools write there. Listing node lets a package the model wrote load into a skill's script.
- **Only the skill's own script, beside its own instructions.** All of these are checked:
  - The skill is offered to the model, or is hidden and active here.
  - It is not an unadopted import, and it comes from an allowed source.
  - It is active in *this* conversation, per the thread's log.
  - The path is a bundle path under `scripts/` that the bundle lists, and is not a binary asset.
- **The bundle is written for one call.**
  - The skill's files (all of them, up to 8 MiB) are written to
    `.felix-skills/<skill>/<random token>/` under the scope's workspace root.
  - The write goes by descriptor from the root with `O_NOFOLLOW`, so a `.felix-skills` or skill
    directory replaced by a symlink is refused, not followed.
  - The files are removed when the script ends, or when the write fails part way.
    `.felix-skills/.gitignore` keeps a repository checkout's `git status` clean.
  - The file tools refuse the directory (`PANE_HIDDEN_DIRS`), so a model cannot put its own module
    beside a skill's script. A shell tool can, and a race with one is the boundary shell tools
    already have.
  - The script runs from the workspace root.
- **The runner checks it again.** The API sends `kind: "skill_script"` to `POST /run`. The runner
  then requires all of these:
  - `argv[0]` is in **its own** `FELIX_SKILL_SCRIPT_INTERPRETERS`;
  - `argv[1]` has the shape `.felix-skills/<skill>/<32 hex>/scripts/…`, with no `.` or `..`
    segment;
  - `cwd` is `.`.

  It consults the shell allowlist for neither, and `/start` refuses the kind. Set the variable on
  both sides; `compose.self.yml` shares it, empty by default.
- **Governed as a shell tool.** The tool's transport is `shell`: command screening reads every
  argument, and content screening reads the output as untrusted. The `skill_script` audit row
  names the skill and the script, as `ok` or as a refusal (`unknown_skill`, `script_refused`,
  `not_active`). An `ok` row also carries the skill's version and the script's sha256, since a
  version published after an approval or an activation runs other code under the same name.

## Publishing commits

`spec.github_publish` binds `publish_commits(branch, head_sha, title?)`, which publishes commits
already made in `FELIX_WORKSPACE_ROOT` to one GitHub repository. It exists so the workspace never
needs a credential: the shell tool above runs repository code, so whatever that process can read,
the agent can print. The token (`auth: secret:NAME`, a secret ref only — a literal is refused)
is resolved in the API process and Felix uses it only in the `Authorization` header of one
egress-guarded HTTP client. The git the tool runs is read-only plumbing (`rev-parse`, `merge-base`, `diff`, `log`,
`cat-file`) with an environment built from nothing — no token, no API secret — and with system
and global config, hooks, fsmonitor, external diff drivers and textconv switched off, because the
repository's own `.git/config` is agent-writable.

What an approval of `publish_commits` binds:

- **The content.** `head_sha` is in the call signature and is content-addressed, so approving one
  sha authorizes that tree and nothing else; a new commit on the same branch is a new approval.
  After building the tree the tool compares GitHub's tree sha with `head_sha^{tree}` and refuses
  to commit on a mismatch, and every blob sha GitHub returns is checked against the local one —
  what lands is byte-for-byte the tree that was approved.
- **The branch.** It must start with `branch_prefix` (required) and may not be `base`, which may
  not itself start with the prefix.
- **No history rewrite.** The parent is the remote tip of the branch (or of `base` for a new
  branch), it must be an ancestor of `head_sha`, and the ref moves with `force: false`.
- **Once, and for one person**, in `contributor.yaml`: the `publish` rule is `one_shot` and
  `bind_principal`, so a grant publishes one call and only for the caller it was granted to.

A preview that raises or takes longer than 60 seconds refuses the call with
`[approval preview failed]` and writes no row. Without that, a `head_sha` that is not yet a
commit when the preview runs — and is one by the time the person answers — would put an
approval on content nobody was shown.

**The token and the workspace.** Felix never passes the token to a git process or into the shell
tool's environment. On the Compose builder stack (`compose.self.yml`) shell tools exec in the
`shell` container, which does not hold `GITHUB_MCP_TOKEN` and cannot see the API's processes, so
code the agent writes and runs through an allowlisted command — `make test` imports it — has no
way to read the token out of `/proc/<api pid>/environ`, nor to make the API's own file tools
read it through a symlink in the workspace; see "The shell runner" above for exactly what that
container holds and shares. Where shell tools exec locally (`FELIX_SHELL_RUNNER_URL`
unset) that code still runs as the API's user and can read the token there, as it could the MCP
token before `publish_commits` existed. In both cases the approval gates what *Felix* publishes;
whoever holds the token can use it directly, within the PAT's permissions and the branch
protections in `docs/SELF.md`.

The preview on the row is `git diff --stat` and the unified diff between that parent and
`head_sha`, the diff capped at 32 KiB with a note saying so (the `--stat` is never cut). One
publish carries at most 300 files and 8 MiB. Whole-file MCP writes (`push_files`,
`create_or_update_file`) put every changed file into the model's context and the approval row —
196 KiB for one CHANGELOG line — which is why `contributor.yaml` no longer binds them.

### As the person, to the thread's repository (`auth: person`)

`github_publish: {auth: person, branch_prefix: ...}` publishes to the repository a person opened in
the thread (`POST /chat/sessions/{thread}/workspace/repo`), as that person, with an access token
minted from their stored GitHub connection (`felix.auth.github_connections`) at the moment of the
call. The properties above hold unchanged, with three additions:

- **The checkout is the thread's own.** It lives under FELIX_REPO_CHECKOUT_ROOT, never under the
  FELIX_WORKSPACE_ROOT, and every workspace tool in that thread works in it and nowhere else.
  A checkout still cloning, failed or expired stops the tools rather than falling back to the shared
  workspace. The remote shell runner cannot see a checkout, so shell calls in such a thread are
  refused rather than sent to run in the shared workspace.
- **The clone holds no credential.** The token reaches `git clone` as environment-only git
  configuration scoped to github.com, in an environment built from nothing; it is in no argv, no
  `.git/config`, no remote URL, and no environment of anything later run in the checkout. Hooks,
  submodules and the `file`/`ext` transports are off during the clone.
- **Bounded.** A repository over FELIX_REPO_CLONE_MAX_MB is refused before cloning; a checkout its
  thread has not used for FELIX_REPO_CHECKOUT_TTL_DAYS is removed by the worker, and the thread is
  told so.
- **Under `FELIX_WORKSPACE_BACKEND=hosted` the clone is in the thread's sandbox.** The token goes to
  the gateway for the one clone and is added to the sandbox's requests to github.com by an
  intercept outside the container, which allows only fetching that one repository and only while
  the clone runs: the sandbox never holds the token and cannot push. The harness reads the
  repository there with the same git (`git` and `lstat` ops) and still publishes through GitHub's
  API. Removing the repository destroys the sandbox. See `docs/WORKSPACE.md` phase 3c.
- **A child task works in a worktree of it, on a branch of its own** (`repos/worktrees.py`): a
  background child always, a foreground one under `spec.delegation.worktree` (not in plan mode,
  where it is read-only anyway). The harness runs `git worktree add` itself, in the API or worker
  process, with the same prelude and empty environment as its other git (hooks, fsmonitor and
  pager off), no lazy fetch, `GIT_ALLOW_PROTOCOL=none` (which outranks a repository's own
  `protocol.<name>.allow`), and `GIT_CEILING_DIRECTORIES` at the checkout root.
  A checkout would also run a `filter.<name>.smudge` driver the repository's configuration names,
  which no fixed key can switch off. So the worktree is registered with `--no-checkout`, its
  configuration is read *from inside it* with includes resolved (an `includeIf "onbranch:…"` can
  first apply there), and its files are written only when git answers that no `filter.*` key
  exists; a match, an error or an over-long answer all refuse, the worktree and its branch are
  removed, and no child starts. A thread holds at most eight task worktrees that hold work (one
  turn's parallel `task` calls can pass the count together, so it is a bound, not an exact cap).
  The file tools refuse `.git`; the dev-box `local` shell runs as the API user and can write a
  checkout's `.git`, as it can everything else that user owns, so against that shell the filter
  check is defence in depth, not a boundary: the file pane's `git status` on the thread's own clone
  runs without it. Use a sandbox backend where that matters. A worktree is the child thread's
  checkout: when a child's run ends -- foreground, failed, cancelled or background -- a worktree it
  left nothing in is removed with its branch, unless its own children's worktrees still hang off
  it; a commit count or status git could not give keeps it. A background run ended some other
  way (cancelled, expired) keeps its worktree until the TTL. Otherwise it is removed with its thread or after FELIX_REPO_CHECKOUT_TTL_DAYS unused, its
  branch kept; using it keeps every checkout above it from expiring. It cannot publish: its commits are in
  the parent's repository, which does. Local checkouts only; under `hosted` a background child is
  told it has no copy of the repository.

A publish whose opener's connection has lapsed or been revoked publishes nothing and returns
`[github disconnected]`, naming who must reconnect.

**Only the opener's run publishes.** The thread is shared by its tenant; the token is the opener's.
`publish_commits` refuses unless the run is the opener's own -- the caller driving the turn, or for
a resumed durable run or a scheduled job the person it runs on behalf of (`on_behalf_of`) -- so
another member driving the thread cannot publish as the opener. A checkout whose state names no
opener, or one that is not the GitHub account the token is minted for, publishes nothing until it
is removed and opened again. Reading, listing and removing the checkout (`GET`/`DELETE`
`/chat/sessions/{thread}/workspace/repo`, `.../files`) and the file pane over it (`/chat/workspace/*`)
are the opener's too: anyone else gets `403 not_checkout_opener`, unless they hold `admin`/`*`. `POST /chat/rewind` with `restore_files` is gated the same way, since it writes the checkout.
**What stays open:** the agent's own workspace and shell tools are not gated by opener. Another
member driving the thread runs an agent that reads the checkout, and can have it edit files and
commit in it; only publishing those commits is refused them. That is what sharing a thread means,
and closing it would mean refusing a non-opener's turns on a checkout thread altogether.

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
| `content_screening.image_model` (image text) | Per image, honours `on_flag`: `block` denies with 503; otherwise the image is removed and replaced by `[quarantined] image could not be screened`. Covers a transcriber error; a transcript that did not finish normally — cut off at the output limit, refused, or filtered; an empty reply (only `NO_TEXT` means "no text"); and every remote `http(s)` image — the provider fetches those itself, so this process cannot screen what the model will be shown. Emits `felix_control_unavailable{control="image_screening"}` on a transcriber error. |
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
as repeatable. Read-only built-ins (`list_dir`, `read_file`, `search_files`, `glob`, `calculator`,
`list_skills`) declare `replay_safe=True`; skill activation, writes and every outbound
integration do not.

Closing the call out is also what makes the thread resumable at all: the provider rejects a
transcript containing a tool call with no answer, so before this an interrupted run could
not be continued.

A durable run whose worker died mid-invoke is re-claimed at the same step, and it resumes
from the thread's session log rather than starting the turn again. Before calling the model
the step records the log's head (`invoke_began` in the fiber's state); a re-claim looks
for the run's own user turn among what was appended since. Not there — the crash came
before it was logged — and the turn runs as new. There, with a reply that has no tool calls
last, and the turn had finished and only the fiber's save was lost: that reply is the run's
answer and no model is called. Otherwise the run continues from the log with no new user
turn: the model sees its own tool calls and results — completed calls are not re-issued —
and a call in flight is closed out as above.

The turn is re-sent, as before, wherever the log cannot say where the run stands: no log
(`memory.checkpointer: none`); a composite pattern (`router`, `reflect`, `parallel`, …),
which reads the request from the incoming turn to route or to score; `session.strategy:
semantic:N`, which ranks history by the incoming text; a request that input redaction
changed before it was logged; or a log that could not be read. On a thread the caller also
writes to directly, a reply to a request sent between the crash and the re-claim can be
taken as the run's own.

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

**Skill jobs are outside a run's limits, and bounded on their own.** The worker's skill
improvements and evaluations (`skill_jobs`) are not a manifest's run, so `spec.limits` does not
apply; every call is still metered to the skill's tenant through `record_usage`. What bounds them:
`FELIX_SKILL_EVAL_MAX_TOKENS` and `FELIX_SKILL_IMPROVE_MAX_TOKENS` on each call;
`FELIX_SKILL_EVAL_MAX_SCENARIOS` (four calls each) per evaluation; `FELIX_SKILL_JOB_DEADLINE_SECONDS`
of wall clock per job, after which it is `failed`; three claims per job, after which it is failed
`attempts_exhausted`; `FELIX_SKILL_JOBS_MAX_QUEUED` jobs queued or running and
`FELIX_SKILL_JOBS_DAILY_LIMIT` created per tenant per UTC day (429 `skill_jobs_cap_reached`),
across evaluations and improvements together; and one sweep at a time across every worker, at most five jobs of each kind per sweep. The sweep
holds a `skill_job_lease` row rather than an advisory lock, so it holds behind a
transaction-mode pooler. The lease lasts one job deadline plus five minutes and is renewed before
every job. A sweep whose lease lapsed and was taken over stops before its next job.

**The caps are exact.** Requests racing at a cap get in one at a time, and the one past it is
refused. Each write that adds a job counts the tenant's jobs and writes its row in one
transaction, under a transaction-scoped advisory lock per tenant. The same holds for an agent's
pending feedback (`skill_authoring.max_pending`, a lock per manifest) and its pending drafts (a
lock per origin manifest). A transaction-scoped lock is released at commit or rollback, so it
holds behind a transaction-mode pooler. A draft save racing another at the cap no longer
refuses both.

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
  | Never longer than the run | `expires_at`, checked before every step by the fiber scheduler. `hibernate_after_seconds` (300s) by default, `execution.resume_token_ttl_seconds` if set, capped at `ABSOLUTE_LIMITS["resume_token_ttl_seconds"]` (24h). |
  | Never longer than the token | Clamped to the token's `exp` when it has one. Felix has no revocation, so `exp` is the only bound on a compromised credential and a durable run must not outlive it. |

  **A resumed run replays its scheme without a credential.** The run's recorded `scheme` is
  presented at resume, but nothing re-presents the token it came from, so
  `auth.inbound.schemes` can only agree with the check made when the run was enqueued. That is
  defence in depth lost, not a hole: the enqueue-side check is the one that ran with a
  credential.

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

## Screening images

Text rendered inside an image is an injection channel: the screeners read a turn's text blocks,
and an image carrying "ignore previous instructions" passed all of them. Set
`content_screening.image_model` to a vision-capable model and each user image is transcribed by
it, and the transcript screened exactly as typed text is — the marker scan, then `model` and the
decider if set. `on_flag` applies per image: `quarantine` removes the image and says so in the
turn, leaving the text and any clean images; `block` refuses the turn with 422.

- **What is screened is what the model gets.** An uploaded file (`felix-file://`) is resolved
  under the caller's tenant before transcription, the way the model call resolves it.
- **Remote image URLs are not screened, and are treated as unscreenable.** The provider fetches
  them separately, so a server can answer the screener and the model with different images.
- **Cost.** One vision call per distinct image, and at most `MAX_SCREEN_IMAGES` (8) per
  *request* — counted across every message in it, since one body can carry many. Past that,
  `block` refuses with 422 `too_many_images` and `quarantine` removes the rest. Transcripts are
  cached per tenant by content, so a client that resends its history pays once per image, and a
  cached image does not count against the eight.
- **Replayed history is screened too.** Threads are scoped to the tenant, not the manifest, so
  a thread can hold images this manifest never screened: sent through another manifest, sent
  before `image_model` was set, or written by a session write-back. The screen sits on the
  session strategy's `render`, so every rebuild of history passes it — the turn's own
  assembly, compaction after a turn, recovery from a context overflow, and any plugin
  pattern's render — without any of them opting in. A router's screen holds for the children
  it forwards the thread to: each compile wraps the strategy it inherits, so a child's own
  rules add to its router's and cannot loosen them. A replayed image is always
  **quarantined, never refused** — even under `block` — because the log is append-only and
  refusing would refuse every later turn of the thread. The image leaves that prompt; the log
  is not rewritten. The incoming turn is not screened a second time here.
- **Cost on replay.** Transcripts *and* verdicts are cached, keyed per tenant and by what the
  verdict depends on (the scorer model and the decider), so a replayed image is transcribed
  and scored once, not every turn. An upload is keyed by its file id, so a cached one costs no
  object-store read; an uncached one past the budget is quarantined before its bytes are read.
  Each render has its own budget of eight, and the incoming turn its own, so a turn makes at
  most sixteen transcription calls. The caches are per process: after a restart or on another
  replica, a long thread's images are re-screened eight per render, and those past the budget
  are left out of the prompt until they have been.
- **Images a tool returns are screened too.** A tool can hand the model an image to *see*
  (`ToolOutputDict.attachments`); the browser's `screenshot` op does. When content screening
  covers the tool (every untrusted tool, plus any named in `tools`), each image it returns is
  transcribed and screened like a user's, on its own surface (`tool_image`). That surface
  **quarantines, never refuses**: under `on_flag: block`, a flagged *text* denies the call,
  while a flagged image is removed and the call stands. Text that is quarantined takes the
  tool's images with it.
- **Without `image_model`, an untrusted tool's images are quarantined.** This fails closed.
  - Untrusted text is always marker-scanned; pixels have no screener, and a page can draw its
    payload rather than write it.
  - The tool result says the image was not shown and how to turn screening on
    (`felix_content_screening{action="image_unscreened"}`).
  - A trusted local tool named in `tools` keeps its images.
  - A manifest that binds `op: screenshot` under screening without `image_model` logs a warning
    at compile time, because every screenshot it takes will be quarantined.
- **A tool's images are stored, not logged, and bounded.** They are written to the attachment
  store under the request's tenant, with the same quota, size limit and retention as an upload.
  The session log keeps a `felix-file://` reference. Each of these is dropped with a note in the
  tool result:
  - an image no caller could have uploaded: not png, jpeg, gif or webp by its bytes, or over
    600 KiB;
  - a remote URL;
  - anything past 4 images per call or 16 per request, counted across every agent the request
    runs (a router's children included). Tool images share the tenant's quota with uploads, and
    the caps keep a page that talks an agent into screenshotting in a loop from filling it.

  A tool's own filename for an image is not kept: secret masking reads text, and a label would
  otherwise reach the ledger and the log unmasked.
- **An after-tool hook that rewrites a tool's text also removes its images**, so a redacting or
  blocking hook covers the whole output.
- **A caller's images are kept on user turns only.** History a caller sends (`role: tool` or
  `assistant` on `/chat` or `/v1`) has its images removed at the door, and so does a queue
  write-back to `/internal/sessions/{id}/events` that is not a user message. Inbound screening reads
  user turns, and an image written into a tool message would otherwise reach the model past
  every screen. Separately, the wires render a tool message's images only from inline bytes, so
  a remote URL on a tool message is never fetched.
- **The OpenAI wire raises a tool's image to a user turn.** That API takes images on user turns
  only, so a tool's images follow its result as a user message. The message labels them as tool
  output to be treated as data, but a user turn still carries more weight than a tool result.
  Anthropic keeps them inside the `tool_result`.
- **Image tools are screened by where their input came from.** `spec.image_tools` can name only
  images in the thread's active branch: `latest`, `#n`, or a `felix-file://` reference that the
  thread holds. A reference to any other upload the tenant owns is refused, because it never passed
  a turn's screen. A workspace file is readable only by a tool whose entry sets `allow_path`. Under
  content screening:
  - With `image_model`, every image-tool result is screened on the `tool_image` surface. That
    covers an image the replay screen had quarantined.
  - Without it, a result made from a workspace file is quarantined. A result made from a thread
    image passes, since it was no less screened than the image it came from.

  Pillow opens only png, jpeg, gif and webp, as sniffed from the bytes, so no other parser is
  reachable: EPS, and the Ghostscript it would invoke, are not.
- **A2A and MCP carry images too.** All of the screening below is under content screening; with it
  off, these images reach the model unscreened, as their text does.
  - **A2A inbound.** A FilePart is a user turn's image. It is held to the upload rules and stored
    like an upload, under the quota, so the log keeps a reference. It is screened at ingest when
    `image_model` is set, under `on_flag` in full.
  - **A2A peers and remote MCP servers.** Their images are an untrusted tool's. They are checked
    against the upload rules, and capped at 4 per call, before the screener reads them. They are
    quarantined without `image_model`.
  - **Felix's own MCP server.** It returns a tool's governed images to the caller as bytes: at
    most 4, each one an image by its bytes, and only stored images that this same call made. A
    reference a tool merely names may be any upload in the tenant.
- **Secrets and PII in a tool's image are not caught.** Secret masking, PII guardrails and
  judges read text only. A screenshot of a page showing a credential reaches the model and is
  stored for the attachment retention period.
- **Injection only.** `guardrails.providers: [pii]` does not read image transcripts; an image of
  an SSN is not caught by the input PII guardrail.
- **Limit.** The transcriber reads hostile input, and an image can tell it to report no text.
  This is one layer, not a guarantee — the same caveat as every model-based screener.

It needs `content_screening.enabled: true`; the manifest is refused without it.

## Content screening targets

`content_screening.tools` is **additive**. Screening covers every untrusted tool — anything
whose transport is not `local`, plus anything whose `source` starts with `mcp`, `peer`, `a2a`,
`queue`, `browser`, `client`, `sandbox`, `container`, `http`, `search`, `documents` or `memory` —
and, in addition, whatever `tools` names. `memory` covers `recall` and `list_memories`: capture
runs over turns that carried untrusted tool output, so a payload quarantined on its way in could
otherwise come back as a remembered "fact". Naming a trusted local tool extends screening to it;
it does not narrow screening away from anything.

**A raised exception is covered too.** `ToolRunner` shows the model a raise as
`[error/<code>] <exception text>`, and for an untrusted tool that text is the remote author's: a
server's error body, a JSON-RPC message. A covered tool's raise is screened as its output is
(markers, then the model or decider), and so is the raise of a tool that relays untrusted text.
Clean, it goes through unchanged. Flagged or unscreenable, it is denied under `on_flag: block`, or
re-raised as `[quarantined] tool error …` with its error code kept, so a timeout is still a timeout
to the runner. A `fatal` tool's blocked raise is re-raised rather than denied, so it still ends the
run. Secret masking likewise redacts a known secret from any tool's exception text (a failed request
often echoes its credential), re-raising with the same code.

The PII guardrail and judges do **not** read a raise: a tool's exception that quotes a row (a
duplicate-key message naming an email) reaches the model past `guardrails: pii`. Tracked in
`docs/ROADMAP.md`.

`create_skill` and `update_skill` are trusted local tools and are not screened by default; their
output is the library's own JSON. What they *save* is checked by the skill security scan on save
and again on publish, and their approval preview is the SKILL.md the harness renders from the
arguments, so an approver reads the instructions rather than the model's summary of them.

There is deliberately no way to turn screening off for an untrusted tool while leaving it on
elsewhere. The two used to be alternatives, so a non-empty `tools` list *replaced* the
untrusted-tool default: naming one local tool silently unscreened every MCP, peer, browser,
sandbox, container and queue tool while the manifest still read as a working control. Turning
screening off for untrusted output is the thing screening exists to prevent, so the narrowing
was removed rather than renamed. On cost: neither `content_screening.model` nor `on_flag` is a per-tool lever, so this removes
the only one there was. It is free in the default configuration — both bundled manifests that
enable screening leave `model` empty, and the marker path is a substring scan — and it costs a
model call per untrusted tool per turn where `model` *is* set.

An MCP server's own `instructions` — the text it returns from `initialize` about how its tools are
meant to be used — are **not** read unless the manifest sets `use_instructions: true` on that
server. Tool descriptions already reach the model as tool schemas; instructions would reach the
*system prompt*, which is the server writing to your agent with the operator's voice. When opted
in they become one line of the system prompt's tool guidance, collapsed to one line, capped at
1,000 characters, and dropped whole (`felix_mcp_instructions{outcome="flagged"}`) when the
injection markers match. Opt in only for servers you would let edit the prompt.

That knob is `content_screening.model_tools`: a glob list of which screened tools get the paid
scoring — `model` and `decider`, a call per window each. Empty, the default, is every screened
tool. It is orthogonal to trust, not a way out of it: every screened tool still runs the marker
scan whatever `model_tools` says, so an untrusted tool it leaves out is screened by markers
alone, never unscreened. It needs `model` or `decider: true` to mean anything and is refused
without one; a pattern that matches no bound tool is counted as `felix_rule_targets_nothing`.

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

Windows are scored four at a time, and the first flagged or unavailable window *in order* decides,
as it did when they were scored one after another. Verdicts are cached for ten minutes, per tenant
and by what a verdict depends on (the window's text, `model`, and the decider), so a conversation a
client re-sends every turn is not model-screened again on every request; a screener that could not
run is never cached. The cache is per process, like the image verdicts'.

Two things to re-measure if you set `model` and previously narrowed `tools`:

- **Availability.** `on_flag: block` plus a screener outage now denies output from every
  untrusted tool rather than the named subset. Right direction, wider radius — watch
  `felix_content_screening{action=unavailable}`.
- **False positives.** The marker scan is a substring match, `"system prompt"` included, so a
  docs server, a code-search tool or an issue tracker quoting a jailbreak can now be
  quarantined where it was exempt. Watch `{action=quarantine}`.

### Summaries are screened before they are stored

A conversation summary is a model's rewrite of a transcript that carried tool output, and once
stored it is replayed into every later turn of the thread. Its input reaches the summariser fenced
as data, but a summariser that followed an injection anyway writes the injection into the summary.
So when `content_screening.enabled` is on, every summary a session strategy writes is held to the
rule a user turn is before it is stored: the injection markers, then `model` and the decider when
they are set. That covers a turn's own compaction, a forced pass when a provider rejects an
oversized request, `POST /chat/compact`, and `summarizing:N`.

A **flagged** summary is stored as `[summary withheld: content screening flagged it]` in its place,
so the range it covers is settled and is not summarised and screened again on every later turn:
storing nothing would let text planted in the history, or a harmless false positive, make every
turn over budget pay the summariser and the screener again. The history it covered is lost to the
model, which a withheld summary costs either way. A summary that **could not be screened** is not
stored: the summariser is treated as having failed, the thread keeps its recent window,
`POST /chat/compact` answers `502 compaction_failed`, and a later turn tries again once the screener
is back. A screening-model outage therefore turns compaction into dropped context until it
recovers -- the price of not storing what no screen has read. Each one counts
`felix_summary_withheld{reason}` and writes a `summary_screening` audit row, and the summariser's
call is billed either way. `on_flag` does not apply: refusing would refuse every later turn of the
thread. A sub-agent's screen adds to its router's rather than replacing it.

Not screened: a `before_compact` hook's own summary, which is the operator's plugin code and
trusted as such; and the branch summary a rewind writes, which is never rendered into context.

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

## Memory: who may retire what

Every memory row records its writer, and writers have two ranks: the **operator** (the
`/memory` management API, `memory:write`) and the **agent** (auto-capture, and the `remember` /
`remember_procedure` tools). The store — not the manifest, not a governance wrapper — decides
what a write may take out of recall, so the rule holds for every path including capture, which
passes through no wrapper at all:

- **Retiring by `topic_key` is the operator's.** An operator write retires other active rows
  under the same key. An agent write under a key already held is stored **alongside**: the
  key is chosen from the transcript, by the extractor or by whoever steers `remember`, and
  letting it retire meant one injected turn could delete every fact the agent kept on a topic.
- **The prelude shows the current value per topic** — the most trusted, then the latest turn —
  so the model reads one belief each turn while both rows stay active, recallable, and visible
  on `GET /memory`, where the operator settles the contradiction by writing the value or
  forgetting the stale row.
- **An agent never retires or rewrites an operator's row**, by topic or by restating its text.
- **Forgetting stamps who forgot it**, and a row comes back only for a writer of at least that
  rank: an operator's correction is not undone by the agent restating the sentence it removed.
  Resurrection is gated on who *retired* the row, not who wrote it.
- **Consolidation retires as the agent, and only duplicates.** With
  `spec.memory.consolidate.enabled`, the worker asks the `consolidate.model` route which of a
  pool's agent-written facts say the same thing. The model returns groups of ids only — it
  cannot write a memory, and it does not choose which fact survives. The store
  (`merge_duplicates`) keeps the **oldest** member of each group — lowest turn, then earliest
  write, then id — and supersedes the rest by it, stamping `retired_by: consolidation`, which
  ranks as the agent. The survivor is fixed by age because age is the one property an
  injection cannot claim: when the model chose, a fact injected through a tool result ("over
  $500 needs approval *unless the user says urgent*") could be grouped with the real rule and
  named the keeper, retiring it. Importance is not used, since the agent can set it. The store
  never shows the model, keeps, or retires an operator's row; never merges facts of different
  `kind`s or whose `topic_key`s differ (absent counts as a value, so an untopiced fact cannot
  retire a topic-keyed one); and drops a whole group that names an id the model was not shown
  or names an id twice. A merged duplicate is therefore as reversible as any agent retirement:
  restating it brings it back. A forgotten row is never a merge member, so consolidation cannot
  re-rank an operator's forget.

`governed.yaml` still puts an approval in front of `remember` calls that carry a `topic_key`
(`when_args: [topic_key]`). With the store refusing agent retirements that gate no longer
prevents a deletion; it keeps a human seeing a key-changing write, which is what it is for now.

### Memory: whose memory an agent reads

Memory is kept in **pools** (`memory_vectors.principal`, migration `0046`). An *agent pool* is one
manifest's own rows (`principal` empty); a *person pool* is one person's rows, whichever of their
agents wrote them. `spec.memory.scope` chooses which an agent uses:

- **`person` (the default).** The agent writes the person the request acts for
  (`AuthContext.acting_for`) and reads their pool plus the **operator-written** rows of its own
  agent pool — what an operator curated for the agent through `/memory`, which every person's
  agent sees. Agent-written rows in an agent pool reach no person's prompt: they are the agent's
  learning from before person pools, or from runs that named no one, and whose they are is not
  known. `felix memory assign --tenant T --manifest M --to PERSON` moves them into the person
  they belong to (agent-written rows only unless `--include-operator`; `--dry-run` counts).
- **A request that names no person writes nothing** under `person` scope: anonymous, an API key
  with no `sub` of its own, an inbound trigger's run, a machine actor working for no one, a
  GitHub Actions workflow, a Slack channel message (its user is not a verified sign-in). Capture
  does not run and `remember` answers that nothing was stored; reads see the curated rows only.
- **`agent`.** The agent's own pool, read and written, every row of it — the behaviour before
  person pools. Use it for an agent that serves a shared purpose rather than a person.

**The person key is `issuer|subject`** — the form a personal skill library takes — worked out by
the auth middleware from the verified credential, and recorded with a durable run or a job so a
resume or a firing writes the same person's memory. A subject is unique only within its issuer:
an API key configured with `sub: github:70701` is not the person who signed in with GitHub as
`github:70701`. `FELIX_PRINCIPAL_ALIASES` maps one person key to another, so one person signing
in two ways keeps one memory: a JSON object, `{"google:123": "github:70701"}`, where a value with
no `|` means Felix's own sign-in issuer (`felix-self`). One hop — a target may not itself be an
alias — and neither side may be a shared credential or a machine actor; the boot refuses either.
Memory alone reads the map; approvals, `bind_principal`, jobs and push keep the raw principal.
A subject that itself contains `|` (an Auth0-style `auth0|5f7c`) must be written in full
`issuer|subject` form in the map, in `--to` and in `principal`: written bare, its first `|` is read
as the issuer's end, and the key names no one. A replayed run's recorded key must match both its
subject and the issuer its recorded scheme produces (`api_key`, `felix-self`), so an edited key
cannot move a run between two sign-ins that share a subject string.

Two things the person key trusts. A **plugin authenticator** chooses its principals' issuer, so
one that claims `felix-self` or `api_key` would share those people's pools (and personal skill
libraries); a plugin must issue under its own issuer. A **JWT without `sub`** takes its subject
from `email` (`auth/jwt.py`), which some identity providers let users edit: point a verifier only
at an issuer whose tokens carry `sub`.

**Person isolation is an application filter inside the tenant**, not a database policy. Row-level
security isolates tenants, as it always has; inside one tenant the memory store's own predicate
(`memory/store.py:_sees`, `_owns`) is what keeps one person's rows from another's agent, on every
read and every retirement — a `forget` by id, a supersession by `topic_key`, a consolidation
merge, an upsert onto an id another pool holds (refused) all stay in the writer's pool.
`memory:read` and `memory:write` are **operator** grants and span every person in the tenant:
`GET /memory` lists everyone's pools (`?principal=` narrows to one), and `POST /memory` writes any
of them (`principal` names the pool; a credential many callers share is refused, 422). Grant
them to operators, not to every member: an org grant (`FELIX_GITHUB_ORG_TENANTS`) that includes
`memory:read` lets each member read every other member's memory.

Person isolation goes as far as **thread access** and no further. Threads belong to the tenant,
not to a person, so a member who posts into another member's thread runs with *their own* pool —
none of the owner's memory reaches them — but their facts appear in that thread's replies, and
capture learns from a turn shaped by its history. Keep threads unguessable and unshared where
that matters; person-bound threads are on the roadmap.

One channel crosses people on purpose: **procedural memory** is the agent's, whatever the scope.
`remember_procedure` writes the agent pool and every caller's turn retrieves from it, so a how-to
an agent learnt while working for one person is offered while it works for another. It is
agent-written, so it never reaches a person's *fact* reads (prelude, `recall`, `list_memories`).
A manifest whose procedures must not travel between people should leave
`spec.procedural_memory` off.

## Approval semantics

**Precedence.** Approvals is the only control that selects *one* rule — policies and judges
apply every match, so for them more matches only tighten. When several approval rules match a
tool, a rule naming it **literally** wins over one matching by pattern, and among equals the
last declared wins. That makes globbing non-weakening: a pattern can only gate a tool nothing
gated before, and never displaces a stricter literal rule.

**A delete is never gated less than a write.** A manifest whose approval rule gates a bound
`write_file` or `edit_file` while it binds `delete_file` or `rename_file` with no rule over it
lets the agent remove a file it would have had to ask to change -- and a delete has no undo. The
client family is held to the same rule: `local_write` / `local_edit` against `local_delete` /
`local_rename`. Such a manifest is **warned about, not refused**
(`felix/manifests/delete_gate.py`): `felix validate-manifest` prints
``warning <path>: approval rule `workspace-write` gates write_file, but delete_file is bound with
no approval rule, …; add delete_file to rule `workspace-write` `` and still exits 0 (1 under
`--strict`, the CI gate; see [GitOps check](#gitops-check));
`PUT /manifests/{name}` stores it and returns the same sentences in the response's `warnings`
list (always present, `[]` when there is nothing to say); and the compile logs each one once per
process for a given manifest content and counts `felix_delete_gated_less_than_write`. A warning
because a manifest stored before these tools existed and since edited to bind one is a working
agent; refusing it would make an upgrade an outage. "Gated" is any rule whose `tools` matches the
name, globs and `when_args` rules included. Gating the delete and not the write is a choice and is
not reported. Every bundled manifest is held to it by
`tests/unit/test_workspace_delete_rename_tools.py`, through the same check.

| Field | Behaviour |
|-------|-----------|
| `ttl_seconds` | How long the run waits for a decision before failing closed. |
| `one_shot` | The grant is spent by the one call it authorizes — the call that waited for the decision, or a later call that found it approved; a replay of the same call needs a new approval. |
| `bind_principal` | Only the principal who was approved may use the grant. Without it, any principal in the tenant can reuse it. |
| `allow_unattended` | Whether the rule still asks when the run is **unattended** — a scheduled job's firing, or anything it starts (see [Unattended runs](#unattended-runs)). `false` (the default) refuses such a call at once; `true` writes the row and waits as for a person. EU AI Act high-risk manifests must set this to `false`. |
| `when_args` | Gate only the calls that carry these arguments (non-empty); empty gates every call. Each name must be an argument some tool the rule reaches takes, or the rule never fires. For tools whose schemas ship with the harness — built-ins, plugin tools, the memory tools — a rule naming them literally is **refused** at `PUT /manifests` and by `felix validate-manifest` when a name is not one of their arguments. For everything else (MCP tools, globs) it is a compile-time warning and `felix_approval_when_args_unknown`, not a refusal: an MCP schema can change under a stored manifest, and that must not become an outage. A glob such as `github__*` with `when_args: [force]` is flagged only if *no* tool it reaches takes `force`. |

**Skill authoring.** `spec.skill_authoring.mode: publish` is refused at validation unless the
approval rule this precedence selects for `create_skill`, and the one it selects for
`update_skill`, both exist and carry no `when_args` — a conditional rule would let the calls
without those arguments publish ungated. A rule naming both tools with no `when_args` is what
satisfies it, and such a rule applies to any manifest that enables authoring, in draft mode as
well as publish.
With `spec.personal_skills: write`, the tools save into the caller's own library (see
`skills:personal`); an approval and the publish gate apply to those saves exactly as to the
tenant's. Which library a call saves into is not in its arguments -- it depends on the caller --
so the signature a grant binds is the hash of the arguments *and* that library: a grant for a
save into Alice's library authorizes no identical call that would save into Bob's or the
tenant's. Every library numbers its versions from `0.1.0`, which is why `parent_version` alone
cannot do it.
An `update_skill` grant binds the version it edits: `parent_version` is a required argument and the
call signature is a hash of the arguments, so a grant found later — by a retry, another replica or
a resumed fiber — authorizes an edit of that version only, and the call is refused with
`parent_changed` if the skill has moved past it.

### Unattended runs

A run is unattended when no person started it and so no one is there to answer: a scheduled job's
firing (the sweep or `POST /jobs/{name}/run` -- both run as `cron` for the job's writer), an
inbound trigger's run (`POST /triggers/{id}`, as `trigger:<id>`), and everything either starts --
a workflow run's agent and tool steps, a durable or background child. A firing marks its caller
`scheduled` and a trigger marks its caller `trigger_run`; both marks travel with the recorded caller
into each of those (only ever added on replay), so `RequestContext.unattended` is derived from them
wherever the run resumes; nothing has to remember to set it. For a trigger this matters twice: the
sender chooses the payload, so an approval row would show an approver arguments an outsider picked,
and every such wait would hold a worker slot for the rule's ttl.

In an unattended run every gate that would wait on a person answers at once instead:

| Gate | Unattended behaviour |
|------|----------------------|
| An approval rule with `allow_unattended: false` (the default) | The call is refused before a preview is computed: the model gets `[approval unavailable] tool=<name> rule=<id>: this run is unattended (no person started it), so no one can approve it; the call was not made.` No approval row is written, nothing is pushed, no `approval_required` frame is emitted, nothing waits. It is a `policy_deny` audit row with `control: approvals`, and `felix_approval_unattended_refused` counts it. |
| An approval rule with `allow_unattended: true` | Unchanged: the row is written, pushed and waited on for `ttl_seconds`, named for the job's writer. Use it where a person reliably answers within the ttl -- and remember the sweep awaits each firing in turn, so a long ttl delays every job after it. |
| A `command_screening` rule with `require_approval` | Refused at once (`[command approval unavailable] tool=<name> rule=command:<reason>: …`, the same wording). It has no `allow_unattended`. |
| A client tool (`spec.client_tools`) | Refused at once with a `transport_unavailable` tool error; no request is recorded for a client. |
| `ask_user` | Answers `no_one_watching` at once, as it does off any live stream. |

A grant that already exists still counts: a call whose exact signature a person approved earlier,
or a permission mode that waives the rule, runs. That includes a mode set on the run's thread: a
holder of `approvals:write` can put a job's thread in `accept_edits`, which waives the job's
edit-tool approvals in its unattended runs too -- only `bypass` is re-checked against the job's
writer. A workflow's own `approval` step is not a tool
gate and is unchanged -- it parks the workflow run, not a worker, and waits for its decision.

Before this, a firing that reached a gated tool wrote a pending row, pushed it to the whole
tenant, and then waited out the rule's ttl (five minutes with none) while every job behind it in
the sweep waited too. A job that relied on that -- someone answering the push within the ttl --
now needs `allow_unattended: true` on the rule; an EU AI Act high-risk manifest cannot set it,
and its scheduled runs therefore never take a gated action.

**A job is not fired while its last run is going** (`last_status: running`, read by every claim,
the sweep's and `POST /jobs/{name}/run`'s, which answers `409 job_running`). A run's end clears the
mark only over its own claim. A stuck mark -- a worker that died mid-run -- stops counting after six
hours; `PUT /jobs/{name}` with `enabled: false` clears it at once, and re-enabling lets the next
sweep fire the job (beside the old run, should it in fact still be going).

`spec.policies` and `spec.approvals` are capped at 64 rules each: matching is O(rules × tools)
and a manifest is compiled per request.

Approvals are matched on `(tenant, manifest, tool, sha256(args))` and stored in Postgres
— never in model-visible state, so the model cannot forge one. Every failure path
(no request context, store error, waiter timeout) denies.

**What an operator sees, on either channel.** A pending row and the `approval_required` stream
frame carry the same story: `rule_id`, `reason` (the rule's `description`, or the finding for a
command-screening gate), `thread_id`, `tool_call_id`, and `expires_at`.

**A preview, for a tool whose arguments are a reference.** A tool may carry an
`approval_preview` — a harness-side function of the call's arguments. When it does, the row's
`args` and the frame's `args` gain a `preview` string computed *before* the row is written, with
known secret values redacted. It is for the person reading, and it is kept out of everything
else: `sha256(args)` is taken over the original arguments, so a preview can neither widen nor
narrow what an approval authorizes; and `edited_args` sent back with a decision have `preview`
stripped before the tool runs. A preview that fails or takes longer than 60 seconds **refuses the
call** — the model gets `[approval preview failed]` with the reason, no row is written, and
`felix_approval_preview_failed` counts it — because a tool has a preview precisely when its
arguments do not show the content, and an approval without one binds content nobody saw. Only `publish_commits` has one today
— see below.

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

## Heartbeats

A heartbeat is a periodic check-in a person turns on for themselves, on a manifest whose operator
defined one (`spec.heartbeat`). The split of who decides what is the control:

| Who | Decides |
|-----|---------|
| The operator (`manifests:write`) | Everything a firing does: the `checklist` the agent works through, the `schedule` (recurring only, never faster than `min_interval_minutes`, itself at least 15), the `model` route firings use, `max_turns`, `keep_threads`, and `max_firings_per_day`. A manifest with a heartbeat must also bind `notify` (refused at write otherwise). |
| The person (`heartbeats:write`) | Only on or off, for themselves: `PUT /heartbeats/{manifest}` with `{enabled}`, `DELETE`, and `POST .../run` to fire it now (`409 heartbeat_running` while a firing is in flight). `GET /heartbeats` (`heartbeats:read`) lists their own and no one else's. |

**It runs as the person.** Enrolling writes an ordinary job (`hb-<manifest>-<sha256(person)[:12]>`,
listed in `GET /jobs`) recording the person and their authority exactly as a job write does, so
each firing runs as `cron` on their behalf with their scopes, through the manifest's inbound auth.
That check also runs at enrolment, before anything about the manifest's heartbeat is answered, so
a heartbeat that could never run is refused at once with the same written message, and a caller
the manifest refuses cannot tell whether it offers one. Enrolling needs a named person: anonymous,
a sub-less API key and other shared subjects are refused (`403 shared_subject` / `no_caller`).

**How long it lasts depends on the credential that enrolled it.** An API key and a Felix sign-in
whose grant for the tenant is configured here (Google accounts, GitHub org grants, GitHub Actions
repositories) are re-checked at every firing, as every job firing re-checks them -- the scopes cut
to what the key or grant holds today, none left refuses the run -- so those heartbeats record no
expiry and run until turned off. Any other credential -- a token from another issuer, a sign-in
to a personal tenant -- leaves Felix nothing to re-check, so enrolment records its token's `exp`
and the heartbeat stops there: the first firing past it is refused, recorded as an error ("the
credential that turned this heartbeat on has expired; turn it on again") and the job disabled, as
an expired agent task is. `GET /heartbeats` shows that end as `expires_at` (null when there is
none).

**Nobody writes its instructions but the operator.** A firing's prompt is built from the manifest's
*current* `checklist`, inside a fixed frame (reply `HEARTBEAT_OK` when nothing needs the person,
else call `notify` once); the job's payload is never read for it, so a holder of `jobs:write`
re-saving the job puts no words in a firing, and an operator's edit applies at the next one. The
stored schedule is brought back to the manifest's at each firing, by a conditional update that
never re-creates or re-enables a heartbeat deleted or turned off meanwhile. A manifest that no
longer defines a heartbeat, or no longer exists, turns it off: the firing is recorded as an error
and the job disabled. `max_turns` only ever narrows: a firing runs at the lower of it and the
manifest's own `recursion_limit` / `max_turns`.

**It is unattended**, like every job firing (see [Unattended runs](#unattended-runs)): a tool behind
an approval rule without `allow_unattended` is refused in it at once, and nothing waits.

**It speaks only through `notify`**, with every `notify` control: to the person and no one else,
secrets redacted, links stripped, screened, `max_per_run` and `max_per_hour`. The notice is also
left in the person's own heartbeat conversation (`{tenant}:hb-<hex>`, created at enrolment and
titled `Heartbeat: <manifest>`) as an entry for the person that no model prompt carries. Each run
record says whether the firing was `quiet` (sent no notice) and keeps the first 500 characters of
its answer.

**An operator can cap its firings per UTC day** with `max_firings_per_day` (1 to 1440; unset, the
default, caps nothing beyond what the schedule fires), counting every firing that ran -- scheduled or asked for with `POST /heartbeats/{manifest}/run`, so a person cannot
fire past it by hand. A firing due past the cap runs nothing: no model call, no tool, no notice. It
is recorded as a run with status `skipped` (result `{"reason": "daily_cap", "max_firings_per_day":
N}`), the job's `last_status` reads `skipped`, and the heartbeat stays on and fires again after UTC
midnight -- whatever `FELIX_TIMEZONE` says, so the day is the same on every install. Skipped runs do
not count toward the cap, so an operator raising it mid-day gives today the difference. A schedule
that fires more often than the cap is allowed -- the cap binds, and the rest of the day's slots are
recorded `skipped` -- and `PUT /manifests` and `felix validate-manifest` say so as a warning. A
firing that ends some other way -- cancelled by a worker shutting down or a caller disconnecting
from a manual run, or failed in the scheduler -- is still recorded (an error, `cancelled` or
`execution failed`) before its `running` mark clears, so it counts: it may already have called the
model.

**Firing transcripts are the tenant's to read**, like an agent task's and any job's: each firing's
thread (`{tenant}:job:hb-…:<hex>`) is an ordinary thread of the tenant, so whoever may read the
tenant's sessions reads what a heartbeat checked on the person's behalf. Write a checklist with
that in mind; the notice itself goes to the person alone.

**Retention.** Each firing runs in a fresh thread; after it, all but the newest `keep_threads`
firing threads of that heartbeat are deleted whole (events and metadata), on every store. The
person's conversation is never among them. `DELETE /heartbeats/{manifest}` removes the job, its run
history and its firing threads, and keeps the conversation, which holds the notices the person was
sent and is theirs to delete. Its run records are trimmed like every job's: after each run, all but
the newest `FELIX_JOB_RUNS_KEEP` (200) are deleted -- except runs of the current UTC day that ran,
which are what the daily cap counts and stay until the day ends, so a small `FELIX_JOB_RUNS_KEEP`
never hands a heartbeat its firings back. Skipped runs are trimmed like any other.

## Permission modes

A thread is in one of four modes, set with `POST /chat/mode` (or by an approved plan) and kept in
thread metadata. The change applies from the next run: a run reads the stored mode once.
Tightening (toward `plan`) is open to anyone driving the thread; loosening — `plan` → `default` →
`accept_edits` — needs `approvals:write`, because a thread is shared by its tenant and waiving its
approvals is deciding them, and `bypass` needs `approvals:bypass`.
So leaving plan mode through `POST /chat/mode` needs `approvals:write` too; the other way out is an
approved plan (`exit_plan_mode`), which needs whoever answers it to hold `approvals:write`.

| Mode | Effect |
|------|--------|
| `default` | Every control runs as the manifest says. |
| `plan` | Only read-only tools run (`Tool.read_only` — the workspace's reads, search, recall, `todo_write`, `ask_user`, foreground `task` — plus `permissions.read_only_tools`); every other call is refused as `[plan mode] …` before an approval is asked, and a background `task` is refused (a worker run would not inherit the mode). **Every agent honours a stored `plan`**, whatever its `allowed_modes`, because it only narrows; `allowed_modes` decides whether the agent offers `exit_plan_mode`. MCP read-only annotations are **not** trusted: the server wrote them. |
| `accept_edits` | **Opt-in** (`allowed_modes` must list it). Approvals are waived for `write_file`, `edit_file`, `delete_file`, `rename_file` and `permissions.edit_tools`; every other gate asks as written. |
| `bypass` | **Opt-in**, never a `default_mode`. Every approval is waived except a plan's and any on `schedule_task`, `notify` or `save_workflow`. Setting it needs `approvals:bypass` (`approvals:write` is not enough), and every run checks the scope again for whoever drives the thread then, so a thread left in bypass is not bypassed for the next caller. Under `auth_mode=none` scope checks pass, so anyone may set it — development only. |

`allowed_modes` defaults to `[default, plan]`: each waiver mode waives approvals an author already
wrote, so allowing one is that author's decision. A stored waiver mode an agent does not allow
falls back to its `default_mode`.

**The plan approval.** `exit_plan_mode(plan)` is gated by the built-in `plan-approval` rule —
`one_shot`, `bind_principal`, a 30-minute TTL — and its grant is bound to the thread as well as the
plan text, so an approved plan is never a standing key out of plan mode for another thread, person
or later turn. No mode waives it. Approval returns the thread to `default_mode`. The approval
constrains the **model**, not the person driving the thread: a lease-holding driver can leave plan
mode with `POST /chat/mode` at any time, without approving anything.

**The scheduling approval.** With `spec.scheduling` (and `approval: true`, the default) the
`schedule_task` tool is gated by the built-in `scheduling.schedule_task` rule — `one_shot`,
`bind_principal` — unless one of the manifest's own rules names `schedule_task` literally and gates
every call to it; a glob such as `*` does not displace it, since the built-in literal is the rule
selected. No mode waives an approval on `schedule_task`, whichever rule asked for it
(`permission_mode.UNWAIVABLE_TOOLS`): a task's prompt runs later on its writer's authority with no
one watching, which `bypass` — a person present now — does not answer for. `approval: false` adds no
rule. A task belongs to its writer's subject, so a subject that names no person is refused: a
sub-less API key's `api_key`, an inbound trigger's `trigger:<id>`, a machine actor acting for no
one. A plugin authenticator that gives many people one subject is not detectable here; avoiding
that is the operator's responsibility.

**A task's answer goes back to its conversation.** When a task's firing answers, the answer is
added to the conversation the task was scheduled from (`spec.scheduling.report_back`, on by
default), so the person learns what it found and the next turn there can talk about it. The entry
is `custom`, in context, in the **assistant** role -- never `user`, because the text is a model's
answer, possibly over pages or tool output it read, and must not reach the next turn as the
person's own words. It lands only where a firing's `notify` may write (`jobs/origin_thread.py`):
the thread `schedule_task` recorded, for a job the person the firing acts for wrote, a chat thread
of the tenant's (no harness thread such as `job:`), one that exists, and never while a run is
going on in it, so a report never splits a turn. A firing that finds a run going on records
`report: queued`: the entry waits on the thread's row (the newest 20 are kept) and is added when
that run ends -- a turn's end, or a durable run's -- by whichever process ends it, taken once under
the row lock. While it waits it is masked as the log would mask it, and no read of the thread's
metadata returns it. A run that died with its process leaves it waiting for the thread's next run to
end; one that has waited more than a day is dropped instead. Delivery does not check again that
`report_back` is still on or the job still exists, and a thread deleted in the moment between the
firing's check and its queueing write can come back holding only the report.
A firing that fails reports that too, with the run's recorded error (written for readers, never the
exception's text), so the conversation hears the task did not finish rather than nothing. The
answer is cut at 16,000 characters; the job's run
history keeps it whole. The answer has had what any reply gets -- the firing manifest's reply
controls (PII guardrails, `final_response` judges) and secret masking on every write to the log --
and then the marker scan a `notify` gets (`screen_content`): an answer that reads as an injection
is recorded `report: screened` and not added, since the next turn there may run another manifest.
Each firing adds an entry, and only the task's newest is in context: the entry carries
`metadata.context_key` (`task_report:<name>`), and of the entries on a branch sharing a key only the
newest reaches the model, in every session strategy (`session.types.context_events`). The person
still sees them all, and a compaction's kept window drops a replaced entry on replay as a re-walk
does. A client's own keyed entries (`/chat/sessions/custom`) are stored as `client:<key>`, and a
queue write-back's (`/internal/sessions/{id}/events`) as `queue:<key>`, so neither can share a key
the harness writes and drop a task's report from context.
`report_back: false` leaves the answer in the run history alone.

**Agent-authored workflows.** `spec.workflow_authoring` binds `workflow_catalog`, `check_workflow`,
`save_workflow`, `list_my_workflows` and `run_workflow`. A workflow the agent saves is an ordinary
one, owned by the person the conversation acts for. The owner is their issuer-qualified key
(`issuer|subject`, `AuthContext.skill_owner`), so the same subject at another issuer is someone
else. `authored_via` records `agent:<manifest>`. The agent sees, changes and runs only workflows
holding both, never an operator's or another person's. The store checks the pair under the same row
lock as the revision (and before it), so a save cannot land on a workflow taken over in between.

A person's save through `PUT /workflows/{id}` takes the workflow over, and the agent can no longer
change it. Whether the agent may still run it depends on whose save it was, decided under the same
lock (`store._replaced_via`):

- The person it was made for (their issuer-qualified key matches `created_by`): it becomes
  `owner:agent:<manifest>`, and the agent may still run it for them. They wrote what it now holds.
- Anyone else: `authored_via` is cleared, and the agent may neither change nor run it. Otherwise
  that person's edit would run as the owner, through the owner's agent, without the owner having
  seen it. Once cleared, the owner's later saves do not bring the mark back.

New workflows get an id minted by the tool (`wf-<hex>`), so an agent cannot
re-create a deleted id that a job or trigger still names.

`save_workflow` is gated by the built-in `workflow_authoring.save_workflow` rule (`one_shot`,
`bind_principal`). It is displaced only by a manifest rule that names `save_workflow` literally and
gates every call, and no permission mode waives it: a saved workflow runs later as whoever starts
it, a job or a trigger included. The approver reads the whole definition, every field, as escaped
JSON. Text the model wrote sits inside quoted strings, so it cannot draw a line of the preview's
layout, and an agent step's prompt is capped at 4,000 characters so nothing is cut off. The tool's
own checks run first, so a call it would refuse never asks anyone. Those checks cover:

- the definition's problems;
- more than `max_steps` steps;
- a step kind outside `kinds` (an allowlist: `agent_turn`, `tool_call`, `approval`, `branch`);
- a step naming a manifest outside `manifests` (default: this one);
- a step naming a manifest whose inbound rules refuse the person (the same check a run's start makes,
  `engine.admitted_manifests`);
- the `max_workflows` cap. It is counted outside the store's lock, so separately approved saves racing
  at the cap can pass it; the tenant's 500 is the hard bound.

Saving and running refuse:

- a caller who is anonymous, uses a shared subject, or has no owner key;
- a scheduled or triggered run (`RequestContext.unattended`);
- any call made on a workflow's own thread, so a workflow cannot write or start another.

A run starts as the person through `engine.start_run`, which checks each step's manifest against
them. The run's trigger is `agent:<manifest>`, and every input is treated as untrusted, so each
agent step reads the inputs fenced as data.

A run's inputs are the model's choice, and a `tool_call` step receives them as tool arguments,
unfenced. Fencing applies to agent steps alone, and a fence is a convention the model is told to
honour: an agent step's model still picks its own tool calls from text the agent chose. Otherwise
only that manifest's own policies, screening and approvals govern them. `run_approval` decides when `run_workflow` waits for the
person:

- **`auto`** (the default): only when a value the call supplies can reach, through any chain of
  edges, a step that can change something:
  - a `tool_call` step whose tool changes things;
  - any `agent_turn` step. The value becomes its prompt and its model chooses the calls, through
    its sub-agents' tools as well as its own, and memory capture can keep what it read. A tool
    list read before the run cannot vouch for all of that, so no agent step is passed over;
  - a step of any kind but `approval` and `branch`. Step kinds are an open registry, so a plugin's
    kind counts as able to act.
  - A tool counts as changing nothing only if it is `read_only` and is not a peer. `task` and A2A
    agents act on what they are told, whatever plan mode makes of them. `read_only` does not mean
    nothing leaves: a `tool_call` step on `web_search` sends the value to the search backend
    without asking. Use `always` when that matters.
  - Two things make it ask instead of guessing: a tool the step's manifest no longer binds, or a
    manifest that does not resolve, counts as one that changes things, and a condition that fails,
    or a call it cannot admit yet, gates.
  - The rule (`workflow_authoring.run_workflow.auto`) is conditional because the compile hands that
    rule *object* to `apply_approvals(conditional=)`. A manifest rule cannot become conditional by
    copying its id.
- **`always`**: every run (`workflow_authoring.run_workflow`).
- **`never`**: no rule.

The approver reads the inputs as escaped JSON and every step they reach. The approval is bound to
the workflow's revision (`approval_binding`), and the run starts that revision, so a save landing
during the wait needs a new approval.

A manifest's own rule over `run_workflow` always wins:

- Under `auto`, any manifest rule that matches `run_workflow` (literal or glob, with or without
  `when_args`) means the harness adds no rule, so an existing gate is never loosened.
- Under `always`, a literal rule without `when_args` replaces the harness rule.

`Tool.approval_condition` is consulted only for the harness's rule, so a tool cannot exempt itself
from an operator's gate. Unlike the save's, this approval can be waived by a permission mode
(`bypass`): the run starts in front of the person, though its steps run later with nobody watching.
`max_active_runs`
(default 3) bounds the runs one person's agent can hold going, so a looping agent cannot fill the
tenant's run quota.

A saved workflow also runs as whoever else starts it: anyone holding `workflows:write`, from the
console, with their own authority. A job is pinned to the revision its writer saw, and a
trigger to the `revision` its config names, which every workflow trigger must: an agent's later
save starts neither until someone pins it again. The person's approval of a save never decides
what an outside sender starts.

`workflow_catalog` relays other manifests' tool descriptions and schemas, which is third-party text,
and is marked `relays_untrusted` so content screening reads it.

**Notices.** `spec.notify` binds `notify`, which sends a title (80 characters) and a message (500) as
a Web Push to the person the run acts for (`acting_for`) — only to browsers that person subscribed
themselves (`POST /push/subscriptions`), never the tenant's. The tool takes no recipient, so a model
cannot be steered into telling someone else; a run for no one (anonymous, no subject) or for a
shared subject (a sub-less API key's `api_key`, a trigger's `trigger:<id>`, a machine actor acting
for no one — `felix/auth/actors.py`) is refused. Before anything leaves, in order: known secret
values are redacted (the masking wrapper covers a tool's *output*, and a notice's text is its
arguments; personal data is not redacted); links are removed — after NFKC normalization and with
invisible format characters deleted, scheme URLs, `www.` hosts, bare `host.tld/path` in any script,
a host with a port, IPv4 and IPv6 addresses and the usual obfuscations (`hxxp`, `[.]`, `(dot)`,
`%2E`, lookalike dots, a host split across lines) become `[link removed]`, so an injected prompt
cannot put a phishing target on a lock screen; and the text is screened, always by the free
marker scan and, when the manifest's `content_screening` is enabled, by its model and decider too,
refusing the call (`screened`) on a flag or when a screener could not run. The text is then
encrypted end to end to the browser (RFC 8291); the push service carries ciphertext, though the
notice may show on that person's lock screen. Unlike an approval or question push, which reach the
whole tenant and carry metadata only, a notice carries text because it is addressed. If a task's
firing sends it, it links to the chat thread the task was scheduled from, and the same cleaned text
is left in that thread, before the push goes, so it is there when the person opens it — even when
no browser took the push, which then is the only place the person sees it. Which thread that is
rests on a record only `schedule_task` writes (`payload.scheduled_by`): `PUT /jobs` ignores one in
the body, so a job written there cannot name somebody else's conversation. A task re-saved by its
own writer (pausing or resuming it re-sends the whole job) keeps the stored record and stays a task;
re-saved by anyone else it becomes an ordinary job (it no longer lists as a task, and its notices
link to its own thread), since keeping the record would let them write into its writer's
conversation. The record is used only for a task the person wrote, and only for a chat
thread of the tenant's own — a suffix in plain id characters with no `:`, which no chat client can
create and every thread the harness mints for itself (`job:`, `channel:`, `a2a:`, `workflow:`,
`fiber:`) has. The entry is for the person and never the model: a `custom` entry
(`metadata.type: notice`) that no prompt is built from, so text a model wrote in one run never
becomes context for another run in a conversation it was not part of. Clients show it, and a client
watching the thread's stream gets it live. It is never written into a thread that does not exist (a
notice creates no conversation) nor while a run is going on in it — a turn that marked itself, from
before its first append to after its last, or a durable run not yet finished — since an entry
landing inside a run would sit between a message and its reply; the push still goes. Without Redis
the turn's mark is per process, so a firing on the worker cannot see a turn running in the API. A
client holding the thread's exclusive lease is not a run: it is the person, watching. The run's own
thread gets nothing, since that conversation already shows the call and the answer. The result
names the channels that delivered it (`["push", "thread"]`, `"push"` only when a browser took it)
and what became of it in the thread (`thread`: `appended`, `run_live`, `missing`, `failed`, or
`not_eligible` when there is no other chat thread to leave it in).

Limits: `max_per_hour` (default 10) per person, per manifest, per tenant, in the shared rate-limit
store, is the real bound; `max_per_run` (default 3) counts per request context, so a durable run's
resume, a background child and a workflow step each start their own count. A call takes its run
slot before it awaits anything, so a turn's parallel calls cannot all pass the cap; it gives the slot
back only if the hourly limit refuses it, and otherwise counts against both, delivered or not. With
`approval: true` the approval preview does not check the hourly limit (the limiter can only count),
so an over-limit call can open an approval row and is refused when it runs. There is no approval by default, since the
runs a notice is for — a scheduled task's firing — have nobody watching; `approval: true` adds a
`notify.notify` rule (`one_shot`, `bind_principal`), and no permission mode waives an approval on
`notify`, whichever rule asked (`permission_mode.UNWAIVABLE_TOOLS`). Each call is audited as
`notify.sent` with the delivery count, the channels that delivered it, the `thread` outcome, the
thread and the recipient, never the text.

**Enforcement.** The `permission mode` wrapper sits just outside approvals, so plan mode refuses
before anyone is asked; approvals asks `waives_approval` itself. A run caches only the stored mode
and the caller's bypass verdict, and each agent — a router's children, a `task` child — resolves
its own mode from them on every call, so a child that does not offer plan mode cannot take the
run out of it, and a waiver needs the child's own manifest to allow the mode. An unreadable stored
mode is read as `plan` (fail closed).

**Audit.** Every mode change is `permission_mode_change` (`payload.via`: `route` or
`exit_plan_mode`), and every approval a mode waived is `approval_waived` with the tool and the mode,
plus the `felix_approval_waived` counter.

`deep`'s `plan_create` / `plan_update_step` are added by the pattern after the wrapper stack and
write only the plans store; they are not governed by plan mode.

## Manifest hooks

`spec.hooks` sends a run's lifecycle events to an endpoint the **operator** registered in
`FELIX_WEBHOOK_ENDPOINTS` **with `"hooks": true`** — a manifest names an id, never a URL, and an
endpoint not open to the run's tenant, or not opened to hooks, is never contacted. Registering an
endpoint for run notifications does not open it to hooks: a hook sends far more. Each call is a
Standard Webhooks–signed POST (the endpoint's secret), egress-pinned unless the endpoint is marked
`private`, bounded by the hook's `timeout_ms` (≤ 10 s), all of one event's hooks together by 15 s,
and the answer by 64 KiB.

| Event | Sent | A `block` answer |
|---|---|---|
| `session_start` | the thread's first run | refuses the run (`422 blocked_by_hook:<id>: <reason>`; a typed `blocked_by_hook` error frame on a stream) |
| `user_prompt_submit` | each run, after inbound screening, before the model | refuses the run, nothing written |
| `pre_tool_use` | before a call to a **bound** tool (`tools` globs narrow it) | refuses the call (`[hook denied]` with the reason screened and fenced; audited as `policy_deny`) |
| `post_tool_use` | after a call returns or raises, with its result | — |
| `stop` | when the agent would finish, with its answer | sends it back (≤ 3 per run, never on the last step) |
| `subagent_stop` | when a `task` child finishes, with its answer | — |

**A hook on this host: run a sidecar, not a command.** There is no `type: command` handler, on
purpose. Felix can exec only where its shell tools do — the hosted sandbox or the shell runner,
inside the agent's workspace — and that is exactly where a governance hook must not run: the agent
can write there (`write_file`, a shell tool, the repository it checked out), so it could rewrite the
script that decides its own calls, or plant a module the hook imports (`python3 -c` and `-m` put
the working directory on `sys.path`; `git` reads the repository's `.git/config`). A hook that the
thing it governs can edit governs nothing. Run the hook as a small HTTP service the agent cannot
reach the files of — a sidecar container or a process beside the API — and register it as a hooks
endpoint marked `private`:

```json
{"local-policy": {"url": "https://policy.internal:9100/hook", "secret": "secret:POLICY_HOOK",
                  "tenants": ["acme"], "private": true, "hooks": true}}
```

`private: true` lets it resolve to a private address; outside development the URL must still be
https (an internal certificate, or a mesh that terminates TLS). The service verifies the Standard
Webhooks signature, reads the event from the body, and answers the same JSON.

**Where `pre_tool_use` sits.** In the tool runner, before the governance wrappers: a call's
arguments reach the hook before policies, permission mode and approvals see the call. An `allow`
cannot bypass any of them; a `block` refuses first. A tool name the agent does not have is refused
without asking any hook.

**What leaves the deployment.** The prompt, a bound tool's arguments and result (after secret
masking, which runs inside the tool), and the agent's answer, each cut to 32,000 characters — only
to an endpoint the operator opened to hooks for the tenant.

**What comes back.** `additional_context` and a `stop` reason are the hook's text and may relay what
it was sent, so each is screened like an untrusted tool result (`[quarantined] …` when flagged),
fenced as reference material from that hook, capped at 8,000 characters per event across all its
hooks, and **transient**: attached to the run's first model call, to the tool result, or — for
`stop` — to the next model call only. None is stored as a turn a later run would replay.

**Failure.** An unreachable or slow endpoint, a non-2xx status, or an answer outside the contract is
an error, and the hook's `on_error` decides: `allow` (the default — advisory) carries on, `block`
refuses what the hook guards, so a hook used as a control fails closed. A `stop` hook that errors
lets the agent finish whatever `on_error` says. A `stop` continuation leaves the answer it rejected
in the transcript, followed by the next one: two assistant turns in a row on replay, which both
wires accept (the Messages API combines them). Every call is a `hook_call` audit row and
`felix_hook_calls`.

Hooks fire only on patterns that run them (`react`, `deep`); a manifest with `spec.hooks` on any
other pattern is refused at compile. A durable run re-sends `session_start`/`user_prompt_submit`
when a crashed run resumes. These are distinct from the in-process plugin hooks in `felix.hooks`,
which are global, Python, and fail open.

## Session leases

A lease keeps two clients from driving one thread at once. It holds **one exclusive hold**
(`mode: exclusive` — the client driving the thread, reported as `locked`) and **any number of
observer holds** (`mode: shared` — read-only watchers, reported as `attached`). Each hold has
its own token and its own expiry.

- An `exclusive` acquire by another holder while the exclusive hold lives is `409 lease_held`.
  Observers never block one: a thread held only by observers is free to drive.
- A `shared` acquire always succeeds, with a token of its own — never the exclusive hold's —
  and `held_by_other: true` when someone else is driving. This is what a second browser tab
  falls back to on `409`.
- Re-acquiring renews the caller's own hold and nothing else, and only with that hold's
  `token`. A holder id is no proof — every status publishes the exclusive holder's and
  `GET …/lease` every observer's, and a duplicated browser tab copies its own — so a
  re-acquire by holder id alone is `409 lease_held` and learns no token. (A duplicated tab
  then falls back to `shared` and observes.) An observer's renewal does not extend the
  exclusive hold, so an observer that keeps renewing cannot keep a closed tab's exclusive hold
  alive.
- Releasing drops the one hold the token names, and needs it: no token is `403
  token_required`, and a `holder_id` sent alongside must be that hold's. `409 lease_contended`
  means concurrent changes kept the release from landing; retry. When the exclusive hold is
  released or lapses its observers stay observers; none is promoted, and a client that wants
  to drive takes the exclusive hold itself.
- `GET /chat/sessions/{id}/lease` reports the exclusive holder (`holder_id`, `expires_at`) and
  every observer (`observer_holds`, each with its own `expires_at`).

**Leases are advisory unless a client opts in.** The routes that drive a thread — `/chat`,
`/chat/stream`, `/chat/continue`, `/chat/abort`, `/chat/rewind`, `/chat/steer`,
`/chat/tool_result`, `/chat/ui`, `/chat/compact`, `/chat/thinking`, `/chat/sessions/name`,
`/chat/sessions/label`, `/chat/sessions/custom` and `DELETE /chat/history/{id}` — accept an
`X-Felix-Lease-Token` header. When it is present the request is refused unless it is the
exclusive hold's token: an observer's is `409 lease_read_only`, and a token whose hold another
holder has since taken is `409 lease_held`. A request without the header is not checked, so a
caller that never takes a lease (a script, `/v1`, A2A) is unaffected — which also means the
header protects a client from its own mistakes, not the thread from a caller that omits it.
`/chat/fork` only reads its source and `/chat/sessions/feedback` rates a reply without writing
the log, so neither checks it. Nor does `/chat/ask`, which writes nothing to the thread; see
below. Approvals are decided through `/approvals`, gated on the
`approvals:write` scope, not on a lease.

**`FELIX_LEASE_ENFORCE=strict` makes the lease binding** on those same routes. A request
*without* the header is then `409 lease_held` while another holder has the thread
exclusively. A thread nobody holds, or one only observers hold, still takes a request without
a token, so scripts and `/v1` keep working whenever no one is driving; with the header the
rules above apply unchanged. `/v1/chat/completions` is checked too when it sends `user`,
because `user` names the same thread as a chat `thread_id` (`{tenant}:{user}`): a `/v1`
call is refused on a thread a chat client drives, and may send `X-Felix-Lease-Token` itself.
A2A threads (`{tenant}:a2a:{task}`) and MCP calls (no thread) cannot reach a chat thread, and
are not checked. The default, `advisory`, is the behaviour described above. Strict is still a
consistency control between cooperating clients rather than an authorization boundary: every
caller in the tenant may acquire a lease, and with no lease taken nothing is refused.

Leases live in Redis so they hold across replicas, and every transition is one `WATCH`/`MULTI`
transaction, so two replicas cannot both grant the exclusive hold. Without Redis they fall back
to per-process state, where each replica can grant its own.

## Side questions

`POST /chat/ask` answers one question about a thread without adding to it, and it is governed as
a turn of that thread is. The manifest is the thread's own, the one its last turn ran under
(`404 unknown_thread` for a thread with none); the caller does not choose it. Before any model
call it passes the turn's admission: the manifest's `spec.auth.inbound`, its compile pin
(`409` on drift, as a turn gets), `validate_governance` including `auth.outbound.providers`, and
the measurable-cost check. The question goes through the manifest's input screening (content
screening and input PII), the rendered history through its replay image screening, and the
answer through its reply controls: PII redacted or blocked, and a final-response judge's denial
returned as `status: withheld` with the denial as the answer (a PII block reads `withheld` too).
The history reaches the model as one transcript, fenced as untrusted data the way a summariser's
is, so thread content written to look like the transcript's end and a question of its own stays
inside the fence. A manifest with `sub_agents` is asked under a neutral prompt rather than its own,
which is a router's or planner's; its admission and controls still apply.

What it skips is every write to the thread. The history is rendered from the stored summary and
the window a pass would keep (`stored_summary_only` in the render options), with no summariser
call and no `before_compact` / `compact_failed` hook, so it costs one model call however long the
thread is; nothing is appended to the session log, no lease is taken or checked, no phase, pin,
steer or follow-up is touched, and this process's leaf index for the thread is not moved. (On
Postgres, reading the leaf of a thread whose state row predates leaf tracking adopts it, as an
export or a snapshot does.) A plugin session strategy that summarises should honour
`stored_summary_only`; one that does not still cannot write, but its summary call is billed. It is metered under the manifest and audited as `side_question`.

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

`/metrics` requires authentication and the `metrics:read` scope: its label values include
tenant-supplied manifest ids and remote MCP tool names, across every tenant, so a tenant's own
credential must not read it.

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

`POST /chat/stream` honours `Idempotency-Key` too, scoped to the principal **and the thread**
(so it needs a `thread_id`; without one it is `400 idempotency_key_requires_thread_id`), in the
same store and for the same TTL. The key is claimed after inbound auth and screening. A resend
while the first stream is still running is `409 idempotency_in_progress` — reattach with
`GET /chat/stream/{thread_id}`. A resend after it ended runs nothing: it streams back, with
`Idempotent-Replayed: true`, the session events that request itself appended (each event is
stamped with the request's origin, so a label, a steer or another turn landing on the thread
meanwhile is not replayed) as `session_event` frames, then the first stream's `event: error`
frame if it ended in one, then `[DONE]` — or it reattaches to the durable run the first
request started. A first request that appended nothing of its own — it failed before its turn
began, or the client left before the body was sent — frees the key, so the resend runs. One
torn down after its user message landed (a client disconnect ends a transient turn) keeps
it: the resend replays that message rather than sending it twice, and the client continues
with `/chat/continue` or a new message.

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
- **Every request verifies its token; there is no verification cache, by decision
  (2026-10-02).** A cache keyed on the token would save the signature check, but a "valid"
  cached before a key rotation would outlive the rotation for as long as the entry lives. Without
  one, restarting with a rotated `FELIX_JWKS_PRIVATE`/`_PUBLIC` (settings are read at startup)
  revokes every self-issued token from the first request, and no cached verdict lingers past it.
  A remote issuer's rotation still waits for its key-set refresh (below).
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

**Tenants are the orgs sharing one install; tenants and API keys are configuration, by decision
(2026-10-02).** Tenancy exists so that several organisations can use the same Felix install, each
isolated from the others: their sessions, memory, manifests, audit and usage are kept apart by
`tenant_id`. There is no tenant table and no key table. A tenant is the string a credential
carries, and an API key is an entry in `FELIX_AUTH_API_KEYS`, so adding an org or issuing or
revoking a key is the install operator's config edit and a restart. An org's people do not need
the operator for each login: `FELIX_GITHUB_ORG_TENANTS` maps their GitHub org to its tenant, so
[GitHub login](#github-login) lets any active member in, and an external IdP under `jwt` does the
same. What an org cannot do is administer its own tenant — mint its own API keys, or be created
without the operator. A tenant or key API for that is not planned.

**One exception, by decision (2026-10-05): a person can be a tenant.** With
[signup](#signup) on, a GitHub account in no mapped org gets a *personal* tenant, `gh-<GitHub
id>`, that comes into existence the first time it signs in. The operator still decides who may
(`FELIX_GITHUB_SIGNUP_LOGINS`) and what such a tenant may do (`FELIX_GITHUB_SIGNUP_SCOPES`); what
changed is that the tenant itself is not listed anywhere in configuration. Organisations are
still configured.

`tenant_id` is the isolation boundary and, in the default `claim` mode, it arrives in a
token claim. Inside a tenant, long-term memory keeps each person's facts from another person's
agents by the store's own filter on the person key (`issuer|subject`), not by RLS — see
[Memory: whose memory an agent reads](#memory-whose-memory-an-agent-reads). Constrain it:

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
| Skill jobs (improvements, evaluations) | `run_skill_jobs` (`skill_jobs`, every minute) | queued jobs in any tenant, claimed one at a time and fairly: the oldest job of the tenant whose last claim is oldest |

Each sweep isolates a tenant's failure so one tenant's bad data cannot stop detection
for the rest, and each takes an RLS bypass for the enumeration only — the per-tenant
work that follows runs scoped. `felix-scheduler` must be running alongside
`felix-worker` or none of them fire at all.

## GitHub login

Two public routes hand out self-issued tokens (`iss: felix-self`), verified by the
`self:felix-self` verifier like any other. Nothing revokes one before it expires, so its TTL is
the window a leaked token stays good. Both log in through `FELIX_GITHUB_ORG_TENANTS`, which maps
a GitHub org, pinned by numeric id, to a tenant and scopes.

| Path | On while | Who gets a token | TTL |
|---|---|---|---|
| `POST /auth/github/device` + `/token` | `FELIX_GITHUB_CLIENT_ID` | An *active member* of a mapped org, after the device flow — or an invited account, into its own tenant ([signup](#signup)) | `FELIX_GITHUB_LOGIN_TTL_SECONDS` (8 h, max 1 d) |
| `POST /auth/github/actions` | `FELIX_GITHUB_OIDC_AUDIENCE` | A GitHub Actions run its org's `actions` block admits | `FELIX_GITHUB_OIDC_TTL_SECONDS` (15 m, max 1 h) |

- **Device flow.** Anyone can start a flow and ask a member to approve its code (consent
  phishing), so name the OAuth app plainly. Starts are capped per client and per deployment
  (`FELIX_GITHUB_DEVICE_STARTS_PER_HOUR[_TOTAL]`). Reaching the deployment cap refuses every new
  login until the hour turns, and issued tokens are unaffected.
- **Actions exchange.** A valid ID token proves only that *some* workflow ran in a repository,
  so the grant has to say which:
  - **Repositories** are pinned by id. The org alone is not enough, because an outside
    collaborator with write access to one repository can run a workflow there.
  - **Narrowing is required:** `refs`, `workflows` (`job_workflow_ref`, the file that ran) or
    `environments`.
  - **`pull_request_target`, `issue_comment` and `workflow_run`** run on the base branch while
    acting on outside input, so they are refused unless the block lists them in `events`.
  - **A protected environment** with required reviewers is the strongest narrowing GitHub offers.
  - **The audience** must be this deployment's own https URL. A shared one lets a token captured
    by a weaker deployment be replayed to a stronger one. Boot refuses GitHub's default audience,
    the owner's `https://github.com/...` URL, which tokens meant for other services carry.
  - **ID tokens are not single-use.** A replay within their few minutes mints another token for
    the same run.
- **Audit:** `github_login` (the GitHub user) and `github_actions_login` (repository, ref,
  workflow file, event, run and actors), each recorded in the tenant the token is for. Neither
  a device code nor an ID token is logged or audited.

### Signup

A GitHub account in none of the mapped orgs is `not_a_member` unless signup is on. With
`FELIX_GITHUB_SIGNUP=invite`, the accounts listed in `FELIX_GITHUB_SIGNUP_LOGINS` sign in — by
device or redirect — to a tenant of their own, and every other account is refused with
`403 not_invited`.

```bash
FELIX_GITHUB_SIGNUP=invite
FELIX_GITHUB_SIGNUP_LOGINS=octocat:583231,hubot   # login, or login:<numeric id>
FELIX_GITHUB_SIGNUP_SCOPES=memory:read,audit:read
```

- **The tenant is `gh-<numeric GitHub id>`**, never the login: a login can be renamed and then
  registered by someone else, and the tenant must not follow it.
- **Pin each invite** as `login:<id>` (`gh api users/<login> --jq .id`). A pinned invite is
  matched on the id alone, so a renamed account keeps it and a re-registered login does not
  inherit it. A bare login is matched case-insensitively, and each match logs the id to pin.
- **Org membership wins.** An invited account that is also an active member of a mapped org
  lands in the org's tenant, as before; nobody is offered a personal tenant beside one.
- **Who may claim a personal tenant.** It is in no `FELIX_ALLOWED_TENANTS`, so the verifier
  admits a `gh-<id>` claim only from a `self:felix-self` token with `idp: github` and subject
  `github:<id>` — this deployment's own sign-in, for that account. A claim of one from any other
  issuer, or for another subject, is refused. With signup off, `gh-<id>` is an ordinary tenant
  id, and boot refuses one configured anywhere (verifier, allowlist, API key, org map) while
  signup is on.
- **Scopes are required** while signup is on, and boot refuses `admin` or `*` among them: they
  are what a stranger's token carries. Leave out the operator's own (`manifests:write`, jobs,
  keys).
- **There is no `open` mode.** Every personal tenant spends this deployment's model credentials,
  and nothing yet caps one tenant's total spend — `limits.max_cost_usd` is per run, and fails open
  for a model the pricing catalog does not price. Admitting any GitHub account waits for that cap.
- **Audit:** the first `github_login` in a personal tenant carries `signup: true`. A refusal has
  no tenant to be audited in; it is logged, with the login and GitHub id.
- Changing the list is a config edit and a restart, like the org map. `GET /auth/methods` reports
  the mode as `github_signup`.

## Stored connections

A tool that acts as a person -- reads their calendar, drafts their mail -- needs that person's own
grant, not an operator's key. `felix.auth.connections` keeps one row per person, provider and
account (`connections`, tenant RLS like every tenant table), written by a provider's consent flow
or a credential the person hands over, and read by tools at run time for the person the run acts
for (`AuthContext.person`: the caller, or the person a scheduled job or resumed run works for).
GitHub's connection predates it and keeps its own table (see [GitHub login](#github-login)).

- **Sealed at rest.** Every credential -- an OAuth refresh and access token, an app password -- is
  AES-GCM sealed with `FELIX_CONNECTION_TOKEN_KEY` (falling back to `FELIX_GITHUB_TOKEN_KEY`),
  with the tenant, the person, the provider, the account and the column bound in as associated
  data: a sealed value copied to another row or column does not open. The key is hydrated from a
  secrets backend and masked like any credential. Rotating it means every connection is re-made.
- **Never handed out.** Only code inside the harness process opens one. No route returns a
  credential; `GET /connections` returns the caller's own (provider, account, label, kind,
  status, scopes, times), and `GET /connections/tenant` everyone's in the tenant
  (`connections:admin`).
- **One writer at a time.** A refresh, a re-consent and a removal all take the row's lock (a
  Postgres advisory lock, with a 40 s `lock_timeout`), so replicas refreshing at once spend a
  rotating refresh token once, a refresh in flight never lands on top of a new consent, and a
  removal withdraws the grant the refresh stored rather than orphaning it. A provider's refresh is
  cut off after 30 s. One the provider refuses marks the row `revoked`; an outage revokes nothing
  and is reported as `ProviderFailed` in words Felix wrote -- whatever the provider raised can hold
  a token, and goes to the log by type alone.
- **Revocable.** `DELETE /connections/{provider}/{account}` (the person's own) and
  `POST /connections/tenant/revoke` (`connections:admin`) delete the row first and regardless,
  then withdraw the grant at the provider, best effort. Every store, removal, refusal and expiry
  is audited (`connection`), naming the person and the provider, never the credential.
- **Who "the person" is.** A person key, `issuer|subject` (`felix/auth/persons.py`), worked out
  by the auth middleware from the verified principal and carried by the recorded caller into a
  durable run or a job's firing -- the key long-term memory's person pools use, without
  `FELIX_PRINCIPAL_ALIASES` (aliases are memory's alone). A subject is only unique within its
  issuer, so a token another issuer signs with the `sub` of a Felix sign-in is someone else here,
  and so is an API key whose `sub` matches. A credential many callers share names no person and
  holds nothing: a sub-less API key, a GitHub Actions workflow, an inbound trigger, a machine actor
  acting for no one (403 `not_a_person` on the routes). Work recorded before the person key
  existed carries none, and finds no connection.
- A call that names no account, for a person with two at one provider, is refused
  (`AmbiguousConnection`) rather than picking one.

### Connecting Google

`POST /connections/google/start` (a person, signed in) answers with Google's consent URL for the
scopes they chose -- `calendar_read`, `calendar_events`, `gmail_read`, `gmail_compose`,
`drive_read`, plus `openid email` to learn which account it is -- asking for an offline grant
(`access_type=offline`, `prompt=consent`, `include_granted_scopes`). Its response sets a sealed
HttpOnly flow cookie carrying the state, nonce, PKCE verifier, the person and the tenant (named
`__Host-…` over https, so no sibling subdomain or plain-http answer can plant one). Google returns
the browser to `GET /connections/google/callback`, the one public connections path (a navigation
carries no bearer): a state that does not match the cookie is refused before the code is
exchanged, and a consent link finished in a browser with no cookie stores nothing. The callback
verifies the ID token as sign-in does and **stores nothing**: it leaves the grant sealed in a
Strict, two-minute, one-use cookie, and the app keeps it with `POST /connections/google/finish` as
the person -- refused (403 `not_your_connection`) unless that is the person who started, and the
grant then dropped. So even a planted flow cookie cannot put one person's Google grant in another's
vault. It is dropped rather than revoked at Google: a revoke ends that account's whole grant with
the client, so a planted flow could otherwise disconnect someone's real connections on purpose.
Over https only the `__Host-` cookie names are read. The grant is kept as the Google account's `sub`, labelled with its verified
email, with the scopes Google says it carries (granular consent may grant fewer than asked;
`include_granted_scopes` adds earlier ones). A grant without a refresh token is not kept.

- Same OAuth client as Google sign-in, with a second redirect URI registered on it
  (`<origin>FELIX_GOOGLE_CONNECT_CALLBACK_PATH`). On while sign-in's client, cookie key and origins
  and a vault key are all set; otherwise the routes answer 404 and the callback is not public.
- `FELIX_GOOGLE_ACCOUNTS` governs who signs in, not what they connect: any person may connect a
  Google account of theirs, which need not be the one they sign in with.
- **The worker refreshes too.** A durable run or a job acting for someone refreshes their grant in
  the worker, so it holds `FELIX_CONNECTION_TOKEN_KEY` and Google's client id and secret. None of
  them mints a Felix token. Set the vault's own key: the fallback to `FELIX_GITHUB_TOKEN_KEY` works
  only where that key is, which under Compose is the api alone.
- **The tools** (`felix-connectors`, the `google` extra) act as `AuthContext.person` -- the person
  the run acts for, a scheduled job's writer or a resumed run's starter included -- and nobody
  else: a run acting for no one person gets no Google account. Each checks the connection granted
  a scope it needs before asking Google. Google's own client builds the request; Felix's
  egress-guarded client sends it, so `network.allow_hosts` and the SSRF guard hold
  (`www.googleapis.com`, `gmail.googleapis.com`). Every answer is untrusted text (mail and shared
  files are anyone's): enable content screening. `connectors__calendar_create_event` and
  `connectors__gmail_draft` change things, so they are not read-only (plan mode holds them, a
  resumed run does not repeat them) and belong behind approvals. Nothing sends: there is no mail
  send, and an event is created with no guests and no notification -- an invitation is mail from
  the person's account, and an instruction hidden in a message or a shared Doc could otherwise
  address one, with what else the agent read in its description, to anyone. An event's id is
  derived from the person, title and time, so a call repeated after a timeout finds the event it
  made. A draft can still stage text for the person to send: approve drafts with care.
- Removal revokes the refresh token at Google, best effort (a refusal is logged as
  `connection_revoke_failed`). At Google that ends the whole grant between that Google account and
  the OAuth client -- so the same Google account connected by another person, or in another
  tenant, stops working too and is marked `revoked` on its next use.

### Connecting iCloud

Apple has no OAuth for iCloud data, so `POST /connections/icloud` (a person, signed in; mounted by
the `felix-connectors` plugin with its `icloud` extra) takes their Apple ID and an **app-specific
password** made at appleid.apple.com. Only that format (`xxxx-xxxx-xxxx-xxxx`) is accepted: an
Apple ID's own password is refused before anything is sent, and an app-specific one can be revoked
at Apple without touching the account. The pair is checked with Apple first -- the CalDAV
principal lookup the tools start from -- and kept only if Apple accepts it, sealed in the person's
vault (provider `icloud`, kind `secret`). It answers 404 without a vault key, 403 to a caller that
names no person, 429 past five tries a person makes in an hour (so the route is no way to test
Apple credentials in bulk from this deployment's address), and a refusal as a written code
(`invalid_body`, `app_password_format`, `apple_id_invalid`, `app_password_refused`,
`icloud_unavailable`, `connect_failed`); the password is never logged, returned or put in an
error -- the body is read by the route, not FastAPI, whose 422 would echo it. Disconnecting (`DELETE /connections/icloud/{apple_id}`) forgets it here; the person revokes
it at Apple.

- An app-specific password opens the person's whole iCloud calendar and contacts, read and write,
  for as long as it lives: there are no scopes to narrow. Grant the tools per manifest.
- **The tools** (`connectors__icloud_calendar_events`, `_calendar_create_event`,
  `_contacts_search`) act as `AuthContext.person`, as the Google tools do. Felix builds the
  CalDAV and CardDAV requests and its egress-guarded client sends them, held to `*.icloud.com`:
  discovery names a partition host of the person's (`p42-caldav.icloud.com`), from Apple's answer,
  never the model's. A manifest with `network.allow_hosts` lists `*.icloud.com`. An answer that
  declares a DTD is refused unparsed, and a redirect is an error, never a success. Reading covers
  every calendar the person sees, shared ones included, and every answer is untrusted text: enable
  content screening. A repeating event is expanded only after its cost is estimated -- a rule
  repeating each second, which anyone can put on the calendar with an invitation, is left out and
  the answer says so. Adding an event is not read-only and belongs behind an approval. It writes
  only to a calendar that is the person's own and writable, never one shared with them (whose
  owner reads it) and never one picked between two of the same name; it writes no ORGANIZER or
  ATTENDEE, so nothing is mailed; and its UID is derived from the person, title and time, so a
  retry finds the event it made (`If-None-Match: *`, 412). Reminders are not offered: Apple
  stopped serving upgraded Reminders lists over CalDAV.

## Management API scopes

When `FELIX_AUTH_MODE` is `jwt` or `api_key`, management routes require scopes
(skipped for `auth_mode=none`). `admin` or `*` bypasses checks; `*:write`
implies the matching `*:read`.

| Scope | Routes |
|-------|--------|
| `manifests:read` / `manifests:write` | `/manifests`; `GET /manifests/{name}/versions` lists stored versions (metadata only) under `manifests:read` |
| `audit:read` | `/audit` |
| `artifacts:read` | `/artifacts` — read back a tool output too large to keep in the transcript. Its own scope rather than part of `audit:read`, because a spilled result is raw tool output and often the most sensitive data a run touches. The model's own way back, the `read_artifact` tool bound beside `spec.artifacts`, checks no scope and is held to something narrower instead: it reads only what its own conversation spilled (the thread, or the request when there is none), so a leaked id does not reach another caller's run through the model. Spill is kept for `FELIX_ARTIFACT_RETENTION_DAYS` (30; `0` keeps forever) and then swept, objects and ledger row together — so evidence meant to outlive that belongs in the audit log, not in an artifact |
| `approvals:bypass` | Setting a thread's permission mode to `bypass` (`POST /chat/mode`), and having a run honour it — see [Permission modes](#permission-modes) |
| `approvals:read` / `approvals:write` | `/approvals`; `approvals:read` also gates the `approval_required` frames on a durable `POST /chat/stream`, and every `/push` route — a push subscription is a standing request to be told about approvals. A decision lands only on a row still pending and inside its deadline (`409 approval_already_decided` / `approval_expired` otherwise), and and, with `FELIX_APPROVALS_REQUIRE_SECOND_PERSON=true` (off by default), never from the principal whose run asked (`403 self_approval_forbidden`) unless they hold `admin`/`*` — the same principal, by subject: a person signed in by GitHub and by Google is two, so Felix refuses to start with the rule on while one tenant has both a Google account and a GitHub org grant — the plan approval excepted, since it is how its asker leaves plan mode. Off, a person may answer their own run's approvals, the ordinary single-user loop. `approvals:write` is also what loosening a thread's permission mode needs — see [Permission modes](#permission-modes) |
| `jobs:read` / `jobs:write` | `/jobs`. A job runs as principal `cron` on behalf of whoever last wrote it (`PUT`), with that writer's scopes, scheme and manifest pin as recorded then; `GET /jobs` shows `created_by` and `recorded_at`. Every firing goes through the manifest's inbound auth and the thread's compile pin, so writing a job reaches no manifest its writer could not run; a refusal is the run's error. For a writer who authenticated with an API key, each firing re-reads `FELIX_AUTH_API_KEYS`: the scopes are cut to what that key (by `sub`, in the job's tenant) holds now, and a key no longer configured refuses the run. A writer who signed in through Felix is re-checked the same way: a Google writer's `FELIX_GOOGLE_ACCOUNTS` entry (by the email recorded at the write, matching its `sub` pin) must still name the job's tenant, a GitHub writer's tenant must still have an org grant in `FELIX_GITHUB_ORG_TENANTS` (org membership itself is not asked at firing), and a GitHub Actions writer's repository must still be let in; the scopes are cut to what that entry or grant holds, and none left refuses the run. A writer whose token another issuer signed keeps the grant recorded at the write — Felix keeps nothing of that token to re-check, so re-save or delete the job when such a person leaves. `POST /jobs/{name}/run` runs a job now and needs `jobs:write` *and* to be the job's writer or an operator (`admin`/`*`): the run carries the writer's authority, so someone else asking would choose when the writer's run happens — including a job the writer disabled. Rewriting the job instead makes it theirs, with their scopes. The caller's subject is recorded on a manual run as `requested_by`. A job written before writers were recorded runs with no scopes and only an operator may run it by hand. A job may name a workflow (`workflow_id`) instead of a manifest: each firing starts a run with `payload.inputs`, as `cron` for the writer, and the run records the writer as its starter (`started_by`, trigger `job:<name>`). Writing one needs `workflows:write` beside `jobs:write`, and each firing checks the recorded scopes still hold it — the same grant starting a run over HTTP needs — and every manifest its steps name against the writer, as at any start. **The job is held to the workflow revision current when it was written** — or the one the writer names as `workflow_revision`, which an editor should send (412 `workflow_changed` if it is no longer current): once anyone saves the workflow again, each firing is refused until the writer re-saves the job, so a `workflows:write` holder cannot put steps of their choosing in front of someone else's scopes. A job-started run is not clamped to a token's `exp` (the job keeps no token): it has the full 24 hours, with the writer's grant as recorded or, for an API key, as that key holds it at the firing |
| `plans:read` / `plans:write` | `/plans` |
| `connections:admin` | `GET /connections/tenant` (every person's stored connections in the tenant, never a credential) and `POST /connections/tenant/revoke` (remove one and withdraw its grant). A person's own `GET /connections` and `DELETE /connections/{provider}/{account}` need no scope: they are theirs. See [Stored connections](#stored-connections) |
| `workflows:read` / `workflows:write` | `/workflows` — authored workflow definitions. Its own grant rather than part of `manifests:write` or `jobs:write`: a workflow names the manifests and tools its runs will call. `/-/check` (check a definition without saving) is a read, and so is `/-/tools?manifest=` — the tools a `tool_call` step may name, which **compiles that manifest as the caller under its inbound rules** (401/403 `not_permitted` otherwise), so it lists only what the caller could run, and never a client tool (`spec.client_tools`), which a run refuses too: nobody is there to answer it. A tenant keeps at most 500 workflows (409 `workflow_limit` past it; replacing one never counts), a definition is capped at 256 KiB however it is built, and a check lists at most 200 problems (then `too_many_problems` with the count left out), each quoting an author's names cut short — so a definition cannot make its own error report large. Every save and delete is audited (`workflow_saved`, `workflow_deleted`) to the caller. Starting and cancelling a run (`POST /workflows/{id}/runs`, `.../cancel`) need `workflows:write`: each agent step runs as a child durable run **as the person who started the workflow, with their scopes** — recorded once at start like a durable chat's, never widened, and dead with the run, whose deadline is 24 hours clamped to the starter's token `exp` (a job-started run has no token to clamp to; see `jobs:*`). A run started with no caller in context runs its steps with no scopes. **Each manifest a step names is checked against the starter's inbound rules and pinned when the run starts** (401/403 `not_permitted` otherwise): a step that starts hours later refuses a version published since, as a durable chat does, so the starter's scopes never run code nobody checked. Each child is also held to its manifest's own `execution` TTL. A tenant may have 50 runs going and a run 8 agent and tool steps in flight. A step may hand on 128 KiB of output, and a run's record — its inputs, and each step's inputs and outputs — holds at most 4 MiB; a step whose inputs or outputs would pass either fails with none of them kept, and a step's error is cut to 500 characters. **When a run ends, the run's record of its starter (scopes, pin) and manifest pins is cleared** — they start no more steps; each step's child run keeps its own copy until `FELIX_FIBER_RETENTION_DAYS` — and the run's record is swept `FELIX_WORKFLOW_RUN_RETENTION_DAYS` (30; 0 keeps every run) after it finished. Approval steps open ordinary `/approvals` rows; one nobody decides fails the step rather than counting as a denial, and cancelling closes them. A `tool_call` step calls one tool from its step's `manifest` with no model, **through that manifest's governed tool set and the agent loop's own dispatch** — every governance wrapper (policies, screening, limits, approvals, …), the plugin `before_tool` / `after_tool` hooks and the manifest's `pre_tool_use` / `post_tool_use` apply as they would to the agent's call, the `tool_call` audit row is written, and the arguments are screened as a direct MCP call's are. Limits count per step, not per run: each step is a fresh compile, as a direct MCP call is. The step's author chooses the tool and its fixed arguments, and the person who starts the run lends their scopes to it; the tool must still be bound and its input schema must still take what the step sends, or the step fails. A call whose worker died mid-way is repeated only for a tool marked `replay_safe`. Two things to know: anyone with `workflows:read` sees every step's inputs and outputs, which were produced with the starter's scopes; and one step's output becomes the next step's prompt, so text an earlier step read (a web page, an issue body) reaches a later step's tools — turn on `spec.inbound` screening on manifests that take workflow input from untrusted sources. Audited as `workflow_run_started` (every start, a job's included, with its `trigger`) and `workflow_run_cancelled` |
| `heartbeats:read` / `heartbeats:write` | `/heartbeats` — the caller's own heartbeats only (see [Heartbeats](#heartbeats)): list, turn on or off, remove, run now. Its own grant rather than part of `jobs:write`: it writes a job only of the operator's design, for the caller alone |
| `eval:read` / `eval:write` | `/eval` |
| `usage:read` | `/usage` |
| `metrics:read` | `GET /metrics`. Its label values (manifest ids, remote MCP tool names) span every tenant, so it is an operator grant: give it to the scrape credential (`scripts/metrics-token.sh`, the Helm `serviceMonitor.bearerTokenSecret`), in a tenant of its own (`metrics`), and to nothing a tenant holds |
| `memory:read` / `memory:write` | `/memory` — inspect, search, correct and prune what an agent has remembered. Spans **every person's** memory in the tenant, not only the caller's: person pools are kept apart from each other's *agents*, not from the operator (see [Memory: whose memory an agent reads](#memory-whose-memory-an-agent-reads)). `?principal=` narrows a read to one pool (empty for agent pools); `POST`'s `principal` writes a person's pool, and is refused (422 `principal_not_a_person`) for a credential many callers share; 409 `memory_held_by_another_pool` when the content's id belongs to another pool |
| `documents:read` / `documents:write` | `/documents` — ingest, search, inspect and remove the corpus an agent retrieves from |
| `skills:read` | `/skills`, and the read routes of `/skill-library` (the library, the review queue at `/-/review`, each version's review record, digest-checked and redacted files, a read-only gate preview, the policy at `/-/policy`, feedback at `/{name}/feedback` and the `/-/feedback` inbox, evaluations at `/{name}/evals`). `/skills` — list the Agent Skills a manifest can reach, read the body `activate_skill` hands the model (secret-redacted, as `manifests:read` redacts a manifest), and see which skill activated on which turn. Separate from `manifests:read` because a skill body is **prompt content**: `activate_skill` hands it to the model as instructions, so reading one is reading instructions the agent will follow. Read-only; activation is the model's decision mid-turn. By default a manifest's `spec.skills` *adds to* the bundled and `FELIX_SKILLS_DIR` catalogue rather than restricting it, so the model is offered every skill on the host; `spec.skills_declared_only: true` makes the declared names the whole set. The `declared` field on each item says which ones this manifest named, and `declared_only` on the response says which rule is in force. Where the manifest sets `spec.personal_skills: read`, the listing includes the caller's own skills, as the caller's turn would. The listing reports no activation state: a skill is active in the conversation whose session log holds its `activate_skill` result, and `list_skills` in that conversation is what says so — `activate_skill` reports back only skills in the caller's own catalog. An `activate_skill` result is never stubbed by `spec.session.clear_tool_results` nor spilled by `spec.artifacts`, and a strategy that drops its turn gets it back as a transient message, so a skill body (at most 256K characters) stays in the model's context for the rest of that conversation. Activation *history* (`/activations/recent`) is the audit trail and shows every caller's activations, personal skill names included, each with a `library` field (`org`, or a personal library's digest — never the subject) so an investigator can tell whose instructions ran. Personal is not private: an `activate_skill` result, body and all, is in the thread's session log like any tool result, readable by whoever can read the thread. `pin_compile` covers neither the tenant's library nor a caller's. Worth setting on any manifest that has to be reviewable: a skill body reaches the model as instructions, so an ambient skill is an instruction the agent follows that `pin_compile` does not cover — the hash is over the manifest, and the drift is on the host's disk |
| `skills:write` | `/skill-library` writes — save a skill or a new version as an operator draft, publish, roll back, reject a draft with a note, archive; file, accept (which queues an AI rewrite into a draft) and reject feedback; queue an evaluation; set or drop the tenant's publish policy. Implies `skills:read`. Its own scope because publishing puts the skill's instructions in front of every manifest in the tenant: a published library skill joins every catalogue there, and `activate_skill` hands its body to the model as instructions. Every change is audited to the caller, and a publish passes the same gate an agent's does (a failing security scan blocks whoever asks) |
| `skills:personal` | `/skill-library/~me/…` writes: save, publish, roll back, reject and archive in the caller's **personal** library. Reading it (list, read, files, preview) needs no scope at all: it is theirs. Writing is a grant of its own, implied by nothing but `admin` / `*`, because a personal skill is instructions every one of the caller's turns may follow (`spec.personal_skills`) — and a principal is a credential, not a person: an API key one backend uses for many users, or a service token an A2A peer presents, is one principal for everyone holding it, and its library is shared by all of them. Grant it to principals that are one person. Refused (403 `no_personal_library`) to a caller who has none: anonymous, `auth_mode=none`, an API key without its own `sub`. The same publish gate applies, except that a tenant requiring an evaluation refuses a personal publish (evaluations are kept for the tenant's library). A personal library holds at most 100 skills in use, 500 names ever (archived ones included) and `FELIX_SKILL_PERSONAL_MAX_BYTES` (50 MiB) across every version it keeps; versions are never deleted, so archiving frees a place in use but no bytes. Under `spec.personal_skills: write` the same grant is asked of the caller behind an agent's `create_skill` (which then saves into their library) and of an `update_skill` editing one of their skills; it is checked when the tool runs, against the caller's scopes (a durable run's, the scopes it started with), and a caller without it or without a library is refused (`missing_scope`, `no_personal_library`) rather than saved into the tenant's library. Approvals on the authoring tools still apply, each grant bound to the library the call saves into, and an approver's preview names it. `update_skill` edits the caller's library whenever it holds the name, unless the manifest names the skill in `spec.skills`. In `skill_authoring.mode: publish`, a personal skill that would replace a tenant or host skill of its name for its owner is held as a draft for them to publish, as is an edit of a version the person wrote. Imports, adopts, feedback, evaluations, the review queue and the publish policy are the tenant's library's alone |
| `admin` / `*` | `GET /skill-library/-/personal` lists every personal library in the tenant — owner (`issuer\|subject`), digest, skill count; `/skill-library/~{digest}/…` reads one, or archives a skill in it, never otherwise writes it. Every such look is audited as `personal_library_accessed` (principal, library digest, method, path). Refused under `auth_mode=none`, which checks no scope and has no administrator to audit |
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
# several at once, failing on warnings as well as refusals
felix validate-manifest agents/*.yaml -e production --strict
# or in CI after editing manifests/
uv run felix bundle-manifests --strict
```

`validate-manifest` takes any number of paths and reports every one -- each refusal, each
`warning` line -- before deciding the exit status. It exits **1** when any manifest is invalid
or fails governance, the same code it always used. Warnings (today, a delete gated less than a
write) are printed and exit **0** by default, because `PUT /manifests` stores those manifests
too and a CI gate that refused them would refuse what the API accepts. `--strict` is the
operator choosing to hold their own manifests tighter than the store does: any warning makes
the run exit **1**, after all of them are printed. `bundle-manifests --strict` does the same
for every bundled manifest, printing warnings to stderr so stdout stays JSON; this repository's
CI and `make bundle` run it that way, so a bundled manifest that gains a warning fails the build.
