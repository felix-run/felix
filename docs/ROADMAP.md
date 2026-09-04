# Felix roadmap

Living tracker for what to build next. Update status in place; keep items
concrete enough to pick up in a single session.

**Repos:** `felix-run/felix` (harness) · `felix-run/web` (chat-ui + docs)
**Live:** [api.felix.run](https://api.felix.run) · [chat.felix.run](https://chat.felix.run) · [docs.felix.run](https://docs.felix.run)
**Last reviewed:** 2026-09-02 (product-depth audit: what the harness lets an agent actually *do*)

Completed waves and what they taught now live in [HISTORY.md](HISTORY.md).

---

## How to use this file

| Status | Meaning |
|--------|---------|
| `[ ]` | Not started |
| `[~]` | In progress / partial |
| `[x]` | Done (fold into [HISTORY.md](HISTORY.md) on the next tidy pass) |
| `[!]` | Blocked / deferred on purpose |

Pick from **Now** unless a demo needs something from **Next**.

---

## The loop

**Dogfood `contributor.yaml` on real Felix work → fix what breaks → write it down here.**

This replaces the loop this file carried until 2026-09-02, which read "dogfood float". `float`
was deleted from `felix-run/web` on 2026-08-23 — *"what it actually contributed was a mode, not
a product"* — and the line survived it by ten days. That matters more than a stale link: with
nothing running real work, the only remaining source of tasks was the source tree, and the
harness spent roughly forty commits auditing itself.

Self-audit is not wasted — the mutation audit in [HISTORY.md](HISTORY.md) found two controls
that were silently absent, which no amount of feature work would have surfaced. But it is a
*supplement* to a running workload, not a substitute for one, and it cannot tell you that the
support agent has no way to look anything up.

### Meta-work budget

Two rules, mirrored in `.claude/rules/felix-invariants.md`:

- **One hardening / invariant / audit item per cycle.** Everything else must add user-visible
  capability. Defects found in a *real run* are exempt — that is the loop working.
- **A control may not be added for a capability that does not exist.** This is the rule whose
  absence produced 1,401 lines of `manifests/builder.py` wrapping a calculator.

---

## The finding this cycle turns on

Audited 2026-09-02. Every number re-derived against the tree, not read from a prior note.

| Measure | Value |
|---|---|
| `felix/tools/` — the capability surface | 2,142 lines |
| `.claude/` — scaffolding for the agent that edits Felix | 3,200 lines |
| `manifests/builder.py` — governance wrapping that surface | 1,401 lines |
| Built-in tool registry | 8 tools |
| `skills/` shipped Agent Skills | 5, of which 4 document Felix itself |

The built-in registry in full: `calculator`, `list_dir`, `read_file`, `write_file`,
`search_files`, `list_skills`, `activate_skill`, `deactivate_skill` — and the three skill tools
are stubs returning `[]` until `builder.py:1251` rebinds them against a catalog.

`manifests/support.yaml` declares `tools: [calculator, list_skills]` — a support agent that
cannot look anything up. `manifests/deep.yaml` declares the same — a deep-research agent that
cannot retrieve. An agent on Felix cannot read a URL, search, query a database, or retrieve a
document. `HttpExecutor` has existed at `tools/transports.py:20` since early on and **no
manifest field constructs it**.

The governance stack is the best-engineered part of the harness and it is guarding almost
nothing. Everything in **Now** follows from that.

---

## Now

### A. Capability surface

First, because everything else governs it.

- [x] **`http_fetch`** — `spec.http_tools` binds a fetch tool per ref; `support` uses it as
      `fetch_docs`, confined to the docs site. Two corrections to this entry as written, both
      found by reading the code rather than the note: the existing `HttpExecutor` was the wrong
      starting point (it posts tool *arguments* to a manifest-fixed URL — operator picks the
      destination; here the model does, which is the whole risk), and the "pin the connection"
      prerequisite was **already met** by `#128`–`#130`, so `safe_async_client` gave it for free
      including on redirect hops. `http` stays out of `_TRUSTED_TRANSPORTS` and was added to
      `_UNTRUSTED_SOURCE_PREFIXES`; both layers are pinned separately, because asserting only
      their combination left either free to regress.
- [x] **Web search** — `felix/search.py` carries the `SearchBackend` Protocol and
      `register_search_backend`, selected by `FELIX_SEARCH_BACKEND` and validated against the
      registry at boot. `spec.search_tools` binds the tool. One correction to this entry: the
      bundled backend needed **no extra**, because SearXNG speaks JSON over httpx, which is
      already core — the extra was assumed rather than checked. SearXNG rather than a hosted
      API because it is the one an operator can run themselves without an account, which is the
      same argument as `FELIX_OBJECT_STORE=fs`.
- [~] **Document retrieval** — the corpus landed: `felix/documents/` (chunking, hybrid store,
      in-memory twin), migration `0010`, conformance against both backends, and `/documents`
      management routes so an operator can ingest, search, inspect and remove. Split from the
      agent-facing half deliberately, on the evidence that the two smaller features in this
      workstream each drew ~7 review findings; **the `search_documents` tool and wiring
      `support` to the Felix docs are the follow-up**, and item 6 stays open until then.
      Reuses the `Embedder` seam and `FELIX_MEMORY_EMBEDDER` rather than adding a second
      embedder setting — one embedder per deployment, one vector dimension.
- [ ] **Structured output** — `spec.output_schema` → `response_format` on the OpenAI wire,
      tool-shaped constrained output on the Anthropic wire, pydantic validation with one repair
      retry. Both wires already emit tool JSON schema (`felix_ai/wire/base.py:133`). There is no
      `response_format` anywhere in `packages/` or `apps/` today.
- [ ] **Attachments** — an upload endpoint backed by the object store, base64 into a content
      block. Image-by-URL already works on `/chat` (`felix_ai/types.py:ContentBlock`, encoded by
      both wires); the gaps are upload, and `openai_compat.py:34` typing content as `str | None`
      so images cannot reach `/v1` at all.
- [~] **Make the bundled manifests use them.** `support` fetches from the docs site and `deep`
      now has `search` + `fetch` with screening on — the two manifests the audit named. Document
      search over the Felix docs waits on the retrieval item above. A tool no manifest declares
      is inert by this repo's own definition, so this stays open until that lands.

Decision gate, not a commitment: the **governed coding toolset** (`read`/`edit`/`bash` behind a
`FilesystemBackend` + `ShellBackend` pair) was deferred as "large, and conditional — only worth
starting if coding-agent use cases are actually on the roadmap". The daily-driver goal makes it
live again. Revisit after the first three land, on evidence, not before.

### B. Close the durable loop

- [ ] **Run a fiber to suspension inside one claim.** `resume_due_fibers` calls
      `_step_with_lease` once per claimed row and `_run_fiber_step` advances exactly one op, so
      a durable chat — whose `steps` has length 1 — needs **two `* * * * *` ticks**: one to run
      `invoke` and set `running`, a second to notice `cursor >= len(steps)` and flip `completed`.
      Minimum ~2 minutes, and the second is pure scheduler latency. A *failure* terminates in one
      sweep, so a failed run reaches its terminal state a full minute faster than a successful
      one. Clear `heartbeat_at` on suspend so sleeping is distinguishable from crashed.
- [ ] **Approvals reach the durable path.** `side_events` is a process-local
      `dict[str, asyncio.Queue]`, so on a fiber the `approval_required` emit lands in the
      worker's own memory and is unreachable by construction — on precisely the path where a
      human would have time to respond. Route it through the Redis layer `session/notify.py`
      already built in `#93`.
- [ ] **Signed completion webhooks**, delivered from the **worker** — the fiber reaches terminal
      state under its cron and the API replica that accepted the request may be gone. Dead letter
      is `status='dead'` on the same durable row, not a second store. `spec.webhooks` selects
      operator-registered endpoint ids and **never carries URLs**: a manifest author holds a
      tenant scope, and a tenant-supplied URL on a path carrying run output is an exfiltration
      channel SSRF checks do not address.
- [ ] **Bound the retry.** A step that raises is caught, logged at `warning`, and released — with
      no attempt counter, no backoff, and no dead letter, so a deterministically failing fiber
      retries every 60 seconds until `expires_at`.
- [ ] **Non-streaming `/chat` approval visibility.** `invoke()` never drains `side_events`, so a
      caller blocked on an approval hangs for the full TTL and then receives a deny, never
      learning an approval was requested.
- [ ] **`ctx.step(key, fn)` memoization** + an append-only `fiber_steps` table, so a crash
      mid-tool-loop resumes instead of replaying a whole `invoke`. Today the only mitigation is
      `_interrupted_tool_results` telling the *model* a call may already have taken effect — a
      prompt-level stand-in for a durability primitive.

Not a gap, checked this cycle: the lease is renewed in flight (`fibers.py:443`, renewal loop at
`:473`), so `FIBER_LEASE_MS` bounds "how long after a worker dies is its fiber stranded", not
"how long may a step take". The replay-on-long-approval bug that shape implies was already found
and fixed; the comment at `fibers.py:36-46` is the record.

### C. Operator console

- [ ] **Persist cost.** `usage/pricing.py` has a real `estimate_cost` with cache and
      long-context tiers; `record_tokens` takes no cost argument and writes none, so `GET /usage`
      (30 lines) returns raw token rows and nothing can answer "what did tenant X spend last
      month". Add cost at write time and return it.
- [ ] **`GET /usage/summary`** — group by manifest / model / day, with totals.
- [ ] **Fill the missing bundled rates** — `gpt-4.1` has no entry and bills at the default, and
      no bundled entry sets a long-context tier, so `limits.max_cost_usd` fails closed on an
      undercount.
- [ ] **Attribute denials in the audit record.** Every wrapper denial emits one undifferentiated
      `policy_deny` carrying `{tool, tool_call_id, thread_id}` — which control fired, and why,
      exists only in the tool message. The wrappers emit Prometheus counters, not audit events.
      An auditor cannot answer "show me every call blocked by policy X in Q3". Then
      `GET /audit/export` over a time range; `audit.py`'s docstring already promises an export
      that does not exist.
- [ ] **Surface eval instrumentation** — `EvalRun.started_at/finished_at` and `ItemScore`'s
      `duration_ms` / token counts / `tool_call_count` are all stored and rendered nowhere. And
      make the judge's fail-open path visible: any exception silently degrades an LLM judge to a
      substring check with `reason: "llm_fallback:<exc>"`, so a misconfigured judge model does not
      fail your eval, it quietly weakens it.
- [ ] **Skills routes.** `grep -rn skill apps/api/src/felix_api/routes/` returns **zero** — 564
      lines of skills subsystem and a `skill_activation` table with no HTTP reachability. List,
      inspect, and report which skill activated on a turn.

### D. Truth in advertising

Small, and blocking for the adopter goal: anyone evaluating Felix on its governance claims reads
`governed.yaml` first. Enforce or delete, per item.

- [ ] **`governed.yaml:142 retention_days: 30` is inert** — defined in `schema.py:595` and read
      by nothing; `jobs/retention.py:17` hardcodes the TTL. Already named in
      `test_inert_manifest_fields.py`. The flagship governed manifest declares a data-retention
      policy that changes nothing.
- [ ] **`governed.yaml:128 guardrails.targets: [input, output]` does not scrub replies.**
      `apply_guardrails` wraps **tools** only; `redact_pii`'s two call sites are user input
      (`inbound.py:200`) and tool output (`builder.py:547`). A reader reasonably concludes the
      agent's replies are PII-scrubbed. Implement outbound redaction, or correct the manifest and
      `deploy/GOVERNANCE.md`.
- [ ] **Five `PlanExecuteSpec` fields are inert** — `planner_model`, `executor_model`,
      `replan_on_failure`, `max_replans`, `planner_few_shots` (`schema.py:435-442`) each have
      exactly one reference: their own definition. The documented replan behaviour does not exist.
- [ ] **Final-response judges do nothing on the streaming path.**
      `wrap_final_response_judges` passes events through unjudged on `stream_events`, so the only
      outbound model-call control is inert on the primary chat surface.
- [ ] **Inbound screening skips two paths** — called from `/chat`, `/v1` and A2A, but not from
      `routes/mcp.py` and not from the durable fiber path. A manifest with
      `content_screening.enabled: true` is unscreened over MCP and on every background run.

Checked and *not* a gap, so nobody "fixes" it: `allow_unattended` is enforced — at compile, under
`eu_ai_act` at `risk_tier: high` (`governance.py:209`), which is why `contributor.yaml` carries a
comment explaining exactly that. It is conditional, not inert.

---
## Next (this quarter)

### Harness

- [ ] **Tamper-evident audit chain** — `seq` + `prev_hash` + keyed HMAC per row, per tenant,
      with `verify_chain` reporting the first break. Allocate the chain at write time inside the
      insert transaction under a per-tenant advisory lock (`session/store.py:93` is the
      precedent), so a `DurableBuffer` drop does not read as tampering. Hash a `payload_sha256`
      column rather than the payload bytes — `jsonb` does not preserve key order. Retention needs
      a pruning anchor or it breaks the chain it prunes. Pairs with **audit export** in C.
- [ ] **Framework mapping earns its name, or loses it.** `validate_governance` is 55 lines of
      compile-time flag assertions with no mapping to a control id (no CC6.1, no Article 14) and
      no artifact — nothing produces "here is your evidence for control X". `_has_boundary_control`
      is satisfied by `any_limit(...)`, and `EffectiveLimits` backfills every limit from
      `ABSOLUTE_LIMITS`, so that check is close to unfalsifiable. Either produce a signed compile
      receipt (`manifests/pin.py` already stores a content hash per thread and is the closest
      thing to evidence in the system), or rename the field so `frameworks: [soc2]` stops
      inviting a reading it cannot support. The schema disclaimer is right and is in the file
      nobody reads.
- [ ] **Temporal: decide.** The arm is a 152-line driver loop using none of Temporal's durability
      primitives — no signals, no queries, no child workflows, no `continue_as_new`, no activity
      retry policy. State still lives in the Postgres `Fiber` row, so an operator choosing it for
      Temporal's guarantees gets Felix's. Four of its six tests assert only that the classes can
      be constructed, and there is no integration test against a dev server. It does fix the
      one-op-per-tick problem — which item B1 fixes for everyone. Either invest properly or
      document it as a compatibility shim.
- [ ] **Live-model eval (optional CI)** — the current gate is 3 fixture items whose mock answers
      satisfy their own rubrics (`_mock_answer` returns `rubric["expect"]` when none is given), so
      it proves the plumbing executes and scores nothing about the agent. Optional nightly against
      `api.felix.run` that does not block PRs.
- [ ] **Eval scoring depth** — four string rules (`equals` / `contains` / `min_chars` / non-empty)
      plus one judge. No regex, no schema check, no tool-call or trajectory assertions, no numeric
      tolerance, no significance test on comparative runs. Nobody can gate a model change on this
      without writing their own scorer.
- [ ] **Long-context price tiers** — `estimate_cost` supports request-wide tiers but no bundled
      entry sets one. Needs current rates per deployment via a manifest price override. Folded
      into C where it touches `max_cost_usd`.
- [ ] **Memory defaults** — `FELIX_MEMORY_EMBEDDER=none` by default, so the vector channel never
      runs out of the box and nothing exercises it outside tests. Of nine bundled manifests only
      `cowork` and `governed` enable capture and recall tools, so `quick` — the manifest every
      README example uses — has no long-term memory at all. Extraction quality is whatever one
      prompt returns; a live run stored an assistant's apology as a durable fact.
      `consolidation.py` is 14 lines against `extraction.py`'s 340, so the store only grows.
- [ ] **Who may retire a memory by naming its `topic_key`** — `put_memory` supersedes any active
      row sharing a `topic_key`, and `capture_from_turn` reaches the same supersession post-turn
      through no governance wrapper at all. The durable fix is store-level: require rank above
      `_DEFAULT_TRUST` for a cross-row sweep, so rank-1 writers store alongside rather than
      retire. A real ergonomic change, which is why it is a decision and not a patch.
- [ ] **`deploy/GOVERNANCE.md`: which layer owns retirement** — follows whichever way the above
      lands. `retired_by` versus `source`, why resurrection is gated on who retired rather than
      who wrote, and which of the manifest, the store and the approval wrapper is authoritative.
      Enforced in `tests/conformance/test_memory_trust_matrix.py`; the prose does not exist.
- [ ] **Warn when `when_args` names nothing** — `ApprovalRule.when_args` is not validated against
      the gated tool's schema, so `when_args: [topickey]` yields a rule that never fires and still
      passes `validate-manifest` and both framework checks. `RememberArgs` is a pydantic model with
      `extra="forbid"`, so the check is cheap. Decide whether it warns or refuses.
- [ ] **Split-turn compaction** — when one turn alone exceeds `keep_recent_tokens` the cut lands
      mid-turn and one summary covers both sides. Two summaries with different prompts and budgets
      is the fix. Narrow: only bites on very long single turns.
- [ ] **Tools carry their own prompt copy** — a `prompt_line` / `prompt_guidance` on `Tool`,
      assembled in `builder.py`, so the system prompt is derived from the active tool set instead
      of hand-maintained. Removes a drift class; more valuable once **A** multiplies the tool set.
- [ ] **Telemetry vocabulary** — no span/attribute schema, and no metric catalog anywhere, so an
      operator cannot know what to graph without grepping call sites. `metrics.py` also silently
      degrades to `logger.info` when a name is reused under a second label set.
- [ ] **Ship one dashboard** — zero matches for grafana / servicemonitor / prometheus config under
      `deploy/`. A Grafana JSON and a ServiceMonitor turn four emitted signal types into something
      an operator sees.
- [ ] **Sandbox ladder extras** — capability-bridge / gVisor as documented extras, not the default
      lean image.
- [ ] **OAuth / dynamic provider keys** — secrets backends cover static keys; refresh /
      `getApiKey(provider)` only if a real customer path needs it.
- [ ] **`append_batch` read-modify-write** — fold the read into the insert
      (`INSERT … SELECT coalesce(max(seq), -1) + :offset … RETURNING seq`) to shorten the lock
      window to one round trip. Deferred as the highest-risk change the ASGI audit named: it is
      the write path for every session event, the multi-row sequence allocation has to move into
      SQL, and the in-memory twin allocates differently. Conformance against real Postgres is
      mandatory, not optional.
- [ ] **Decide on a JWT verification cache** — `verify_jwt` verifies signatures on the event loop
      for every request in `jwt` mode. A TTL cache keyed on the token digest removes the repeat
      cost, but a cached "valid" survives a revocation for as long as it lives. That is a posture
      call about how stale an authorisation may be, and it wants an owner rather than a default.

#### From the governance mutation audit (Sep 2026, #141–#150)

Carried forward intact. These are the exempt kind under the meta-work budget: several are
security findings on the durable-run authority path, and they came from mutating live controls
rather than from re-reading a file. The wave itself is written up in [HISTORY.md](HISTORY.md).

- [ ] **Governance-gap counters vs the tenant metric allowlist.** `runtime.py`'s
      `_apply_metric_allowlist` runs before `build_agent`, and `observability/metrics.py` drops
      any counter not in `spec.observability.metrics`. So a tenant-authored manifest that sets
      that field for any reason silently suppresses `felix_untrusted_tools_unscreened`,
      `felix_policy_unsatisfiable` and `felix_rule_targets_nothing` — the exact signals
      `deploy/GOVERNANCE.md` now tells operators to watch. The WARNING still fires, so this is
      partial. **Decide:** should governance-gap counters be exempt from the manifest allowlist?
- [ ] **Keep growing the fiber scheduler, or make Temporal the documented multi-step path.**
      Temporal already wraps the same `advance_fiber`; what fibers duplicates is the scheduling
      envelope, and that is where this audit's durability bugs were — a lease that equalled the
      approval timeout (#150), resolution outside the tenant context (#150). **B6** above
      proposes step memoization and an append-only `fiber_steps` table, which is an activity
      model by another name. **Decide before starting that item.**
- [ ] **`cowork.yaml` sets `auth.inbound.allow_anonymous: true` on a manifest that binds a
      local shell.** The `client-shell` approval rule and the `thread_id`/`tool_call_id`
      requirement are what stand between an anonymous caller and command execution on a
      developer's machine. Untouched by the audit; wants a conscious yes or no.
- [ ] **A per-tool screener cost lever.** `content_screening.tools` became additive in #146, so
      the only per-tool cost control is gone. Free in the default configuration (no `model`
      set), and a manifest binding twenty MCP tools that named three now pays twenty screener
      calls per turn where it does. If that shows up: add `model_tools:` — *which tools get the
      expensive screener*, marker screening unconditional — never a way to exempt an untrusted
      tool from screening.
- [ ] **Should `felix validate-manifest` hard-fail on a pattern matching no declared
      integration?** Compile-time tolerance exists for the dynamic tool set (a failed MCP
      discovery binds nothing). At author time the builtins plus declared refs are statically
      known, so `github__*` against a builtin-only agent is a typo with no runtime excuse.
      Author-friction call.
- [ ] **Fiber rows are never swept.** `jobs/retention.py` covers `audit_events`, `plans` and
      `memory_vectors`, not `fibers`. So `state.auth` — principal, scopes, scheme — accumulates
      indefinitely, outliving both the run's usability and the 30-day audit TTL that motivated
      it. Retention for `fibers` is the fix.
- [ ] **Temporal carries `state["auth"]` into workflow history.** `start_fiber_workflow` passes
      the whole fiber dict as the workflow argument, and the activity re-passes it per step, so
      `{principal_sub, scopes, scheme}` for every tenant accumulates in one namespace outside
      the RLS boundary and outside the run's TTL. User message content already went there; a
      scope inventory is new.
- [ ] **The Temporal path trusts the fiber row wholesale.** `fiber_step` calls `advance_fiber`
      with the row straight from the workflow argument, never re-read from Postgres, and
      `_save_fiber` writes under `rls_bypass()`. Anyone who can start a workflow on the
      `felix-fibers` task queue therefore chooses `tenant_id`, `expires_at` and now
      `state["auth"]`. Temporal access is privileged; this should be a documented assumption.
- [ ] **Memory tools are not untrusted.** `recall` and `list_memories` are `transport: local`
      with `source: memory`, which is not in `_UNTRUSTED_SOURCE_PREFIXES`, so recall is not
      screened by default — `cowork.yaml` names them explicitly instead. Capture runs over turns
      containing untrusted tool output, so recall is a re-entry path for content quarantined on
      the way in. Either add `memory` to the untrusted prefixes or keep it a per-manifest choice.
- [ ] **`scheme` replay on resume.** A resumed fiber presents the recorded scheme without
      holding a credential, so `auth.inbound.schemes` can only ever agree with the enqueue-side
      check. Defence in depth lost, not a hole; worth a sentence in GOVERNANCE.md.
- [ ] **`pr-quality-gate.sh` does not treat `durability/` as a control path.** It reported
      "felix-security-reviewer is not needed" on #149, the most security-relevant change of the
      session — a resumed run's authority comes from there. Add `durability` to the token list.
- [ ] **felix-web docs lag #148–#150.** `internals/governance.mdx` covers screening and glob
      targeting; the durable-run authority model, the lease semantics and the RLS ordering are
      only in `deploy/GOVERNANCE.md`.
- [ ] **`durability` stays a closed `Literal`.** Fibers-vs-Temporal is not a factory swap, so a
      registry there is a feature, not a refactor. Recorded so it is not "opened" by mistake.

### Headless / contract

- [ ] **Nothing enforces RLS coverage for a new tenant table.** `0006_tenant_rls` applied a
      policy to a fixed list; a table added later is covered only if whoever added it
      remembered, and the failure is silent — the table simply is not isolated.
      `document_chunks` (migration `0010`) carries its policy because it was written by hand,
      which is the argument, not the reassurance. An invariant comparing tables with a
      `tenant_id` column against those carrying `felix_tenant_isolation` is ~15 lines and is
      the natural candidate for this cycle's one hardening item.

- [ ] **Headless invariant is prose only** — CLAUDE.md asserts it; nothing fails when it stops
      being true. An AST/file check over `apps/api` for `StaticFiles`, `Jinja2Templates` and
      `app.mount`, plus a tracked-file check for asset extensions, is ~20 lines in the existing
      idiom. Cheapest item here.
- [ ] **No-CORS contract undocumented** — the stack is body-limit → rate-limit → auth with no CORS
      layer, so a browser on another origin cannot call Felix directly. Deliberate, written down
      nowhere; the requirement survives only inside felix-web's `worker/index.ts`. A self-hoster
      pointing a browser app at `:8080` hits an opaque wall.
- [ ] **`POST /chat/ui` sub-protocol unspecified** — the route exists and the harness can block on
      a waiter for `DEFAULT_TIMEOUT_SECONDS = 300`. Document the frames and move the timeout to a
      `FELIX_` setting. Related: `request_ui` / `request_confirm` / `request_select` have **zero
      callers in core**, so no tool exposes them and an agent cannot currently ask the user a
      structured question — a capability-surface item hiding in a documentation one.
- [ ] **Wire-contract snapshot** — snapshot `/openapi.json` and the SSE event-name set.
      `felix-run/web` mirrors `StreamEvent` by hand and its union has an open arm, so an added or
      renamed frame silently does nothing on both sides. Fold in **snapshot-authoritative
      streaming** if it happens.
- [ ] **Publish the SDK, or say it is not one.** `felix/sdk.py` is 570 lines covering ~27 of ~72
      operations, all in `/chat` + `/approvals`, returning `dict[str, Any]` throughout — no
      response models, no event-name enums, no typed exceptions, no pagination helpers — and it
      lives inside the harness, so importing it drags the whole server dependency tree. The
      durable-run polling and lease handling in it are genuinely good. The README never mentions
      it, so a Python adopter finds it by reading source.

### Control plane

- [ ] **A tenant is a string.** There is no `Tenant` table and no `ApiKey` table; `tenant_id` is a
      column on every row and never a foreign key. Minting a key means editing
      `FELIX_AUTH_API_KEYS` JSON and restarting. Manifest CRUD, canary and rollback are real and
      API-driven; onboarding tenant #2 is a config edit and a process restart. Decide whether that
      is the product (single-operator self-host) or a gap, and write the answer down either way.
- [ ] **Manifest version listing** — `GET /manifests/{name}?version=N` fetches one; nothing
      enumerates what exists, so rollback requires knowing the number already.
- [ ] **Run a job now** — `jobs.py` is CRUD plus run history; you can only wait for cron.

### Repo / release hygiene

- [ ] **Required status checks + `CODEOWNERS`** — CI runs 14 jobs and nothing blocks a merge on
      them, so the review gate is convention rather than mechanism. Make the `changes` job report
      success so doc-only PRs stay mergeable.
- [ ] **Tag-driven release** — build, push to GHCR, attach an SBOM, sign with cosign via OIDC,
      then point `deploy/` at the published image. CI already builds and scans the image and
      throws it away.
- [ ] **Single-source the version** — `0.2.2` lives in four `pyproject.toml` files, the root, and
      the Helm `appVersion`. Releasing means editing six files correctly from memory.
- [ ] **`uv --exclude-newer`** — refuse dependency versions published in the last day or two, the
      analogue of an npm `min-release-age`. Cheap; the rest of the supply-chain posture is covered.
- [ ] **Postgres 18** — `pgvector/pgvector:0.8.6-pg18-trixie` exists. Own branch with a rollback
      plan: compatibility pass over the revisions, FTS index, RLS, and a dump/restore path.
- [ ] **`.cursor/plans/` decision** — tracked but ungitignored. Keep as versioned planning notes
      or ignore; either is fine, drifting is not.

### Product (`felix-run/web`)

- [ ] **Rails before toast.** The last several PRs were all one toast component; `PRODUCT.md` says
      failure looks like "the rails are wallpaper". Cost view and eval instrumentation (C) are the
      two panels that make the right rail answer its own brief.
- [ ] **Session-control UX gaps** — export JSONL from the UI, clearer lease-contention copy,
      reconnect-to-snapshot after a hard refresh, empty/search states.
- [ ] **Labels name the thing, not the wire key** — `agent-sheet.tsx` prints `max_tokens`,
      `checkpointer`, `full_replay` verbatim.
- [ ] **Prune leftover TS-harness skills/copy** in the docs sync sources. The getting-started
      rewrite landed (that item is done, and this file claimed otherwise until 2026-09-02); the
      residual TS-era prose elsewhere did not go with it.

### Deploy

- [ ] **Cowork completion smoke on GCE** — local durable poll reaches `completed`; prod smoke
      still only asserts a cowork `202` accept. Extend `.github/workflows/smoke.yml` with a
      **soft** completion poll (`continue-on-error: true`, ~3 min). Cheaper once **B1** removes the
      two-tick floor.
- [ ] **Governed demo path (decide)** — either enable on GCE (RBAC scopes for chat keys) **or**
      keep the demo anonymous and document that choice in `deploy/GOVERNANCE.md`.
- [ ] **GKE dogfood** — Helm + ESO → one known-good install note under `deploy/gcp/`.
- [ ] **AWS smoke checklist** — mirror the GCP path (Secrets Manager / S3) in `deploy/aws/`.
- [ ] **Postgres RLS dogfood** — migration `0006` + `FELIX_DATABASE_RLS=true` on a non-prod
      branch; verify retention bypass + mixed-tenant audit flush.
- [!] **Rotate Anthropic API key** — only when you say go. Then Secret Manager
      `felix-anthropic-api-key` + recreate API/worker.

---
## Later / explicit non-goals

- [!] **`memory.consolidate` LLM merge** — worker already hash-dedupes.
      `enabled` / `model` / `after_facts` stay unused on purpose (v1).
- [x] **`memory.checkpointer` aliases** — resolved by implementing the field
      rather than deleting it. `postgres` / `none` are built in and the registry is
      open (`register_checkpointer`), so `agentcore` can be a plugin. `do` names
      Durable Objects and stays unimplementable here by invariant. There is no
      in-process built-in: a thread is not manifest-scoped, so a per-manifest
      *backend* would split-brain with the fifteen session routes.
- [!] **`FELIX_POLICY_BUNDLE_PUBKEY` / OPA** — no signed policy-bundle
      runtime in v1 (governance stays manifest compile + wrappers).
- [!] **Commerce / billing plugin** — seam only; no Stripe in-tree.
- [!] **Cloudflare compute in the harness** — Workers/DOs out of `felix`;
      CF hosts web + named tunnel to GCE API only. Scoped to *compute*: the
      `workers_ai` model provider is an outbound HTTPS call to api.cloudflare.com,
      the same shape as every other hosted provider, and does not reopen this.
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


## Shipped

Moved to [HISTORY.md](HISTORY.md) — the wave-by-wave record of what shipped and what each wave
taught, including the audit conclusions that did not survive being measured.
