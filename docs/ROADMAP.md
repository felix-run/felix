# Felix roadmap

Living tracker for what to build next. Update status in place; keep items
concrete enough to pick up in a single session.

**Repos:** `felix-run/felix` (harness) · `felix-run/web` (chat-ui + float + docs)
**Live:** [api.felix.run](https://api.felix.run) · [chat.felix.run](https://chat.felix.run) · [float.felix.run](https://float.felix.run) · [docs.felix.run](https://docs.felix.run)
**Last reviewed:** 2026-08-23 (post cross-harness port audit, PRs #43–#44; headless-first audit)

---

## How to use this file

| Status | Meaning |
|--------|---------|
| `[ ]` | Not started |
| `[~]` | In progress / partial |
| `[x]` | Done (keep briefly for history, then move to **Shipped**) |
| `[!]` | Blocked / deferred on purpose |

When you finish an item: mark `[x]`, note the commit if useful, then fold it
into **Shipped** on the next tidy pass. Pick from **Now** unless a demo needs
something from **Next**.

---

## Suggested pick-up order

1. **Durable client poll** (`FelixClient.prompt` + `/chat/stream` behavior).
2. **Cowork completion smoke** on GCE (soft poll in `smoke.yml`).
3. **`spec.a2a` agent card** → then **`spec.anomaly`** wiring.
4. **`inbound.schemes`** enforce-or-drop; same for **`outbound.providers`**.
5. Docs getting-started (Python) + governed demo policy decision.
6. Tag `v0.1.1` / `v0.2.0` from Unreleased (`CHANGELOG.md`).

When in doubt: **dogfood float → fix what breaks → write it down here.**

---

## Now (next 1–2 sessions)

### 1. Close the durable loop

`spec.execution.mode: durable` on `POST /chat` returns **202** +
`resume_token`. Poll is `GET /chat/runs/{resume_token}`. The rest of the
surface still pretends every chat is synchronous.

- [ ] **`FelixClient.prompt`** — on HTTP 202, poll `GET /chat/runs/{resume_token}`
      until `completed` / `failed` / `expired` (honor `expires_at`). Emit
      progress events to subscribers. Add `wait_s` so callers can return the
      202 payload without waiting.
- [ ] **`POST /chat/stream`** — same durable enqueue as `/chat`, then either
      SSE-poll the run (status + final) or return 202 and document that
      streaming stays inline-only. Do not silently ignore `mode: durable`.
- [ ] **Cowork completion on GCE** — idle BRPOP / scheduler fixes shipped
      (`9f79a18`); local durable poll reached `completed`. Prod smoke still
      only asserts cowork `202` accept. Dogfood one Background run on float →
      `completed`, then extend `.github/workflows/smoke.yml` with a
      **soft** completion poll (`continue-on-error: true`, ~3 min).

Files: `packages/harness/src/felix/sdk.py`,
`apps/api/src/felix_api/routes/chat.py`,
`packages/harness/src/felix/durability/runs.py`,
`.github/workflows/smoke.yml`.

### 2. Manifest fields that still do nothing

- [x] **`spec.a2a` → agent card** — `publish` and `capabilities` are unused.
      `GET /.well-known/agent-card.json` always advertises streaming + MCP and
      never lists manifest skills. Honor `publish` (404 or empty card when
      false), merge `spec.a2a.capabilities`, and emit `spec.skills`.
      File: `packages/harness/src/felix/a2a/card.py`.
- [x] **`spec.anomaly` → worker scan** — `AnomalySpec` (`enabled`,
      `min_volume`, `min_rate`, `baseline_factor`) is ignored.
      `jobs/anomaly.py` uses hardcoded `MIN_VOLUME=10` / `BASELINE_FACTOR=3.0`.
      Load the tenant manifest (or per-manifest rows) and skip when
      `enabled: false`.
- [x] **`spec.auth.inbound.schemes`** — `allow_anonymous` and
      `required_scopes` are enforced. `schemes` is only a governance compile
      check. Enforce against the request principal, or drop the field.
- [x] **`spec.auth.outbound.providers`** — unused. Constrain which secret
      backends / model providers a manifest may call, or remove it.
- [x] **`spec.observability.metrics`** — tracing is process-global; the
      per-manifest name list does nothing. Allowlist `record_counter` names
      or delete the field.

### 3. Docs + demo policy

- [ ] **Docs getting-started (Python)** — Starlight at docs.felix.run still
      has TS/Workers-era prose. Operator path: Compose → migrate → `quick` /
      `cowork` → chat-ui / float against `api.felix.run`. (Likely
      `felix-run/web` docs sources; keep a short pointer in this README.)
- [ ] **Governed demo path (decide)** — `manifests/governed.yaml` +
      `deploy/GOVERNANCE.md` + Helm ESO are in. Either enable on GCE
      (RBAC scopes for chat/float keys) **or** keep demo anonymous and
      document that choice explicitly in `deploy/GOVERNANCE.md`.
- [!] **Rotate Anthropic API key** — only when you say go. Then Secret
      Manager `felix-anthropic-api-key` + recreate API/worker.

### 4. Release hygiene

- [ ] **Tag from Unreleased** — fold worker fix, Redis leases, session
      control, full governance wave, inbound screening, and the DX / CI
      hardening wave into `v0.1.1` or `v0.2.0` (`CHANGELOG.md`). Update
      Unreleased notes before tagging. Release *automation* (GHCR publish,
      SBOM, signing) is deferred — see **Repo / release hygiene** under Next.

---

## Next (this quarter)

### Harness

- [ ] **Temporal Compose profile** — `FELIX_DURABILITY=temporal` +
      `felix temporal-worker` exist; optional compose profile + README for
      long HITL demos. Keep Postgres fibers as default.
- [ ] **Live-model eval (optional CI)** — fixture/`--mock` is in CI;
      optional nightly against `api.felix.run` that does not block PRs
      (`--llm-judge` opt-in).
- [ ] **Scale-out proof** — Redis leases + steer/waiters are multi-replica
      capable; document and smoke two API replicas behind one origin.
- [ ] **Sandbox ladder extras** — capability-bridge / gVisor as documented
      extras, not default lean image.
- [ ] **OAuth / dynamic provider keys** — secrets backends cover static keys;
      refresh/`getApiKey(provider)` only if a real customer path needs it.
- [ ] **Schema cleanup pass** — delete dead `memory.checkpointer` aliases
      (`agentcore` / `do` / `sqlite`) and any other unused fields after
      enforce-or-drop decisions above.

#### From the cross-harness port audit (Aug 2026)

Read against a sibling runtime that solved the same problems from a stateful,
per-agent-SQLite starting point. Its actor / hibernation / hash-ring machinery is
a **non-goal** here — all of it exists to serialise writes to owner-local SQLite,
which shared Postgres makes unnecessary. What is portable is what it built *on
top of* durable state. Ordered by value; the first two need no migration.

- [x] **SSE reconnect-to-snapshot** — shipped in #54. The original entry here was
      wrong on three counts, all verified before building: the turn runs *inside*
      the SSE generator, so turn and stream are always the same process and there
      is no cross-replica `side_events` gap on the streaming path; tearing the run
      down on disconnect is deliberate, with a comment saying so ("let the
      cancellation propagate so the run is torn down instead of continuing to burn
      model tokens"); and `event: error` was already emitted at `chat.py:467`, so
      "never add an `event:` line" was false when written. What shipped is the
      honest slice: `id:` cursors plus `GET /chat/stream/{thread_id}` to recover
      the thread. Still open, and now separable — **detached turns** would reverse
      that teardown decision on cost grounds and needs its own argument, and
      **durable runs still stream nothing** (approvals are pollable via
      `/approvals`, but nothing watches a background run).
- [~] **Long-term memory** — schema and provenance in #46, hybrid recall and
      the `Embedder` seam in #47, the agent-facing tools in #49, the management
      routes in #50, and turned on in `governed` and `cowork` in #51 — which is
      also what caught `capture.model` being inert and tool writes carrying no
      provenance. Remaining, and only worth doing on evidence: semantic recall
      is still off by default (`FELIX_MEMORY_EMBEDDER=none`), so nothing
      exercises the vector channel outside tests; and extraction quality is
      whatever one prompt returns — a live run happily stored an assistant's
      apology as a durable fact.
- [ ] **Tamper-evident audit chain** — `seq` + `prev_hash` + keyed-HMAC per row,
      per tenant, with `verify_chain` reporting the first break. Allocate the
      chain at write time inside the insert transaction, under a per-tenant
      advisory lock (`session/store.py:93` is the precedent): that is what keeps
      a `DurableBuffer` drop from reading as tampering, since a seq is only ever
      consumed by a row that is actually inserted. Hash a `payload_sha256`
      column rather than the payload bytes — `jsonb` does not preserve key order.
      Retention needs a pruning anchor or it breaks the chain it prunes.
- [ ] **Signed completion webhooks** — closes the durable loop above: today a
      `202` can only be polled. Deliver from the **worker**, since the fiber
      reaches terminal state under its cron and the API replica that accepted the
      request may be gone. A dead letter is `status='dead'` on the same durable
      row, not a second store. `spec.webhooks` should select operator-registered
      endpoint ids, never carry URLs — a manifest author holds a tenant scope,
      and a tenant-supplied URL on a path carrying run output is an exfiltration
      channel that SSRF checks do not address.
- [ ] **Fiber engine ergonomics** — `_run_fiber_step` advances **one op per
      sweep** against a `* * * * *` cron, so a four-op fiber takes four minutes
      of wall clock, nearly all idle. Run the handler to suspension inside one
      claim; clear `heartbeat_at` on suspend so sleeping is distinguishable from
      crashed. Then `ctx.step(key, fn)` memoization and an append-only
      `fiber_steps` table, so a crash mid-tool-loop resumes instead of replaying
      a whole `invoke`. Extend the lease inside `step` — it is sized for one op.
- [ ] **Governed `http_fetch` tool** — the model cannot read a URL today.
      The sibling's capability bridge is the wrong shape here: `dispatch_rpc`
      bypasses all nine governance wrappers, including content screening on a
      fetched page, which is attacker-controlled input. As a tool it inherits the
      whole stack. Prerequisite: pin the connection to the validated address —
      `HttpExecutor` validates the URL then hands it to httpx, which re-resolves.
- [!] **Model-call middleware chain** — considered and rejected. A
      plugin-supplied `wrap_tool_call` is an unordered hole through the
      nine-wrapper stack whose order `test_invariants.py` pins as immutable, and
      `wrap_model_call`'s only real users are retry and fallback, which already
      exist and belong where they can see the provider. A custom fallback is a
      registered `ModelProvider`. The one genuine gap is a per-tool per-turn call
      cap, which is a field on `Limits`.

#### From the harness audit (Aug 2026)

Deferred deliberately; each has a written reason, not just a lack of time.

- [ ] **Governed coding toolset** — `read` / `write` / `edit` / `bash` / `grep`
      / `find` behind a `FilesystemBackend` + `ShellBackend` protocol pair under
      a new `felix/exec/`, every method returning a result rather than raising,
      with a stable backend-independent error-code set. Would unify
      workspace / sandbox / container / client-bridge execution behind one seam,
      and wrapped in the governance stack it is a product a local coding agent
      structurally cannot ship. **Large, and conditional**: only worth starting
      if coding-agent use cases are actually on the roadmap, because a
      half-built tool suite is worse than none. Design notes worth stealing when
      it happens: multi-edit applied against original offsets, fuzzy matching
      that rewrites only touched lines and copies the rest byte-for-byte,
      read truncation that tells the model the exact `offset=` to resume from,
      bounded rolling output capture that spills to a temp file and names it.
- [ ] **Split-turn compaction** — when one turn alone exceeds
      `keep_recent_tokens` the cut lands mid-turn and a single summary has to
      cover both sides of it. Two summaries with different prompts and budgets
      (history, then a turn-prefix) is the fix. Narrow: only bites on very long
      single turns.
- [ ] **Tools carry their own prompt copy** — a `prompt_line` and
      `prompt_guidance` on `Tool`, assembled in `manifests/builder.py`, so the
      system prompt is *derived* from the active tool set instead of
      hand-maintained. Removes a drift class rather than fixing a live bug; a
      tool with no `prompt_line` stays deliberately invisible to the model.
- [ ] **Telemetry vocabulary** — OTel and Prometheus are wired but there is no
      span/attribute vocabulary and no schema. Most of the value was the
      conformance-suite pattern, which `tests/conformance/` already has.
- [ ] **Scripted model provider** — a `scripted` provider in
      `patterns/model_registry.py` to replace the ad-hoc doubles each test file
      builds. Convenience: the doubles work, and this would not find bugs.
- [ ] **Long-context price tiers** — `estimate_cost` supports request-wide
      tiers but **no bundled entry sets them**, so any provider that bills long
      context at a premium is still under-counted, and `limits.max_cost_usd`
      fails closed on that number. Needs current rates per deployment via a
      manifest price override. `gpt-4.1` likewise has no bundled rate and bills
      at the default.
- [ ] **`uv --exclude-newer`** — refuse dependency versions published in the
      last day or two, the analogue of an npm `min-release-age`. Cheap; the rest
      of the supply-chain posture is already covered.
- [~] **`react.py` is 928 lines against a 600 budget** — the duplicated loop is
      gone (that was the real defect); what remains is one sequential turn loop
      plus session plumbing. Splitting further trades readability for a number.
      Revisit only if it grows again.

#### From the headless-first audit (Aug 2026)

The property holds: no asset pipeline, no `StaticFiles` / template engine /
`app.mount` anywhere in `apps/`, no UI service in Compose or the Helm chart, and
every capability reachable over REST/SSE, `/v1`, A2A, or MCP. chat-ui consumes a
generated `harness-openapi.json` and works *around* missing routes (skills, eval
per-item, job run-now) rather than the harness growing them for it. What follows
is the gap between that being true and it being **enforced or documented**.

- [ ] **Headless invariant is prose only** — CLAUDE.md asserts it; nothing fails
      when it stops being true. `tests/unit/test_invariants.py` already makes
      the argument in its own docstring ("rules hold only while whoever is
      editing has them in context"). An AST/file check over `apps/api` for
      `StaticFiles`, `Jinja2Templates` and `app.mount`, plus a tracked-file
      check for asset extensions, is ~20 lines in the existing idiom and costs
      nothing at runtime. Cheapest item here; do it first.
- [ ] **No-CORS contract undocumented** — the stack is body-limit → rate-limit →
      auth with no CORS layer, so a browser on any other origin cannot call
      Felix directly. Deliberate, but written down nowhere: `README.md`,
      `deploy/README.md`, `deploy/GOVERNANCE.md` and `docs/` never mention CORS,
      and the requirement survives only inside felix-web's `worker/index.ts`,
      which proxies `/api/*` and injects the bearer token. A self-hoster
      pointing a browser app at `:8080` hits an opaque wall. Short block in the
      deploy docs, plus the reverse-proxy shape it implies.
- [ ] **`POST /chat/ui` sub-protocol unspecified** — README:193 lists the route
      but not the contract: the harness can emit a `ui_request` SSE side-event
      and block on a waiter for `DEFAULT_TIMEOUT_SECONDS = 300`. Nothing in core
      calls it today (`felix/ui/` has no call sites outside its own module and
      the route), so no headless caller can stall on it *yet* — but a plugin
      using `request_confirm` would make a non-answering client eat five minutes
      per prompt. Document the frames, and move the timeout to a `FELIX_`
      setting instead of a module constant.

Not gaps, recorded so they are not "fixed" by mistake: the chat-ui-shaped
accommodations in `eval.py:132` (alias route named for the client),
`audit.py:34` (an `events` alias for the TS shape) and `approvals.py:24`
(accepting `status`) are deliberate compat with a shipped client — removing them
breaks felix-web. Whether `felix/ui/` is dead code or a plugin-seam affordance
needs a real reachability check (entry points, registry, string lookup) before
anyone deletes it.

#### From the memory-trust + scanner wave (Aug 2026)

Left open deliberately after the wave that landed #57–#64. The first two are
decisions about behaviour rather than fixes someone can just apply; the third
is prose that should follow whichever way the first one goes.

- [ ] **Who may retire a memory by naming its `topic_key`** — `put_memory`
      supersedes any active row sharing a `topic_key`, so writing a new value
      under an existing key retires whatever held it. `remember` is gated on
      `topic_key` in `governed.yaml`, which closes the route a model can aim
      deliberately (`recall` prints every stored key, so they are
      enumerable). `capture_from_turn` reaches the same supersession
      post-turn without a tool call, so it passes through no governance
      wrapper at all — harder to aim, since it needs the extractor steered
      onto the victim's key, but ungated and free against `max_tool_calls`.
      The durable fix is store-level: require rank above `_DEFAULT_TRUST` for
      a *cross-row* sweep, so rank-1 writers store alongside rather than
      retire. That is a real ergonomic change (agents could no longer correct
      their own captured facts by re-keying), which is why it is a decision
      and not a patch.
- [ ] **`deploy/GOVERNANCE.md`: which layer owns retirement** — whichever way
      the above lands, the memory trust model needs writing down: `retired_by`
      versus `source`, why resurrection is gated on who retired rather than
      who wrote, and which of the manifest, the store and the approval wrapper
      is authoritative. The rules are enforced in
      `tests/conformance/test_memory_trust_matrix.py`; the prose does not
      exist yet.
- [ ] **Warn when `when_args` names nothing** — `ApprovalRule.when_args` is
      not validated against the gated tool's schema, so `when_args:
      [topickey]` yields a rule that never fires. It still passes
      `validate-manifest`, and still satisfies the `eu_ai_act` and SOC 2
      boundary checks, because both test that approvals exist rather than
      that they cover anything. Not a regression — `tools: [typo]` was
      already vacuous the same way — but `when_args` makes the mistake much
      easier to make and invisible once made. A `logger.warning` at bind time
      is the fix; `RememberArgs` is a pydantic model with `extra="forbid"`,
      so the check is cheap, and tools carrying a raw JSON schema are
      checkable too. Decide whether it warns or refuses.

### Product (`felix-run/web`)

- [ ] **Session-control UX gaps** — export JSONL from UI, clearer
      lease-contention copy, reconnect-to-snapshot after hard refresh,
      empty/search states.
- [ ] **Usage → cost view** — rollup by manifest/day + $ estimate from
      model catalog metadata.
- [ ] **Float as primary cowork surface** — chat-ui stays general console;
      float owns mount/VFS/approvals/Background.
- [ ] **Prune leftover TS-harness skills/copy** in docs sync sources after
      the getting-started rewrite.

### Repo / release hygiene

Deferred from the DX audit (2026-08-22). Phases 1–3 shipped; this is the
remainder, none of it blocking.

- [ ] **Required status checks + `CODEOWNERS`** — CI now runs 14 jobs, but
      nothing blocks a merge on them, so the human review gate is convention
      rather than mechanism. Repo-settings change plus a `CODEOWNERS` file;
      make the `changes` job report success so doc-only PRs stay mergeable.
- [ ] **Tag-driven release** — build, push to GHCR, attach an SBOM, sign with
      cosign via OIDC, then point `deploy/` at the published image instead of a
      locally built `felix:latest`. CI already builds and scans the image; it
      just throws it away.
- [ ] **Single-source the version** — `0.1.0` currently lives in four
      `pyproject.toml` files, the root, and the Helm `appVersion`. Releasing
      means editing six files correctly from memory.
- [ ] **Wire-contract snapshot** — snapshot `/openapi.json` and the SSE
      event-name set. `felix-run/web` mirrors `StreamEvent` by hand and its
      union has an open arm, so an added or renamed frame silently does
      nothing on both sides.
- [ ] **Postgres 18** — `pgvector/pgvector:0.8.6-pg18-trixie` exists. Own
      branch with a rollback plan: compatibility pass over the six revisions,
      FTS index, and RLS, plus a dump/restore path for existing deployments.
- [ ] **`.cursor/plans/` decision** — tracked but ungitignored. Keep as
      versioned planning notes or ignore; either is fine, drifting is not.

### Deploy

- [ ] **GKE dogfood** — Helm + ESO → one known-good install note under
      `deploy/gcp/`.
- [ ] **AWS smoke checklist** — mirror GCP path (Secrets Manager / S3) in
      `deploy/aws/`.
- [ ] **Postgres RLS dogfood** — migration `0006` + `FELIX_DATABASE_RLS=true`
      on a non-prod branch; verify retention bypass + mixed-tenant audit flush.

---

## Later / explicit non-goals

- [!] **`memory.consolidate` LLM merge** — worker already hash-dedupes.
      `enabled` / `model` / `after_facts` stay unused on purpose (v1).
- [!] **`memory.checkpointer` aliases** — dead vendor names; delete on
      schema cleanup, do not implement.
- [!] **`FELIX_POLICY_BUNDLE_PUBKEY` / OPA** — no signed policy-bundle
      runtime in v1 (governance stays manifest compile + wrappers).
- [!] **Commerce / billing plugin** — seam only; no Stripe in-tree.
- [!] **Cloudflare compute in the harness** — Workers/DOs out of `felix`;
      CF hosts web + named tunnel to GCE API only.
- [!] **Merging web into `felix`** — keep repos split.
- [!] **Third-party TUI / CBOR / npm package installer** — session/loop
      ideas only; composition is YAML + `felix.plugins` entry points.
- [!] **Multi-cursor session views ("lanes")** — several named cursors over one
      entry tree, each with its own leaf and queue, as a substitute for
      multi-writer storage. Elegant, but `fork` / `rewind` plus Redis leases
      already cover what Felix does; this would be a session-layer rewrite for a
      problem not yet hit.
- [~] **Snapshot-authoritative streaming** — every command result carries the
      full post-command snapshot with a monotonic `revision`, and progress
      deltas are explicitly advisory and never reduced into authoritative state.
      A better contract than the current SSE union. Not worth doing on its own —
      fold it into **wire-contract snapshot** under Repo / release hygiene when
      that lands.
- [~] **Shared vs exclusive session leases** — leases are binary today. Distinct
      shared/exclusive modes with asymmetric detach-vs-dispose semantics are
      worth remembering when reconnect-to-snapshot UX gets built.

---

## Shipped (recent)

### Cross-harness port audit (Aug 2026)

Second pass against a sibling runtime, this time looking for features to port.
Like the first, it mostly found bugs — in code that two sessions had each half
fixed, and in a delivery path nothing asserted end to end.

- [x] **`memory_vectors` rejected every insert on Postgres.** The column
      `embedding vector(768) NOT NULL` has existed since `0001_baseline` — added
      by raw SQL, so invisible from `db/models.py` — and `put_memory` never
      supplied a vector. Every insert raised NotNullViolation, and the only
      caller swallowed it into a debug log, so long-term memory had never stored
      a row outside the in-memory twin. Found by the first Postgres conformance
      test the store ever had (#46)
- [x] **Migrations were never executed by CI.** The `conformance` job runs a real
      pgvector Postgres (#39), but built its schema with `Base.metadata.create_all`
      — so no revision ran, and the DDL that lives only in a migration was never
      present. `create_all` cannot produce a generated column or a GIN index, which
      is exactly what the memory and audit work depends on. The Postgres arm now
      applies `alembic upgrade head`, and three tests assert every revision applies,
      reverses, and produces `session_events.content_tsv` and its index (#45)

- [x] **Recalled memory facts never reached the model on a threaded chat.**
      `_assemble_messages` built the list with the prelude, then let the session
      strategy replace it wholesale. Four existing tests asserted the block was
      *built* correctly; none that it survived assembly (#43)
- [x] **Embeddings ran on the event loop**, stalling every concurrent request on
      the worker — tool retrieval reaches the encoder up to four times per loop
      step. Threaded, with the cheap keyword path kept inline and a guard pinned
      to the code it mirrors by test (#44)

The port items themselves are under **Next → Harness → From the cross-harness
port audit**. The streaming double-inference this pass also flagged was already
fixed by `#36`.

### Harness audit wave (Aug 2026)

Cross-harness audit that set out to find portable packages and mostly found
bugs — fifteen of them, clustered almost entirely in code that existed in two
copies with nothing comparing them, or in code nothing exercised.

- [x] Truncated turns executed their tool calls, past command screening —
      arguments can be cut off mid-write and still parse (#34)
- [x] Extended thinking was write-only: sent, never read back, never replayed,
      so a thinking manifest lost its reasoning at the first tool call (#34)
- [x] Flat pricing, generic 429 retry, and unguarded parallel writes (#34)
- [x] Model metadata was three tables with three matching rules that disagreed
      on `claude-opus-4-5`'s context window; one record now, and the dead
      `context_window` field that let them drift is gone (#35)
- [x] Compaction sized itself to a hardcoded 128K regardless of the model (#35)
- [x] **Streaming ran the inference twice** — double billed, half metered
      against fail-closed budgets, and the streamed text could differ from what
      was saved (#36)
- [x] Provider context-overflow was a hard failure instead of compact-and-retry,
      including two providers that overflow without raising (#37)
- [x] `invoke` and `stream_events` were two copies of one loop that had drifted:
      streaming wrote **no** turn-level audit record, did not record an abort,
      and drained follow-ups after a fatal tool error (#38)
- [x] In-memory stores were never checked against the Postgres ones they stand
      in for; one contract, both backends, and a CI job that fails rather than
      skips when the database is missing (#39)
- [x] A run killed mid-tool left a thread that could not be resumed at all, and
      `Tool.replay_safe` now says whether a tool may be re-run; adding it
      exposed that wrapper cloning silently dropped unknown fields (#40)
- [x] Side requests — compaction, memory, screening, branch summaries — spent
      the conversation's prompt cache (#41)

PRs `#34` … `#41`. Remaining items are under **Next → Harness → From the
harness audit**.

### DX / CI hardening wave (Aug 2026)

Three-phase remediation of a developer-experience and supply-chain audit.
CI went from 6 jobs that skipped on doc-only PRs to 14 that run.

- [x] `pre-commit` was never installable — the ruff repo entry lacked its URL
      scheme, so `install-hooks` failed for every contributor. Fixed, plus a CI
      job so it cannot rot again (#8)
- [x] `make check` failed on any machine with a `.env` (pytest inherited
      `FELIX_DATABASE_URL`; `make type` checked `tests/` while CI does not).
      `scripts/test.sh` is now the single test entry point for make and CI (#8)
- [x] Docker and CI use `uv sync --locked`, not `--frozen` with a fallback —
      a stale lock fails the build instead of silently shipping a different
      resolution (#8)
- [x] Actions at current majors and pinned by commit SHA; every base and
      service image pinned by digest; Dependabot's docker entry fixed (it
      pointed at `/`, where there is no Dockerfile) and grouped weekly (#9)
- [x] Repo invariants are tests, not prose: `.env.example` covers every
      setting, no optional dependency imported at module scope, every
      Postgres-touching module has a `memory://` path, and the governance
      wrapper order is fixed (`tests/unit/test_invariants.py`) (#10)
- [x] `lean` CI job imports all 156 modules with no extras installed;
      `toolkit` job validates `.claude/` hooks, settings, and skill
      frontmatter — both previously outside the CI path filter (#10)
- [x] Security scanning: CodeQL, `pip-audit` over the locked set with extras,
      gitleaks over full history, Trivy on the built image (#11)
- [x] Runtime image hardened — dropped `pip` (its vendored `msgpack` and
      `setuptools` carried HIGH CVEs that are not Felix dependencies) and
      applied pending OS updates. Scans clean; +15 MB (#11)
- [x] Coverage measured and gated at 60% (#11)

PRs `#8` … `#11`. Toolkit itself landed in `#6`.

### Governance / secrets wave (Aug 2026)

- [x] Manifest `secret:NAME` refs + plaintext forbid + output masking
- [x] State scrub (session / audit / fiber) + `pin_compile` drift refuse
- [x] Inbound auth (`allow_anonymous` / `required_scopes`) on chat / v1 / A2A
- [x] Inbound `/mcp` through compiled agent wrappers
- [x] `spec.governance` SOC2 / EU AI Act fail-closed + Art. 50 notice + audit emit
- [x] `felix validate-manifest` + `manifests/governed.yaml` + `deploy/GOVERNANCE.md`
- [x] Management-API RBAC scopes; Helm External Secrets template
- [x] `command_screening.include_defaults`
- [x] Presidio (opt-in extra) + regex residual PII
- [x] Opt-in LLM judges (`--llm-judge` / rubric / `JudgeRule.model`)
- [x] Opt-in Postgres RLS (`0006_tenant_rls`, `FELIX_DATABASE_RLS`)
- [x] Inbound user-turn screening (injection + input PII) on chat / v1 / A2A
- [x] CI green (ruff/format, ty scope, compose secrets, OpenAPI URL)

Commits (approx): `34c667f` … `87ae629`.

### Session / Pi-shaped depth (Aug 2026)

- [x] Snapshots, list/name/label, abort/continue, thinking levels
- [x] Parallel tools, branch summaries, compaction, steering modes
- [x] FTS search, Redis-backed leases, JSONL export, UI prompts
- [x] Comparative evals; chat-ui + float wired for search / thinking / rewind

### Product / cowork

- [x] Client tools bridge + approval interrupt/resume + workspace tools
- [x] `manifests/cowork.yaml` + Float + chat-ui cowork default
- [x] Shared `@felix/cowork-client`

### Ops

- [x] `api.felix.run` tunnel; GCE loopback; GCP Secret Manager hydrate
- [x] Smoke workflow (health + quick + cowork accept + session surfaces)
- [x] Worker idle BRPOP / scheduler `asyncio.run` / Compose healthcheck fix
- [x] `v0.1.0` release
