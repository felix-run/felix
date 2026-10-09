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
| Manifest outbound (`mcp_servers.auth`, `env`, peer/container `auth`) | `secret:NAME` or `{secret: NAME}` resolved at compile; **never** store resolved values in `manifest_json` |
| Redaction | Known secrets scrubbed from tool output, session events, audit payloads, fiber state, skill bodies read over `/skills`, and every `/skill-library` read and write response — file text, and the reason, description, decision note, review-check and security-issue messages a saver or the scan wrote, and feedback bodies, suggested patches, evaluation scenarios, judge reasons and job errors. `FELIX_SKILL_IMPORT_GITHUB_TOKEN` is hydrated from the secrets backend and masked like any credential; it is sent only as a header to `api.github.com`, never in a URL, a log line or a response |

Production (`FELIX_ENVIRONMENT=production`) or `governance.forbid_plaintext_secrets: true`
rejects Bearer/long-token auth and non-ref MCP `env` values.

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
`guardrails_reply` or `judge_deny` audit event. The session log is screened at the
write with the same verdicts: every assistant message is redacted before it is appended,
and every one without tool calls is judged, so a denied reply is stored as its denial —
replays, exports, `/chat/history` and a durable run's live tail carry what the client got.
The store is handed to sub-agents too, since a router's child writes the caller's thread,
and reflect's critique quotes its draft redacted. The log is never less screened than the
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
outage degrades a judge rather than opening it. On tool output, the judges that will score with
the heuristic run first, one at a time — they are free, and one that denies spares every model
call — and the decider and model judges then run together; a tool result passes only if every
judge passes it, as before, and the denial named is the first heuristic one, else the first of the
rest in the manifest's order. A decider-scored judge sends the judged text to the
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

### What a caller can put in the system tier: nothing

Every system-role message a model sees is written by the operator or the harness: the
manifest's prompt, skills catalogue, handoff and session notes. Text a caller, a tool or a
model produced reaches the model as a user turn at most, labelled for what it is — a
conversation summary (reference material), the summary of the earlier steps of a turn a
compaction cut through (also reference material), a recalled memory, or a
`POST /chat/sessions/custom` entry. That last one takes its `role` from the caller and is
stored as written, but a `system`-role entry with `in_context: true` is sent as a user turn marked as added by the
client, on live history and on a compaction checkpoint alike (`session/types.py`,
`_model_role_and_content`).

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
keys. Otherwise the library's live version answers an unpinned ref, and **an explicit pin to an
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

## Shell tools

`spec.shell_tools` execs an argv in the `FELIX_WORKSPACE_ROOT` checkout — as a child of the API
process by default, or in a separate `felix-shell-runner` when `FELIX_SHELL_RUNNER_URL` is set
(below). There is no shell interpreter: `&&`, `|` and `;` are literal arguments. The
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
  treat it as agent-written. `read_file`, `write_file`, `edit_file`, `list_dir`, `search_files`,
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

A publish whose opener's connection has lapsed or been revoked publishes nothing and returns
`[github disconnected]`, naming who must reconnect.

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
as repeatable. Read-only built-ins (`list_dir`, `read_file`, `search_files`, `calculator`,
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

## Approval semantics

**Precedence.** Approvals is the only control that selects *one* rule — policies and judges
apply every match, so for them more matches only tighten. When several approval rules match a
tool, a rule naming it **literally** wins over one matching by pattern, and among equals the
last declared wins. That makes globbing non-weakening: a pattern can only gate a tool nothing
gated before, and never displaces a stricter literal rule.


| Field | Behaviour |
|-------|-----------|
| `ttl_seconds` | How long the run waits for a decision before failing closed. |
| `one_shot` | The grant is spent by the one call it authorizes — the call that waited for the decision, or a later call that found it approved; a replay of the same call needs a new approval. |
| `bind_principal` | Only the principal who was approved may use the grant. Without it, any principal in the tenant can reuse it. |
| `allow_unattended` | EU AI Act high-risk manifests must set this to `false`. |
| `when_args` | Gate only the calls that carry these arguments (non-empty); empty gates every call. Each name must be an argument some tool the rule reaches takes, or the rule never fires. For tools whose schemas ship with the harness — built-ins, plugin tools, the memory tools — a rule naming them literally is **refused** at `PUT /manifests` and by `felix validate-manifest` when a name is not one of their arguments. For everything else (MCP tools, globs) it is a compile-time warning and `felix_approval_when_args_unknown`, not a refusal: an MCP schema can change under a stored manifest, and that must not become an outage. A glob such as `github__*` with `when_args: [force]` is flagged only if *no* tool it reaches takes `force`. |

**Skill authoring.** `spec.skill_authoring.mode: publish` is refused at validation unless the
approval rule this precedence selects for `create_skill`, and the one it selects for
`update_skill`, both exist and carry no `when_args` — a conditional rule would let the calls
without those arguments publish ungated. `governed.yaml` carries such a rule (`skill-author`,
both tools, no `when_args`) but does not enable `spec.skill_authoring`, so it binds neither tool;
the rule applies to any manifest that enables authoring, in draft mode as well as publish.
An `update_skill` grant binds the version it edits: `parent_version` is a required argument and the
call signature is a hash of the arguments, so a grant found later — by a retry, another replica or
a resumed fiber — authorizes an edit of that version only, and the call is refused with
`parent_changed` if the skill has moved past it.

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
the log, so neither checks it. Approvals are decided through `/approvals`, gated on the
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

## Management API scopes

When `FELIX_AUTH_MODE` is `jwt` or `api_key`, management routes require scopes
(skipped for `auth_mode=none`). `admin` or `*` bypasses checks; `*:write`
implies the matching `*:read`.

| Scope | Routes |
|-------|--------|
| `manifests:read` / `manifests:write` | `/manifests`; `GET /manifests/{name}/versions` lists stored versions (metadata only) under `manifests:read` |
| `audit:read` | `/audit` |
| `artifacts:read` | `/artifacts` — read back a tool output too large to keep in the transcript. Its own scope rather than part of `audit:read`, because a spilled result is raw tool output and often the most sensitive data a run touches. The model's own way back, the `read_artifact` tool bound beside `spec.artifacts`, checks no scope and is held to something narrower instead: it reads only what its own conversation spilled (the thread, or the request when there is none), so a leaked id does not reach another caller's run through the model. Spill is kept for `FELIX_ARTIFACT_RETENTION_DAYS` (30; `0` keeps forever) and then swept, objects and ledger row together — so evidence meant to outlive that belongs in the audit log, not in an artifact |
| `approvals:read` / `approvals:write` | `/approvals`; `approvals:read` also gates the `approval_required` frames on a durable `POST /chat/stream`, and every `/push` route — a push subscription is a standing request to be told about approvals |
| `jobs:read` / `jobs:write` | `/jobs`. `POST /jobs/{name}/run` runs a job now and needs `jobs:write`: whoever may rewrite a job's prompt may already make it run. The job runs as itself — principal `cron`, no scopes — not as the caller, whose subject is recorded on the run as `requested_by` |
| `plans:read` / `plans:write` | `/plans` |
| `eval:read` / `eval:write` | `/eval` |
| `usage:read` | `/usage` |
| `memory:read` / `memory:write` | `/memory` — inspect, search, correct and prune what an agent has remembered |
| `documents:read` / `documents:write` | `/documents` — ingest, search, inspect and remove the corpus an agent retrieves from |
| `skills:read` | `/skills`, and the read routes of `/skill-library` (the library, the review queue at `/-/review`, each version's review record, digest-checked and redacted files, a read-only gate preview, the policy at `/-/policy`, feedback at `/{name}/feedback` and the `/-/feedback` inbox, evaluations at `/{name}/evals`). `/skills` — list the Agent Skills a manifest can reach, read the body `activate_skill` hands the model (secret-redacted, as `manifests:read` redacts a manifest), and see which skill activated on which turn. Separate from `manifests:read` because a skill body is **prompt content**: `activate_skill` hands it to the model as instructions, so reading one is reading instructions the agent will follow. Read-only; activation is the model's decision mid-turn. By default a manifest's `spec.skills` *adds to* the bundled and `FELIX_SKILLS_DIR` catalogue rather than restricting it, so the model is offered every skill on the host; `spec.skills_declared_only: true` makes the declared names the whole set. The `declared` field on each item says which ones this manifest named, and `declared_only` on the response says which rule is in force. Where the manifest sets `spec.personal_skills: read`, the listing includes the caller's own skills, as the caller's turn would, and its `active` field — like what `activate_skill` reports back — names only skills in the caller's own catalog. Activation *history* (`/activations/recent`) is the audit trail and shows every caller's activations, personal skill names included, each with a `library` field (`org`, or a personal library's digest — never the subject) so an investigator can tell whose instructions ran. Personal is not private: an `activate_skill` result, body and all, is in the thread's session log like any tool result, readable by whoever can read the thread. `pin_compile` covers neither the tenant's library nor a caller's. Worth setting on any manifest that has to be reviewable: a skill body reaches the model as instructions, so an ambient skill is an instruction the agent follows that `pin_compile` does not cover — the hash is over the manifest, and the drift is on the host's disk |
| `skills:write` | `/skill-library` writes — save a skill or a new version as an operator draft, publish, roll back, reject a draft with a note, archive; file, accept (which queues an AI rewrite into a draft) and reject feedback; queue an evaluation; set or drop the tenant's publish policy. Implies `skills:read`. Its own scope because publishing puts the skill's instructions in front of every manifest in the tenant: a published library skill joins every catalogue there, and `activate_skill` hands its body to the model as instructions. Every change is audited to the caller, and a publish passes the same gate an agent's does (a failing security scan blocks whoever asks) |
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
