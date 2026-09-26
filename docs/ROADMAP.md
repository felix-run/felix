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

The program that turns that sentence into rungs — who proposes, who decides, what a ticket must
cite, what Felix may never edit, and the numbers that graduate each rung — is
[SELF.md](SELF.md). This file stays the *what*; that one is the *how*.

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
| Built-in tool registry | 9 tools |
| `skills/` shipped Agent Skills | 5, of which 4 document Felix itself |

The built-in registry in full: `calculator`, `list_dir`, `read_file`, `write_file`,
`edit_file`, `search_files`, `list_skills`, `activate_skill`, `deactivate_skill` — and the three skill tools
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
      workstream each drew ~7 review findings. The follow-up landed: `spec.document_tools`
      binds a retrieval tool and `support` declares it as `search_docs`. What remains of this
      item is ingesting the Felix docs themselves into a deployment's corpus, which is an
      operations task rather than a harness one.
      Reuses the `Embedder` seam and `FELIX_MEMORY_EMBEDDER` rather than adding a second
      embedder setting — one embedder per deployment, one vector dimension.
- [x] **Structured output** — `spec.output_schema` is a JSON Schema the answer must match, and
      `/v1/chat/completions` accepts OpenAI's `response_format` for the same thing per request
      (the manifest's wins). The OpenAI wire emits `response_format`, strict when the schema
      closes every object and requires every property; the Anthropic wire, which has no
      equivalent, sends the schema as a tool the model must call and folds the call back into the
      reply, so `message.content` is a JSON document on either. `tool_choice` is `any` rather than
      naming that tool whenever real tools are also bound, so a react loop can still reach them.
      - **The composite patterns** — done for four of five. `router`, `parallel`, `reflect`
        and `plan_execute` now thread `ModelChatOptions` onto the one turn whose output the
        caller receives, and declare `honours_output_schema`. Placement is per pattern and
        explicit at each call site: the parallel synthesis but not its specialists, the
        plan_execute synthesis but not the plan or the steps — which needed the schema
        stripped from the context its executor is built from, since `build_react_agent` reads
        it onto the agent itself — the routed child but not the classifier, and every reflect
        draft because the loop exits early. The manifest outranks a request's
        `response_format`, matching react. `_child_input` no longer
        drops `model_options`, so a caller's `/v1` `response_format` reaches a child too.
        `groupchat` stays refused with the reason recorded next to its registration: its answer
        is the last speaker's message stamped `[name] …`, so a child's JSON comes back with a
        prefix on it. Supporting it means dropping the stamp or adding a synthesis turn.
      - Not done, and deliberately a separate item: **validation with a repair retry.** The
        provider is what enforces the shape here, which is the guarantee worth having and is why
        this shipped without a retry loop. Extended thinking is the hole — Anthropic forbids a
        forced `tool_choice` while `thinking` is set, so there the schema is offered and logged as
        not guaranteed. A validate-and-retry pass would close that arm; until then, do not promise
        the shape on a thinking-enabled Anthropic agent.
- [~] **Attachments** — split, on the evidence that the two smaller features in the document
      retrieval workstream each drew ~7 review findings where one wide branch would have drawn
      them all at once.
      - Landed: **inline images actually work.** `/v1/chat/completions` takes OpenAI's list of
        content parts, so an SDK can send an image; and the Anthropic wire encodes a `data:` URL
        as a `base64` source instead of putting it in a `url` source, which that API rejects.
        That second one was a live defect rather than a missing feature — both wires had an
        image encoder, and an image reached `gpt-4o` and 400'd on `claude-sonnet`, the default.
        Found by running the encoder, not by reading it.
      - Landed: **the storage half of upload.** `POST /files` / `GET /files/{file_id}`, backed
        by the object store and gated on new `files:read` / `files:write` scopes; tenant from the
        caller's credentials and never from the path, 404 for a malformed reference, server-issued
        uuid4 id. Capped at 600 KiB decoded and to the image types both wires encode — the cap
        sits below `CORE_BODY_LIMIT_BYTES` deliberately, since above it the middleware answers 413
        before the route and hides the real ceiling.
      - Landed: **the `file_id` content block**, resolved to bytes at the wire. Resolution is
        late, per turn, so the session event log keeps the reference rather than the base64 it
        expands to. One correction to this entry as written: the reference does **not** need a
        new field. It rides in the `url` an inline image already uses, as `felix-file://<id>`,
        because `session/types.py` persists and restores `attachments[].url` and both wires read
        it — a parallel `file_id` field would have had to be threaded through each, and the one
        that would have been missed is the session layer, which is the only path a *second* turn
        takes. Nothing would have failed until replay, which is this repo's defect shape exactly.
        `packages/ai` still does no resolving (it may not import `felix`); it parses the part into
        a reference and drops any reference that reaches a wire unexpanded, since that means the
        harness did not do its half.
      - Landed, and it was the stated condition on granting `files:write` to an untrusted
        tenant: **a per-tenant quota and a retention sweep**.
        `FELIX_ATTACHMENTS_MAX_BYTES_PER_TENANT` bounds stored bytes (409 over the line, not
        413 — the request is a fine size and the account is full) and
        `FELIX_ATTACHMENT_RETENTION_DAYS` lets the nightly sweep collect old uploads;
        `attachments/` had joined `artifacts/` as a prefix nothing collected. Both needed
        migration `0016`'s ledger, because the `ObjectStore` Protocol has no `list` — so
        nothing could count what a tenant held or find what was old. Bytes rather than a
        count, since each upload is already capped at 600 KiB and the resource is disk.
        The bytes stay the system of record: the row is written before the object and deleted
        after it, so every interruption leaves a row whose bytes may not exist -- visible and
        collectable -- rather than bytes no count can name. Both reviewers caught this the
        other way round on the first pass, where it would have made the quota fail open. Existing uploads are not backfilled — a backfill would need the `list`
        this table exists because we lack.
      - Decided, and the answer made the choice smaller than it looked: resolution sits **after**
        `apply_inbound_screening`. Checked rather than reasoned — `governance/inbound.py:_message_text`
        collects only blocks whose type is `text`, so image content has never reached a screener
        and resolving early would have handed it a block it ignores. So this is not a coverage
        regression; it puts `file_id` images exactly where inline images already were. What
        remains open is the real item underneath: **image content is not screened at all**, and
        text rendered inside an uploaded image is an injection channel on both paths.
      - Follow-up, from the quality review of the quota: **split `felix/attachments.py`**. It is
        ~600 lines doing four jobs — magic-number validation, key and containment rules, the
        `felix-file://` resolver, and now a Postgres-backed ledger — and the fourth brought a
        dependency class the others do not have. The tell is that the ledger was inserted
        *through the middle* of the magic-number subject, which is no longer contiguous. Seam,
        in dependency order with no cycles: `attachments/ledger.py` (the only module that knows
        Postgres exists), `attachments/store.py` (constants, magic, keys, put/read/delete),
        `attachments/refs.py` (`resolve_file_refs`), with `__init__.py` re-exporting the current
        `__all__` verbatim so no import site changes.
      - Open, and a change to a security control rather than a feature: uploads are bounded by the
        single global `BodyLimitMiddleware` limit, so a larger ceiling means per-route limits.
        That middleware has a bypass in its history; it should not be widened as a side effect of
        an attachments change. Multipart ingest would also remove the base64 inflation.
- [x] **Make the bundled manifests use them.** `support` fetches from the docs site and now
      searches the corpus as `search_docs`; `deep` has `search` + `fetch`. Both with screening
      on, which is what keeps the unscreened-tools warning silent on what we ship. A tool no
      manifest declares is inert by this repo's own definition, and none of these are now.

- [~] **Governed shell tool.** The decision gate that sat here — the `read`/`edit`/`bash` coding
      toolset, deferred as "only worth starting if coding-agent use cases are actually on the
      roadmap" — is decided: [SELF.md](SELF.md) puts Felix building Felix on the roadmap,
      and rung 2 of it cannot exist without a way to run `./scripts/test.sh`. Landing as
      `spec.shell_tools` behind `FELIX_SHELL_ALLOWED_COMMANDS` (argv prefixes, no shell interpreter,
      scrubbed env, cwd pinned under the workspace root), not as a `ShellBackend` registry — one
      implementation does not earn a registry.

- [x] **Decision models (Jev).** Plan: `~/.claude/plans/we-want-to-use-sprightly-sprout.md`.
      Some calls decide rather than write, and each one asked a chat model for prose and
      parsed it. Examples: the router's "reply with only the agent name", which silently
      falls back to the first sub-agent; the judges' `{score}` JSON; the injection scorer's
      "number only". The seam is `felix_ai.decide` plus `FELIX_DECISION_ROUTES`, with the
      `typesafe`, `workers_ai` and `llm` backends. Each consumer opts in on its own, and keeps
      its current path as the fallback when the decider errors or is not confident enough.
      Land in order, one PR each:
      1. [x] The seam, plus `tools_retrieval.decider` (#314).
      2. [x] The router's `_choose_child`, and confidence escalation's `_low_confidence` (#315).
      3. [x] (#318) Judges, reply judges, eval `llm_judge`, and the reflect verifier: a `Noul` per
         criterion.
      4. [x] (#321) Skill suggestion: the two-stage rank-then-rerank, emitted as a transient prompt hint.
      5. [x] Injection screening as a `Noul` battery. It is *additive* to the regex and the
         LLM scorer, because Jev is not adversarially robust. Needs a security review.

      Not yet verified: the Workers AI response envelope, against a live call.

- [x] **Sub-agents are compiled from bundled YAML only.** Found in a real run of the router
      e2e test: `runtime.py:build_tenant_agent` never sets `BuildDeps.sub_agent_builder`, so
      `builder.py` compiles each `spec.sub_agents` name with `build_agent(name)` →
      `load_bundled(name)`, and a name missing from `manifests/` becomes an *empty* manifest
      (`You are <name>.`, no tools). A router whose children live in the manifest store — the
      tenant's own agents — routes to blank agents without an error. Fix: resolve children
      through `resolve_tenant_manifest` with the parent's tenant, and fail the compile on a
      name that resolves nowhere.

      Two decisions this surfaced, both taken:
      - [x] **Child inbound auth — inherited.** `enforce_inbound_auth` has never run on sub-agents — bundled
        `router` (`allow_anonymous: true`) already reaches `deep` (`allow_anonymous: false`).
        With tenant-authored children, an author can no longer rely on a child's
        `required_scopes` once it sits behind a public router. Enforce at compile (a child
        stricter than its parent fails — which breaks bundled `router` → `deep` as written), or
        document that routing inherits the parent's auth. Decided: inherited, documented in
        `deploy/GOVERNANCE.md` under inbound constraints.
      - [x] **Shallow compile pins — now deep.** `ensure_thread_pin` and `assert_pin_matches` hash the parent
        only. Children used to change only with a deploy; stored children change mid-thread,
        so a `pin_compile` thread picks up an edited child's tools and policies. Fold
        `(child, version, hash)` into the pin, or say in `deploy/GOVERNANCE.md` that pins are
        shallow. Done: a `sub_agents_hash` beside the parent's, checked on every turn and
        durable resume; pins taken before it gain the digest on their next turn.
      - [x] **Children are resolved twice per turn.** The pin digest resolves them in
        `ensure_thread_pin`, the compile again in `runtime._tenant_sub_agent_builder`, so a child
        activated between the two (a 30s active-pointer expiry) compiles for one turn under a pin
        that checked its predecessor. Closing it means handing the checked tree to the build —
        `prepare_tenant_invoke` returning what it resolved, through its ~10 callers. A
        request-scoped cache is the tempting shortcut and the wrong one: worker tasks run
        fibers back to back, and a stale entry would compile the wrong child.

### B. Close the durable loop

- [x] **Run a fiber to suspension inside one claim.** Closed. The entry undercounted it: the
      cost is one tick *per op* plus one, not two overall — measured before and after rather
      than reasoned about, three stashes took four sweeps and now take one, a durable chat two
      and now one. A claim runs the fiber until it suspends, where suspension is exactly
      `status != "running"`. Fairness is what makes it safe and it is unchanged: a claim runs
      at most one `invoke` — the only op that can take seconds — so wall-clock per fiber per
      sweep is what it always was, and only the bookkeeping ticks go away.
      Two things the change turned up that the entry did not predict. The claim has to be held
      across the loop and released once at the end (`hold_claim`), because releasing per step
      and re-acquiring is *not* equivalent — `_renew_lease` only renews a lease this worker
      still holds, so the gap lets a second worker take the fiber and run the next `invoke`
      concurrently: a duplicated side effect, not a lost write. And `attempts` counts
      consecutive failures, which a landed step and a failed step sharing one claim would
      otherwise break — a failure after progress is charged as the first of a new streak.
      The entry's last sentence was stale rather than wrong: **there is no `heartbeat_at`
      column**, anywhere in the models or migrations. Sleeping is already distinguishable from
      crashed by `status` plus `lease_until`, which `_save_fiber` clears on every save.
- [x] **A durable run streams its transcript** (felix-run/felix#238). `POST /chat/stream` on a
      durable manifest sent `run_accepted` → `run_status` → `final` and nothing between, so the
      answer arrived and the tool calls behind it did not. Correction to the premise this started
      from, and it matters for the entry below: bridging `side_events` through Valkey fixes
      nothing here, twice over. `drain` is called inside the agent's own loop and `emit` comes
      from tool execution in the same process, so on a fiber **both ends are already in the
      worker**; and the events do not exist to be bridged anyway, because `invoke` is
      `_run(..., emit_events=False)` — the deltas and `on_tool_start`/`on_tool_end` are dropped
      at the source and the `drain_side_events` loop is itself inside `if emit_events:`. What the
      fiber does produce is the session log, written incrementally by `_append_produced` outside
      every `emit_events` guard. So the stream now tails it, through the same helper
      `GET /chat/stream/{thread_id}` uses. No new transport, no second source of truth. Completed
      messages only — chunks are never persisted, so a durable run never streams token deltas.
- [x] **Split the SSE tail out of `routes/chat.py`** — `routes/_streaming.py`, beside
      `routes/_sse.py`. Deferred from #238 as motion that would have obscured the change it rode
      in on, and done first here because the approvals entry below is itself a streaming change.
      `chat.py` was 1,774 lines and the largest module in the repo; 417 of them were one subject —
      *tailing a session log over SSE, and how fast to ask* — with no route decorator among them
      and no importer outside that file and its tests. The division against `_sse.py` is that it
      knows the frame *envelope* and this knows the *source*. Names lost their leading underscore
      on the way, matching `_sse.py`: a private module with a public surface. Verified as a pure
      move by diffing the relocated blocks against `main` modulo the renames — the only other
      changes are one comment re-attached to the constants it explains (it had drifted onto
      `RUN_TERMINAL`), `json` hoisted to module scope, and a return annotation.
- [x] **An approval says why it fired and what it blocks** (migration
      `0015_approval_reason_and_call`) — the half of the entry below that does not need a
      transport. The two channels were each missing what the other had, and both sides of the
      wire had already written it down: `builder.py` at the emit ("the `/approvals` row does not
      carry it either") and `@felix/client`'s `PendingApproval`, which documents `reason` as
      frame-only and `expiresAt` as poll-only. So the row gains `reason` and `tool_call_id`, the
      frame gains `expires_at` read off the row, and `list_approvals` takes a `thread_id` filter
      (under-reporting by construction, since `create_pending` reuses a row across threads). This
      matters most on the durable path, where the poll is the only channel and was the half that
      could not say *why*.
- [x] **`approvals` can be reclaimed, and `jobs/retention.py` stopped claiming it already was.**
      The docstring listed the table as "bounded by ... its run or job"; there is no FK, no
      cascade and no `delete(Approval)` anywhere, so it grew for the life of the deployment, and
      nothing moves a timed-out row off `pending` either. Closed by *both* halves of the choice
      this entry offered, because they were not alternatives: `FELIX_APPROVAL_RETENTION_DAYS`
      (default 0 = keep) sweeps **settled** rows on both backends, and the docstring now says
      what is and is not bounded. Settled is the exact negation of `find_approved`'s predicate —
      an unexpired `approved` grant is never swept however old, and neither is one with a null
      `expires_at`, because retention must not silently revoke authorization from a cron job.
      Off by default, matching session retention: the row is the record of a human decision and
      the `soc2` / `eu_ai_act` profiles lean on it.
- [x] **A2A `taskId` went off the wire straight into a thread id.** Closed, and it was not
      only A2A: `eval/runner.py` composes `{tenant}:eval:{run}:{item_id}` the same way, and
      dataset items arrive through `PUT /eval/datasets/{name}`, so that id is caller-supplied
      too. Both skipped every rule `effective_thread_id` applies — the `#` rejection and
      `MAX_THREAD_ID`. The cap turned out to be load-bearing rather than hygienic: `thread_id`
      is the tail of the `session_events` primary key and `task_id` half of the `a2a_tasks`
      one, both btree, so an incompressible id past ~2700 bytes *fails the insert* ("index row
      size 3864 exceeds btree version 4 maximum 2704") rather than merely bloating the index —
      a 500 repeatable at the rate limit. Verified against a throwaway Postgres, because
      `memory://` keys a dict and shows none of it. Both sites now compose through
      `thread_ids.a2a_thread_id` / `eval_thread_id` — one composer per namespace, so which
      segment may carry a `:` is fixed by a signature rather than by call-site arity. A2A
      answers `-32602` before writing the task row, and an
      eval item with an unusable id fails that item rather than the run. `:` stays legal in the
      last segment so `urn:uuid:…` task ids keep working. `felix_api/threads.py` moved to
      `felix/thread_ids.py` to make one definition reachable from the harness. The security
      review then found the half this missed: `PUT /eval/datasets/{name}` writes `item_id`
      into the `eval_dataset_items` primary key, so the same insert failure sat one route
      *earlier* than the new guard. `validate_items` refuses it now — a new 422 on ids that
      were already unrunnable.
- [x] **`replica_id` was `"local"` on every worker, so lease ownership named nothing.**
      `durability/fibers.py` decides whether a claim is its own with
      `lease_owner == replica_id`, and nothing ever set `FELIX_REPLICA_ID` — not the chart,
      not a Compose overlay, and `validate_runtime()` did not require it under `scale_out`.
      Every worker therefore claimed under one name and those predicates matched each other's
      claims. Found by the security review on #262. Closed both halves, because the default
      and the deployment were separate failures: the default is now `{hostname}:{pid}` —
      host and pid rather than a uuid, since in Kubernetes the hostname is the pod name and
      this is a value an operator reads — and the chart sets it from the downward API in the
      env tier every deployment shares. Compose needed no change: containers already get
      distinct hostnames, checked rather than assumed. An empty value is refused outright: it
      would be *worse* than the constant, because `lease_owner` is `""` on every unclaimed
      row. Nothing was broken end to end — claim exclusion rests on `lease_until` plus
      `FOR UPDATE SKIP LOCKED` — but the second line of defence those predicates are written
      to be was absent.
- [ ] **The `ui` waiter is a bearer capability with no tenant in it.** `ui:{request_id}` carries
      no tenant (`ui/prompts.py`), and `POST /chat/ui` does `_ = request` — no tenant, no thread,
      no ownership check (`routes/chat.py:952-963`). The whole control is the secrecy of a 96-bit
      `token_urlsafe`, which is adequate in practice (it is emitted only on that thread's side-event
      stream, and stream access is tenant-gated) but is the one surface where every other route
      checks ownership and this does not. The fix is `waiter_name("ui", thread_id, request_id)`
      plus a `thread_belongs_to_tenant` check — deliberately *not* folded into #250, because it
      changes the `ui` name shape that PR's upgrade note promises is unchanged, so it wants its
      own commit and its own note. Decide before the next release.
- [ ] **`waiters._local` never shrinks on the signal-first path.** `waiters.py:127-131`: a
      `signal` with no waiter registers a *completed* future and only `wait` pops it. While Redis
      is in fallback, an authenticated caller POSTing `/chat/tool_result` with random
      `tool_call_id`s grows the dict without bound. Pre-existing and not made worse by #250 (the
      name space was already caller-chosen); capping `tool_call_id` there bounds each entry's
      size but not the count.
- [x] **`tool_call_id` was provider input spliced into a `:`-delimited waiter key.** Closed, and
      the entry understated it: the collision needs no hostile `tool_call_id` at all, because
      *`thread_id` already contains colons* -- `{tenant}:{suffix}`, and `{tenant}:fiber:{id}` for a
      durable run. `fiber` is a legal thread suffix, so a caller can create `acme:fiber` and post a
      `tool_result` for call `F123:call_9`, forging the waiter of the durable run on
      `acme:fiber:F123` answering `call_9`, and satisfying its pending client tool with content
      they chose. Same tenant only; the tenant prefix cannot be forged. Waiter names now go through
      `waiters.waiter_name`, which percent-encodes each part (`%` before `:`) so the join is
      injective; approval and UI names are byte-identical since their ids are a uuid and a
      `token_urlsafe`. The repo's named defect shape, and worth noting that the *second* grammar
      here was one the harness minted itself rather than one it received.
- [x] **Approvals reach the durable path.** Closed in two halves, and *not* the way this entry
      proposed. It said to route `side_events` through the Redis layer from `#93`; that would have
      been wrong for the reason the Valkey bridge was wrong on #238, and the reason is in
      `notify.py`'s own docstring: "the notification is a hint, never the source of truth", because
      every wake re-reads Postgres. Pub/sub is at-most-once, and here the message *is* the frame —
      a dropped one means the run blocks its whole `ttl_seconds` and then denies with no human ever
      asked. So instead: felix#245 put `reason` and `tool_call_id` on the approvals row (the frame
      carried them, the row did not), and the durable stream now reads the pending rows for its
      thread and **rebuilds** `approval_required` from them. Same principle as the transcript tail,
      applied to the half the session log cannot carry — `_append_produced` writes assistant turns
      and tool results, and a request for permission is neither. Announced once per stream, deduped
      by id because `list_approvals` answers "what is pending" and has no cursor; thread-scoped,
      because `GET /approvals` is tenant-wide and every pending approval in the tenant on one run's
      stream would leak other conversations' tool names and arguments. The poll remains the channel
      of record: a stream that was never open sees nothing.
- [ ] **Signed completion webhooks**, delivered from the **worker** — the fiber reaches terminal
      state under its cron and the API replica that accepted the request may be gone. Dead letter
      is `status='dead'` on the same durable row, not a second store. `spec.webhooks` selects
      operator-registered endpoint ids and **never carries URLs**: a manifest author holds a
      tenant scope, and a tenant-supplied URL on a path carrying run output is an exfiltration
      channel SSRF checks do not address.
- [x] **Bound the retry.** Correction to the entry as written: an `invoke` that raises is
      terminal in one tick (`status: failed`); it was the failures *outside* that handler — a
      save, a lease write, a store down — that were released and re-claimed once a minute until
      `expires_at`. Now: `fibers.attempts` (migration `0013`), backoff 1m→1h doubling, and
      `status: dead` at `FELIX_FIBER_MAX_ATTEMPTS` (5), with the error on the run view and every
      terminal-status set (`sdk.py`, the resume stream) agreeing under an invariant.
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

- [x] **The summarizer is metered.** Found by the 2026-09-04 readiness audit, not on this
      list: compaction billed its summarizer call to the literal tenant `"default"`, priced it
      by the logical route name, and never touched `limit_state`, so the largest input-token
      call in a long thread escaped `max_cost_usd`; `summarizing:N` recorded nothing. Both go
      through `record_model_usage` now, and the react loop's reported usage block is priced by
      the wire id (it was the logical name, so every custom route reported `$0` on the turn).
- [x] **Persist cost.** Migration `0011_usage_cost`: `cost_usd` and `wire_model_id` on every
      row, priced at write time by the wire id and any `spec.model.price` override (which was
      documented as doing this and decorated only the `/v1/models` listing). Stored `model_id`
      stays the logical route name, which is what an operator recognises.
- [x] **`GET /usage/summary`** — by manifest / model / UTC day, with totals; both backends
      under conformance.
- [ ] **Fill the missing bundled rates** — `gpt-4.1` has no entry and bills at the default, and
      no bundled entry sets a long-context tier. Correction to this entry as written: an
      unpriced model contributes `$0`, so `limits.max_cost_usd` fails **open** for it, not
      closed — `felix_model_unpriced` now says when that is happening.
- [x] **An approval row names the thread it is blocking** (migration `0014_approval_thread_id`,
      felix-run/felix#232). The `approval_required` frame carried `thread_id`; the row did not —
      and the two channels do not cover the same runs. Side events are an in-process queue keyed
      by thread, so a durable run (agent in the worker, stream served by the API) is reachable
      only through `GET /approvals`: the channel that is the whole story for an unwatched run was
      the half with nothing to attribute. It is the *originating* thread, because `create_pending`
      still reuses a pending row across threads. Widening that reuse key would change grant scope
      and is a product decision, not part of this.
- [~] **Attribute denials in the audit record.** Landed: `policy_deny` rows carry
      `payload.control` naming the wrapper that refused — the source was on every deny output
      already (`deny_output` stamps it) and the loop was the one reader that dropped it, so the
      fix was a read, not a design. Not landed, and still the auditor's second question:
      `GET /audit/export` over a time range; `audit.py`'s docstring already promises an export
      that does not exist.
- [ ] **Surface eval instrumentation** — `EvalRun.started_at/finished_at` and `ItemScore`'s
      `duration_ms` / token counts / `tool_call_count` are all stored and rendered nowhere. And
      make the judge's fail-open path visible: any exception silently degrades an LLM judge to a
      substring check with `reason: "llm_fallback:<exc>"`, so a misconfigured judge model does not
      fail your eval, it quietly weakens it.
- [x] **Skills routes.** Landed: `GET /skills/{manifest}` lists what a manifest can reach and
      what is active, `GET /skills/{manifest}/{skill}` returns the body `activate_skill` would
      hand the model, and `GET /skills/{manifest}/activations/recent` says which skill activated
      on which turn — all on a new `skills:read` scope, kept off `manifests:read` because a skill
      body is prompt content. Read-only: activation is the model's decision mid-turn and the
      store is keyed by `(tenant, manifest)` rather than thread, so an operator writing it would
      be racing a run with no turn to attribute the change to.
      The third ask needed a fix to be answerable at all: `tool_runner` audits every tool call
      but its payload carries the tool's *name*, not its arguments, so the trail said a skill
      activated and never which one. `skills/tools.py` now emits `skill_activation` naming it —
      safe to store because `activate` resolves the name against the catalog first.
- [x] **Decided: `spec.skills` only adds, and a manifest can now opt out of that.**
      `spec.skills_declared_only: true` makes the declared names the whole catalogue. Opt-in
      rather than a default change, because narrowing silently would alter behaviour for every
      manifest already in Postgres — a migration, by this repo's own rule, not a
      reinterpretation. The reason to want it: a skill body is appended to the system prompt,
      so an ambient skill is the one prompt-shaping input `pin_compile` cannot cover, since the
      hash is over the manifest and the drift is on the host's disk. `GET /skills/{manifest}`
      reports `declared_only` so the two readings are distinguishable from outside.

### D. Truth in advertising

Small, and blocking for the adopter goal: anyone evaluating Felix on its governance claims reads
`governed.yaml` first. Enforce or delete, per item.

- [x] **`governed.yaml retention_days: 30` is inert.** Wired: the nightly sweep prunes the
      manifest's own `audit_events` past that many days, capped by `FELIX_AUDIT_RETENTION_DAYS`
      (a manifest shortens the deployment TTL, never extends it). Removed from
      `test_inert_manifest_fields.py`.
- [x] **`governed.yaml:128 guardrails.targets: [input, output]` does not scrub replies.**
      Implemented: the reply-path wrapper redacts (or blocks) PII in the agent's reply on
      `invoke` and on the streaming path; `output` covers tool output and the reply,
      `final_response` the reply alone. `deploy/GOVERNANCE.md` says so.
- [x] **Five `PlanExecuteSpec` fields were inert** — four are wired and one is gone.
      `planner_model` and `executor_model` route the planning call and the subtask agent;
      `replan_on_failure` and `max_replans` replan the *remainder* when a step ends early, on a
      deliberately narrow definition of "early" (`_FAILED_STOP_REASONS` — a refusal or a cut-off
      answer, not an empty one). `planner_few_shots` was removed rather than wired: it named a
      count of examples with no corpus behind it, so there was nothing to make it mean, and it is
      in `RETIRED` so stored manifests keep loading. Two things found on the way.
      `_pipe_stream` keeps the *last* terminal event and `react` puts `stop_reason` on `done`
      as well as `on_chain_end`, so a test double that omits it reports every streamed step
      as `end_turn` — which looks exactly like a streaming bug in the pattern; the composite
      `_terminal_events` had the same hole, which also meant `/v1` reported a default
      `finish_reason` for every streamed composite run. And `executor_model` was wired first
      on `_DelegatingAgent`'s `self.inner or self._base_agent(...)` fallback, which is dead
      from core because `_build_plan_execute` always passes `inner` — the field stayed inert
      *and* the textual ratchet started reporting it as fixed.
- [ ] **The session log keeps the unscreened reply.** The reply controls above govern the
      reply as it leaves the run; the react loop appends the assistant message to the session
      log before the wrapper sees it, so a resume stream or a thread export replays the raw
      text. Either append the screened reply (the wrapper would need the store) or record a
      redaction event the replay path applies. Named in `deploy/GOVERNANCE.md` as an exception
      until it lands.
- [x] **Final-response judges do nothing on the streaming path.** Fixed with the reply-path
      wrapper: reply text is held until the run ends and released judged, or replaced by the
      denial; structural frames still stream as they happen.
- [x] **Inbound screening skips two paths.** Correction to the entry as written: the durable
      fiber path *was* screened, at `/chat` before enqueue; the unscreened paths were cron
      jobs (a prompt writable with `jobs:write`), eval items, `/chat/continue` and MCP
      `tools/call` arguments. The screen is now a wrapper the compile puts around the agent
      (`InboundScreeningAgent`), so every path that runs the agent screens without a list of
      paths; MCP screens the argument tree instead.

Checked and *not* a gap, so nobody "fixes" it: `allow_unattended` is enforced — at compile, under
`eu_ai_act` at `risk_tier: high` (`governance.py:209`), which is why `contributor.yaml` carries a
comment explaining exactly that. It is conditional, not inert.

---
## Next (this quarter)

### Harness

- [x] **Moved off `psycopg[binary]` to `psycopg[c]`** — done well before the ignore expired,
      and `.trivyignore.yaml` is empty again because the findings went away with the library
      rather than being suppressed. One correction to this entry as written: the swap is in
      `deploy/docker/Dockerfile`, **not** in `pyproject.toml`. Changing the dependency would
      have made `pip install felix-harness` and every contributor's `make install` require a
      compiler and libpq headers, which is a steep price for an OSS project to pay for a
      property only the shipped image needs. The image installs `psycopg[c]` over the synced
      venv and asserts `psycopg-binary` is gone; everywhere else keeps the wheel.
      The measurement the entry asked for: **577 MB against 587 MB**, so dropping the vendored
      libraries more than paid for `libpq5`. `psycopg.pq.__impl__` reports `c`, migrations run
      to head against real Postgres 17.11, and the scan exits 0 with no ignore file at all.

- [ ] **`session.context_window_tokens` should default to a sentinel, not a number.** It is the
      one field in the schema where writing the default and omitting it mean different things:
      `runtime.py` reads `model_fields_set` to tell them apart, so an explicit `128000` compacts
      against 128K while omitting it compacts against the model's real window (1M on a
      large-context route). Everything else in the repo treats the serialized form as the
      meaning — including the compile-pin hash, which cannot see the difference and never could.
      Making the default `None` would make the two agree, remove the only `model_fields_set`
      read outside `Settings`, and let the pin notice a change that currently slips past it.
      Touches `react.py`, `usage/catalog.py`, four bundled manifests and
      `test_compaction_window.py`, so it wants its own change rather than riding along.

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
- [ ] **Temporal: decide.** (`make up-temporal` now runs it end to end, and the backend's
      writes actually persist — see CHANGELOG — so the decision can be made against something
      that works. Still no TLS/API-key on `Client.connect`, so Temporal Cloud is unreachable.)
      Original note: The arm is a 152-line driver loop using none of Temporal's durability
      primitives — no signals, no queries, no child workflows, no `continue_as_new`, no activity
      retry policy. State still lives in the Postgres `Fiber` row, so an operator choosing it for
      Temporal's guarantees gets Felix's. Four of its six tests assert only that the classes can
      be constructed, and there is no integration test against a dev server. It does fix the
      one-op-per-tick problem — which item B1 fixes for everyone. Either invest properly or
      document it as a compatibility shim.
- [ ] **Live-model eval (optional CI)** — the gate is now a pair of mock fixtures: `smoke.json`
      passes by construction and `negative.json` must fail, checked by
      `scripts/eval-counter-smoke.sh` in both CI and `make check-ci`. That proves the scorer can
      say no, which it could not before, but both halves still score a canned answer — nothing
      here scores the agent. Optional nightly against `api.felix.run` that does not block PRs.
- [x] **Validate eval dataset items.** Done: `felix/eval/validation.py`, called by
      `PUT /eval/datasets/{name}` and by `felix eval --fixture`. An item with no `user_input`
      is refused and the near-miss key it used is named back (`input` — the spelling the
      bundled fixtures once used — plus `prompt`, `question`, `query`, `user_message`, `text`);
      so is a non-object rubric and a repeated `item_id`. A rubric naming no rule is legal and
      warns instead, because it scores as `nonempty` and passes anything. The rubric stays
      free-form. `tests/e2e/test_mgmt_routes.py` pinned the old accept-and-store-empty
      behaviour and said it should fail when this landed; it now pins the refusal.

      One consequence for the item below: an unscoreable rubric no longer reaches the runner
      through `--fixture`, so the counter-smoke's fourth check is unreachable from a fixture
      and now guards only the paths that bypass validation — items written straight to the
      store by the continuous-eval job, and a manifest that fails to resolve.

- [x] **An eval run cannot report how many items errored.** `error_count` is on the run row
      (migration `0017`) as the subset of `fail_count` that never reached the scorer;
      `fail_count` keeps meaning "did not pass", which the CLI exit code relies on. The
      counter-smoke checks both, so the row and the score rows cannot drift.

- [~] **Eval scoring depth** — landed: trajectory rules (`tools_called`, `tools_not_called`,
      `max_tool_calls`, `max_errors`), read off the run's messages and off `mock_tool_calls` /
      `mock_tool_errors` under `--mock`, with `invalid_rubric` for the shapes that cannot reject.
      `fixtures/eval/contributor.json` is the first dataset that scores the *agent*. Still no
      regex, no schema check, no numeric tolerance, no significance test on comparative runs. Nobody can gate a model change on this without writing their own scorer.
      A new rule inherits two things: `_score_answer`'s docstring states the empty-value policy,
      and `tests/unit/test_eval_gate_can_fail.py` reads the rule names off the function, so the
      rule fails there until `negative.json` has an item that has seen it reject something.
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
- [x] **Telemetry vocabulary** — `docs/OBSERVABILITY.md` carries the metric catalog and the span
      schema, and `tests/unit/test_metric_catalog.py` re-derives it from the source so it cannot
      drift. Spans now follow the OTel GenAI semantic conventions, and a model call is a span at
      all (it was not). The `metrics.py` silent degrade on a reused label set is unchanged and is
      documented as a known limitation rather than fixed.
- [x] **Ship one dashboard** — `deploy/docker/compose.observability.yml` (`make up-observability`)
      brings up an OTel Collector, Prometheus, Grafana, Jaeger, Loki and Postgres/Valkey exporters,
      with a provisioned `Felix — harness overview` dashboard whose governance row surfaces the
      counters GOVERNANCE.md tells operators to watch. A gated `serviceMonitor` template covers the
      Kubernetes side. The scrape credential is a zero-scope API key, because `/metrics` is
      authenticated on purpose.
- [ ] **Sandbox ladder extras** — capability-bridge / gVisor as documented extras, not the default
      lean image. The workspace tools need this more than the snippet rung does: see
      [WORKSPACE.md](WORKSPACE.md), which puts workspaces in a hosted sandbox service in production and keeps gVisor for the
      `broker` fallback.
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
- [x] **`cowork.yaml` sets `auth.inbound.allow_anonymous: true` on a manifest that binds a
      local shell.** Now `false`, with the reason in the manifest. Checked at the same time:
      `validate_runtime` already confines `auth_mode=none` to loopback, so the reachable case
      was the developer's own machine. Also landed: `PUT /manifests` refuses plaintext
      credentials and disallowed sandbox images at write time, and `GET /manifests/{name}`
      redacts an embedded credential — `manifests:read` could read one before. The `client-shell` approval rule and the `thread_id`/`tool_call_id`
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
- [x] **Fiber rows are never swept** — and neither were `usage_events`, `a2a_tasks` or
      `session_events`; four tables grew for the life of a deployment. The sweep now covers
      every appended table on both backends, with `FELIX_{AUDIT,USAGE,FIBER,SESSION}_RETENTION_DAYS`
      in place of module constants (`0` keeps; sessions keep by default). The memory:// arm
      never pruned audit rows at all — it filtered the `DurableBuffer` as if it were the list —
      and swept plans for tenant `default` only; both fixed, and `tests/conformance/test_retention.py`
      runs the contract against both arms.
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

- [x] **Nothing enforces RLS coverage for a new tenant table** (readiness pass, 2026-09-04).
      `tests/unit/test_rls_coverage.py` renders every migration offline and checks the DDL:
      every `tenant_id` table carries `felix_tenant_isolation` and `FORCE`, every table has a
      `tenant_id` (allowlist: `memory_vector_config`), one Alembic head. `oauth_token_cache`,
      tenant-less and never read or written, is dropped in `0013` with its setting and helper.

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

- [x] **Edge posture behind a proxy and an IdP** (readiness pass, 2026-09-04). The rate-limit
      key read the *leftmost* `X-Forwarded-For` entry, the one the client wrote; a `tenant=claim`
      verifier with no `FELIX_ALLOWED_TENANTS` accepted any claimed tenant; `exp` had zero
      clock leeway; and a remote JWKS past its TTL 401ed every token from that issuer while
      `/ready` stayed green. Now: `FELIX_TRUSTED_PROXY_HOPS` counts from the right,
      `validate_runtime` refuses the unguarded claim mode outside development and colliding
      `tenant=issuer` verifiers everywhere, sixty seconds of leeway, and a `jwks` row on
      `/ready` that fails when no verifier is usable (refresh 300s, retry 30s).
- [x] **`Idempotency-Key` on `POST /chat`** (readiness pass, 2026-09-04) — one turn per key per
      tenant, Redis-backed claims across replicas, replay with `Idempotent-Replayed: true`,
      `FELIX_IDEMPOTENCY_TTL_SECONDS`.
- [ ] **A tenant is a string.** There is no `Tenant` table and no `ApiKey` table; `tenant_id` is a
      column on every row and never a foreign key. Minting a key means editing
      `FELIX_AUTH_API_KEYS` JSON and restarting. Manifest CRUD, canary and rollback are real and
      API-driven; onboarding tenant #2 is a config edit and a process restart. Decide whether that
      is the product (single-operator self-host) or a gap, and write the answer down either way.
- [ ] **Manifest version listing** — `GET /manifests/{name}?version=N` fetches one; nothing
      enumerates what exists, so rollback requires knowing the number already.
- [ ] **Run a job now** — `jobs.py` is CRUD plus run history; you can only wait for cron.

### Testing strategy

From the audit of 2026-09-05. Phase 1 (the `tests/e2e/` harness), the vendor-credential hole in
`scripts/test.sh` and the invariant that pins it shipped together; the rest are queued in leverage
order. One hardening item per cycle under the meta-work budget; the scanner guards were this
cycle's, and the route contracts below are the next capability-adjacent step.

- [x] **Guard the structural scanners.** Fifteen scanners could pass by finding nothing:
      `_python_files()` and `_py_files()` answered `[]` for a directory that had moved, so a
      renamed package would have silenced the whole family at once. Both helpers now fail on a
      missing or empty root, and every scanner without a positive control gained one at its
      measured count — including the `httpx` timeout scanner the `test-quality` skill cites as
      having been green-on-broken once. `test_bash_guard_hooks.py` gained the
      `FELIX_REQUIRE_OPTIONAL_EXTRAS` hatch its sibling already had, and
      `test_resume_poll_backoff.py` now imports the symbol under test directly, so a rename
      fails collection instead of silently skipping the nineteen tests that carried the
      condition. Each guard proved by mutation, and two floors were corrected after review:
      one counted only non-exempt files, and both were re-derived from measurement.
      One overclaim surfaced while measuring: `test_every_record_usage_call_prices_by_the_wire_model`
      covers a single call site, not the eight its docstring named — `record_model_usage`
      absorbed the rest — and the docstring now says so.

- [~] **Route contracts through the e2e harness.** The nine `/chat/sessions/*` routes and both
      lease endpoints are covered (`tests/e2e/test_chat_sessions.py`), and the fixture now serves
      one shared script queue so a multi-request flow can be written at all. The run controls
      (steer, follow-up, abort, continue, fork, rewind, thinking, ui) followed, and the spy now
      records the prompts and the model specs. `/chat/compact` is covered on all three paths,
      including the one that actually summarises — which only became reachable once the route
      stopped reading `keep_recent_tokens: 0` as 20000. Still open: the `/chat/continue` success
      path (both tests are its 400 guards), `/chat/rewind` at its default `summarize: true`,
      `/chat/fork` with `from_event_id`, and a steer against a run genuinely in flight. The
      management routers are covered (`tests/e2e/test_mgmt_routes.py`): jobs, eval datasets and
      runs, audit and its metrics rollup, and approvals including a decide, under
      `auth_mode=api_key` so the scope gates are exercised rather than skipped. Two things
      there remain unpinned and are marked as such in the tests: the eval route's `tools`
      hand-off (no dataset item calls a tool) and its `use_llm_judge` inversion.
      `POST /eval/runs/compare` still has no caller at all. `patterns/delegating.py` still has no named test, and needs a per-client
      sub-queue in the fixture before it can have one — the last piece of this item.
- [ ] **Decide what a steer queued on an idle thread should do.** Today it is accepted with
      200, counted on the snapshot, then dropped before reaching the model or the transcript —
      `kind: follow_up` is the path that works. Found by asserting on what reached the model
      rather than on the reply. Options: refuse it, promote it to a follow-up, or hold it until
      a run starts. Pinned as-is by
      `tests/e2e/test_chat_run_control.py::test_a_steer_queued_while_idle_is_dropped_without_reaching_anyone`,
      which should fail and be rewritten when this is decided.

- [~] **Postgres arms for the ten stores that have none.** Approvals, session search and the
      fiber *claim* path are done (`tests/conformance/test_approvals_store.py`,
      `test_session_search.py`, `test_fiber_claim.py`) behind a generic `store_settings`
      fixture, and each of them found a real divergence: the approvals twin returned the oldest
      matching grant where Postgres returns the newest, an expired grant could hide a live one
      on Postgres only, and a batch of Temporal-backed fibers starved a tenant's ordinary ones.
      Manifests are done too, and found two more: the twin accepted a canary weight the CHECK
      constraint refuses, and handed back the stored document by reference. Still open, in the
      order their SQL diverges most from the twin: plans and a2a tasks.

      Jobs is done (`test_jobs_store.py`) and found two orderings that were not orders at all:
      `list_runs` broke ties by nothing, so the twin's stable sort returned a job's *oldest*
      runs as its most recent, and `list_jobs` was unordered on both arms.

      Audit is done (`test_audit_store.py`). It found a defect both arms shared rather than a
      divergence: the cursor carried only a millisecond timestamp, so paging stepped over every
      event sharing the boundary millisecond and returned them on no page at all.

      Eval is done (`test_eval_store.py`) and found the worst one yet: `put_dataset` overwrote an
      existing item on the twin and raised `UniqueViolation` on Postgres, so the second run of any
      eval failed on every real deployment while CI stayed green — and the continuous-eval sweep,
      which swallows a per-tenant exception, had scored nothing since its first ever tick while
      reporting a normal result.

- [ ] **`put_version` has a read-modify-write race on Postgres only.** It computes
      `SELECT coalesce(max(version),0)` then inserts, with no lock and no retry, so four
      concurrent publishes of one manifest name leave one winner and three `UniqueViolation`s —
      a 500 for a concurrent double-publish. The twin cannot race at all, since nothing awaits
      between its max and its write, so the contract cannot state a shared behaviour until one
      is chosen. Measured against a live database while verifying the manifest contract.

- [x] **An enforcing-RLS arm for the conformance suite.** Done
      (`tests/conformance/test_rls_enforcement.py`): a `NOSUPERUSER NOBYPASSRLS` role with
      `database_rls=True`, which is the configuration no other arm can reach — and the only
      one where a lost `rls_bypass()` is visible at all, since every other arm connects as the
      schema owner, where a bypass is a no-op. It guards one of the twelve bypasses in the
      tree; the item below is the rest of them.

- [ ] **Parametrise the cross-tenant sweep arm over every `rls_bypass()`.**
      `test_a_cross_tenant_sweep_still_sees_every_tenant` covers `list_tenants_with_events`
      and nothing else. There are twelve bypasses — `memory/store.py`, `durability/fibers.py`
      (five), `audit/store.py` (two), `manifests/store.py`, `jobs/store.py` and
      `jobs/retention.py` (two) — and each can lose its bypass with the whole suite green,
      because only this arm builds a role the policy applies to. The failure mode is not a
      cross-tenant read: the schedulers re-bind per tenant afterwards, so it degrades to a
      sweep that reads an empty tenant list and reports success. That is the shape an operator
      cannot diagnose from outside, and it only bites deployments that opted into
      `FELIX_DATABASE_RLS`. The `rls_settings` fixture already builds the role, so this is a
      parametrisation rather than new machinery. Worth a line in `deploy/GOVERNANCE.md` too,
      beside the RLS guidance, since that is who it happens to.

- [ ] **`test_migrations.py` still wants an autogenerate-empty check** (models versus
      migrations drift) **and stepwise per-revision up/down**; today it only goes base to head
      in one hop.

- [ ] **`create_fiber` cannot insert under an enforcing RLS role.** It is the one write in
      `durability/fibers.py` that neither wraps `rls_bypass()` nor binds the tenant GUC, so with
      `FELIX_DATABASE_RLS=true` and a non-superuser it fails with "new row violates row-level
      security policy". `get_fiber` has the same gap. Invisible to the conformance suite because
      that connects as a superuser with RLS off. Found while verifying the fiber claim contract
      against a live database; fixed in a separate change.

- [ ] **Promote the ordering rule to a scanner.** It has now been fixed six times — the audit
      and usage cursors, `list_runs`, `list_jobs`'s collation, `list_active` twice — and two
      more shapes are still open below. The repo's own rule is that a lesson learned this often
      earns a structural gate rather than another round of review. The shape: over *any* module
      that truncates an ordered list, every `order_by(...)` and every `sort(key=...)` whose
      result is then cut must end on a primary-key component. Not `**/store.py` — that glob is
      what let `memory/recall.py` go unexamined through a survey written for exactly this
      defect, because its channels are hand-rolled `sorted(...)[:n]` rather than an `ORDER BY`.
      The site floor has to name `memory/recall.py`, `documents/store.py` and the session
      modules, or the gate will miss the seventh instance the way the survey missed the sixth.
      It must carry a floor
      on the number of ordering sites it matched, because a scanner that quietly stops matching
      is the failure mode this repo has already shipped once — an AST invariant here matched
      `timeout=<Constant>` while every literal it hunted lived inside `httpx.Timeout(...)`.
      Cheaper than catching the seventh instance in review, and it cannot be satisfied by a fake.

- [x] **`recall()`'s ordering defect, and its tiebreak that read a column nothing writes.**
      Done (`tests/conformance/test_memory_recall.py`, the first conformance arm this path has
      had). All three in-memory channels sorted on the score alone and all three SQL channels
      ordered on rank alone, so the candidate set entering fusion came from insertion order on
      one backend and the query plan on the other — and reciprocal-rank fusion scores on
      *position*, so that was amplified rather than absorbed. The ranking pass then tied again
      on score and recency. All six channels and the ranking now end on the id.

      `_rank`'s recency term read `last_used_at or created_at`, and `last_used_at` has no
      writer: the migration adds the column, `put_memory` sets it to None, the upsert excludes
      it. The dead half is gone and the docstring says "newest" means `created_at` — confirmed
      by mutation, since restoring the dead read changes no test.

      Still open, and deliberately not decided here: whether to write `last_used_at` on recall
      (a write on a read path) or drop the column in a revision. Until one of those, the column
      exists and nothing populates it.

- [ ] **The per-channel savepoint costs a round trip each, and that scales with latency.**
      Measured: +1.0 to +1.15 ms per recall on loopback, *flat* as the corpus grows from 500 to
      20,500 rows — six extra round trips (`SAVEPOINT` and `RELEASE` per channel) at ~0.19 ms
      each, so it is fixed overhead rather than corpus-dependent. On a managed Postgres at
      ~1 ms RTT that is roughly +6 ms per recall, which is 4–6x the tiebreak cost below and
      lands inline in a turn. It buys the thing the comment always claimed and did not deliver:
      one broken channel loses a channel rather than the turn. If the latency matters, the
      cheaper shape is optimistic — run without savepoints and, on the first failure, roll back
      once and retry only the remaining channels inside them — which costs nothing on the path
      that always succeeds. More code for a path that only opens mid-upgrade, so measure the
      real deployment before taking it.

- [ ] **The recall tiebreak costs an HNSW index scan its selectivity.** Measured at 20,500
      rows: `ORDER BY embedding <=> :vec` alone is an `Index Scan using idx_memvec_hnsw`
      pulling 16 rows in ~0.35 ms, while adding any tiebreak turns it into an
      `Incremental Sort` over that index pulling 391 rows in ~1.25 ms — 4x the time and 5.6x
      the buffers. Going from one extra key to three is then free (~4%, inside run-to-run
      spread), so the lever if this ever matters is dropping the tiebreak, not trimming it.
      It is worth the millisecond today: an undecided cut changes *which* memories a turn
      gets. Re-measure before changing either way, and note the planner does not choose the
      index at all below a few thousand rows, so small deployments pay nothing.

- [ ] **`kind` is unindexed, and recall now filters on it.** `memory_vectors` has no index on
      `kind`, so on the full-text and topic channels `kind = ANY(...)` is a heap recheck after
      the GIN scan — a selective `kinds` over a broad tsquery walks a long way to fill a
      `LIMIT 16`. Unmeasured, and expected to be invisible at realistic sizes; measure before
      adding an index rather than adding one on the strength of this line.

- [ ] **A read is a copy in one store and a window in three.** The jobs store now deepcopies
      its JSON columns on read *and* write, because the twin was handing back the dict it held
      and Postgres deserializes fresh — so a caller editing what it read edited the store, on
      one backend only. `audit/store.py`, `usage/store.py` and `memory/store.py` still return a
      shallow `dict(row)`, which aliases their JSON column the same way. Nothing mutates a read
      today, so this is latent; what makes it worth recording is that the repo now answers the
      same question two ways in files edited by one commit, and the next store copies whichever
      neighbour its author opens. The carrier would be a shared conformance assertion that
      every store's read is mutation-isolated, not four more deepcopies.

- [ ] **More listings whose two arms can disagree about order.** Not one shape but three, and
      the first survey found only the first: a tie the twin breaks by insertion order and
      Postgres by nothing; a text key ordered by database collation on one arm and code point
      on the other; and the two arms sorting the same keys in *opposite directions*. The jobs
      contract found the first two and `list_jobs` now uses `COLLATE "C"`; the usage summary
      was the third, reversing `manifest_id` and `model_id` where the SQL ascended them, and
      is fixed with the jobs work because it already had a contract to assert it in.

      `memory/store.py`'s `list_active` (both sorts) and `as_of` are done, and so is
      `memory/recall.py` — see the item above. Remaining, ranked by what a wrong answer costs,
      and by function rather than line so the list stops rotting on every edit:
      `approvals/store.py`'s `list_approvals` (`created_at`,
      limited); `plans/store.py`'s `list_plans` (`updated_at`, limited); `eval/store.py`'s
      `list_runs` (`started_at`, unlimited, so ties only reorder). `approvals/store.py`'s
      `find_approved` already does it right — `decided_at`, `created_at`, then `id` — and is
      the pattern to copy.

      Approvals and memory have conformance files already, so those are cases to add rather
      than files to write; plans and eval are covered by the seam bullet above. Fix each with
      the arm that proves it rather than in one sweep.

- [ ] **The audit and usage reads do not set the tenant GUC, so RLS empties them.** Both open
      their read session through `get_session_factory(...)` without `rls_tenant(tenant_id)`,
      unlike `_write_batch`, which does. With `FELIX_DATABASE_RLS=1` on a role that cannot
      bypass, `_rls_after_begin` resolves no tenant, sets neither setting, logs its warning,
      and the policy filters every row — `/audit` and `/usage` return empty pages to an
      operator whose history is there. Pre-existing, and explicitly *not* covered by the new
      conformance arm: it connects as the database owner with `database_rls` unset, so that
      suite must not be read as evidence about this.

- [ ] **One unwritable audit event blocks every later one.** `flush_pending` requeues a batch
      whose write failed, so the compliance record survives a transient outage — and so a
      *permanently* unwritable event is retried forever, with every subsequent event stuck
      behind it until the 10k ceiling starts dropping the oldest. Two concrete triggers, both
      invisible to the in-memory twin, which stores anything. The `\u0000` trigger is fixed at
      source — `record_event` strips it, because it was reachable by any authenticated client
      in one request — but the shape remains for a `payload_json` Postgres refuses for another
      reason, and for a caller-supplied `id` colliding on the `(tenant_id, id)` primary key
      (reachable only from tests today: nothing in `packages` or `apps` supplies an id). `tests/conformance/test_audit_store.py`
      now pins that a failed flush keeps its batch; what is missing is telling a transient
      failure from a poisonous one — quarantine the offending event, count it the way
      `DurableBuffer` counts drops, and let the rest through.

- [ ] **The keyset cursor's tie-break is collation-dependent.** `felix/cursors.py` pairs the
      timestamp with the row id, and `id` is text — so Postgres orders it by the database
      collation while the in-memory twin orders it by Python code point. The ids actually
      written are `uuid4().hex`, which sorts the same under every common collation, and
      `record_event` accepts a caller-supplied id only. Paging stays complete on both, since
      each backend is self-consistent; the exposure is the order of two rows in one
      millisecond differing between them, which no contract would catch because every test
      asserts set equality over pages. Fix if a caller-supplied id ever becomes ordinary.

- [ ] **An index for the active-memory ordering's new tiebreak.** `idx_memory_active` is
      `(tenant_id, manifest_id, status, created_at DESC)` and does not carry `id`, so the
      tiebreak adds an Incremental Sort over each `created_at` group. Bounded and cheap in the
      common case — but the case the tiebreak exists for is the batch write where one group is
      large (`consolidate_pools`, the memory writer), which is exactly when it is not. An index
      on `(tenant_id, manifest_id, status, created_at DESC, id DESC)` would make the
      unprioritised read a pure index scan, and would have to match the `COLLATE "C"`
      expression to be used at all. The prioritised branch leads on a `metadata`-derived trust
      expression no btree covers, so it benefits from none of this. Measured plan, not a guess.

- [ ] **An index for the job run history's ordering.** `list_runs` filters
      `(tenant_id, job_name)` and orders by `(started_at DESC, run_id DESC)`, while the only
      index on `job_runs` is the primary key `(tenant_id, job_name, run_id)` — so the ordering
      column is unindexed and the plan sorts a job's entire history before the `LIMIT` applies,
      unbounded in runs per job. A `(tenant_id, job_name, started_at DESC, run_id DESC)` index
      makes it a plain index scan. Read off the index set rather than measured, so measure
      first. Same family as the audit and usage item below, and one revision could carry all
      three.

- [ ] **An index for the audit and usage listings' new ordering.** Both now
      `ORDER BY ts DESC, id DESC` so the keyset cursor has a total order to page on, while
      `idx_audit_tenant_ts` and `idx_usage_tenant_ts` cover `(tenant_id, ts)` only. Measured at
      100k rows, 50 per distinct `ts`, the plan is an `Index Scan Backward` on that index under
      an `Incremental Sort` with `ts` presorted — correct, and cheap while a single
      `(tenant_id, ts)` group stays small, because only the group holding the page boundary is
      sorted. It degrades when one millisecond's group gets large. A `(tenant_id, ts DESC,
      id DESC)` index removes the sort node and makes the cursor a pure index seek; that is one
      revision, and a refinement rather than a correctness gap.

- [ ] **An index for the fiber claim's ordering.** `ORDER BY updated_at LIMIT 50` has no
      supporting index; measured at 200k rows it is 11 ms, and a partial index matching the
      claim's WHERE takes it to 0.15 ms at a fifth the size of `idx_fibers_due`. Worth doing now
      that the WHERE clause is stable.

- [x] **Worker cron bodies and CLI commands.** The eight cron bodies are covered and their
      schedules pinned (`tests/unit/test_worker_cron_tasks.py`), and every subcommand is now
      invoked (`tests/unit/test_cli_commands.py`, plus `eval` in
      `tests/unit/test_eval_gate_can_fail.py`). Running them found five defects, four of the
      same shape — exit 0, looks right, nothing downstream accepts it. `mint-jwt` wrapped its
      token at the console width so a captured one was rejected as `invalid_token`, accepted a
      `--tenant` the verifier refuses, and died on an unhandled `RuntimeError` with no signing
      key; `migrate` met `memory://` with a raw SQLAlchemy dialect traceback; and
      `temporal-worker` named its Postgres connections `felix-cli` for as long as it ran.
      `eval` printed its run dict through the same renderer, which splits any value longer
      than the console width mid-token — the fixtures are short so CI survived it, a real run
      would not.

### Repo / release hygiene

- [ ] **Credentials survive a `repr`.** `Settings` renders `anthropic_api_key` / `openai_api_key`
      in clear, `RequestContext` carries `Settings` on every request, and `HttpModelClient` keeps
      `api_key` as a plain dataclass field — no call site logs any of them today, so this is one
      `logger.debug("%r", client)` away rather than live. `SecretStr` on the credential fields
      and `field(repr=False)` on the client close it (`_ReactAgent.settings` got the latter in
      the `/v1` streaming change). Found by the 2026-09-04 readiness security review.

- [~] **Required status checks + `CODEOWNERS`** — the status-check half is **done** and this
      entry was wrong: `main` requires 13 contexts, all bound to the Actions app, and the
      `changes` job does report success so doc-only PRs stay mergeable. Found by hitting it —
      renaming a job for the CodeQL matrix broke `CodeQL` with "not set by the expected GitHub
      app", which only happens when protection is real. Two things follow. **`CODEOWNERS` still
      does not exist**, so review remains convention. And a required context is now coupled to a
      *job name*: rename one and merges block repo-wide with an error that names GitHub apps
      rather than the rename. `security.yml` pins its own name with an aggregate gate job;
      nothing stops the next rename elsewhere, and an invariant comparing required contexts
      against the names the workflows actually produce is ~20 lines
      (`.github/workflows/*.yml` → job `name`, matrix expanded).
- [x] **Tag-driven release** — `release.yml` on `v*.*.*`: version verified against the tree,
      both images for both architectures to GHCR, Trivy on the pushed digest, SPDX SBOM attached
      and attested, cosign keyless signing, GitHub release from the changelog section.
- [x] **Single-source the version** — every workspace member's `pyproject.toml` and
      `__init__.py` plus `Chart.yaml` `version` + `appVersion` and `values.yaml` `image.tag`
      (more than the six this entry counted). `scripts/bump-version.py` is the list, a test
      proves it matches the tree, the release workflow refuses a tag that disagrees.
- [x] **`uv --exclude-newer`** — the CI lock check refuses anything published in the last 48h.
- [x] **CODEOWNERS** — `.github/CODEOWNERS` covers the controls, migrations, deploy and the
      supply chain; with branch protection's code-owner review, nothing on those paths merges
      without one.
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
